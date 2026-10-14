"""Find stack traces in log messages (Python, Java/JVM, Node.js) and summarise them."""

import re
from dataclasses import dataclass

MAX_FRAMES = 10

_PY_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
_AT_FRAME = re.compile(r"^\s*at\s+(.+?)\s*$")
_EXCEPTION_LINE = re.compile(
    r"^(?:Exception in thread \"[^\"]*\"\s+|Caused by:\s+|\[ERROR\]\s+|Uncaught\s+)?"
    r"([A-Za-z_][\w.$]*(?:Error|Exception|Throwable|Exit|Interrupt|Fault))"
    r"(?::\s*(.*))?$"
)


@dataclass(frozen=True)
class StackTrace:
    exception_type: str
    message: str
    # Where the error was raised: the innermost frame of the root cause.
    origin: str | None
    frames: tuple[str, ...]


def extract_stack_trace(message: str) -> StackTrace | None:
    lines = [line.rstrip() for line in message.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    if any("Traceback (most recent call last)" in line for line in lines):
        return _python(lines)
    if any(_AT_FRAME.match(line) for line in lines[1:]):
        return _jvm_or_node(lines)
    return None


def _exception(line: str) -> tuple[str, str] | None:
    match = _EXCEPTION_LINE.match(line.strip())
    if not match:
        return None
    return match.group(1), (match.group(2) or "").strip()


def _python(lines: list[str]) -> StackTrace:
    frames = [
        f"{m.group(3)} ({m.group(1)}:{m.group(2)})"
        for m in (_PY_FRAME.match(line) for line in lines)
        if m
    ]
    # The exception line is normally last; Lambda also prints it first as "[ERROR] Type: msg".
    exception = next((exc for exc in (_exception(line) for line in reversed(lines)) if exc), None)
    exception_type, text = exception or ("UnknownError", lines[-1].strip())
    return StackTrace(
        exception_type=exception_type,
        message=text,
        origin=frames[-1] if frames else None,
        frames=tuple(frames[-MAX_FRAMES:]),
    )


def _jvm_or_node(lines: list[str]) -> StackTrace:
    # Use the root cause: the last "Caused by:" section if there is one.
    root = max(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("Caused by:")), default=0
    )
    section = lines[root:]
    frames = [m.group(1) for m in (_AT_FRAME.match(line) for line in section) if m]
    exception = next((exc for exc in (_exception(line) for line in section) if exc), None)
    exception_type, text = exception or ("UnknownError", section[0].strip())
    return StackTrace(
        exception_type=exception_type,
        message=text,
        origin=frames[0] if frames else None,
        frames=tuple(frames[:MAX_FRAMES]),
    )
