from datetime import UTC, datetime, timedelta

from log_inspector.analysis.analyzer import LogEvent, analyze
from log_inspector.analysis.redaction import Redactor
from log_inspector.analysis.spikes import Spike, VolumeBucket
from log_inspector.diagnosis.evidence import evidence_is_verified
from log_inspector.diagnosis.prompt import DEFAULT_QUESTION, SYSTEM_PROMPT, build_prompt
from log_inspector.diagnosis.schema import (
    DIAGNOSIS_SCHEMA,
    Diagnosis,
    Evidence,
    FollowUpQuery,
    RootCause,
)
from log_inspector.services.analysis import AnalysisReport
from log_inspector.services.logs import TimeRange
from tests.test_analyzer import DEMO
from tests.test_stack_traces import PYTHON

T0 = datetime(2026, 10, 2, 18, 15, tzinfo=UTC)


def _report(messages=DEMO) -> AnalysisReport:
    events = [
        LogEvent(T0 + timedelta(seconds=i), "/log-inspector/demo", "s", m)
        for i, m in enumerate(messages)
    ]
    spike = Spike(T0, T0 + timedelta(minutes=1), 4, 4, 0)
    return AnalysisReport(
        log_groups=["/log-inspector/demo"],
        time_range=TimeRange(T0 - timedelta(hours=1), T0 + timedelta(minutes=5)),
        bucket_seconds=60,
        events_truncated=False,
        total_events=8,
        total_errors=4,
        volume=[VolumeBucket(T0, 4, 8), VolumeBucket(T0 + timedelta(minutes=1), 0, 0)],
        analysis=analyze(events, spikes=[spike], redactor=Redactor()),
    )


def test_prompt_contains_the_summary():
    prompt = build_prompt("Why did checkout fail?", _report(), max_chars=100_000)

    assert prompt.system == SYSTEM_PROMPT
    text = prompt.user
    assert "## Question\nWhy did checkout fail?" in text
    assert "Log groups: /log-inspector/demo" in text
    assert "Log lines in range: 8; lines matching the error pattern: 4" in text
    assert "- 2026-10-02 18:15:00 to 2026-10-02 18:16:00: 4 errors" in text
    assert "[spike_start]" in text
    assert "- 2026-10-02 18:15:00: errors=4 total=8" in text
    assert "18:16:00: errors=0" not in text  # empty buckets are left out
    assert "### P1 | ERROR | 2 occurrences" in text
    assert f"<log_line>\n{DEMO[1]}\n</log_line>" in text
    assert list(prompt.patterns) == ["P1", "P2", "P3"]
    assert prompt.patterns_omitted == 0


def test_default_question():
    assert DEFAULT_QUESTION in build_prompt(None, _report(), max_chars=100_000).user


def test_stack_traces_are_included():
    text = build_prompt(None, _report([PYTHON]), max_chars=100_000).user
    assert (
        "Stack trace: KeyError: 'order_id' (raised at load_order (/var/task/orders.py:10))" in text
    )


def test_size_limit_drops_lowest_priority_patterns():
    full = build_prompt(None, _report(), max_chars=100_000)
    limited = build_prompt(None, _report(), max_chars=len(full.user) - 50)

    assert list(limited.patterns) == ["P1", "P2"]
    assert limited.patterns_omitted == 1
    assert "### P3" not in limited.user
    assert "1 lower-priority patterns were left out" in limited.user


def test_first_pattern_is_kept_even_if_over_the_limit():
    prompt = build_prompt(None, _report(), max_chars=10)
    assert list(prompt.patterns) == ["P1"]


def test_schema_fields_match_the_models():
    # Regression: a field named "title" was once stripped along with Pydantic's title
    # annotations, so Claude was never asked for RootCause.title and validation failed.
    props = DIAGNOSIS_SCHEMA["properties"]
    root_cause = props["root_causes"]["items"]
    cases = [
        (DIAGNOSIS_SCHEMA, Diagnosis),
        (root_cause, RootCause),
        (root_cause["properties"]["evidence"]["items"], Evidence),
        (props["follow_up_queries"]["items"], FollowUpQuery),
    ]
    for schema, model in cases:
        assert set(schema["properties"]) == set(model.model_fields), model.__name__
        assert set(schema["required"]) == set(model.model_fields), model.__name__


def test_schema_is_strict_and_self_contained():
    def walk(node):
        if isinstance(node, dict):
            assert "$ref" not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert sorted(node["required"]) == sorted(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(DIAGNOSIS_SCHEMA)
    assert "$defs" not in DIAGNOSIS_SCHEMA
    severity = DIAGNOSIS_SCHEMA["properties"]["severity"]
    assert severity["enum"] == ["critical", "high", "medium", "low", "none"]


def test_evidence_verification():
    patterns = build_prompt(None, _report(), max_chars=100_000).patterns

    assert evidence_is_verified("P1", "Database connection timeout after 30000ms", patterns)
    assert evidence_is_verified("P1", "  Database   connection timeout  ", patterns)
    assert evidence_is_verified("P1", "connection timeout ... order_id=1002", patterns)
    assert evidence_is_verified("P1", "timeout after <num>ms", patterns)  # from the template
    assert not evidence_is_verified("P1", "pool at 90% capacity", patterns)  # wrong pattern
    assert not evidence_is_verified("P9", "Database connection timeout", patterns)
    assert not evidence_is_verified("P1", "...", patterns)
    assert not evidence_is_verified("P1", "connection refused by orders-db", patterns)
