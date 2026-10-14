from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from log_inspector import cli
from log_inspector.db import Base, create_db_engine
from log_inspector.migrate import alembic_config, upgrade
from log_inspector.services import auth


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar()


def test_migrations_match_the_models():
    engine = create_db_engine("sqlite://")
    upgrade(engine)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    assert _version(engine) == "0004"


def test_upgrade_is_idempotent():
    engine = create_db_engine("sqlite://")
    upgrade(engine)
    upgrade(engine)
    assert _version(engine) == "0004"


def _legacy_db(path, with_investigations=True):
    """A database as M1-M5 left it: tables made by create_all, no alembic_version."""
    engine = create_db_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0002" if with_investigations else "0001")
        conn.execute(text("drop table alembic_version"))
        conn.execute(
            text(
                "insert into connections (id, name, region, external_id, status, created_at, "
                "updated_at) values ('c1', 'mine', 'us-east-1', 'ext', 'VERIFIED', "
                "'2026-10-01 00:00:00', '2026-10-01 00:00:00')"
            )
        )
    return engine


def test_legacy_database_is_upgraded_without_losing_data(tmp_path):
    engine = _legacy_db(tmp_path / "legacy.db")

    upgrade(engine)

    assert _version(engine) == "0004"
    with engine.connect() as conn:
        assert "owner_id" in [c["name"] for c in inspect(conn).get_columns("connections")]
        assert conn.execute(text("select name, owner_id from connections")).all() == [
            ("mine", None)
        ]


def test_m1_era_database_gets_the_investigations_table(tmp_path):
    engine = _legacy_db(tmp_path / "m1.db", with_investigations=False)
    upgrade(engine)
    with engine.connect() as conn:
        assert "investigations" in inspect(conn).get_table_names()


def test_cli_creates_a_user_and_claims_old_connections(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.db"
    _legacy_db(path)
    monkeypatch.setenv("LOG_INSPECTOR_DATABASE_URL", f"sqlite:///{path}")

    assert cli.main(["create-user", "akash", "--claim-unowned"]) == 0
    output = capsys.readouterr().out
    assert "Assigned 1 existing connection(s)" in output
    key = next(word for word in output.split() if word.startswith("li_"))

    engine = create_db_engine(f"sqlite:///{path}")
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        user = auth.authenticate(db, key)
        assert user is not None and user.name == "akash"
        owner = db.execute(text("select owner_id from connections")).scalar()
        assert owner == user.id

    assert cli.main(["list-users"]) == 0
    assert "akash" in capsys.readouterr().out
