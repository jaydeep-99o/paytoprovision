"""ORM models.

Listing         a repo an owner has put up for paid previews
PreviewSession  one paid preview: PayPal order -> Render service -> expiry/refund
TimelineEvent   human-readable steps shown on the session page (deploys, AI fixes, refunds)
WebhookEvent    processed PayPal webhook IDs, for idempotency
Payout          owner payout for a finished session
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, UTCDateTime, utcnow

Money = Numeric(10, 2)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ListingStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


class SessionStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    DEPLOYING = "deploying"
    HEALING = "healing"
    LIVE = "live"
    EXPIRED = "expired"
    FAILED_REFUNDED = "failed_refunded"


# Allowed lifecycle transitions. The healer bounces between DEPLOYING and HEALING
# for each retry; an abandoned checkout goes PENDING_PAYMENT -> EXPIRED.
SESSION_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.PENDING_PAYMENT: {SessionStatus.PAID, SessionStatus.EXPIRED},
    SessionStatus.PAID: {SessionStatus.DEPLOYING, SessionStatus.FAILED_REFUNDED},
    SessionStatus.DEPLOYING: {SessionStatus.HEALING, SessionStatus.LIVE, SessionStatus.FAILED_REFUNDED},
    SessionStatus.HEALING: {SessionStatus.DEPLOYING, SessionStatus.LIVE, SessionStatus.FAILED_REFUNDED},
    SessionStatus.LIVE: {SessionStatus.EXPIRED},
    SessionStatus.EXPIRED: set(),
    SessionStatus.FAILED_REFUNDED: set(),
}

# Sessions that hold (or are about to hold) a Render service; these count toward
# the concurrent-preview limit and are what the reaper watches.
ACTIVE_SESSION_STATUSES = {
    SessionStatus.PAID,
    SessionStatus.DEPLOYING,
    SessionStatus.HEALING,
    SessionStatus.LIVE,
}


class InvalidTransition(Exception):
    pass


class Listing(TimestampMixin, Base):
    __tablename__ = "listings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    repo_url: Mapped[str] = mapped_column(String(300))
    repo_full_name: Mapped[str] = mapped_column(String(200), index=True)  # "owner/name"
    branch: Mapped[str | None] = mapped_column(String(100))  # None = repo default branch
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    owner_paypal_email: Mapped[str] = mapped_column(String(254))
    price_per_hour: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(20), default=ListingStatus.DRAFT)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    # Deploy config the previews use: runtime, build_command, start_command,
    # env_vars, health_path. Starts as the AI analysis, editable by the owner.
    deploy_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # raw AI output
    confidence: Mapped[float | None] = mapped_column(Float)
    # Random per-browser key (cookie) identifying the owner; there are no user accounts.
    owner_key: Mapped[str | None] = mapped_column(String(64), index=True)

    sessions: Mapped[list["PreviewSession"]] = relationship(back_populates="listing")

    @property
    def stack(self) -> str:
        return ((self.analysis or {}).get("result") or {}).get("stack") or self.deploy_config.get("runtime", "")

    @property
    def is_live(self) -> bool:
        """Visible to buyers: published and allowlisted/approved."""
        return self.status == ListingStatus.PUBLISHED and self.approved


class PreviewSession(TimestampMixin, Base):
    __tablename__ = "preview_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("listings.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=SessionStatus.PENDING_PAYMENT, index=True)
    hours: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    buyer_email: Mapped[str | None] = mapped_column(String(254))

    paypal_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    paypal_capture_id: Mapped[str | None] = mapped_column(String(64))
    paypal_refund_id: Mapped[str | None] = mapped_column(String(64))

    render_service_id: Mapped[str | None] = mapped_column(String(64), index=True)
    render_service_name: Mapped[str | None] = mapped_column(String(100))
    render_deploy_id: Mapped[str | None] = mapped_column(String(64))
    live_url: Mapped[str | None] = mapped_column(String(300))
    deploy_attempts: Mapped[int] = mapped_column(Integer, default=0)
    active_config: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # config of the current attempt
    failure_reason: Mapped[str | None] = mapped_column(Text)

    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    live_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    listing: Mapped[Listing] = relationship(back_populates="sessions")
    events: Mapped[list["TimelineEvent"]] = relationship(
        back_populates="session", order_by="TimelineEvent.id", cascade="all, delete-orphan"
    )
    payout: Mapped["Payout | None"] = relationship(back_populates="session")

    def transition(self, new_status: SessionStatus) -> None:
        current = SessionStatus(self.status)
        if new_status not in SESSION_TRANSITIONS[current]:
            raise InvalidTransition(f"{current} -> {new_status}")
        self.status = new_status

    def add_event(self, kind: str, message: str, detail: dict[str, Any] | None = None) -> "TimelineEvent":
        event = TimelineEvent(kind=kind, message=message, detail=detail)
        self.events.append(event)
        return event


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("preview_sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # e.g. paid, deploying, build_failed, ai_fix, live, refunded
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    session: Mapped[PreviewSession] = relationship(back_populates="events")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PayPal event id
    event_type: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Payout(TimestampMixin, Base):
    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("preview_sessions.id"), unique=True)
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("listings.id"), index=True)
    receiver_email: Mapped[str] = mapped_column(String(254))
    gross_amount: Mapped[Decimal] = mapped_column(Money)
    fee_amount: Mapped[Decimal] = mapped_column(Money)
    net_amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|sent|success|failed
    paypal_batch_id: Mapped[str | None] = mapped_column(String(64))
    paypal_item_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)

    session: Mapped[PreviewSession] = relationship(back_populates="payout")
