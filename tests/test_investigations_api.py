import threading
import time
from datetime import UTC, datetime, timedelta

import anthropic
import pytest
from fastapi.testclient import TestClient

from log_inspector.aws.errors import LogsAccessDenied
from log_inspector.aws.insights import QueryResult
from log_inspector.diagnosis.diagnoser import Diagnoser
from log_inspector.main import create_app
from log_inspector.models import Investigation, InvestigationStatus
from log_inspector.services import analysis as analysis_service
from log_inspector.services import auth
from log_inspector.services.investigations import InvestigationRunner
from tests.conftest import login, make_settings, use_key
from tests.fake_claude import FakeClaude
from tests.test_analysis_api import END, START, FakeInsights
from tests.test_diagnoser import _api_error
from tests.test_logs_api import _create_verified


class InlineExecutor:
    """Runs submitted work immediately, so tests don't need to wait."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class NeverRunExecutor(InlineExecutor):
    """Accepts work and never runs it, like a server that stopped mid-queue."""

    def submit(self, fn, *args, **kwargs):
        pass


def make_app(settings, claude, executor):
    app = create_app(settings)
    diagnoser = Diagnoser(settings, client=claude)
    app.state.diagnoser = diagnoser
    app.state.investigation_runner = InvestigationRunner(
        app.state.session_factory, app.state.role_assumer, diagnoser, settings, executor=executor
    )
    return app


@pytest.fixture
def insights(monkeypatch):
    fake = FakeInsights()
    monkeypatch.setattr(analysis_service, "run_query", fake)
    return fake


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def client(aws, claude):
    with TestClient(make_app(make_settings(), claude, InlineExecutor())) as test_client:
        login(test_client)
        yield test_client


@pytest.fixture
def conn_id(client, customer_role_arn, log_groups):
    return _create_verified(client, customer_role_arn)


def _start(client, conn_id, **extra):
    body = {
        "connection_id": conn_id,
        "log_groups": ["/log-inspector/demo"],
        "start_time": START.isoformat(),
        "end_time": END.isoformat(),
        **extra,
    }
    return client.post("/investigations", json=body)


def test_start_returns_202_then_result_is_available(client, conn_id, insights, claude):
    response = _start(client, conn_id, question="Why did checkout fail?")
    assert response.status_code == 202, response.text
    started = response.json()
    assert started["status"] == "queued"
    assert started["finished"] is False
    assert started["result"] is None

    body = client.get(f"/investigations/{started['id']}").json()
    assert body["status"] == "done"
    assert body["finished"] is True
    assert body["question"] == "Why did checkout fail?"
    assert body["error"] is None
    assert body["duration_seconds"] >= 0
    result = body["result"]
    assert result["diagnosis"]["severity"] == "high"
    assert result["model"] == "claude-opus-5"
    assert result["analysis"] is None  # omitted unless asked for
    assert len(claude.calls) == 1

    full = client.get(f"/investigations/{started['id']}", params={"include_analysis": True}).json()
    assert full["result"]["analysis"]["cluster_count"] == 4


def test_time_range_is_fixed_when_started(client, conn_id, insights):
    body = client.post(
        "/investigations", json={"connection_id": conn_id, "log_groups": ["/x"]}
    ).json()
    start = datetime.fromisoformat(body["start_time"])
    end = datetime.fromisoformat(body["end_time"])
    assert end - start == timedelta(hours=1)
    assert insights.calls[0]["start"] == start


def test_runs_in_background_and_reports_progress(
    aws, insights, customer_role_arn, log_groups, tmp_path
):
    release = threading.Event()

    class SlowClaude(FakeClaude):
        def _stream(self, **params):
            release.wait(timeout=10)
            return super()._stream(**params)

    settings = make_settings(database_url=f"sqlite:///{tmp_path / 'jobs.db'}")
    app = make_app(settings, SlowClaude(), executor=None)  # None: the real thread pool
    with TestClient(app) as client:
        login(client)
        conn_id = _create_verified(client, customer_role_arn)
        response = _start(client, conn_id)
        assert response.status_code == 202
        investigation_id = response.json()["id"]

        # The request returned while Claude is still "thinking".
        assert _wait_for(client, investigation_id, "diagnosing")["result"] is None
        release.set()
        assert (
            _wait_for(client, investigation_id, "done")["result"]["diagnosis"]["severity"] == "high"
        )


def _wait_for(client, investigation_id, status, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/investigations/{investigation_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"never reached {status}; last: {body['status']}")


@pytest.mark.parametrize(
    ("claude_outcome", "status", "message"),
    [
        (_api_error(anthropic.RateLimitError, 429), 429, "rate limiting"),
        (_api_error(anthropic.AuthenticationError, 401), 503, "credentials"),
    ],
)
def test_claude_failures_are_recorded(
    client, conn_id, insights, claude, claude_outcome, status, message
):
    claude.outcome = claude_outcome
    investigation_id = _start(client, conn_id).json()["id"]

    body = client.get(f"/investigations/{investigation_id}").json()
    assert body["status"] == "failed"
    assert body["error_status"] == status
    assert message in body["error"]
    assert body["result"] is None
    assert body["finished_at"] is not None


def test_aws_failures_are_recorded(client, conn_id, monkeypatch):
    def denied(*args, **kwargs):
        raise LogsAccessDenied("The role is not allowed to call logs:StartQuery.")

    monkeypatch.setattr(analysis_service, "run_query", denied)
    body = client.get(f"/investigations/{_start(client, conn_id).json()['id']}").json()
    assert (body["status"], body["error_status"]) == ("failed", 403)
    assert "logs:StartQuery" in body["error"]


def test_unexpected_errors_are_recorded_without_details(client, conn_id, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(analysis_service, "run_query", broken)
    body = client.get(f"/investigations/{_start(client, conn_id).json()['id']}").json()
    assert (body["status"], body["error_status"]) == ("failed", 500)
    assert "secret internal detail" not in body["error"]


def test_no_errors_in_logs_finishes_without_claude(client, conn_id, claude, monkeypatch):
    monkeypatch.setattr(
        analysis_service, "run_query", lambda *a, **kw: QueryResult("q", [], 0, 0, 0)
    )
    body = client.get(f"/investigations/{_start(client, conn_id).json()['id']}").json()
    assert body["status"] == "done"
    assert body["result"]["llm_skipped"] is True
    assert claude.calls == []


def test_start_validation(client, conn_id, customer_role_arn, insights):
    assert _start(client, "missing").status_code == 404
    assert _start(client, conn_id, log_groups=[]).status_code == 422
    assert _start(client, conn_id, question="x" * 2001).status_code == 422
    too_long = _start(client, conn_id, start_time=(END - timedelta(days=8)).isoformat())
    assert too_long.status_code == 400

    unverified = client.post("/connections", json={"name": "new"}).json()["connection"]["id"]
    assert _start(client, unverified).status_code == 409

    assert client.get("/investigations").json() == []


def test_list_history(client, conn_id, insights, claude, customer_role_arn):
    first = _start(client, conn_id, question="first").json()["id"]
    claude.outcome = _api_error(anthropic.RateLimitError, 429)
    second = _start(client, conn_id, question="second").json()["id"]

    history = client.get("/investigations").json()
    assert [i["id"] for i in history] == [second, first]
    assert history[0]["status"] == "failed"
    assert history[0]["summary"] is None
    assert history[1]["severity"] == "high"
    assert history[1]["summary"].startswith("The orders database")

    assert [i["id"] for i in client.get("/investigations", params={"status": "done"}).json()] == [
        first
    ]
    assert len(client.get("/investigations", params={"limit": 1}).json()) == 1
    assert client.get("/investigations", params={"connection_id": "other"}).json() == []
    assert client.get("/investigations", params={"status": "nope"}).status_code == 422


def test_unknown_investigation(client):
    assert client.get("/investigations/missing").status_code == 404


def test_deleting_a_connection_deletes_its_investigations(client, conn_id, insights):
    investigation_id = _start(client, conn_id).json()["id"]
    assert client.delete(f"/connections/{conn_id}").status_code == 204
    assert client.get(f"/investigations/{investigation_id}").status_code == 404


def test_history_survives_restart_and_interrupted_jobs_fail(
    aws, insights, customer_role_arn, log_groups, tmp_path
):
    settings = make_settings(database_url=f"sqlite:///{tmp_path / 'history.db'}")

    with TestClient(make_app(settings, FakeClaude(), InlineExecutor())) as first:
        key = login(first)
        conn_id = _create_verified(first, customer_role_arn)
        done_id = _start(first, conn_id).json()["id"]

    # A server that stops before running its queue.
    with TestClient(make_app(settings, FakeClaude(), NeverRunExecutor())) as second:
        use_key(second, key)
        stuck_id = _start(second, conn_id).json()["id"]
        assert second.get(f"/investigations/{stuck_id}").json()["status"] == "queued"

    with TestClient(make_app(settings, FakeClaude(), InlineExecutor())) as third:
        use_key(third, key)
        assert third.get(f"/investigations/{done_id}").json()["status"] == "done"
        stuck = third.get(f"/investigations/{stuck_id}").json()
        assert (stuck["status"], stuck["error_status"]) == ("failed", 503)
        assert "server restarted" in stuck["error"]
        assert len(third.get("/investigations").json()) == 2


# --- Runs whose worker died without recording a failure ---


@pytest.fixture
def stuck_client(aws, claude):
    """A server that accepts investigations and never runs them, like a dead worker."""
    with TestClient(make_app(make_settings(), claude, NeverRunExecutor())) as test_client:
        login(test_client)
        yield test_client


def _age(client, investigation_id, created=None, started=None, status=None):
    """Make an investigation look like it was requested/started that long ago."""
    now = datetime.now(UTC)
    with client.app.state.session_factory() as db:
        row = db.get(Investigation, investigation_id)
        if created is not None:
            row.created_at = now - created
        if started is not None:
            row.started_at = now - started
        if status is not None:
            row.status = status
        db.commit()


def _db_status(client, investigation_id):
    with client.app.state.session_factory() as db:
        return db.get(Investigation, investigation_id).status


HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def test_fetching_a_dead_run_reports_it_as_failed(stuck_client, customer_role_arn, log_groups):
    conn_id = _create_verified(stuck_client, customer_role_arn)
    investigation_id = _start(stuck_client, conn_id).json()["id"]
    _age(stuck_client, investigation_id, created=HOUR)

    body = stuck_client.get(f"/investigations/{investigation_id}").json()

    assert (body["status"], body["finished"], body["error_status"]) == ("failed", True, 504)
    assert "Timed out" in body["error"]
    assert body["finished_at"] is not None


def test_a_recent_run_is_left_alone(stuck_client, customer_role_arn, log_groups):
    conn_id = _create_verified(stuck_client, customer_role_arn)
    investigation_id = _start(stuck_client, conn_id).json()["id"]
    _age(stuck_client, investigation_id, created=5 * MINUTE)

    assert stuck_client.get(f"/investigations/{investigation_id}").json()["status"] == "queued"


def test_a_run_that_waited_in_the_queue_but_just_started_is_not_failed(
    stuck_client, customer_role_arn, log_groups
):
    conn_id = _create_verified(stuck_client, customer_role_arn)
    investigation_id = _start(stuck_client, conn_id).json()["id"]
    # Requested an hour ago (a long queue), but a worker picked it up a minute ago.
    _age(
        stuck_client,
        investigation_id,
        created=HOUR,
        started=MINUTE,
        status=InvestigationStatus.QUERYING,
    )

    assert stuck_client.get(f"/investigations/{investigation_id}").json()["status"] == "querying"


def test_a_sweep_triggered_by_a_dead_run_spares_one_that_just_started(
    stuck_client, customer_role_arn, log_groups
):
    conn_id = _create_verified(stuck_client, customer_role_arn)
    dead = _start(stuck_client, conn_id).json()["id"]
    working = _start(stuck_client, conn_id).json()["id"]
    _age(stuck_client, dead, created=HOUR)
    _age(
        stuck_client,
        working,
        created=HOUR,
        started=MINUTE,
        status=InvestigationStatus.QUERYING,
    )

    # Reading the dead run triggers the sweep, which must judge the other run by when it
    # started, not by when it was requested.
    assert stuck_client.get(f"/investigations/{dead}").json()["status"] == "failed"
    assert _db_status(stuck_client, working) == InvestigationStatus.QUERYING


def test_a_run_that_started_long_ago_is_failed(stuck_client, customer_role_arn, log_groups):
    conn_id = _create_verified(stuck_client, customer_role_arn)
    investigation_id = _start(stuck_client, conn_id).json()["id"]
    _age(
        stuck_client,
        investigation_id,
        created=HOUR,
        started=20 * MINUTE,
        status=InvestigationStatus.DIAGNOSING,
    )

    body = stuck_client.get(f"/investigations/{investigation_id}").json()
    assert (body["status"], body["error_status"]) == ("failed", 504)


def test_finished_investigations_are_never_touched(client, conn_id, insights):
    investigation_id = _start(client, conn_id).json()["id"]
    assert client.get(f"/investigations/{investigation_id}").json()["status"] == "done"
    _age(client, investigation_id, created=HOUR)

    body = client.get(f"/investigations/{investigation_id}").json()
    assert (body["status"], body["error"]) == ("done", None)


def test_listing_settles_dead_runs(stuck_client, customer_role_arn, log_groups):
    conn_id = _create_verified(stuck_client, customer_role_arn)
    dead = _start(stuck_client, conn_id).json()["id"]
    fresh = _start(stuck_client, conn_id).json()["id"]
    _age(stuck_client, dead, created=HOUR)

    rows = {r["id"]: r for r in stuck_client.get("/investigations").json()}

    assert rows[dead]["status"] == "failed"
    assert "Timed out" in rows[dead]["error"]
    assert rows[fresh]["status"] == "queued"


def test_dead_runs_stop_counting_against_the_active_limit(
    stuck_client, customer_role_arn, log_groups
):
    conn_id = _create_verified(stuck_client, customer_role_arn)
    ids = [_start(stuck_client, conn_id).json()["id"] for _ in range(3)]

    # Three live runs fill the limit...
    assert _start(stuck_client, conn_id).status_code == 429

    # ...but once they are long dead, a new one is accepted.
    for investigation_id in ids:
        _age(stuck_client, investigation_id, created=HOUR)
    assert _start(stuck_client, conn_id).status_code == 202
    assert {_db_status(stuck_client, i) for i in ids} == {InvestigationStatus.FAILED}


def test_reading_only_settles_the_readers_own_runs(stuck_client, customer_role_arn, log_groups):
    mine_conn = _create_verified(stuck_client, customer_role_arn)
    mine = _start(stuck_client, mine_conn).json()["id"]
    _age(stuck_client, mine, created=HOUR)

    # Someone else, with their own dead run.
    with stuck_client.app.state.session_factory() as db:
        _, other_key = auth.create_api_key(db, auth.create_user(db, "other"), "test")
    use_key(stuck_client, other_key)
    other_conn = _create_verified(stuck_client, customer_role_arn)
    theirs = _start(stuck_client, other_conn).json()["id"]
    _age(stuck_client, theirs, created=HOUR)

    stuck_client.get("/investigations")  # the other user reads

    assert _db_status(stuck_client, theirs) == InvestigationStatus.FAILED
    assert _db_status(stuck_client, mine) == InvestigationStatus.QUEUED
