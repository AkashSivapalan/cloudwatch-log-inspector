from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Named constraints, so migrations can alter them (SQLite needs names to rebuild tables).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_db_engine(database_url: str) -> Engine:
    if not database_url.startswith("sqlite"):
        # pool_pre_ping replaces connections the database closed (restarts, idle timeouts).
        return create_engine(database_url, pool_pre_ping=True)
    connect_args = {"check_same_thread": False}
    if database_url in ("sqlite://", "sqlite:///:memory:"):
        # Share one in-memory database across threads (used by tests).
        return create_engine(database_url, connect_args=connect_args, poolclass=StaticPool)
    return create_engine(database_url, connect_args=connect_args)


def init_db(engine: Engine) -> None:
    """Bring the database schema up to date by running any pending migrations."""
    from log_inspector.migrate import upgrade

    upgrade(engine)


def get_db(request: Request) -> Iterator[Session]:
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session
