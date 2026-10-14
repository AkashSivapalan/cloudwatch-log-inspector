import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

from log_inspector.aws.errors import (
    AwsRequestFailed,
    QueryFailed,
    QueryTimedOut,
    translate_logs_error,
)

logger = logging.getLogger(__name__)

_FAILED_STATUSES = {"Failed", "Cancelled", "Timeout", "Unknown"}


@dataclass
class QueryResult:
    query_id: str
    rows: list[dict[str, str]]
    records_matched: float
    records_scanned: float
    bytes_scanned: float


def run_query(
    logs_client,
    log_groups: list[str],
    query: str,
    start: datetime,
    end: datetime,
    limit: int,
    timeout_seconds: float,
    poll_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> QueryResult:
    """Run a Logs Insights query and wait for it to finish, stopping it on timeout."""
    try:
        query_id = logs_client.start_query(
            logGroupNames=log_groups,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=query,
            limit=limit,
        )["queryId"]
    except ClientError as exc:
        raise translate_logs_error(exc, "StartQuery") from exc
    except BotoCoreError as exc:
        raise AwsRequestFailed(f"CloudWatch Logs StartQuery failed: {exc}") from exc

    deadline = clock() + timeout_seconds
    while True:
        try:
            response = logs_client.get_query_results(queryId=query_id)
        except ClientError as exc:
            raise translate_logs_error(exc, "GetQueryResults") from exc
        except BotoCoreError as exc:
            raise AwsRequestFailed(f"CloudWatch Logs GetQueryResults failed: {exc}") from exc

        status = response["status"]
        if status == "Complete":
            return _to_result(query_id, response)
        if status in _FAILED_STATUSES:
            raise QueryFailed(f"CloudWatch Logs Insights ended the query with status {status}.")
        if clock() >= deadline:
            _stop_quietly(logs_client, query_id)
            raise QueryTimedOut(
                f"The query did not finish within {timeout_seconds:g} seconds and was stopped. "
                "Narrow the time range or the number of log groups."
            )
        sleep(poll_interval)


def _to_result(query_id: str, response: dict) -> QueryResult:
    rows = [
        {field["field"]: field["value"] for field in row if field["field"] != "@ptr"}
        for row in response.get("results", [])
    ]
    stats = response.get("statistics", {})
    return QueryResult(
        query_id=query_id,
        rows=rows,
        records_matched=stats.get("recordsMatched", 0.0),
        records_scanned=stats.get("recordsScanned", 0.0),
        bytes_scanned=stats.get("bytesScanned", 0.0),
    )


def _stop_quietly(logs_client, query_id: str) -> None:
    # Best effort: the query may have just finished, or StopQuery may not be permitted.
    try:
        logs_client.stop_query(queryId=query_id)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Could not stop query %s: %s", query_id, exc)
