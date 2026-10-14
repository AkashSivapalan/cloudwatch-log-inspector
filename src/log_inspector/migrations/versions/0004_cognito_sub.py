"""Link users to Cognito logins (B2).

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("cognito_sub", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_users_cognito_sub", ["cognito_sub"])


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_cognito_sub", type_="unique")
        batch.drop_column("cognito_sub")
