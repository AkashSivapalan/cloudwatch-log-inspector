"""Ready-made Logs Insights queries for common failure signals."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CannedQuery:
    name: str
    description: str
    query: str


ERROR_PATTERN = r"/(?i)(error|exception|fatal|panic|traceback|unhandled)/"

CANNED_QUERIES: dict[str, CannedQuery] = {
    q.name: q
    for q in [
        CannedQuery(
            name="errors",
            description="Log lines that mention errors, exceptions, fatals, panics or tracebacks.",
            query=(
                "fields @timestamp, @log, @logStream, @message"
                f" | filter @message like {ERROR_PATTERN}"
                " | sort @timestamp desc"
            ),
        ),
        CannedQuery(
            name="http_5xx",
            description="HTTP 5xx responses, from JSON status fields or text such as 'status=503'.",
            query=(
                "fields @timestamp, @log, @logStream, @message"
                " | filter status >= 500 or statusCode >= 500"
                r" or @message like /(?i)(status|code)\W{1,5}5\d\d\b/"
                " | sort @timestamp desc"
            ),
        ),
        CannedQuery(
            name="lambda_failures",
            description="Lambda timeouts, out-of-memory kills and runtime crashes.",
            query=(
                "fields @timestamp, @log, @logStream, @message"
                " | filter @message like /Task timed out/"
                r" or @message like /(?i)(Runtime\.ExitError|Runtime exited|out of memory)/"
                " | sort @timestamp desc"
            ),
        ),
        CannedQuery(
            name="error_volume",
            description="Count of error lines per 5 minutes, for spotting when a problem started.",
            query=(
                f"filter @message like {ERROR_PATTERN}"
                " | stats count(*) as errors by bin(5m) as time"
                " | sort time asc"
            ),
        ),
        CannedQuery(
            name="recent",
            description="The most recent log lines, unfiltered.",
            query="fields @timestamp, @log, @logStream, @message | sort @timestamp desc",
        ),
    ]
}
