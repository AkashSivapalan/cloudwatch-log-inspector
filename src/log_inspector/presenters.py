"""Turn service results into API response models. Shared by the API and background jobs."""

from log_inspector.diagnosis.evidence import evidence_is_verified
from log_inspector.diagnosis.prompt import DEFAULT_QUESTION
from log_inspector.schemas import (
    AnalysisOut,
    ClusterOut,
    DiagnoseResponse,
    DiagnosisOut,
    DiagnosisUsageOut,
    EvidenceOut,
    FollowUpQueryOut,
    PatternRefOut,
    RootCauseOut,
)
from log_inspector.services.analysis import AnalysisReport
from log_inspector.services.diagnosis import DiagnosisReport


def analysis_out(report: AnalysisReport) -> AnalysisOut:
    analysis = report.analysis
    return AnalysisOut(
        log_groups=report.log_groups,
        start_time=report.time_range.start,
        end_time=report.time_range.end,
        bucket_seconds=report.bucket_seconds,
        total_events=report.total_events,
        total_errors=report.total_errors,
        events_analyzed=analysis.events_analyzed,
        events_truncated=report.events_truncated,
        cluster_count=analysis.cluster_count,
        clusters=[ClusterOut.model_validate(c) for c in analysis.clusters],
        spikes=analysis.spikes,
        timeline=analysis.timeline,
        volume=report.volume,
        redactions=analysis.redactions,
    )


def diagnose_response(
    question: str | None, report: DiagnosisReport, include_analysis: bool
) -> DiagnoseResponse:
    patterns = report.prompt.patterns
    diagnosis = report.diagnosis
    time_range = report.analysis.time_range
    return DiagnoseResponse(
        question=question or DEFAULT_QUESTION,
        log_groups=report.analysis.log_groups,
        start_time=time_range.start,
        end_time=time_range.end,
        diagnosis=DiagnosisOut(
            summary=diagnosis.summary,
            severity=diagnosis.severity,
            root_causes=[
                RootCauseOut(
                    title=cause.title,
                    explanation=cause.explanation,
                    confidence=cause.confidence,
                    evidence=[
                        EvidenceOut(
                            pattern_id=item.pattern_id,
                            log_excerpt=item.log_excerpt,
                            relevance=item.relevance,
                            verified=evidence_is_verified(
                                item.pattern_id, item.log_excerpt, patterns
                            ),
                        )
                        for item in cause.evidence
                    ],
                    remediation=cause.remediation,
                )
                for cause in diagnosis.root_causes
            ],
            affected_components=diagnosis.affected_components,
            follow_up_queries=[
                FollowUpQueryOut.model_validate(q) for q in diagnosis.follow_up_queries
            ],
            open_questions=diagnosis.open_questions,
        ),
        patterns=[
            PatternRefOut(id=pid, level=c.level, count=c.count, template=c.template)
            for pid, c in patterns.items()
        ],
        patterns_omitted=report.prompt.patterns_omitted,
        model=report.model,
        fallback_used=report.fallback_used,
        llm_skipped=report.llm_skipped,
        usage=DiagnosisUsageOut(
            input_tokens=report.input_tokens, output_tokens=report.output_tokens
        ),
        analysis=analysis_out(report.analysis) if include_analysis else None,
    )
