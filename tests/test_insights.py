from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from log_inspector.aws.errors import (
    InvalidQuery,
    LogGroupNotFound,
    LogsAccessDenied,
    QueryFailed,
    QueryLimitExceeded,
    QueryTimedOut,
)
from log_inspector.aws.insights import run_query

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
END = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)


class FakeLogs:
    """Scripted Logs client: get_query_results returns the given statuses in order."""

    def __init__(self, statuses, results=None, stop_error=None):
        self.statuses = list(statuses)
        self.results = results or []
        self.stop_error = stop_error
        self.start_kwargs = None
        self.stopped = []

    def start_query(self, **kwargs):
        self.start_kwargs = kwargs
        return {"queryId": "q-1"}

    def get_query_results(self, queryId):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {
            "status": status,
            "results": self.results if status == "Complete" else [],
            "statistics": {"recordsMatched": 2.0, "recordsScanned": 10.0, "bytesScanned": 512.0},
        }

    def stop_query(self, queryId):
        self.stopped.append(queryId)
        if self.stop_error:
            raise self.stop_error
        return {"success": True}


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _run(logs, timeout=60, clock=None):
    clock = clock or FakeClock()
    return run_query(
        logs,
        log_groups=["/app"],
        query="fields @message",
        start=START,
        end=END,
        limit=100,
        timeout_seconds=timeout,
        poll_interval=1.0,
        sleep=clock.sleep,
        clock=clock,
    )


def test_polls_until_complete_and_parses_rows():
    results = [
        [
            {"field": "@timestamp", "value": "2026-10-01 12:30:00.000"},
            {"field": "@message", "value": "ERROR boom"},
            {"field": "@ptr", "value": "opaque"},
        ]
    ]
    logs = FakeLogs(["Scheduled", "Running", "Complete"], results)
    clock = FakeClock()

    result = _run(logs, clock=clock)

    assert result.query_id == "q-1"
    assert result.rows == [{"@timestamp": "2026-10-01 12:30:00.000", "@message": "ERROR boom"}]
    assert (result.records_matched, result.records_scanned, result.bytes_scanned) == (2, 10, 512)
    assert clock.sleeps == [1.0, 1.0]
    assert logs.start_kwargs == {
        "logGroupNames": ["/app"],
        "startTime": int(START.timestamp()),
        "endTime": int(END.timestamp()),
        "queryString": "fields @message",
        "limit": 100,
    }


@pytest.mark.parametrize("status", ["Failed", "Cancelled", "Timeout", "Unknown"])
def test_failed_statuses_raise(status):
    with pytest.raises(QueryFailed, match=status):
        _run(FakeLogs(["Running", status]))


def test_timeout_stops_the_query():
    logs = FakeLogs(["Running"])

    with pytest.raises(QueryTimedOut, match="5 seconds"):
        _run(logs, timeout=5)
    assert logs.stopped == ["q-1"]


def test_timeout_still_raised_if_stop_fails():
    error = ClientError({"Error": {"Code": "InvalidParameterException"}}, "StopQuery")
    logs = FakeLogs(["Running"], stop_error=error)

    with pytest.raises(QueryTimedOut):
        _run(logs, timeout=2)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("MalformedQueryException", InvalidQuery),
        ("InvalidParameterException", InvalidQuery),
        ("LimitExceededException", QueryLimitExceeded),
        ("ResourceNotFoundException", LogGroupNotFound),
        ("AccessDeniedException", LogsAccessDenied),
    ],
)
def test_start_query_errors_are_translated(code, expected):
    logs = boto3.client("logs", region_name="us-east-1")
    stubber = Stubber(logs)
    stubber.add_client_error("start_query", service_error_code=code, http_status_code=400)
    stubber.activate()

    with pytest.raises(expected):
        _run(logs)
