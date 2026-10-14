import pytest

from log_inspector.analysis.templates import detect_level, more_severe, normalize


@pytest.mark.parametrize(
    ("message", "template"),
    [
        (
            "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1002",
            "ERROR Database connection timeout after <num>ms "
            "host=orders-db.internal order_id=<num>",
        ),
        (
            "2026-10-02T14:41:02.019Z\t3f1c2a9e-1b2c-4d5e-8f90-123456789abc\tERROR\tboom",
            "<ts> <uuid> ERROR boom",
        ),
        ("Oct  2 14:41:02 host sshd: failed", "<ts> host sshd: failed"),
        ("2026-10-02 14:41:02,123 WARN pool at 90%", "<ts> WARN pool at <num>%"),
        ("request 0x7ffd5e8c failed", "request <hex> failed"),
        ("trace a3f9c2b1e4d5f6a7 lost", "trace <hex> lost"),
        ("latency 12.5 s, retries 3", "latency <num> s, retries <num>"),
        # Digits inside words are part of the name, not a variable.
        ("s3 upload via http2 to ec2", "s3 upload via http2 to ec2"),
        ("pool-3   worker   7", "pool-<num> worker <num>"),
    ],
)
def test_normalize(message, template):
    assert normalize(message) == template


def test_lines_that_differ_only_in_ids_share_a_template():
    a = normalize("ERROR Checkout failed status=503 order_id=1002")
    b = normalize("ERROR Checkout failed status=503 order_id=1003")
    assert a == b


def test_long_templates_are_truncated():
    assert len(normalize("x" * 1000)) == 301


@pytest.mark.parametrize(
    ("message", "level"),
    [
        ("ERROR Database connection timeout", "ERROR"),
        ("[ERROR]\t2026-10-02T14:41:02Z\tid\tKeyError: 'x'", "ERROR"),
        ("2026-10-02T14:41:02Z\tid\tWARN\tslow query", "WARN"),
        ("WARNING: disk almost full", "WARN"),
        ("INFO request failed with error, retrying", "INFO"),
        ("CRITICAL out of memory", "FATAL"),
        ('{"level": "error", "msg": "boom"}', "ERROR"),
        ('{"severity":"warning","msg":"slow"}', "WARN"),
        ("Task timed out after 3.00 seconds", "ERROR"),
        ("java.lang.NullPointerException: x", "ERROR"),
        ("panic: runtime error", "FATAL"),
        ("KeyError: 'order_id'", "ERROR"),
        ("everything is fine", "UNKNOWN"),
        ("0 errors so far", "UNKNOWN"),
    ],
)
def test_detect_level(message, level):
    assert detect_level(message) == level


def test_more_severe():
    assert more_severe("WARN", "ERROR") == "ERROR"
    assert more_severe("FATAL", "ERROR") == "FATAL"
    assert more_severe("UNKNOWN", "INFO") == "INFO"
