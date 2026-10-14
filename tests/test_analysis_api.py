from datetime import UTC, datetime, timedelta

import pytest

from log_inspector.aws.errors import LogsAccessDenied
from log_inspector.aws.insights import QueryResult
from log_inspector.services import analysis as analysis_service
from tests.test_logs_api import _create_verified

END = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
START = END - timedelta(hours=1)
SPIKE_MINUTE = datetime(2026, 10, 2, 12, 41, tzinfo=UTC)

DEMO_ROWS = [
    ("12:41:02", "WARN  DB connection pool at 90% capacity pool=orders-db"),
    (
        "12:41:03",
        "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1002",
    ),
    ("12:41:04", "ERROR Checkout failed: upstream dependency error status=503 order_id=1002"),
    (
        "12:41:05",
        "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1003",
    ),
    ("12:41:06", "ERROR Checkout failed: upstream dependency error status=503 order_id=1003"),
    ("12:41:07", "ERROR login failed for bob@example.com"),
]


def _insights_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.000")


class FakeInsights:
    """Stands in for run_query: answers by query type, like Insights would on the demo logs."""

    def __init__(self):
        self.calls = []

    def __call__(self, logs_client, *, log_groups, query, start, end, limit, timeout_seconds):
        self.calls.append({"query": query, "limit": limit, "start": start, "end": end})
        if "as errors by bin" in query:
            rows = [{"bucket": _insights_time(SPIKE_MINUTE), "errors": "5"}]
        elif "as total by bin" in query:
            rows = [
                {"bucket": _insights_time(SPIKE_MINUTE - timedelta(minutes=1)), "total": "2"},
                {"bucket": _insights_time(SPIKE_MINUTE), "total": "8"},
            ]
        else:
            rows = [
                {
                    "@timestamp": f"2026-10-02 {t}.000",
                    "@log": "123456789012:/log-inspector/demo",
                    "@logStream": "checkout-service",
                    "@message": message,
                }
                for t, message in reversed(DEMO_ROWS)
            ][:limit]
        return QueryResult("q", rows, len(rows), 8, 1024)


@pytest.fixture
def insights(monkeypatch):
    fake = FakeInsights()
    monkeypatch.setattr(analysis_service, "run_query", fake)
    return fake


@pytest.fixture
def conn_id(client, customer_role_arn, log_groups):
    return _create_verified(client, customer_role_arn)


def _analyze(client, conn_id, **extra):
    body = {
        "log_groups": ["/log-inspector/demo"],
        "start_time": START.isoformat(),
        "end_time": END.isoformat(),
        **extra,
    }
    return client.post(f"/connections/{conn_id}/analyze", json=body)


def test_analyze_demo_logs(client, conn_id, insights):
    response = _analyze(client, conn_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["bucket_seconds"] == 60
    assert (body["total_events"], body["total_errors"]) == (10, 5)
    assert body["events_analyzed"] == 6
    assert body["events_truncated"] is False
    assert body["cluster_count"] == 4

    top = body["clusters"][0]
    assert top["template"] == (
        "ERROR Database connection timeout after <num>ms host=orders-db.internal order_id=<num>"
    )
    assert top["count"] == 2
    assert top["log_groups"] == ["/log-inspector/demo"]
    assert top["stack_trace"] is None
    assert body["clusters"][-1]["level"] == "WARN"

    assert len(body["spikes"]) == 1
    spike = body["spikes"][0]
    assert datetime.fromisoformat(spike["start"]) == SPIKE_MINUTE
    assert spike["error_count"] == 5

    assert body["timeline"][0]["kind"] == "spike_start"
    assert len(body["volume"]) == 60
    assert body["redactions"] == {"email": 1}
    assert all(
        "bob@example.com" not in example for c in body["clusters"] for example in c["examples"]
    )


def test_runs_three_queries_with_matching_bins(client, conn_id, insights):
    _analyze(client, conn_id, max_events=100)

    queries = sorted(call["query"] for call in insights.calls)
    assert len(queries) == 3
    assert any("bin(1m) as bucket" in q and "as errors" in q for q in queries)
    assert any("bin(1m) as bucket" in q and "as total" in q for q in queries)
    events_call = next(
        c for c in insights.calls if "@message" in c["query"] and "stats" not in c["query"]
    )
    assert events_call["limit"] == 100
    assert all(c["start"] == START and c["end"] == END for c in insights.calls)


def test_longer_ranges_use_bigger_buckets(client, conn_id, insights):
    body = _analyze(client, conn_id, start_time=(END - timedelta(days=2)).isoformat()).json()
    assert body["bucket_seconds"] == 3600
    assert any("bin(1h)" in c["query"] for c in insights.calls)


def test_truncation_is_reported(client, conn_id, insights):
    body = _analyze(client, conn_id, max_events=3).json()
    assert body["events_analyzed"] == 3
    assert body["events_truncated"] is True


def test_analyze_validation(client, conn_id, insights):
    assert _analyze(client, conn_id, max_events=0).status_code == 422
    assert _analyze(client, conn_id, max_events=10001).status_code == 422
    assert _analyze(client, conn_id, log_groups=[]).status_code == 422
    too_long = _analyze(client, conn_id, start_time=(END - timedelta(days=8)).isoformat())
    assert too_long.status_code == 400


def test_analyze_requires_verified_connection(client, customer_role_arn, insights):
    conn_id = client.post("/connections", json={"name": "x"}).json()["connection"]["id"]
    assert _analyze(client, conn_id).status_code == 409
    assert _analyze(client, "missing").status_code == 404
    assert insights.calls == []


def test_analyze_aws_errors_are_surfaced(client, conn_id, monkeypatch):
    def denied(*args, **kwargs):
        raise LogsAccessDenied("The role is not allowed to call logs:StartQuery.")

    monkeypatch.setattr(analysis_service, "run_query", denied)
    response = _analyze(client, conn_id)
    assert response.status_code == 403
    assert "logs:StartQuery" in response.json()["detail"]


def test_parse_insights_time():
    assert analysis_service.parse_insights_time("2026-10-02 12:41:03.019") == datetime(
        2026, 10, 2, 12, 41, 3, 19000, tzinfo=UTC
    )
    assert analysis_service.parse_insights_time("1790953050908").tzinfo == UTC
