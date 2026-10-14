"""Ask Claude for a diagnosis and turn its response into a validated Diagnosis."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import anthropic
from pydantic import ValidationError

from log_inspector.config import Settings
from log_inspector.diagnosis.prompt import DiagnosisPrompt
from log_inspector.diagnosis.schema import DIAGNOSIS_SCHEMA, Diagnosis

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Don't start a Claude request with less time than this left: it could not finish.
MIN_SECONDS_TO_START = 30


class DiagnosisError(Exception):
    """A diagnosis could not be produced. Carries the HTTP status to return."""

    status_code = 502


class DiagnosisUnavailable(DiagnosisError):
    """The service has no working Anthropic credentials."""

    status_code = 503


class DiagnosisRateLimited(DiagnosisError):
    status_code = 429


class DiagnosisRefused(DiagnosisError):
    """Claude (and any fallback model) declined the request."""


class DiagnosisFailed(DiagnosisError):
    pass


class DiagnosisTimedOut(DiagnosisError):
    """Claude did not finish within the time allowed."""

    status_code = 504


@dataclass
class DiagnosisResult:
    diagnosis: Diagnosis
    model: str
    input_tokens: int
    output_tokens: int
    fallback_used: bool


class Diagnoser:
    def __init__(
        self,
        settings: Settings,
        client: anthropic.Anthropic | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()

    def _get_client(self) -> anthropic.Anthropic:
        with self._lock:
            if self._client is None:
                key = self._settings.anthropic_api_key
                try:
                    self._client = anthropic.Anthropic(
                        api_key=key.get_secret_value() if key else None,
                        timeout=self._settings.diagnosis_idle_timeout_seconds,
                        max_retries=self._settings.diagnosis_max_retries,
                    )
                except anthropic.CredentialsError as exc:
                    raise DiagnosisUnavailable(
                        "No Anthropic credentials are configured. Set ANTHROPIC_API_KEY in .env "
                        "and restart the server."
                    ) from exc
            return self._client

    def diagnose(self, prompt: DiagnosisPrompt, deadline: float | None = None) -> DiagnosisResult:
        """Ask Claude. deadline is an optional point in time (on this diagnoser's clock, by
        default time.monotonic) by which the answer must be in, for callers that have less
        time than diagnosis_timeout_seconds, such as a Lambda close to its own limit.
        """
        settings = self._settings
        allowed = settings.diagnosis_timeout_seconds
        end = self._clock() + allowed
        if deadline is not None and deadline < end:
            end = deadline
            allowed = deadline - self._clock()
        if allowed < min(MIN_SECONDS_TO_START, settings.diagnosis_timeout_seconds):
            raise DiagnosisTimedOut(
                "There was not enough time left to ask Claude. Start a new investigation with a "
                "shorter time range or fewer log groups."
            )
        params: dict = {
            "model": settings.diagnosis_model,
            "max_tokens": settings.diagnosis_max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": settings.diagnosis_effort,
                "format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA},
            },
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
        }
        if settings.diagnosis_fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        timeout_message = (
            f"Claude did not finish within {allowed:.0f} seconds, so the diagnosis was stopped. "
            "Try a shorter time range or fewer log groups."
        )
        try:
            # Streamed so long responses don't hit HTTP timeouts. The HTTP timeout only catches
            # a stream that goes silent, so the overall limit is checked as events arrive.
            with self._get_client().beta.messages.stream(
                **params,
                timeout=anthropic.Timeout(
                    connect=10,
                    read=max(1.0, min(settings.diagnosis_idle_timeout_seconds, allowed)),
                    write=30,
                    pool=30,
                ),
            ) as stream:
                for _ in stream:
                    if self._clock() >= end:
                        raise DiagnosisTimedOut(timeout_message)
                message = stream.get_final_message()
        except anthropic.APITimeoutError as exc:
            raise DiagnosisTimedOut(timeout_message) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise DiagnosisUnavailable(
                f"The Anthropic API rejected the service's credentials: {exc.message}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise DiagnosisRateLimited(
                "The Anthropic API is rate limiting requests. Try again shortly."
            ) from exc
        except anthropic.APIStatusError as exc:
            logger.warning("Anthropic API error %s: %s", exc.status_code, exc.message)
            raise DiagnosisFailed(
                f"The Anthropic API returned an error ({exc.status_code}): {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise DiagnosisFailed(f"Could not reach the Anthropic API: {exc}") from exc

        logger.info(
            "Diagnosis by %s: %s input / %s output tokens, stop_reason=%s, request_id=%s",
            message.model,
            message.usage.input_tokens,
            message.usage.output_tokens,
            message.stop_reason,
            getattr(message, "_request_id", None),
        )
        if message.stop_reason == "refusal":
            details = message.stop_details
            reason = f" (category: {details.category})" if details and details.category else ""
            raise DiagnosisRefused(f"Claude declined to diagnose these logs{reason}.")
        if message.stop_reason == "max_tokens":
            raise DiagnosisFailed(
                "The diagnosis was cut off by the token limit. Raise "
                "LOG_INSPECTOR_DIAGNOSIS_MAX_TOKENS or narrow the time range."
            )

        text = _final_text(message.content)
        try:
            diagnosis = Diagnosis.model_validate_json(text)
        except ValidationError as exc:
            logger.warning("Diagnosis failed validation: %s\nResponse: %s", exc, text[:2000])
            raise DiagnosisFailed("Claude's response did not match the diagnosis format.") from exc

        return DiagnosisResult(
            diagnosis=diagnosis,
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            fallback_used=_fallback_used(message),
        )


def _final_text(content: list) -> str:
    # If a model declined mid-response and a fallback model took over, only the text after
    # the last switch point belongs to the final answer.
    text: list[str] = []
    for block in content:
        if block.type == "fallback":
            text = []
        elif block.type == "text":
            text.append(block.text)
    return "".join(text)


def _fallback_used(message) -> bool:
    if any(block.type == "fallback" for block in message.content):
        return True
    iterations = getattr(message.usage, "iterations", None) or []
    return any(getattr(entry, "type", None) == "fallback_message" for entry in iterations)
