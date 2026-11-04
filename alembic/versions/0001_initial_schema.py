"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
TIMESTAMP = sa.DateTime(timezone=True)
MONEY = sa.Numeric(precision=10, scale=2)


def upgrade() -> None:
    op.create_table(
        "listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("repo_url", sa.String(length=300), nullable=False),
        sa.Column("repo_full_name", sa.String(length=200), nullable=False),
        sa.Column("branch", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("owner_name", sa.String(length=120), nullable=False),
        sa.Column("owner_paypal_email", sa.String(length=254), nullable=False),
        sa.Column("price_per_hour", MONEY, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("deploy_config", JSON, nullable=False),
        sa.Column("analysis", JSON, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listings")),
    )
    op.create_index(op.f("ix_listings_repo_full_name"), "listings", ["repo_full_name"], unique=False)
    op.create_index(op.f("ix_listings_slug"), "listings", ["slug"], unique=True)

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("received_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
    )

    op.create_table(
        "preview_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("hours", sa.Integer(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("buyer_email", sa.String(length=254), nullable=True),
        sa.Column("paypal_order_id", sa.String(length=64), nullable=True),
        sa.Column("paypal_capture_id", sa.String(length=64), nullable=True),
        sa.Column("paypal_refund_id", sa.String(length=64), nullable=True),
        sa.Column("render_service_id", sa.String(length=64), nullable=True),
        sa.Column("render_service_name", sa.String(length=100), nullable=True),
        sa.Column("render_deploy_id", sa.String(length=64), nullable=True),
        sa.Column("live_url", sa.String(length=300), nullable=True),
        sa.Column("deploy_attempts", sa.Integer(), nullable=False),
        sa.Column("active_config", JSON, nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("paid_at", TIMESTAMP, nullable=True),
        sa.Column("live_at", TIMESTAMP, nullable=True),
        sa.Column("expires_at", TIMESTAMP, nullable=True),
        sa.Column("ended_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], name=op.f("fk_preview_sessions_listing_id_listings")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preview_sessions")),
        sa.UniqueConstraint("paypal_order_id", name=op.f("uq_preview_sessions_paypal_order_id")),
    )
    op.create_index(op.f("ix_preview_sessions_expires_at"), "preview_sessions", ["expires_at"], unique=False)
    op.create_index(op.f("ix_preview_sessions_listing_id"), "preview_sessions", ["listing_id"], unique=False)
    op.create_index(op.f("ix_preview_sessions_render_service_id"), "preview_sessions", ["render_service_id"], unique=False)
    op.create_index(op.f("ix_preview_sessions_status"), "preview_sessions", ["status"], unique=False)

    op.create_table(
        "payouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("receiver_email", sa.String(length=254), nullable=False),
        sa.Column("gross_amount", MONEY, nullable=False),
        sa.Column("fee_amount", MONEY, nullable=False),
        sa.Column("net_amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("paypal_batch_id", sa.String(length=64), nullable=True),
        sa.Column("paypal_item_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], name=op.f("fk_payouts_listing_id_listings")),
        sa.ForeignKeyConstraint(["session_id"], ["preview_sessions.id"], name=op.f("fk_payouts_session_id_preview_sessions")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payouts")),
        sa.UniqueConstraint("session_id", name=op.f("uq_payouts_session_id")),
    )
    op.create_index(op.f("ix_payouts_listing_id"), "payouts", ["listing_id"], unique=False)

    op.create_table(
        "timeline_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", JSON, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["preview_sessions.id"], name=op.f("fk_timeline_events_session_id_preview_sessions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timeline_events")),
    )
    op.create_index(op.f("ix_timeline_events_session_id"), "timeline_events", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_table("timeline_events")
    op.drop_table("payouts")
    op.drop_table("preview_sessions")
    op.drop_table("webhook_events")
    op.drop_table("listings")
