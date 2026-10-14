"""M6: authentication, per-user isolation, API keys, rate limits, and request logging."""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from log_inspector.main import create_app
from log_inspector.models import ApiKey
from log_inspector.ratelimit import RateLimiter
from log_inspector.services import analysis as analysis_service
from tests.conftest import login, make_settings, use_key
from tests.test_analysis_api import FakeInsights
from tests.test_investigations_api import NeverRunExecutor, make_app
from tests.test_logs_api import _create_verified

PUBLIC_PATHS = {"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


# --- Authentication ---


def test_every_endpoint_except_health_requires_a_key(app):
    # Walk the OpenAPI schema: it lists every path and method, however routers are nested.
    with TestClient(app) as anonymous:
        checked = 0
        for path, operations in app.openapi()["paths"].items():
            if path in PUBLIC_PATHS:
                continue
            filled = path.replace("{", "").replace("}", "")  # any value for path params
            for method in operations:
                response = anonymous.request(method.upper(), filled, json={})
                assert response.status_code == 401, f"{method.upper()} {path}"
                assert response.headers["WWW-Authenticate"] == "Bearer"
                checked += 1
        assert checked >= 15  # guard against silently checking nothing
        assert anonymous.get("/health").status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "Bearer not-a-key",
        "Bearer li_abc",
        "Bearer li_000000000000_wrongsecret",
        "Basic dXNlcjpwYXNz",
        "li_abc_def",
    ],
)
def test_invalid_keys_are_rejected(client, header):
    response = client.get("/connections", headers={"Authorization": header})
    assert response.status_code == 401


def test_right_prefix_wrong_secret_is_rejected(client):
    key = client.headers["Authorization"].removeprefix("Bearer ")
    tampered = key[:-4] + ("AAAA" if not key.endswith("AAAA") else "BBBB")
    assert (
        client.get("/connections", headers={"Authorization": f"Bearer {tampered}"}).status_code
        == 401
    )


def test_keys_are_stored_hashed(client):
    key = client.headers["Authorization"].removeprefix("Bearer ")
    with client.app.state.session_factory() as db:
        stored = db.scalars(select(ApiKey)).all()
    assert len(stored) == 1
    assert stored[0].key_hash != key
    assert key not in (stored[0].key_hash, stored[0].prefix)
    assert stored[0].last_used_at is None  # the key hasn't been used yet

    client.get("/me")
    with client.app.state.session_factory() as db:
        assert db.scalars(select(ApiKey)).one().last_used_at is not None


# --- Per-user isolation ---


@pytest.fixture
def two_users(aws, customer_role_arn, log_groups, monkeypatch):
    """Alice has a verified connection and an investigation; Bob has nothing."""
    monkeypatch.setattr(analysis_service, "run_query", FakeInsights())
    app = make_app(make_settings(), claude=None, executor=NeverRunExecutor())
    with TestClient(app) as alice:
        bob = TestClient(app)  # no `with`: the app is already running
        login(alice, "alice")
        login(bob, "bob")
        conn_id = _create_verified(alice, customer_role_arn)
        investigation_id = alice.post(
            "/investigations", json={"connection_id": conn_id, "log_groups": ["/x"]}
        ).json()["id"]
        yield alice, bob, conn_id, investigation_id


def test_users_only_see_their_own_data(two_users):
    alice, bob, conn_id, investigation_id = two_users

    assert [c["id"] for c in alice.get("/connections").json()] == [conn_id]
    assert bob.get("/connections").json() == []
    assert len(alice.get("/investigations").json()) == 1
    assert bob.get("/investigations").json() == []
    assert bob.get("/investigations", params={"connection_id": conn_id}).json() == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/connections/{c}", None),
        ("GET", "/connections/{c}/setup", None),
        ("PUT", "/connections/{c}/role", {"role_arn": "arn:aws:iam::123456789012:role/Mine"}),
        ("POST", "/connections/{c}/verify", None),
        ("DELETE", "/connections/{c}", None),
        ("GET", "/connections/{c}/log-groups", None),
        ("POST", "/connections/{c}/query", {"log_groups": ["/x"], "canned_query": "errors"}),
        ("POST", "/connections/{c}/analyze", {"log_groups": ["/x"]}),
        ("POST", "/connections/{c}/diagnose", {"log_groups": ["/x"]}),
        ("POST", "/investigations", {"connection_id": "{c}", "log_groups": ["/x"]}),
        ("GET", "/investigations/{i}", None),
    ],
)
def test_other_users_data_looks_like_it_does_not_exist(two_users, method, path, body):
    alice, bob, conn_id, investigation_id = two_users

    def fill(value):
        return value.replace("{c}", conn_id).replace("{i}", investigation_id)

    if body and "connection_id" in body:
        body = {**body, "connection_id": fill(body["connection_id"])}
    response = bob.request(method, fill(path), json=body)
    assert response.status_code == 404, response.text

    # Alice's data is untouched.
    assert alice.get(f"/connections/{conn_id}").json()["status"] == "verified"


# --- Managing API keys ---


def test_api_key_lifecycle(client):
    me = client.get("/me").json()
    assert me["name"] == "tester"

    created = client.post("/me/api-keys", json={"name": "postman"})
    assert created.status_code == 201
    new_key = created.json()["key"]
    assert new_key.startswith("li_")

    listed = client.get("/me/api-keys").json()
    assert [k["name"] for k in listed] == ["test", "postman"]
    assert all("key" not in k and "key_hash" not in k for k in listed)

    other = TestClient(client.app)
    use_key(other, new_key)
    assert other.get("/me").json()["id"] == me["id"]
    assert client.delete(f"/me/api-keys/{created.json()['id']}").status_code == 204
    assert other.get("/me").status_code == 401

    revoked = next(k for k in client.get("/me/api-keys").json() if k["name"] == "postman")
    assert revoked["revoked_at"] is not None


def test_cannot_revoke_another_users_key(client):
    other = TestClient(client.app)
    login(other, "other")
    other_key_id = other.get("/me/api-keys").json()[0]["id"]
    assert client.delete(f"/me/api-keys/{other_key_id}").status_code == 404
    assert client.delete("/me/api-keys/missing").status_code == 404


# --- Rate limits and cost guards ---


def test_rate_limiter_window():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])

    assert limiter.hit("k", limit=2, window_seconds=60) is None
    assert limiter.hit("k", limit=2, window_seconds=60) is None
    assert limiter.hit("k", limit=2, window_seconds=60) == 60
    assert limiter.hit("other", limit=2, window_seconds=60) is None
    now[0] = 30
    assert limiter.hit("k", limit=2, window_seconds=60) == 30
    now[0] = 60.5
    assert limiter.hit("k", limit=2, window_seconds=60) is None


def _client(**settings):
    client = TestClient(create_app(make_settings(**settings)))
    client.__enter__()
    login(client)
    return client


def test_general_rate_limit_is_per_user(aws):
    client = _client(rate_limit_per_minute=3)
    for _ in range(3):
        assert client.get("/connections").status_code == 200
    limited = client.get("/connections")
    assert limited.status_code == 429
    assert 0 < int(limited.headers["Retry-After"]) <= 60
    assert "3 requests per minute" in limited.json()["detail"]

    other = TestClient(client.app)
    login(other, "other")
    assert other.get("/connections").status_code == 200
    client.__exit__(None, None, None)


def test_expensive_endpoints_have_a_stricter_limit(aws, customer_role_arn, log_groups, monkeypatch):
    monkeypatch.setattr(analysis_service, "run_query", FakeInsights())
    client = _client(expensive_rate_limit_per_hour=2)
    conn_id = _create_verified(client, customer_role_arn)
    body = {"log_groups": ["/log-inspector/demo"]}

    assert client.post(f"/connections/{conn_id}/analyze", json=body).status_code == 200
    assert (
        client.post(
            f"/connections/{conn_id}/query",
            json={"log_groups": ["/aws/lambda/checkout"], "canned_query": "errors"},
        ).status_code
        == 200
    )
    limited = client.post(f"/connections/{conn_id}/analyze", json=body)
    assert limited.status_code == 429
    assert "per hour" in limited.json()["detail"]
    # Cheap endpoints are unaffected.
    assert client.get(f"/connections/{conn_id}").status_code == 200
    client.__exit__(None, None, None)


def test_rate_limits_can_be_disabled(aws):
    client = _client(rate_limit_per_minute=1, rate_limits_enabled=False)
    assert all(client.get("/connections").status_code == 200 for _ in range(5))
    client.__exit__(None, None, None)


def test_active_investigations_are_capped(aws, customer_role_arn, log_groups):
    settings = make_settings(max_active_investigations=2)
    with TestClient(make_app(settings, claude=None, executor=NeverRunExecutor())) as client:
        login(client)
        conn_id = _create_verified(client, customer_role_arn)
        body = {"connection_id": conn_id, "log_groups": ["/x"]}
        assert client.post("/investigations", json=body).status_code == 202
        assert client.post("/investigations", json=body).status_code == 202
        capped = client.post("/investigations", json=body)
        assert capped.status_code == 429
        assert "2 investigations running" in capped.json()["detail"]


# --- Request logging ---


def test_request_ids(client):
    generated = client.get("/me").headers["X-Request-ID"]
    assert len(generated) == 32

    assert (
        client.get("/me", headers={"X-Request-ID": "trace-123"}).headers["X-Request-ID"]
        == "trace-123"
    )
    forged = client.get("/me", headers={"X-Request-ID": "bad\nINFO forged line"})
    assert forged.headers["X-Request-ID"] != "bad\nINFO forged line"


def test_requests_are_logged_without_the_key(client, caplog):
    key = client.headers["Authorization"].removeprefix("Bearer ")
    user_id = client.get("/me").json()["id"]
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="log_inspector.requests"):
        client.get("/connections", params={"token": "secret-in-query"})

    lines = [r for r in caplog.records if r.name == "log_inspector.requests"]
    assert len(lines) == 1
    record = lines[0]
    assert record.fields["path"] == "/connections"
    assert record.fields["status"] == 200
    assert record.fields["user_id"] == user_id
    text = record.getMessage() + str(record.fields)
    assert key not in text
    assert "secret-in-query" not in text
