import time
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from log_inspector.aws.canned_queries import CANNED_QUERIES
from log_inspector.main import create_app
from tests.conftest import login, make_settings


def _create_verified(client, role_arn) -> str:
    conn_id = client.post("/connections", json={"name": "prod"}).json()["connection"]["id"]
    client.put(f"/connections/{conn_id}/role", json={"role_arn": role_arn})
    assert client.post(f"/connections/{conn_id}/verify").json()["verified"] is True
    return conn_id


@pytest.fixture
def conn_id(client, customer_role_arn, log_groups) -> str:
    return _create_verified(client, customer_role_arn)


@pytest.fixture
def app_events(aws) -> list[str]:
    logs = boto3.client("logs", region_name="us-east-1")
    logs.create_log_stream(logGroupName="/aws/lambda/checkout", logStreamName="s1")
    now_ms = int(time.time() * 1000)
    messages = ["INFO request received", "ERROR Database connection timeout"]
    logs.put_log_events(
        logGroupName="/aws/lambda/checkout",
        logStreamName="s1",
        logEvents=[
            {"timestamp": now_ms - 60_000 + i * 1000, "message": m} for i, m in enumerate(messages)
        ],
    )
    return messages


def test_list_canned_queries(client):
    body = client.get("/canned-queries").json()
    assert [q["name"] for q in body] == list(CANNED_QUERIES)
    assert all(q["query"] and q["description"] for q in body)


# --- Log groups ---


def test_list_log_groups(client, conn_id, log_groups):
    body = client.get(f"/connections/{conn_id}/log-groups").json()

    assert sorted(g["name"] for g in body["log_groups"]) == sorted(log_groups)
    assert body["next_token"] is None
    group = body["log_groups"][0]
    assert group["arn"].startswith("arn:aws:logs:us-east-1:123456789012:log-group:")
    assert group["created_at"] is not None


def test_list_log_groups_with_prefix(client, conn_id):
    body = client.get(f"/connections/{conn_id}/log-groups", params={"prefix": "/aws/lambda/"})
    assert sorted(g["name"] for g in body.json()["log_groups"]) == [
        "/aws/lambda/checkout",
        "/aws/lambda/payments",
    ]


def test_list_log_groups_paginates(client, conn_id, log_groups):
    url = f"/connections/{conn_id}/log-groups"
    first = client.get(url, params={"limit": 2}).json()
    assert len(first["log_groups"]) == 2
    assert first["next_token"]

    second = client.get(url, params={"limit": 2, "next_token": first["next_token"]}).json()
    names = [g["name"] for g in first["log_groups"] + second["log_groups"]]
    assert sorted(names) == sorted(log_groups)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"prefix": ""}])
def test_list_log_groups_validates_params(client, conn_id, params):
    assert client.get(f"/connections/{conn_id}/log-groups", params=params).status_code == 422


def test_unverified_connection_is_rejected(client, customer_role_arn):
    conn_id = client.post("/connections", json={"name": "prod"}).json()["connection"]["id"]
    client.put(f"/connections/{conn_id}/role", json={"role_arn": customer_role_arn})

    response = client.get(f"/connections/{conn_id}/log-groups")
    assert response.status_code == 409
    assert "verify" in response.json()["detail"]

    response = client.post(
        f"/connections/{conn_id}/query", json={"log_groups": ["/x"], "canned_query": "errors"}
    )
    assert response.status_code == 409


def test_unknown_connection_is_404(client):
    assert client.get("/connections/nope/log-groups").status_code == 404
    body = {"log_groups": ["/x"], "canned_query": "errors"}
    assert client.post("/connections/nope/query", json=body).status_code == 404


# --- Queries ---


def test_custom_query_returns_rows(client, conn_id, app_events):
    response = client.post(
        f"/connections/{conn_id}/query",
        json={
            "log_groups": ["/aws/lambda/checkout"],
            "query": "fields @timestamp, @message | sort @timestamp desc",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query_id"]
    assert body["row_count"] == len(app_events)
    assert sorted(r["@message"] for r in body["rows"]) == sorted(app_events)
    assert all("@ptr" not in r for r in body["rows"])
    assert set(body["statistics"]) == {"records_matched", "records_scanned", "bytes_scanned"}


def test_default_time_range_is_last_hour(client, conn_id, app_events):
    body = client.post(
        f"/connections/{conn_id}/query",
        json={"log_groups": ["/aws/lambda/checkout"], "canned_query": "recent"},
    ).json()

    start = datetime.fromisoformat(body["start_time"])
    end = datetime.fromisoformat(body["end_time"])
    assert end - start == timedelta(hours=1)
    assert abs(datetime.now(UTC) - end) < timedelta(minutes=1)


def test_canned_query_is_expanded(client, conn_id, app_events):
    body = client.post(
        f"/connections/{conn_id}/query",
        json={"log_groups": ["/aws/lambda/checkout"], "canned_query": "errors"},
    ).json()
    assert body["query"] == CANNED_QUERIES["errors"].query


def test_naive_times_are_treated_as_utc(client, conn_id, app_events):
    body = client.post(
        f"/connections/{conn_id}/query",
        json={
            "log_groups": ["/aws/lambda/checkout"],
            "canned_query": "recent",
            "start_time": "2026-10-01T10:00:00",
            "end_time": "2026-10-01T11:00:00",
        },
    ).json()
    assert datetime.fromisoformat(body["start_time"]) == datetime(2026, 10, 1, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "payload",
    [
        {"log_groups": ["/x"]},
        {"log_groups": ["/x"], "query": "fields @message", "canned_query": "errors"},
        {"log_groups": ["/x"], "canned_query": "not-a-query"},
        {"log_groups": [], "canned_query": "errors"},
        {"log_groups": [f"/g{i}" for i in range(51)], "canned_query": "errors"},
        {"log_groups": ["/x"], "canned_query": "errors", "limit": 0},
        {"log_groups": ["/x"], "canned_query": "errors", "limit": 10001},
    ],
)
def test_query_validation(client, conn_id, payload):
    assert client.post(f"/connections/{conn_id}/query", json=payload).status_code == 422


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-10-01T12:00:00Z", "2026-10-01T11:00:00Z", "before"),
        ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", "7 days"),
    ],
)
def test_query_time_range_is_checked(client, conn_id, start, end, message):
    response = client.post(
        f"/connections/{conn_id}/query",
        json={"log_groups": ["/x"], "canned_query": "errors", "start_time": start, "end_time": end},
    )
    assert response.status_code == 400
    assert message in response.json()["detail"]


# --- AWS failures surfaced through the API ---


class _StubbedSession:
    def __init__(self, logs_client):
        self._logs = logs_client

    def client(self, name):
        return self._logs


def _stub_logs(app) -> Stubber:
    logs = boto3.client("logs", region_name="us-east-1")
    stubber = Stubber(logs)
    stubber.activate()
    app.state.role_assumer.session_for = lambda **_: _StubbedSession(logs)
    return stubber


@pytest.mark.parametrize(
    ("code", "status", "detail"),
    [
        ("AccessDeniedException", 403, "logs:StartQuery"),
        ("MalformedQueryException", 400, "rejected the query"),
        ("LimitExceededException", 429, "Too many"),
        ("ResourceNotFoundException", 404, "not found"),
        ("ServiceUnavailableException", 502, "ServiceUnavailableException"),
    ],
)
def test_query_aws_errors(app, client, conn_id, code, status, detail):
    _stub_logs(app).add_client_error("start_query", service_error_code=code)

    response = client.post(
        f"/connections/{conn_id}/query", json={"log_groups": ["/x"], "canned_query": "errors"}
    )
    assert response.status_code == status
    assert detail in response.json()["detail"]


def test_log_groups_access_denied(app, client, conn_id):
    _stub_logs(app).add_client_error(
        "describe_log_groups", service_error_code="AccessDeniedException"
    )

    response = client.get(f"/connections/{conn_id}/log-groups")
    assert response.status_code == 403
    assert "logs:DescribeLogGroups" in response.json()["detail"]


def test_query_timeout_returns_504(aws, customer_role_arn, log_groups):
    app = create_app(make_settings(query_timeout_seconds=0))
    with TestClient(app) as client:
        login(client)
        conn_id = _create_verified(client, customer_role_arn)
        stubber = _stub_logs(app)
        stubber.add_response("start_query", {"queryId": "q-1"})
        stubber.add_response("get_query_results", {"status": "Running", "results": []})
        stubber.add_response("stop_query", {"success": True})

        response = client.post(
            f"/connections/{conn_id}/query", json={"log_groups": ["/x"], "canned_query": "errors"}
        )

    assert response.status_code == 504
    assert "stopped" in response.json()["detail"]
    stubber.assert_no_pending_responses()
