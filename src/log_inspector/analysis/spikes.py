"""Bucket error counts over time and find spikes above the normal level."""

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# A bucket is a spike when it has at least this many errors...
MIN_SPIKE_ERRORS = 3
# ...and at least this many times the median errors per bucket.
SPIKE_FACTOR = 3.0


@dataclass
class VolumeBucket:
    start: datetime
    errors: int
    total: int | None


@dataclass
class Spike:
    start: datetime
    end: datetime
    error_count: int
    peak_errors: int
    baseline_errors: float


MAX_BUCKETS = 300


def bucket_seconds_for(time_range: timedelta) -> int:
    """The smallest of 1 minute, 5 minutes or 1 hour that gives at most MAX_BUCKETS buckets."""
    for seconds in (60, 300):
        if time_range.total_seconds() <= seconds * MAX_BUCKETS:
            return seconds
    return 3600


def insights_bin(bucket_seconds: int) -> str:
    """The Logs Insights bin() argument for a bucket size."""
    if bucket_seconds % 3600 == 0:
        return f"{bucket_seconds // 3600}h"
    if bucket_seconds % 60 == 0:
        return f"{bucket_seconds // 60}m"
    return f"{bucket_seconds}s"


def align(moment: datetime, bucket_seconds: int) -> datetime:
    """Round down to a bucket boundary, the same way Insights' bin() does."""
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % bucket_seconds, UTC)


def fill_buckets(
    start: datetime,
    end: datetime,
    bucket_seconds: int,
    errors: dict[datetime, int],
    totals: dict[datetime, int] | None,
) -> list[VolumeBucket]:
    """One bucket per interval in [start, end), including empty ones."""
    step = timedelta(seconds=bucket_seconds)
    buckets = []
    current = align(start, bucket_seconds)
    while current < end:
        buckets.append(
            VolumeBucket(
                start=current,
                errors=errors.get(current, 0),
                total=None if totals is None else totals.get(current, 0),
            )
        )
        current += step
    return buckets


def detect_spikes(buckets: list[VolumeBucket], bucket_seconds: int) -> list[Spike]:
    if not buckets:
        return []
    baseline = statistics.median(b.errors for b in buckets)
    threshold = max(MIN_SPIKE_ERRORS, baseline * SPIKE_FACTOR)
    step = timedelta(seconds=bucket_seconds)

    spikes: list[Spike] = []
    run: list[VolumeBucket] = []
    for bucket in [*buckets, None]:
        if bucket is not None and bucket.errors >= threshold and bucket.errors > baseline:
            run.append(bucket)
            continue
        if run:
            spikes.append(
                Spike(
                    start=run[0].start,
                    end=run[-1].start + step,
                    error_count=sum(b.errors for b in run),
                    peak_errors=max(b.errors for b in run),
                    baseline_errors=baseline,
                )
            )
            run = []
    return spikes
