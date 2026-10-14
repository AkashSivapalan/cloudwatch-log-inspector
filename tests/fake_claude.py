"""A scripted stand-in for anthropic.Anthropic, used by the diagnosis tests."""

import json
from types import SimpleNamespace

DEMO_DIAGNOSIS = {
    "summary": "The orders database connection pool ran out, so checkout requests timed out.",
    "severity": "high",
    "root_causes": [
        {
            "title": "Database connection pool exhausted",
            "explanation": "The pool hit 90% just before connections started timing out.",
            "confidence": "high",
            "evidence": [
                {
                    "pattern_id": "P1",
                    "log_excerpt": "Database connection timeout after 30000ms",
                    "relevance": "Connections to orders-db time out.",
                },
                {
                    "pattern_id": "P3",
                    "log_excerpt": "DB connection pool at 90% capacity",
                    "relevance": "The pool was nearly full first.",
                },
                {
                    "pattern_id": "P2",
                    "log_excerpt": "this text is not in the logs",
                    "relevance": "Made up.",
                },
            ],
            "remediation": ["Raise the pool size", "Find slow queries holding connections"],
        }
    ],
    "affected_components": ["checkout-service", "orders-db"],
    "follow_up_queries": [
        {"purpose": "Pool usage over time", "query": "filter @message like /pool/"}
    ],
    "open_questions": ["Did traffic increase?"],
}


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def make_message(content=None, stop_reason="end_turn", stop_details=None, iterations=None):
    if content is None:
        content = [
            SimpleNamespace(type="thinking", thinking=""),
            text_block(json.dumps(DEMO_DIAGNOSIS)),
        ]
    return SimpleNamespace(
        model="claude-opus-5",
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=1200, output_tokens=800, iterations=iterations),
    )


class _Stream:
    def __init__(self, outcome, events=(), on_event=None):
        self._outcome = outcome
        self._events = events
        self._on_event = on_event

    def __enter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for event in self._events:
            if self._on_event:
                self._on_event(event)
            yield event

    def get_final_message(self):
        return self._outcome


class FakeClaude:
    """Records each request and returns (or raises) the scripted outcome."""

    def __init__(self, outcome=None, events=(), on_event=None):
        """events are yielded one by one while streaming; on_event runs before each (tests use
        it to advance a fake clock)."""
        self.outcome = outcome if outcome is not None else make_message()
        self.events = events
        self.on_event = on_event
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.calls.append(params)
        return _Stream(self.outcome, self.events, self.on_event)
