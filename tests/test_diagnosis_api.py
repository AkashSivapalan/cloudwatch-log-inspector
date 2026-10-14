from datetime import timedelta

import anthropic
import pytest

from log_inspector.aws.insights import QueryResult
from log_inspector.diagnosis.diagnoser import Diagnoser
from log_inspector.services import analysis as analysis_service
from tests.conftest import make_settings
from tests.fake_claude import FakeClaude, make_message
from tests.test_analysis_api import END, START, FakeInsights
from tests.test_diagnoser import _api_error
from tests.test_logs_api import _create_verified


@pytest.fixture
def insights(monkeypatch):
    fake = FakeInsights()
    monkeypatch.setattr(analysis_service, "run_query", fake)
    return fake


@pytest.fixture
def claude(app):
    fake = FakeClaude()
    app.state.diagnoser = Diagnoser(make_settings(), client=fake)
    return fake


@pytest.fixture
def conn_id(client, customer_role_arn, log_groups):
    return _create_verified(client, customer_role_arn)


def _diagnose(client, conn_id, **extra):
    body = {
        "log_groups": ["/log-inspector/demo"],
        "start_time": START.isoformat(),
        "end_time": END.isoformat(),
        **extra,
    }
    return client.post(f"/connections/{conn_id}/diagnose", json=body)


def test_diagnose_demo_logs(client, conn_id, insights, claude):
    response = _diagnose(client, conn_id, question="Why did checkout fail?")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["question"] == "Why did checkout fail?"
    assert body["model"] == "claude-opus-5"
    assert body["llm_skipped"] is False
    assert body["fallback_used"] is False
    assert body["usage"] == {"input_tokens": 1200, "output_tokens": 800}
    assert body["analysis"] is None

    diagnosis = body["diagnosis"]
    assert diagnosis["severity"] == "high"
    cause = diagnosis["root_causes"][0]
    assert cause["confidence"] == "high"
    # Only the first citation checks out: here P3 is the login failure, so the pool-warning
    # quote names the wrong pattern, and the third quote isn't in the logs at all.
    assert [e["verified"] for e in cause["evidence"]] == [True, False, False]
    assert diagnosis["follow_up_queries"][0]["query"] == "filter @message like /pool/"

    assert [p["id"] for p in body["patterns"]] == ["P1", "P2", "P3", "P4"]
    assert body["patterns"][0]["template"].startswith("ERROR Database connection timeout")
    assert body["patterns_omitted"] == 0


def test_prompt_sent_to_claude(client, conn_id, insights, claude):
    _diagnose(client, conn_id, question="Why did checkout fail?")

    user = claude.calls[0]["messages"][0]["content"]
    assert "## Question\nWhy did checkout fail?" in user
    assert "### P1 | ERROR | 2 occurrences" in user
    # Redaction happened before anything reached Claude.
    assert "bob@example.com" not in user
    assert "<email>" in user


def test_default_question_and_full_analysis(client, conn_id, insights, claude):
    body = _diagnose(client, conn_id, include_analysis=True).json()
    assert body["question"].startswith("What went wrong")
    assert body["analysis"]["cluster_count"] == 4


def test_no_errors_skips_claude(client, conn_id, claude, monkeypatch):
    def quiet(logs_client, *, query, limit, **kwargs):
        rows = [] if "@message" in query and "stats" not in query else []
        return QueryResult("q", rows, 0, 0, 0)

    monkeypatch.setattr(analysis_service, "run_query", quiet)
    body = _diagnose(client, conn_id).json()

    assert body["llm_skipped"] is True
    assert body["model"] is None
    assert body["diagnosis"]["severity"] == "none"
    assert body["diagnosis"]["root_causes"] == []
    assert claude.calls == []


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (_api_error(anthropic.AuthenticationError, 401), 503, "credentials"),
        (_api_error(anthropic.RateLimitError, 429), 429, "rate limiting"),
        (make_message([], stop_reason="refusal"), 502, "declined"),
    ],
)
def test_claude_errors(app, client, conn_id, insights, outcome, status, detail):
    app.state.diagnoser = Diagnoser(make_settings(), client=FakeClaude(outcome))

    response = _diagnose(client, conn_id)
    assert response.status_code == status
    assert detail in response.json()["detail"]


def test_no_api_key_configured(client, conn_id, insights):
    # The app's real Diagnoser, with no credentials anywhere (see conftest).
    response = _diagnose(client, conn_id)
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_diagnose_validation(client, conn_id, insights, claude):
    assert _diagnose(client, conn_id, question="x" * 2001).status_code == 422
    assert _diagnose(client, conn_id, log_groups=[]).status_code == 422
    long_range = _diagnose(client, conn_id, start_time=(END - timedelta(days=8)).isoformat())
    assert long_range.status_code == 400
    assert claude.calls == []


def test_diagnose_requires_verified_connection(client, insights, claude):
    conn_id = client.post("/connections", json={"name": "x"}).json()["connection"]["id"]
    assert _diagnose(client, conn_id).status_code == 409
    assert _diagnose(client, "missing").status_code == 404
