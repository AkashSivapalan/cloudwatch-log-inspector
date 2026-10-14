"""Remove secrets and personal data from log text before it is stored or sent anywhere."""

import re
from collections import Counter
from dataclasses import dataclass

_CREDENTIAL_KEYS = (
    r"password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization|credentials?"
)


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


# Order matters: more specific patterns run before broader ones.
_SECRET_RULES = [
    _Rule(
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        "<private_key>",
    ),
    _Rule(
        "url_credentials", re.compile(r"(\b[a-z][\w+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1<redacted>@"
    ),
    _Rule("jwt", re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}"), "<jwt>"),
    _Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<aws_access_key>"),
    _Rule("bearer_token", re.compile(r"(?i)\b(bearer)\s+[\w\-.~+/]{8,}=*"), r"\1 <token>"),
    _Rule(
        "credential",
        re.compile(
            rf"(?i)\b([\w-]*(?:{_CREDENTIAL_KEYS})[\w-]*)(\"?\s*[:=]\s*\"?)"
            # Skip values already redacted ("<jwt>") and auth schemes ("Bearer <token>").
            rf"(?!<|bearer\b|basic\b)([^\s\"',;&}}]+)"
        ),
        r"\1\2<redacted>",
    ),
]
_EMAIL_RULE = _Rule("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<email>")
_IP_RULE = _Rule(
    "ip_address",
    re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    "<ip>",
)


class Redactor:
    """Replaces sensitive values with placeholders and counts what it removed."""

    def __init__(self, redact_emails: bool = True, redact_ip_addresses: bool = True) -> None:
        self._rules = list(_SECRET_RULES)
        if redact_emails:
            self._rules.append(_EMAIL_RULE)
        if redact_ip_addresses:
            self._rules.append(_IP_RULE)
        self.counts: Counter[str] = Counter()

    def redact(self, text: str) -> str:
        for rule in self._rules:
            text, n = rule.pattern.subn(rule.replacement, text)
            if n:
                self.counts[rule.name] += n
        return text
