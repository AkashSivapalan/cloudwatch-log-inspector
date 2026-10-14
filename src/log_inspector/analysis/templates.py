"""Turn log lines into templates (variable parts replaced) and detect their severity."""

import re

MAX_TEMPLATE_LENGTH = 300

# Applied in order: specific shapes first, bare numbers last.
_NORMALIZERS = [
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
        ),
        "<ts>",
    ),
    (
        re.compile(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b"
        ),
        "<ts>",
    ),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    (
        re.compile(
            r"\b(?:0x[0-9a-fA-F]+|(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{12,})\b"
        ),
        "<hex>",
    ),
    (re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?"), "<num>"),
    (re.compile(r"\s+"), " "),
]


def normalize(message: str) -> str:
    """Replace IDs, timestamps and numbers so that similar lines share one template."""
    for pattern, replacement in _NORMALIZERS:
        message = pattern.sub(replacement, message)
    message = message.strip()
    if len(message) > MAX_TEMPLATE_LENGTH:
        message = message[:MAX_TEMPLATE_LENGTH] + "…"
    return message


# --- Severity ---

LEVELS = ("FATAL", "ERROR", "WARN", "INFO", "DEBUG", "UNKNOWN")
_SEVERITY = {level: rank for rank, level in enumerate(LEVELS)}

_LEVEL_ALIASES = {
    "FATAL": "FATAL",
    "CRITICAL": "FATAL",
    "CRIT": "FATAL",
    "EMERG": "FATAL",
    "PANIC": "FATAL",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "WARN": "WARN",
    "WARNING": "WARN",
    "INFO": "INFO",
    "NOTICE": "INFO",
    "DEBUG": "DEBUG",
    "TRACE": "DEBUG",
}

# An explicit uppercase level token near the start, e.g. "ERROR ...", "[WARN]", "\tERROR\t".
_LEVEL_TOKEN = re.compile(r"\b(FATAL|CRITICAL|PANIC|ERROR|WARNING|WARN|INFO|DEBUG|TRACE)\b")
_JSON_LEVEL = re.compile(r'"(?:level|severity|levelname|log_level)"\s*:\s*"(\w+)"', re.IGNORECASE)
_KEYWORDS = [
    ("FATAL", re.compile(r"(?i)\b(fatal|panic)\b")),
    (
        "ERROR",
        # "error"/"exception" may end a word too: KeyError, NullPointerException.
        re.compile(
            r"(?i)((error|exception)\b|\b(traceback|unhandled|task timed out|runtime exited)\b)"
        ),
    ),
    ("WARN", re.compile(r"(?i)\bwarn(ing)?\b")),
]


def detect_level(message: str) -> str:
    head = message[:120]
    token = _LEVEL_TOKEN.search(head)
    if token:
        return _LEVEL_ALIASES[token.group(1)]
    json_level = _JSON_LEVEL.search(message)
    if json_level:
        return _LEVEL_ALIASES.get(json_level.group(1).upper(), "UNKNOWN")
    for level, pattern in _KEYWORDS:
        if pattern.search(message):
            return level
    return "UNKNOWN"


def severity_rank(level: str) -> int:
    """Lower is more severe."""
    return _SEVERITY.get(level, len(LEVELS))


def more_severe(a: str, b: str) -> str:
    return a if severity_rank(a) <= severity_rank(b) else b
