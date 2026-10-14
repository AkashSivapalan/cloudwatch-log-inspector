from log_inspector import __version__


def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "environment": "local"}


def test_openapi_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert "/health" in client.get("/openapi.json").json()["paths"]


def _preflight(client, origin):
    return client.options(
        "/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_cors_allows_configured_origin():
    from fastapi.testclient import TestClient

    from log_inspector.config import Settings
    from log_inspector.main import create_app

    app = create_app(
        Settings(database_url="sqlite://", cors_origins="http://localhost:4200, https://x.example")
    )
    with TestClient(app) as client:
        allowed = _preflight(client, "https://x.example")
        denied = _preflight(client, "https://evil.example")

    assert allowed.headers["access-control-allow-origin"] == "https://x.example"
    assert "access-control-allow-origin" not in denied.headers
