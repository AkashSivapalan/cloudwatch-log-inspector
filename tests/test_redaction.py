import pytest

from log_inspector.analysis.redaction import Redactor


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [
        ("key=AKIAIOSFODNN7EXAMPLE used", "key=<aws_access_key> used", "aws_access_key"),
        (
            "Authorization: Bearer abc.def-ghi_123456",
            "Authorization: Bearer <token>",
            "bearer_token",
        ),
        ("token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlXzEy", "token=<jwt>", "jwt"),
        (
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlXzEy here",
            "token <jwt> here",
            "jwt",
        ),
        ("password=hunter2 user=bob", "password=<redacted> user=bob", "credential"),
        (
            '{"db_password": "s3cr3t!", "x": 1}',
            '{"db_password": "<redacted>", "x": 1}',
            "credential",
        ),
        ("api_key: abcd1234", "api_key: <redacted>", "credential"),
        (
            "aws_secret_access_key=wJalrXUt/K7MDENG",
            "aws_secret_access_key=<redacted>",
            "credential",
        ),
        (
            "connect postgres://admin:pa55@db.internal:5432/app",
            "connect postgres://<redacted>@db.internal:5432/app",
            "url_credentials",
        ),
        ("user jane.doe+test@example.com failed", "user <email> failed", "email"),
        ("from 10.0.12.34 port 443", "from <ip> port 443", "ip_address"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
            "<private_key>",
            "private_key",
        ),
    ],
)
def test_redacts(text, expected, kind):
    redactor = Redactor()
    assert redactor.redact(text) == expected
    if kind:
        assert redactor.counts[kind] >= 1


@pytest.mark.parametrize(
    "text",
    [
        "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1002",
        "Payment authorized order_id=1001",
        "status=503 latency=120ms",
        "Retrying with 3 tokens left",
    ],
)
def test_leaves_normal_lines_alone(text):
    redactor = Redactor()
    assert redactor.redact(text) == text
    assert not redactor.counts


def test_personal_data_redaction_can_be_turned_off():
    redactor = Redactor(redact_emails=False, redact_ip_addresses=False)
    text = "user a@b.com from 10.0.0.1 password=x"
    assert redactor.redact(text) == "user a@b.com from 10.0.0.1 password=<redacted>"


def test_counts_accumulate_across_calls():
    redactor = Redactor()
    redactor.redact("a@b.com")
    redactor.redact("c@d.com and e@f.org")
    assert redactor.counts["email"] == 3
