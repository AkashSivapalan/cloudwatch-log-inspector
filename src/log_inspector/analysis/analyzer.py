"""Summarise log events: cluster similar lines, find spikes, and build a timeline."""

from dataclasses import dataclass, field
from datetime import datetime

from log_inspector.analysis.redaction import Redactor
from log_inspector.analysis.spikes import Spike, VolumeBucket
from log_inspector.analysis.stack_traces import StackTrace, extract_stack_trace
from log_inspector.analysis.templates import detect_level, more_severe, normalize, severity_rank

MAX_EXAMPLES = 3
MAX_EXAMPLE_LENGTH = 1000
MAX_TIMELINE_ENTRIES = 30
_NOTABLE_LEVELS = ("FATAL", "ERROR", "WARN")


@dataclass
class LogEvent:
    timestamp: datetime
    log_group: str
    log_stream: str
    message: str


@dataclass
class Cluster:
    template: str
    level: str
    count: int
    first_seen: datetime
    last_seen: datetime
    log_groups: list[str]
    examples: list[str]
    stack_trace: StackTrace | None


@dataclass
class TimelineEntry:
    time: datetime
    kind: str  # "spike_start", "spike_end" or "first_seen"
    description: str


@dataclass
class Analysis:
    events_analyzed: int
    cluster_count: int
    clusters: list[Cluster]
    spikes: list[Spike]
    timeline: list[TimelineEntry]
    redactions: dict[str, int] = field(default_factory=dict)


@dataclass
class _Accumulator:
    template: str
    level: str
    first_seen: datetime
    last_seen: datetime
    stack_trace: StackTrace | None
    count: int = 0
    log_groups: set[str] = field(default_factory=set)
    examples: list[str] = field(default_factory=list)


def analyze(
    events: list[LogEvent],
    spikes: list[Spike],
    redactor: Redactor,
    max_clusters: int = 50,
) -> Analysis:
    clusters: dict[tuple[str, str], _Accumulator] = {}

    for event in sorted(events, key=lambda e: e.timestamp):
        message = redactor.redact(event.message)
        stack = extract_stack_trace(message)
        if stack:
            template = normalize(f"{stack.exception_type}: {stack.message}")
            key = (template, normalize(stack.origin or ""))
        else:
            template = normalize(_first_line(message))
            key = (template, "")
        level = detect_level(message)

        acc = clusters.get(key)
        if acc is None:
            acc = clusters[key] = _Accumulator(
                template=template,
                level=level,
                first_seen=event.timestamp,
                last_seen=event.timestamp,
                stack_trace=stack,
            )
        acc.count += 1
        acc.last_seen = event.timestamp
        acc.level = more_severe(acc.level, level)
        acc.log_groups.add(event.log_group)
        example = message[:MAX_EXAMPLE_LENGTH]
        if len(acc.examples) < MAX_EXAMPLES and example not in acc.examples:
            acc.examples.append(example)

    ranked = sorted(clusters.values(), key=lambda a: (severity_rank(a.level), -a.count))
    result = [
        Cluster(
            template=a.template,
            level=a.level,
            count=a.count,
            first_seen=a.first_seen,
            last_seen=a.last_seen,
            log_groups=sorted(a.log_groups),
            examples=a.examples,
            stack_trace=a.stack_trace,
        )
        for a in ranked[:max_clusters]
    ]
    return Analysis(
        events_analyzed=len(events),
        cluster_count=len(clusters),
        clusters=result,
        spikes=spikes,
        timeline=_timeline(result, spikes),
        redactions=dict(redactor.counts),
    )


def _first_line(message: str) -> str:
    return next((line for line in message.splitlines() if line.strip()), message)


def _timeline(clusters: list[Cluster], spikes: list[Spike]) -> list[TimelineEntry]:
    entries = [
        TimelineEntry(
            time=c.first_seen,
            kind="first_seen",
            description=f"{c.level} first seen ({c.count}x): {c.template}",
        )
        for c in clusters
        if c.level in _NOTABLE_LEVELS
    ]
    for spike in spikes:
        entries.append(
            TimelineEntry(
                time=spike.start,
                kind="spike_start",
                description=(
                    f"Error spike began: {spike.error_count} errors, peak {spike.peak_errors} per "
                    f"bucket vs. a normal {spike.baseline_errors:g}"
                ),
            )
        )
        entries.append(
            TimelineEntry(time=spike.end, kind="spike_end", description="Error spike ended")
        )
    # Spike boundaries sort before cluster first-seen entries at the same moment.
    entries.sort(key=lambda e: (e.time, e.kind != "spike_start"))
    return entries[:MAX_TIMELINE_ENTRIES]


def volume_totals(buckets: list[VolumeBucket]) -> tuple[int, int | None]:
    errors = sum(b.errors for b in buckets)
    totals = [b.total for b in buckets if b.total is not None]
    return errors, (sum(totals) if totals else None)
