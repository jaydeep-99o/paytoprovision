"""listing owner key

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.add_column(sa.Column("owner_key", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_listings_owner_key"), ["owner_key"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.drop_index(batch_op.f("ix_listings_owner_key"))
        batch_op.drop_column("owner_key")
