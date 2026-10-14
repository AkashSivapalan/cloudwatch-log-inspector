from alembic import context

from log_inspector import models  # noqa: F401  (registers tables on Base.metadata)
from log_inspector.db import Base

target_metadata = Base.metadata


def _run(connection) -> None:
    # Batch mode lets SQLite alter tables (it rebuilds them under the hood).
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The app passes its own connection (see log_inspector.migrate); the alembic CLI doesn't.
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    from log_inspector.config import get_settings
    from log_inspector.db import create_db_engine

    with create_db_engine(get_settings().database_url).begin() as connection:
        _run(connection)


run_migrations_online()
