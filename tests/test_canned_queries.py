"""Sanity checks for the canned queries.

Moto ignores Insights filters, so these check the regexes with Python's re. Whether CloudWatch
accepts the full query syntax can only be confirmed against a real account.
"""

import re

import pytest

from log_inspector.aws.canned_queries import CANNED_QUERIES


def _regexes(name: str) -> list[re.Pattern]:
    return [re.compile(p) for p in re.findall(r"like /(.+?)/(?:\s|$)", CANNED_QUERIES[name].query)]


def _matches(name: str, line: str) -> bool:
    return any(p.search(line) for p in _regexes(name))


def test_every_query_has_a_description():
    for query in CANNED_QUERIES.values():
        assert query.description
        assert query.query


@pytest.mark.parametrize(
    "line",
    [
        "ERROR Database connection timeout",
        "java.lang.NullPointerException: x",
        "Traceback (most recent call last):",
        "panic: runtime error: index out of range",
        "FATAL could not bind port",
        "Unhandled promise rejection",
    ],
)
def test_errors_query_matches(line):
    assert _matches("errors", line)


@pytest.mark.parametrize("line", ["INFO Checkout request received", "Health check ok"])
def test_errors_query_ignores_normal_lines(line):
    assert not _matches("errors", line)


@pytest.mark.parametrize(
    "line",
    ['"status": 503', "status=500", "HTTP status code 502", 'statusCode":504', "code: 500"],
)
def test_http_5xx_query_matches(line):
    assert _matches("http_5xx", line)


@pytest.mark.parametrize("line", ["status=200", "status=404", "took 503ms", "order_id=5001"])
def test_http_5xx_query_ignores_other_lines(line):
    assert not _matches("http_5xx", line)


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-01T12:00:00Z abc Task timed out after 3.00 seconds",
        "RequestId: abc Error: Runtime exited with error: signal: killed",
        "Runtime.ExitError",
    ],
)
def test_lambda_failures_query_matches(line):
    assert _matches("lambda_failures", line)
