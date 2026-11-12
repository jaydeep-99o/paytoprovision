"""Checkout: quote, create the PayPal order, capture it. Independent of HTTP.

Capture is done server-side (from the browser's onApprove, the return URL, or the
CHECKOUT.ORDER.APPROVED webhook, whichever comes first); every path funnels into
`complete_payment`, which is idempotent.
"""

import logging
import threading
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import budget
from app.config import get_settings
from app.models import ACTIVE_SESSION_STATUSES, Listing, PreviewSession, SessionStatus
from app.payments.paypal import PayPalClient, PayPalError, capture_details

log = logging.getLogger(__name__)

# One app instance, so a process lock is enough to stop two captures racing for the last slot.
_capacity_lock = threading.Lock()


class CheckoutError(Exception):
    def __init__(self, message: str, *, status_code: int = 400, issue: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.issue = issue


def quote(listing: Listing, hours: int) -> Decimal:
    return (listing.price_per_hour * hours).quantize(Decimal("0.01"))


def active_sessions(db: Session) -> int:
    return db.scalar(
        select(func.count()).select_from(PreviewSession).where(PreviewSession.status.in_(ACTIVE_SESSION_STATUSES))
    )


def _check_capacity(db: Session) -> None:
    limit = get_settings().max_concurrent_previews
    if active_sessions(db) >= limit:
        raise CheckoutError(
            f"All {limit} preview slots are busy right now. Try again in a few minutes. You have not been charged.",
            status_code=409,
            issue="CAPACITY",
        )


def begin_session(db: Session, listing: Listing, hours: int) -> PreviewSession:
    """Validate a purchase and add a pending session (not committed). Shared by the PayPal
    buttons and the checkout agent, so both enforce the same price, hours and capacity rules."""
    settings = get_settings()
    if not listing.is_live:
        raise CheckoutError("This listing isn't available.", status_code=404)
    if not 1 <= hours <= settings.max_session_hours:
        raise CheckoutError(f"Choose between 1 and {settings.max_session_hours} hours.")
    _check_capacity(db)
    if budget.remaining(budget.DEPLOY) <= 0:
        raise CheckoutError(
            "Today's demo capacity is used up. Try again tomorrow. You have not been charged.",
            status_code=409,
            issue="DAILY_LIMIT",
        )
    session = PreviewSession(
        listing=listing,
        hours=hours,
        amount=quote(listing, hours),
        currency=listing.currency,
        status=SessionStatus.PENDING_PAYMENT,
    )
    db.add(session)
    db.flush()  # assigns session.id
    return session


def attach_order(db: Session, session: PreviewSession, order_id: str, *, via: str = "") -> None:
    session.paypal_order_id = order_id
    hours = session.hours
    session.add_event(
        "checkout", f"PayPal order created{via}: {hours} hour{'s' if hours > 1 else ''} for ${session.amount}"
    )
    db.commit()


def start_checkout(
    db: Session, paypal: PayPalClient, listing: Listing, hours: int, *, redirect: bool = False
) -> PreviewSession:
    """Create a pending session and its PayPal order (redirect=True for an approval link)."""
    session = begin_session(db, listing, hours)
    base = get_settings().app_base_url.rstrip("/")
    try:
        order = paypal.create_order(
            amount=session.amount,
            currency=session.currency,
            custom_id=str(session.id),
            description=f"{listing.title} live demo, {hours}h",
            return_url=f"{base}/checkout/return",
            cancel_url=f"{base}/l/{listing.slug}?cancelled=1",
            request_id=f"order-{session.id}",
            redirect=redirect,
        )
    except PayPalError as exc:
        db.rollback()
        log.error("create order failed: %s", exc)
        raise CheckoutError("PayPal couldn't start the checkout. Please try again.", status_code=502) from exc

    attach_order(db, session, order["id"])
    return session


def complete_payment(db: Session, paypal: PayPalClient, order_id: str) -> PreviewSession:
    """Capture the order and mark the session PAID. Idempotent: if the session is already
    past PENDING_PAYMENT it's returned untouched. Raises CheckoutError on failure."""
    with _capacity_lock:
        session = db.scalar(select(PreviewSession).where(PreviewSession.paypal_order_id == order_id))
        if session is None:
            raise CheckoutError("Unknown order.", status_code=404)
        if session.status != SessionStatus.PENDING_PAYMENT:
            return session
        if session.paypal_capture_id is None:
            _check_capacity(db)  # before taking the money

        try:
            order = paypal.capture_order(order_id)
        except PayPalError as exc:
            log.warning("capture failed for %s: %s", order_id, exc)
            if exc.issue == "INSTRUMENT_DECLINED":
                raise CheckoutError(
                    "Your payment method was declined. Choose another one.", status_code=422, issue=exc.issue
                ) from exc
            if exc.issue == "ORDER_NOT_APPROVED":
                raise CheckoutError("The payment hasn't been approved yet.", status_code=409, issue=exc.issue) from exc
            raise CheckoutError(
                "PayPal couldn't complete the payment. You have not been charged.", status_code=502
            ) from exc

        capture_id, capture_status, payer_email = capture_details(order)
        session.paypal_capture_id = capture_id
        session.buyer_email = payer_email or session.buyer_email
        if capture_status == "COMPLETED":
            mark_paid(session)
        elif capture_status == "PENDING":
            session.add_event("payment_pending", "PayPal is still processing the payment. We'll start once it clears.")
        else:
            session.add_event("payment_failed", f"PayPal reported the payment as {capture_status}.")
            db.commit()
            raise CheckoutError("PayPal didn't complete the payment.", status_code=402, issue=capture_status)
        db.commit()
        return session


def mark_paid(session: PreviewSession) -> None:
    session.transition(SessionStatus.PAID)
    session.paid_at = datetime.now(UTC)
    session.add_event("paid", f"Payment of ${session.amount} captured via PayPal")
