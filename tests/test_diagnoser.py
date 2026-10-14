import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from log_inspector.diagnosis.diagnoser import (
    Diagnoser,
    DiagnosisFailed,
    DiagnosisRateLimited,
    DiagnosisRefused,
    DiagnosisTimedOut,
    DiagnosisUnavailable,
)
from log_inspector.diagnosis.prompt import DiagnosisPrompt
from log_inspector.diagnosis.schema import DIAGNOSIS_SCHEMA, Severity
from tests.conftest import make_settings
from tests.fake_claude import DEMO_DIAGNOSIS, FakeClaude, make_message, text_block

PROMPT = DiagnosisPrompt(
    system="system prompt", user="logs summary", patterns={}, patterns_omitted=0
)


def _diagnoser(outcome=None, **settings):
    fake = FakeClaude(outcome)
    return Diagnoser(make_settings(**settings), client=fake), fake


def test_request_shape():
    diagnoser, fake = _diagnoser()

    diagnoser.diagnose(PROMPT)

    params = fake.calls[0]
    assert params["model"] == "claude-opus-5"
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA},
    }
    assert params["system"] == "system prompt"
    assert params["messages"] == [{"role": "user", "content": "logs summary"}]
    assert params["betas"] == ["server-side-fallback-2026-07-01"]
    assert params["fallbacks"] == "default"


def test_settings_change_the_request():
    diagnoser, fake = _diagnoser(
        diagnosis_model="claude-sonnet-5", diagnosis_effort="medium", diagnosis_fallbacks=False
    )
    diagnoser.diagnose(PROMPT)

    params = fake.calls[0]
    assert params["model"] == "claude-sonnet-5"
    assert params["output_config"]["effort"] == "medium"
    assert "fallbacks" not in params and "betas" not in params


def test_parses_the_diagnosis():
    diagnoser, _ = _diagnoser()

    result = diagnoser.diagnose(PROMPT)

    assert result.diagnosis.severity is Severity.HIGH
    assert result.diagnosis.root_causes[0].title == "Database connection pool exhausted"
    assert (result.model, result.input_tokens, result.output_tokens) == ("claude-opus-5", 1200, 800)
    assert result.fallback_used is False


def test_uses_text_after_a_fallback_switch():
    content = [
        text_block('{"summary": "partial'),
        SimpleNamespace(type="fallback"),
        text_block(json.dumps(DEMO_DIAGNOSIS)),
    ]
    diagnoser, _ = _diagnoser(make_message(content))

    result = diagnoser.diagnose(PROMPT)
    assert result.fallback_used is True
    assert result.diagnosis.summary == DEMO_DIAGNOSIS["summary"]


def test_detects_sticky_fallback_from_usage():
    message = make_message(iterations=[SimpleNamespace(type="fallback_message")])
    diagnoser, _ = _diagnoser(message)
    assert diagnoser.diagnose(PROMPT).fallback_used is True


def test_refusal():
    details = SimpleNamespace(category="cyber", explanation="...")
    diagnoser, _ = _diagnoser(make_message([], stop_reason="refusal", stop_details=details))

    with pytest.raises(DiagnosisRefused, match="cyber"):
        diagnoser.diagnose(PROMPT)


def test_cut_off_by_max_tokens():
    diagnoser, _ = _diagnoser(make_message([text_block('{"summary": "')], stop_reason="max_tokens"))
    with pytest.raises(DiagnosisFailed, match="token limit"):
        diagnoser.diagnose(PROMPT)


def test_response_not_matching_schema():
    diagnoser, _ = _diagnoser(make_message([text_block('{"summary": "x"}')]))
    with pytest.raises(DiagnosisFailed, match="format"):
        diagnoser.diagnose(PROMPT)


def _api_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "expected", "status"),
    [
        (_api_error(anthropic.AuthenticationError, 401), DiagnosisUnavailable, 503),
        (_api_error(anthropic.PermissionDeniedError, 403), DiagnosisUnavailable, 503),
        (_api_error(anthropic.RateLimitError, 429), DiagnosisRateLimited, 429),
        (_api_error(anthropic.BadRequestError, 400), DiagnosisFailed, 502),
        (_api_error(anthropic.InternalServerError, 500), DiagnosisFailed, 502),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            DiagnosisFailed,
            502,
        ),
    ],
)
def test_api_errors(error, expected, status):
    diagnoser, _ = _diagnoser(error)
    with pytest.raises(expected) as caught:
        diagnoser.diagnose(PROMPT)
    assert caught.value.status_code == status


def test_missing_credentials(monkeypatch):
    def no_credentials(**kwargs):
        raise anthropic.CredentialsError("no credentials")

    monkeypatch.setattr(anthropic, "Anthropic", no_credentials)
    diagnoser = Diagnoser(make_settings())

    with pytest.raises(DiagnosisUnavailable, match="ANTHROPIC_API_KEY"):
        diagnoser.diagnose(PROMPT)


def test_api_key_from_settings_is_used(monkeypatch):
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return FakeClaude()

    monkeypatch.setattr(anthropic, "Anthropic", capture)
    Diagnoser(make_settings(anthropic_api_key="sk-ant-test")).diagnose(PROMPT)
    assert seen["api_key"] == "sk-ant-test"


# --- Time limits ---


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _timed(events=0, seconds_per_event=0.0, **settings):
    """A diagnoser on a fake clock that advances `seconds_per_event` as each event arrives."""
    clock = FakeClock()

    def tick(_event):
        clock.now += seconds_per_event

    fake = FakeClaude(events=[object()] * events, on_event=tick)
    return Diagnoser(make_settings(**settings), client=fake, clock=clock), fake, clock


def test_client_is_built_with_an_idle_timeout_and_few_retries(monkeypatch):
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return FakeClaude()

    monkeypatch.setattr(anthropic, "Anthropic", capture)
    Diagnoser(make_settings(diagnosis_idle_timeout_seconds=45, diagnosis_max_retries=0)).diagnose(
        PROMPT
    )
    assert seen["timeout"] == 45
    assert seen["max_retries"] == 0


def test_requests_carry_a_read_timeout_for_silent_streams():
    diagnoser, fake, _ = _timed(diagnosis_idle_timeout_seconds=90, diagnosis_timeout_seconds=480)
    diagnoser.diagnose(PROMPT)
    assert fake.calls[0]["timeout"].read == 90


def test_a_steady_stream_is_stopped_at_the_overall_limit():
    # The stream never goes quiet, so no HTTP timeout would fire; the wall clock must.
    diagnoser, fake, clock = _timed(events=10, seconds_per_event=100, diagnosis_timeout_seconds=250)
    with pytest.raises(DiagnosisTimedOut, match="250 seconds") as caught:
        diagnoser.diagnose(PROMPT)
    assert caught.value.status_code == 504
    assert "shorter time range" in str(caught.value)
    assert clock.now == 1300  # gave up after the third event, not after all ten


def test_a_stream_that_finishes_in_time_is_fine():
    diagnoser, _, _ = _timed(events=5, seconds_per_event=10, diagnosis_timeout_seconds=480)
    assert diagnoser.diagnose(PROMPT).diagnosis.severity == Severity.HIGH


def test_a_caller_deadline_shortens_the_limit():
    diagnoser, fake, clock = _timed(events=10, seconds_per_event=40, diagnosis_timeout_seconds=480)
    with pytest.raises(DiagnosisTimedOut, match="60 seconds"):
        diagnoser.diagnose(PROMPT, deadline=clock.now + 60)
    # With less time than the idle timeout, the read timeout shrinks to match.
    assert fake.calls[0]["timeout"].read == 60


def test_a_distant_caller_deadline_changes_nothing():
    diagnoser, _, clock = _timed(events=3, seconds_per_event=10)
    assert diagnoser.diagnose(PROMPT, deadline=clock.now + 10_000).model == "claude-opus-5"


def test_does_not_start_a_request_that_could_not_finish():
    diagnoser, fake, clock = _timed()
    with pytest.raises(DiagnosisTimedOut, match="not enough time"):
        diagnoser.diagnose(PROMPT, deadline=clock.now + 10)
    assert fake.calls == []


def test_an_http_timeout_is_reported_as_a_timeout():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    diagnoser, _ = _diagnoser(anthropic.APITimeoutError(request=request))
    with pytest.raises(DiagnosisTimedOut) as caught:
        diagnoser.diagnose(PROMPT)
    assert caught.value.status_code == 504
