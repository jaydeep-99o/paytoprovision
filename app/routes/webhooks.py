"""PayPal webhooks: signature-verified, idempotent (processed event IDs are stored).

Webhooks are a safety net, not the main path: the browser captures right after approval.
They cover a buyer who closes the tab after approving (CHECKOUT.ORDER.APPROVED -> we
capture), late-clearing payments (PAYMENT.CAPTURE.COMPLETED) and refund confirmations.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.checkout import CheckoutError, complete_payment, mark_paid
from app.config import get_settings
from app.db import DbSession
from app.models import PreviewSession, SessionStatus, WebhookEvent
from app.routes.checkout import PayPal, Submitter

log = logging.getLogger(__name__)
router = APIRouter()


def _order_id_of_capture(resource: dict[str, Any]) -> str | None:
    return ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")


def _session_for(db: Session, *, order_id: str | None = None, capture_id: str | None = None):
    if order_id:
        return db.scalar(select(PreviewSession).where(PreviewSession.paypal_order_id == order_id))
    if capture_id:
        return db.scalar(select(PreviewSession).where(PreviewSession.paypal_capture_id == capture_id))
    return None


def handle_event(db: Session, paypal, submit, event: dict[str, Any]) -> str:
    """Apply one verified, first-seen event. Returns a short outcome for logs/tests."""
    kind = event.get("event_type", "")
    resource = event.get("resource") or {}

    if kind == "CHECKOUT.ORDER.APPROVED":
        try:
            session = complete_payment(db, paypal, resource.get("id", ""))
        except CheckoutError as exc:
            return f"capture skipped: {exc.message}"
        if session.status == SessionStatus.PAID:
            submit(session.id)
        return f"session {session.id} is {session.status}"

    if kind == "PAYMENT.CAPTURE.COMPLETED":
        session = _session_for(db, order_id=_order_id_of_capture(resource))
        if session is None:
            return "no matching session"
        if session.status == SessionStatus.PENDING_PAYMENT:  # e.g. a capture that was PENDING earlier
            session.paypal_capture_id = session.paypal_capture_id or resource.get("id")
            mark_paid(session)
            db.commit()
            submit(session.id)
            return "marked paid"
        return "already handled"

    if kind == "PAYMENT.CAPTURE.DENIED":
        session = _session_for(db, order_id=_order_id_of_capture(resource))
        if session and session.status == SessionStatus.PENDING_PAYMENT:
            session.add_event("payment_failed", "PayPal denied the payment. You have not been charged.")
            db.commit()
        return "denied"

    if kind == "PAYMENT.CAPTURE.REFUNDED":
        capture_id = next(
            (link["href"].rsplit("/", 1)[-1] for link in resource.get("links", []) if link.get("rel") == "up"), None
        )
        session = _session_for(db, capture_id=capture_id)
        if session:
            session.add_event("refund_confirmed", "PayPal confirmed the refund.")
            db.commit()
        return "refund noted"

    return "ignored"


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, db: DbSession, paypal: PayPal, submit: Submitter):
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    webhook_id = get_settings().paypal_webhook_id
    if paypal is None or not webhook_id:
        # 503 makes PayPal retry later, once the webhook is configured.
        return JSONResponse({"error": "webhooks not configured"}, status_code=503)
    if not await run_in_threadpool(paypal.verify_webhook, request.headers, event, webhook_id):
        log.warning("rejected webhook with invalid signature: %s", event.get("id"))
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    event_id = event.get("id")
    if not event_id:
        return JSONResponse({"error": "missing event id"}, status_code=400)
    if db.get(WebhookEvent, event_id) is not None:
        return {"status": "duplicate"}
    db.add(
        WebhookEvent(
            id=event_id,
            event_type=event.get("event_type", ""),
            resource_id=(event.get("resource") or {}).get("id"),
            payload=event,
        )
    )
    try:
        db.commit()
    except IntegrityError:  # a concurrent delivery of the same event won the race
        db.rollback()
        return {"status": "duplicate"}

    outcome = await run_in_threadpool(handle_event, db, paypal, submit, event)
    log.info("webhook %s %s: %s", event.get("event_type"), event_id, outcome)
    return {"status": "ok", "outcome": outcome}
