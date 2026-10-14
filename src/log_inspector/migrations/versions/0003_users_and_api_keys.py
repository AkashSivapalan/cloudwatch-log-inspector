"""Users, API keys, and connection owners (M6).

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from log_inspector.models import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_used_at", UTCDateTime(), nullable=True),
        sa.Column("revoked_at", UTCDateTime(), nullable=True),
        sa.UniqueConstraint("prefix"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])

    # Existing connections get no owner; `log-inspector create-user --claim-unowned`
    # assigns them.
    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_connections_owner_id_users", "users", ["owner_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_connections_owner_id", ["owner_id"])


def downgrade() -> None:
    with op.batch_alter_table("connections") as batch:
        batch.drop_index("ix_connections_owner_id")
        batch.drop_constraint("fk_connections_owner_id_users", type_="foreignkey")
        batch.drop_column("owner_id")
    op.drop_table("api_keys")
    op.drop_table("users")
