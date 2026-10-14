"""Run database migrations (Alembic) from code, e.g. at server startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def upgrade(engine: Engine) -> None:
    with engine.begin() as connection:
        config = alembic_config(connection)
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" not in tables and "connections" in tables:
            # Created before migrations existed (M1-M5 used create_all). Record which
            # schema it already has, so only the newer migrations run.
            command.stamp(config, "0002" if "investigations" in tables else "0001")
        command.upgrade(config, "head")
