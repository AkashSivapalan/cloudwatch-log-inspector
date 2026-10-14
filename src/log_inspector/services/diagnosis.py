from collections.abc import Callable
from dataclasses import dataclass

from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.config import Settings
from log_inspector.diagnosis.diagnoser import Diagnoser
from log_inspector.diagnosis.prompt import DiagnosisPrompt, build_prompt, pattern_ids
from log_inspector.diagnosis.schema import Diagnosis, Severity
from log_inspector.models import Connection
from log_inspector.services.analysis import AnalysisReport, analyze_logs
from log_inspector.services.logs import TimeRange


@dataclass
class DiagnosisReport:
    analysis: AnalysisReport
    prompt: DiagnosisPrompt
    diagnosis: Diagnosis
    model: str | None
    input_tokens: int
    output_tokens: int
    fallback_used: bool
    # True when there was nothing to diagnose, so Claude wasn't called.
    llm_skipped: bool


def diagnose_logs(
    connection: Connection,
    assumer: RoleAssumer,
    diagnoser: Diagnoser,
    question: str | None,
    log_groups: list[str],
    time_range: TimeRange,
    max_events: int,
    settings: Settings,
    on_progress: Callable[[str], None] | None = None,
    deadline: float | None = None,
) -> DiagnosisReport:
    """on_progress, if given, is called with "analyzing" and then "diagnosing".

    deadline (a time.monotonic() value) is the latest moment the Claude step may run until.
    """
    report = analyze_logs(
        connection, assumer, log_groups, time_range, max_events, settings, on_progress
    )
    prompt = build_prompt(question, report, settings.diagnosis_max_prompt_chars)

    if not report.analysis.clusters:
        # No error or warning lines: don't spend tokens confirming that.
        return DiagnosisReport(
            analysis=report,
            prompt=prompt,
            diagnosis=_nothing_found(report),
            model=None,
            input_tokens=0,
            output_tokens=0,
            fallback_used=False,
            llm_skipped=True,
        )

    if on_progress:
        on_progress("diagnosing")
    result = diagnoser.diagnose(prompt, deadline=deadline)
    return DiagnosisReport(
        analysis=report,
        prompt=prompt,
        diagnosis=result.diagnosis,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        fallback_used=result.fallback_used,
        llm_skipped=False,
    )


def _nothing_found(report: AnalysisReport) -> Diagnosis:
    if report.total_events:
        found = f"{report.total_events} log lines, none of which look like a failure"
    else:
        found = "no log lines at all"
    return Diagnosis(
        summary=f"No errors or warnings were found: the selected range contains {found}.",
        severity=Severity.NONE,
        root_causes=[],
        affected_components=[],
        follow_up_queries=[],
        open_questions=[
            "Is the problem outside this time range? Try a wider range.",
            "Does the affected service log to a different log group?",
        ],
    )


__all__ = ["DiagnosisReport", "diagnose_logs", "pattern_ids"]
