from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from log_inspector.analysis.analyzer import Analysis, LogEvent, analyze, volume_totals
from log_inspector.analysis.redaction import Redactor
from log_inspector.analysis.spikes import (
    VolumeBucket,
    bucket_seconds_for,
    detect_spikes,
    fill_buckets,
    insights_bin,
)
from log_inspector.aws.canned_queries import ERROR_PATTERN
from log_inspector.aws.insights import run_query
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.config import Settings
from log_inspector.models import Connection
from log_inspector.services.logs import TimeRange, logs_client_for

# Lines worth clustering: errors plus warnings and common failure words.
SIGNAL_PATTERN = (
    r"/(?i)(error|exception|fatal|panic|traceback|unhandled|warn|timed out|timeout|refused|"
    r"denied|critical)/"
)
_MAX_BUCKET_ROWS = 10000


@dataclass
class AnalysisReport:
    log_groups: list[str]
    time_range: TimeRange
    bucket_seconds: int
    events_truncated: bool
    total_events: int | None
    total_errors: int
    volume: list[VolumeBucket]
    analysis: Analysis


def analyze_logs(
    connection: Connection,
    assumer: RoleAssumer,
    log_groups: list[str],
    time_range: TimeRange,
    max_events: int,
    settings: Settings,
    on_progress: Callable[[str], None] | None = None,
) -> AnalysisReport:
    """on_progress, if given, is called with "analyzing" once the queries have returned."""
    logs = logs_client_for(connection, assumer)
    bucket_seconds = bucket_seconds_for(time_range.end - time_range.start)
    bin_size = insights_bin(bucket_seconds)
    queries = {
        "events": (
            "fields @timestamp, @log, @logStream, @message"
            f" | filter @message like {SIGNAL_PATTERN}"
            " | sort @timestamp desc",
            max_events,
        ),
        "errors": (
            f"filter @message like {ERROR_PATTERN}"
            f" | stats count(*) as errors by bin({bin_size}) as bucket",
            _MAX_BUCKET_ROWS,
        ),
        "totals": (f"stats count(*) as total by bin({bin_size}) as bucket", _MAX_BUCKET_ROWS),
    }

    # The three queries are independent, so run them at the same time.
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futures = {
            name: pool.submit(
                run_query,
                logs,
                log_groups=log_groups,
                query=query,
                start=time_range.start,
                end=time_range.end,
                limit=limit,
                timeout_seconds=settings.query_timeout_seconds,
            )
            for name, (query, limit) in queries.items()
        }
        results = {name: future.result() for name, future in futures.items()}

    if on_progress:
        on_progress("analyzing")

    events = [_to_event(row) for row in results["events"].rows]
    volume = fill_buckets(
        time_range.start,
        time_range.end,
        bucket_seconds,
        errors=_bucket_counts(results["errors"].rows, "errors"),
        totals=_bucket_counts(results["totals"].rows, "total"),
    )
    total_errors, total_events = volume_totals(volume)
    redactor = Redactor(
        redact_emails=settings.redact_emails,
        redact_ip_addresses=settings.redact_ip_addresses,
    )
    return AnalysisReport(
        log_groups=log_groups,
        time_range=time_range,
        bucket_seconds=bucket_seconds,
        events_truncated=len(events) >= max_events,
        total_events=total_events,
        total_errors=total_errors,
        volume=volume,
        analysis=analyze(
            events,
            spikes=detect_spikes(volume, bucket_seconds),
            redactor=redactor,
            max_clusters=settings.analysis_max_clusters,
        ),
    )


def parse_insights_time(value: str) -> datetime:
    """Insights returns '2026-10-02 14:41:02.019' (UTC); moto returns epoch milliseconds."""
    if value.isdigit():
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC)


def _to_event(row: dict[str, str]) -> LogEvent:
    # @log is "<account-id>:<log-group-name>".
    log_group = row.get("@log", "")
    return LogEvent(
        timestamp=parse_insights_time(row["@timestamp"]),
        log_group=log_group.split(":", 1)[1] if ":" in log_group else log_group,
        log_stream=row.get("@logStream", ""),
        message=row.get("@message", ""),
    )


def _bucket_counts(rows: list[dict[str, str]], field: str) -> dict[datetime, int]:
    counts: dict[datetime, int] = {}
    for row in rows:
        if "bucket" in row and field in row:
            counts[parse_insights_time(row["bucket"])] = int(float(row[field]))
    return counts
