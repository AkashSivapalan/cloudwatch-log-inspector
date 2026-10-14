"""Request IDs, request logging, and optional JSON logs."""

import json
import logging
import re
import time
import uuid

from fastapi import FastAPI, Request

logger = logging.getLogger("log_inspector.requests")

REQUEST_ID_HEADER = "X-Request-ID"
# Accept a caller's request ID only if it's short and plain, so it can't forge log lines.
_VALID_REQUEST_ID = re.compile(r"^[\w-]{1,64}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str, log_format: str) -> None:
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def add_request_logging(app: FastAPI) -> None:
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        fields = {
            "request_id": request_id,
            "method": request.method,
            # The path only: query strings can carry tokens. Headers (with API keys) are
            # never logged.
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "user_id": getattr(request.state, "user_id", None),
        }
        logger.info(
            "%s %s -> %s (%sms) user=%s request_id=%s",
            fields["method"],
            fields["path"],
            fields["status"],
            fields["duration_ms"],
            fields["user_id"],
            request_id,
            extra={"fields": fields},
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
