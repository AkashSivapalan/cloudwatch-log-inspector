"""The same schema and API flow on Postgres, which production uses (M7).

Skipped unless TEST_POSTGRES_URL is set, e.g.
  TEST_POSTGRES_URL=postgresql+psycopg://log_inspector:log_inspector@localhost:5432/log_inspector
CI provides one. The database is wiped at the start of each test.
"""

import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import text

from log_inspector.db import Base, create_db_engine
from log_inspector.migrate import upgrade
from log_inspector.services import analysis as analysis_service
from tests.conftest import login, make_settings
from tests.fake_claude import FakeClaude
from tests.test_analysis_api import FakeInsights
from tests.test_investigations_api import InlineExecutor, make_app
from tests.test_logs_api import _create_verified

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL not set")


@pytest.fixture
def postgres_url():
    engine = create_db_engine(POSTGRES_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return POSTGRES_URL


def test_migrations_match_the_models_on_postgres(postgres_url):
    engine = create_db_engine(postgres_url)
    upgrade(engine)
    upgrade(engine)  # idempotent
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0003"
    engine.dispose()


def test_full_flow_on_postgres(postgres_url, aws, customer_role_arn, log_groups, monkeypatch):
    monkeypatch.setattr(analysis_service, "run_query", FakeInsights())
    settings = make_settings(database_url=postgres_url)
    with TestClient(make_app(settings, FakeClaude(), InlineExecutor())) as client:
        login(client)
        conn_id = _create_verified(client, customer_role_arn)

        started = client.post(
            "/investigations",
            json={
                "connection_id": conn_id,
                "log_groups": ["/log-inspector/demo"],
                "start_time": "2026-10-02T12:00:00Z",
                "end_time": "2026-10-02T13:00:00Z",
            },
        )
        assert started.status_code == 202
        body = client.get(f"/investigations/{started.json()['id']}").json()
        assert body["status"] == "done"
        assert body["result"]["diagnosis"]["severity"] == "high"
        assert (
            client.get("/investigations", params={"status": "done"}).json()[0]["severity"] == "high"
        )

        # Timestamps round-trip as UTC.
        assert body["created_at"].endswith(("Z", "+00:00"))
