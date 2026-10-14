import enum
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from log_inspector.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes, even on SQLite (which drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def _new_external_id() -> str:
    return secrets.token_hex(16)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100))
    # The Cognito user's "sub" claim, for users who log in through the frontend.
    cognito_sub: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)

    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    """A user's API key. Only a SHA-256 hash is stored; the key itself is shown once."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # The public part of the key ("li_<prefix>_..."), used to look the key up.
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="api_keys")


class ConnectionStatus(enum.StrEnum):
    AWAITING_ROLE = "awaiting_role"
    PENDING_VERIFICATION = "pending_verification"
    VERIFIED = "verified"
    FAILED = "failed"


class Connection(Base):
    """A user's AWS account link: an IAM role the service assumes to read CloudWatch logs."""

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Nullable only for connections made before users existed; the CLI can claim them.
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64), default=_new_external_id)
    role_arn: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus, native_enum=False, length=32),
        default=ConnectionStatus.AWAITING_ROLE,
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    investigations: Mapped[list["Investigation"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class InvestigationStatus(enum.StrEnum):
    QUEUED = "queued"
    QUERYING = "querying"
    ANALYZING = "analyzing"
    DIAGNOSING = "diagnosing"
    DONE = "done"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (InvestigationStatus.DONE, InvestigationStatus.FAILED)


class Investigation(Base):
    """One diagnosis request, run in the background, with its result kept as history."""

    __tablename__ = "investigations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[InvestigationStatus] = mapped_column(
        Enum(InvestigationStatus, native_enum=False, length=32),
        default=InvestigationStatus.QUEUED,
        index=True,
    )
    question: Mapped[str | None] = mapped_column(Text)
    log_groups: Mapped[list[str]] = mapped_column(JSON)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime)
    end_time: Mapped[datetime] = mapped_column(UTCDateTime)
    max_events: Mapped[int] = mapped_column(Integer)
    # The full DiagnoseResponse (including the analysis) once done.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    # The HTTP status the same failure would have had on a synchronous call (e.g. 403, 504).
    error_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    connection: Mapped[Connection] = relationship(back_populates="investigations")
