from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError

from log_inspector.aws.canned_queries import CANNED_QUERIES
from log_inspector.aws.errors import AwsRequestFailed, InvalidQuery, translate_logs_error
from log_inspector.aws.insights import QueryResult, run_query
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.models import Connection, ConnectionStatus
from log_inspector.services.connections import session_for_connection

DEFAULT_QUERY_WINDOW = timedelta(hours=1)


class ConnectionNotReady(Exception):
    pass


@dataclass
class LogGroup:
    name: str
    arn: str | None
    stored_bytes: int | None
    retention_days: int | None
    created_at: datetime | None


@dataclass
class LogGroupPage:
    log_groups: list[LogGroup]
    next_token: str | None


@dataclass
class TimeRange:
    start: datetime
    end: datetime


def logs_client_for(connection: Connection, assumer: RoleAssumer):
    if connection.status != ConnectionStatus.VERIFIED:
        raise ConnectionNotReady(
            f"Connection is '{connection.status}'. Verify it with "
            f"POST /connections/{connection.id}/verify before reading logs."
        )
    return session_for_connection(connection, assumer).client("logs")


def list_log_groups(
    connection: Connection,
    assumer: RoleAssumer,
    prefix: str | None,
    limit: int,
    next_token: str | None,
) -> LogGroupPage:
    logs = logs_client_for(connection, assumer)
    params: dict = {"limit": limit}
    if prefix:
        params["logGroupNamePrefix"] = prefix
    if next_token:
        params["nextToken"] = next_token
    try:
        response = logs.describe_log_groups(**params)
    except ClientError as exc:
        raise translate_logs_error(exc, "DescribeLogGroups") from exc
    except BotoCoreError as exc:
        raise AwsRequestFailed(f"CloudWatch Logs DescribeLogGroups failed: {exc}") from exc

    return LogGroupPage(
        log_groups=[
            LogGroup(
                name=group["logGroupName"],
                arn=group.get("arn"),
                stored_bytes=group.get("storedBytes"),
                retention_days=group.get("retentionInDays"),
                created_at=(
                    datetime.fromtimestamp(group["creationTime"] / 1000, UTC)
                    if "creationTime" in group
                    else None
                ),
            )
            for group in response.get("logGroups", [])
        ],
        next_token=response.get("nextToken"),
    )


def resolve_time_range(
    start: datetime | None, end: datetime | None, max_range: timedelta
) -> TimeRange:
    """Fill in defaults (the last hour) and check the range is valid."""
    end = _as_utc(end) if end else datetime.now(UTC)
    start = _as_utc(start) if start else end - DEFAULT_QUERY_WINDOW
    if start >= end:
        raise InvalidQuery("start_time must be before end_time.")
    if end - start > max_range:
        raise InvalidQuery(
            f"The time range is limited to {max_range.days} days per query to control cost."
        )
    return TimeRange(start=start, end=end)


def _as_utc(value: datetime) -> datetime:
    # Times without a timezone are taken to be UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def query_string_for(query: str | None, canned_query: str | None) -> str:
    if canned_query is not None:
        return CANNED_QUERIES[canned_query].query
    assert query is not None
    return query


def run_insights_query(
    connection: Connection,
    assumer: RoleAssumer,
    log_groups: list[str],
    query: str,
    time_range: TimeRange,
    limit: int,
    timeout_seconds: float,
) -> QueryResult:
    return run_query(
        logs_client_for(connection, assumer),
        log_groups=log_groups,
        query=query,
        start=time_range.start,
        end=time_range.end,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )
