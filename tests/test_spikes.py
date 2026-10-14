from datetime import UTC, datetime, timedelta

import pytest

from log_inspector.analysis.spikes import (
    VolumeBucket,
    align,
    bucket_seconds_for,
    detect_spikes,
    fill_buckets,
    insights_bin,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("span", "seconds", "bin_size"),
    [
        (timedelta(hours=1), 60, "1m"),
        (timedelta(hours=3), 60, "1m"),
        (timedelta(hours=5), 60, "1m"),
        (timedelta(hours=12), 300, "5m"),
        # "Last 24 hours" ends a few seconds after it was computed; it must stay at 5 minutes.
        (timedelta(days=1, seconds=5), 300, "5m"),
        (timedelta(days=7), 3600, "1h"),
    ],
)
def test_bucket_size(span, seconds, bin_size):
    assert bucket_seconds_for(span) == seconds
    assert insights_bin(seconds) == bin_size


def test_align_rounds_down():
    assert align(T0 + timedelta(seconds=299), 300) == T0
    assert align(T0 + timedelta(seconds=300), 300) == T0 + timedelta(minutes=5)


def test_fill_buckets_includes_empty_ones():
    errors = {T0 + timedelta(minutes=2): 4}
    totals = {T0 + timedelta(minutes=2): 10, T0: 3}

    buckets = fill_buckets(
        T0 + timedelta(seconds=30), T0 + timedelta(minutes=4), 60, errors, totals
    )

    assert [b.start for b in buckets] == [T0 + timedelta(minutes=m) for m in range(4)]
    assert [b.errors for b in buckets] == [0, 0, 4, 0]
    assert [b.total for b in buckets] == [3, 0, 10, 0]


def _buckets(counts):
    return [VolumeBucket(T0 + timedelta(minutes=i), c, None) for i, c in enumerate(counts)]


def test_detects_and_merges_spike():
    spikes = detect_spikes(_buckets([0, 1, 0, 0, 6, 9, 0, 1, 0]), 60)

    assert len(spikes) == 1
    spike = spikes[0]
    assert spike.start == T0 + timedelta(minutes=4)
    assert spike.end == T0 + timedelta(minutes=6)
    assert (spike.error_count, spike.peak_errors, spike.baseline_errors) == (15, 9, 0)


def test_separate_spikes():
    spikes = detect_spikes(_buckets([5, 0, 0, 0, 7, 0]), 60)
    assert [s.start for s in spikes] == [T0, T0 + timedelta(minutes=4)]


def test_steady_errors_are_not_a_spike():
    assert detect_spikes(_buckets([10, 12, 9, 11, 10, 13]), 60) == []


def test_small_bumps_are_not_spikes():
    assert detect_spikes(_buckets([0, 0, 2, 0, 0]), 60) == []


def test_spike_relative_to_baseline():
    spikes = detect_spikes(_buckets([4, 5, 4, 30, 5, 4]), 60)
    assert len(spikes) == 1
    assert spikes[0].peak_errors == 30
    assert spikes[0].baseline_errors == 4.5
