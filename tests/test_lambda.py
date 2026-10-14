"""M7: the AWS Lambda entry points and the SQS-based investigation queue."""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import pytest
from fastapi.testclient import TestClient

from log_inspector import lambda_handlers
from log_inspector.models import Investigation, InvestigationStatus
from log_inspector.services import analysis as analysis_service
from log_inspector.services import auth
from tests.conftest import login, make_settings
from tests.fake_claude import FakeClaude
from tests.test_analysis_api import END, START, FakeInsights
from tests.test_investigations_api import NeverRunExecutor, make_app
from tests.test_logs_api import _create_verified


@pytest.fixture(autouse=True)
def clean_lambda_env(monkeypatch):
    for var in (
        "LOG_INSPECTOR_DATABASE_URL",
        "LOG_INSPECTOR_DATABASE_SECRET_ARN",
        "LOG_INSPECTOR_ANTHROPIC_API_KEY_PARAMETER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(lambda_handlers, "_api_handler", None)


# --- Secrets ---


def test_load_secrets_from_secrets_manager_and_ssm(aws, monkeypatch):
    secret = boto3.client("secretsmanager", region_name="us-east-1").create_secret(
        Name="rds-secret",
        SecretString=json.dumps({"username": "log_inspector", "password": "p@ss:w/rd#1"}),
    )
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/log-inspector/anthropic-api-key", Value="sk-ant-from-ssm", Type="SecureString"
    )
    monkeypatch.setenv("LOG_INSPECTOR_DATABASE_SECRET_ARN", secret["ARN"])
    monkeypatch.setenv("LOG_INSPECTOR_DATABASE_HOST", "db.internal:5432")
    monkeypatch.setenv("LOG_INSPECTOR_DATABASE_NAME", "log_inspector")
    monkeypatch.setenv(
        "LOG_INSPECTOR_ANTHROPIC_API_KEY_PARAMETER", "/log-inspector/anthropic-api-key"
    )

    lambda_handlers.load_secrets()

    # Special characters in the password are escaped, so the URL still parses.
    assert os.environ["LOG_INSPECTOR_DATABASE_URL"] == (
        "postgresql+psycopg://log_inspector:p%40ss%3Aw%2Frd%231@db.internal:5432/log_inspector"
        "?sslmode=require"
    )
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-ssm"


def test_load_secrets_does_nothing_when_not_configured(monkeypatch):
    lambda_handlers.load_secrets()
    assert "LOG_INSPECTOR_DATABASE_URL" not in os.environ


# --- Admin ---


def test_admin_runs_cli_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_INSPECTOR_DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")

    result = lambda_handlers.admin({"args": ["create-user", "akash"]}, None)

    assert result["exit_code"] == 0
    assert "Created user 'akash'" in result["output"]
    assert "li_" in result["output"]
    # The key went back to the caller only, not to stdout (Lambda would log it).
    assert capsys.readouterr().out == ""

    listed = lambda_handlers.admin({"args": ["list-users"]}, None)
    assert "akash" in listed["output"]
    assert lambda_handlers.admin({"args": ["migrate"]}, None)["exit_code"] == 0


@pytest.mark.parametrize("event", [{}, {"args": "create-user"}, {"args": [1, 2]}])
def test_admin_rejects_malformed_events(event):
    assert lambda_handlers.admin(event, None)["exit_code"] == 2


def test_admin_reports_unknown_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_INSPECTOR_DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    result = lambda_handlers.admin({"args": ["drop-everything"]}, None)
    assert result["exit_code"] == 2
    assert "invalid choice" in result["output"]


# --- Queue and worker ---


@pytest.fixture
def queued_app(aws, monkeypatch, customer_role_arn, log_groups, tmp_path):
    """An app whose investigations go to an SQS queue, like the Lambda deployment."""
    monkeypatch.setattr(analysis_service, "run_query", FakeInsights())
    queue_url = boto3.client("sqs", region_name="us-east-1").create_queue(
        QueueName="investigations"
    )["QueueUrl"]
    settings = make_settings(
        database_url=f"sqlite:///{tmp_path / 'queued.db'}",
        investigation_queue_url=queue_url,
        recover_interrupted_on_startup=False,
    )
    app = make_app(settings, FakeClaude(), NeverRunExecutor())
    monkeypatch.setattr(lambda_handlers, "_app", lambda: app)
    with TestClient(app) as client:
        login(client)
        conn_id = _create_verified(client, customer_role_arn)
        yield client, conn_id, queue_url


def lambda_context(seconds_left: float = 900):
    """What Lambda passes to a handler; only the time remaining matters here."""
    return SimpleNamespace(get_remaining_time_in_millis=lambda: int(seconds_left * 1000))


def _start(client, conn_id):
    body = {
        "connection_id": conn_id,
        "log_groups": ["/log-inspector/demo"],
        "start_time": START.isoformat(),
        "end_time": END.isoformat(),
    }
    response = client.post("/investigations", json=body)
    assert response.status_code == 202
    return response.json()["id"]


def _receive(queue_url):
    sqs = boto3.client("sqs", region_name="us-east-1")
    return sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])


def test_investigation_goes_through_sqs_to_the_worker(queued_app):
    client, conn_id, queue_url = queued_app
    investigation_id = _start(client, conn_id)

    messages = _receive(queue_url)
    assert [json.loads(m["Body"]) for m in messages] == [{"investigation_id": investigation_id}]
    assert client.get(f"/investigations/{investigation_id}").json()["status"] == "queued"

    event = {"Records": [{"body": m["Body"]} for m in messages]}
    assert lambda_handlers.worker(event, lambda_context()) == {"processed": 1}

    body = client.get(f"/investigations/{investigation_id}").json()
    assert body["status"] == "done"
    assert body["result"]["diagnosis"]["severity"] == "high"

    # SQS may deliver a message twice; the second delivery changes nothing.
    lambda_handlers.worker(event, lambda_context())
    assert (
        client.get(f"/investigations/{investigation_id}").json()["finished_at"]
        == body["finished_at"]
    )


def test_worker_gives_up_cleanly_when_lambda_is_nearly_out_of_time(queued_app):
    client, conn_id, queue_url = queued_app
    investigation_id = _start(client, conn_id)
    event = {"Records": [{"body": m["Body"]} for m in _receive(queue_url)]}

    # Too little left for a Claude call once the safety margin is kept back.
    lambda_handlers.worker(event, lambda_context(seconds_left=40))

    body = client.get(f"/investigations/{investigation_id}").json()
    assert (body["status"], body["error_status"]) == ("failed", 504)
    assert "not enough time" in body["error"]
    assert body["finished_at"] is not None


def test_worker_passes_the_time_left_on_to_the_investigation(queued_app, monkeypatch):
    client, conn_id, queue_url = queued_app
    _start(client, conn_id)
    event = {"Records": [{"body": m["Body"]} for m in _receive(queue_url)]}
    runner = client.app.state.investigation_runner
    seen = {}
    monkeypatch.setattr(runner, "run", lambda _id, deadline=None: seen.update(deadline=deadline))
    monkeypatch.setattr(lambda_handlers.time, "monotonic", lambda: 5000.0)

    lambda_handlers.worker(event, lambda_context(seconds_left=900))

    assert seen["deadline"] == 5000.0 + 900 - lambda_handlers.WORKER_SAFETY_MARGIN_SECONDS


def test_queueing_failure_is_recorded(queued_app):
    client, conn_id, queue_url = queued_app
    boto3.client("sqs", region_name="us-east-1").delete_queue(QueueUrl=queue_url)

    investigation_id = _start(client, conn_id)
    body = client.get(f"/investigations/{investigation_id}").json()
    assert (body["status"], body["error_status"]) == ("failed", 503)
    assert "Could not queue" in body["error"]


def test_worker_times_out_stale_investigations(queued_app):
    client, conn_id, _ = queued_app
    stale_id = _start(client, conn_id)
    with client.app.state.session_factory() as db:
        db.get(Investigation, stale_id).created_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()

    lambda_handlers.worker({"Records": []}, lambda_context())

    body = client.get(f"/investigations/{stale_id}").json()
    assert (body["status"], body["error_status"]) == ("failed", 504)
    assert "Timed out" in body["error"]


def test_restart_does_not_fail_other_instances_work_when_recovery_is_off(queued_app):
    client, conn_id, _ = queued_app
    investigation_id = _start(client, conn_id)

    # A new instance starting up (Lambda cold start) leaves recent work alone.
    with TestClient(client.app):
        pass
    with client.app.state.session_factory() as db:
        assert db.get(Investigation, investigation_id).status == InvestigationStatus.QUEUED


# --- API through API Gateway ---


def _http_api_event(method: str, path: str, headers: dict | None = None) -> dict:
    """An API Gateway HTTP API (payload v2.0) event, as Lambda receives it."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "api.example.com", **(headers or {})},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "api",
            "domainName": "api.example.com",
            "domainPrefix": "api",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.1",
                "userAgent": "test",
            },
            "requestId": "request",
            "routeKey": "$default",
            "stage": "$default",
            "time": "07/Oct/2026:12:00:00 +0000",
            "timeEpoch": 1791460800000,
        },
        "isBase64Encoded": False,
    }


def test_api_handler_serves_requests(aws, monkeypatch, tmp_path):
    settings = make_settings(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    app = make_app(settings, FakeClaude(), NeverRunExecutor())
    monkeypatch.setattr(lambda_handlers, "_app", lambda: app)

    health = lambda_handlers.api(_http_api_event("GET", "/health"), None)
    assert health["statusCode"] == 200
    assert json.loads(health["body"])["status"] == "ok"

    denied = lambda_handlers.api(_http_api_event("GET", "/connections"), None)
    assert denied["statusCode"] == 401

    with app.state.session_factory() as db:
        _, plaintext = auth.create_api_key(db, auth.create_user(db, "lambda"), "test")
    allowed = lambda_handlers.api(
        _http_api_event("GET", "/me", {"authorization": f"Bearer {plaintext}"}), None
    )
    assert allowed["statusCode"] == 200
    assert json.loads(allowed["body"])["name"] == "lambda"
