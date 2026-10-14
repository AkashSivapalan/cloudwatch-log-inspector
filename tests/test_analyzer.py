from datetime import UTC, datetime, timedelta

from log_inspector.analysis.analyzer import LogEvent, analyze
from log_inspector.analysis.redaction import Redactor
from log_inspector.analysis.spikes import Spike
from tests.test_stack_traces import PYTHON

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

DEMO = [
    "WARN  DB connection pool at 90% capacity pool=orders-db",
    "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1002",
    "ERROR Checkout failed: upstream dependency error status=503 order_id=1002",
    "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1003",
    "ERROR Checkout failed: upstream dependency error status=503 order_id=1003",
]


def _events(messages, group="/log-inspector/demo"):
    return [
        LogEvent(T0 + timedelta(seconds=i), group, "checkout-service", m)
        for i, m in enumerate(messages)
    ]


def test_clusters_demo_logs():
    result = analyze(_events(DEMO), spikes=[], redactor=Redactor())

    assert result.events_analyzed == 5
    assert result.cluster_count == 3
    timeout, checkout, warn = result.clusters
    assert timeout.template == (
        "ERROR Database connection timeout after <num>ms host=orders-db.internal order_id=<num>"
    )
    assert (timeout.level, timeout.count) == ("ERROR", 2)
    assert timeout.first_seen == T0 + timedelta(seconds=1)
    assert timeout.last_seen == T0 + timedelta(seconds=3)
    assert timeout.examples == [DEMO[1], DEMO[3]]
    assert timeout.log_groups == ["/log-inspector/demo"]
    assert checkout.count == 2
    assert (warn.level, warn.count) == ("WARN", 1)


def test_errors_rank_above_warnings_even_when_rarer():
    events = _events(["WARN slow query id=1"] * 10 + ["ERROR boom"])
    clusters = analyze(events, spikes=[], redactor=Redactor()).clusters
    assert [c.level for c in clusters] == ["ERROR", "WARN"]


def test_examples_are_redacted():
    events = _events(["ERROR login failed for alice@example.com password=hunter2"])
    result = analyze(events, spikes=[], redactor=Redactor())

    cluster = result.clusters[0]
    assert cluster.examples == ["ERROR login failed for <email> password=<redacted>"]
    assert "alice" not in cluster.template
    assert result.redactions == {"credential": 1, "email": 1}


def test_stack_traces_cluster_by_exception_and_origin():
    other_origin = PYTHON.replace("orders.py", "payments.py")
    result = analyze(_events([PYTHON, PYTHON, other_origin]), spikes=[], redactor=Redactor())

    assert result.cluster_count == 2
    first = result.clusters[0]
    assert first.template == "KeyError: 'order_id'"
    assert first.count == 2
    assert first.stack_trace.origin == "load_order (/var/task/orders.py:10)"


def test_cluster_limit_keeps_total_count():
    events = _events([f"ERROR failure kind {chr(65 + i)}" for i in range(10)])
    result = analyze(events, spikes=[], redactor=Redactor(), max_clusters=3)
    assert result.cluster_count == 10
    assert len(result.clusters) == 3


def test_timeline_orders_spikes_and_first_occurrences():
    spike = Spike(
        start=T0, end=T0 + timedelta(minutes=1), error_count=4, peak_errors=4, baseline_errors=0
    )
    result = analyze(_events(DEMO + ["INFO ok"]), spikes=[spike], redactor=Redactor())

    kinds = [(e.kind, e.time) for e in result.timeline]
    assert kinds[0] == ("spike_start", T0)
    assert ("spike_end", T0 + timedelta(minutes=1)) in kinds
    first_seen = [e.description for e in result.timeline if e.kind == "first_seen"]
    assert len(first_seen) == 3  # 2 ERROR + 1 WARN clusters; INFO is not on the timeline
    assert first_seen[0].startswith("WARN first seen (1x)")


def test_no_events():
    result = analyze([], spikes=[], redactor=Redactor())
    assert (result.events_analyzed, result.clusters, result.timeline) == (0, [], [])
