"""Owner payouts: after a session ends normally, pay the owner the price minus the platform fee.

One Payout row per session (unique), created before calling PayPal; the PayPal batch id is
derived from the session id, so neither a crash nor a retry can pay an owner twice.
Status comes from webhooks (PAYMENT.PAYOUTSBATCH.*) and, as a fallback, the reaper polling.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Payout, PreviewSession, SessionStatus
from app.payments.paypal import PayPalClient, PayPalError

log = logging.getLogger(__name__)

PENDING = {"pending", "PENDING", "PROCESSING"}


def split(gross: Decimal) -> tuple[Decimal, Decimal]:
    """(platform fee, owner net) for a gross amount."""
    fee = (gross * Decimal(get_settings().platform_fee_percent) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return fee, gross - fee


def payout_session(db: Session, s: PreviewSession, paypal: PayPalClient) -> Payout | None:
    """Pay the owner for a session that went live and ended. Idempotent."""
    if s.status != SessionStatus.EXPIRED or not s.live_at or not s.paypal_capture_id or s.paypal_refund_id:
        return None
    payout = db.scalar(select(Payout).where(Payout.session_id == s.id))
    if payout and payout.paypal_batch_id:
        return payout
    email = s.listing.owner_paypal_email
    if not email:
        return None
    if payout is None:
        fee, net = split(s.amount)
        payout = Payout(
            session_id=s.id,
            listing_id=s.listing_id,
            receiver_email=email,
            gross_amount=s.amount,
            fee_amount=fee,
            net_amount=net,
            currency=s.currency,
            status="pending",
        )
        db.add(payout)
        db.commit()

    try:
        result = paypal.create_payout(
            sender_batch_id=f"ptp-{s.id.hex}",
            receiver_email=payout.receiver_email,
            amount=payout.net_amount,
            currency=payout.currency,
            note=f"{s.hours}h demo of {s.listing.title} (after {get_settings().platform_fee_percent}% platform fee)",
        )
    except PayPalError as exc:
        log.warning("payout failed for session %s: %s", s.id, exc)
        payout.error = str(exc)[:500]
        db.commit()
        return payout
    header = result.get("batch_header") or {}
    payout.paypal_batch_id = header.get("payout_batch_id")
    payout.status = header.get("batch_status", "PENDING")
    payout.error = None
    s.add_event("payout", f"Owner paid ${payout.net_amount} via PayPal Payouts (platform fee ${payout.fee_amount})")
    db.commit()
    return payout


def refresh_payout(db: Session, payout: Payout, paypal: PayPalClient) -> None:
    """Poll PayPal for a pending payout's status (webhooks are the fast path)."""
    if not payout.paypal_batch_id or payout.status not in PENDING:
        return
    try:
        data = paypal.get_payout(payout.paypal_batch_id)
    except PayPalError as exc:
        log.warning("could not refresh payout %s: %s", payout.paypal_batch_id, exc)
        return
    payout.status = (data.get("batch_header") or {}).get("batch_status", payout.status)
    items = data.get("items") or []
    if items:
        payout.paypal_item_id = items[0].get("payout_item_id")
    db.commit()


def apply_payout_event(db: Session, event_type: str, resource: dict) -> str:
    """Update a payout from a PAYMENT.PAYOUTSBATCH.* or PAYMENT.PAYOUTS-ITEM.* webhook."""
    if event_type.startswith("PAYMENT.PAYOUTSBATCH."):
        batch_id = (resource.get("batch_header") or {}).get("payout_batch_id")
        status = (resource.get("batch_header") or {}).get("batch_status")
    else:
        batch_id = resource.get("payout_batch_id")
        status = {"PAYMENT.PAYOUTS-ITEM.SUCCEEDED": "SUCCESS", "PAYMENT.PAYOUTS-ITEM.FAILED": "FAILED"}.get(event_type)
    payout = db.scalar(select(Payout).where(Payout.paypal_batch_id == batch_id)) if batch_id else None
    if payout is None or not status:
        return "no matching payout"
    payout.status = status
    if resource.get("payout_item_id"):
        payout.paypal_item_id = resource["payout_item_id"]
    db.commit()
    return f"payout {status}"
