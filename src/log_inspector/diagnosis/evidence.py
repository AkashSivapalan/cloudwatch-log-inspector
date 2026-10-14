"""Check that the evidence Claude cites really appears in the logs it was shown."""

import re

from log_inspector.analysis.analyzer import Cluster

_WHITESPACE = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")


def _squash(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def evidence_is_verified(pattern_id: str, excerpt: str, patterns: dict[str, Cluster]) -> bool:
    """True if the pattern exists and the excerpt is quoted from its examples or template.

    Whitespace differences are ignored, and an excerpt shortened with "..." passes if each
    quoted piece appears in the same source line.
    """
    cluster = patterns.get(pattern_id)
    if cluster is None:
        return False
    pieces = [_squash(p) for p in _ELLIPSIS.split(excerpt) if p.strip()]
    if not pieces:
        return False
    sources = [_squash(text) for text in [*cluster.examples, cluster.template]]
    if cluster.stack_trace:
        trace = cluster.stack_trace
        sources.append(_squash(f"{trace.exception_type}: {trace.message}"))
    return any(all(piece in source for piece in pieces) for source in sources)
