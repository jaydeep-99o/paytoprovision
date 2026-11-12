"""Buyer checkout: PayPal order create/capture (JSON for the JS SDK), return URL, session pages."""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.checkout import CheckoutError, complete_payment, start_checkout
from app.config import get_settings
from app.db import DbSession
from app.models import Listing, PreviewSession, SessionStatus
from app.payments.paypal import PayPalClient
from app.ratelimit import RateLimiter
from app.routes.deps import get_paypal, get_provision_submitter
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

PayPal = Annotated[PayPalClient | None, Depends(get_paypal)]
Submitter = Annotated[Any, Depends(get_provision_submitter)]

TERMINAL = {SessionStatus.EXPIRED, SessionStatus.FAILED_REFUNDED}


class OrderRequest(BaseModel):
    hours: int = 1


def _error(exc: CheckoutError) -> JSONResponse:
    return JSONResponse({"error": exc.message, "issue": exc.issue}, status_code=exc.status_code)


def _require_paypal(paypal: PayPalClient | None) -> PayPalClient:
    if paypal is None:
        raise HTTPException(503, "Payments aren't configured on this server.")
    return paypal


# Best-effort per-visitor limit on opening PayPal orders (the hard caps live in app/budget.py).
order_limiter = RateLimiter(limit=12, window_seconds=600)


@router.post("/api/checkout/{slug}/orders")
def create_order(slug: str, body: OrderRequest, request: Request, db: DbSession, paypal: PayPal):
    if not order_limiter.allow(request.client.host if request.client else "unknown"):
        return JSONResponse({"error": "Too many checkouts in a short time. Wait a few minutes."}, status_code=429)
    listing = db.scalar(select(Listing).where(Listing.slug == slug))
    if listing is None:
        raise HTTPException(404, "Listing not found")
    try:
        session = start_checkout(db, _require_paypal(paypal), listing, body.hours)
    except CheckoutError as exc:
        return _error(exc)
    return {"id": session.paypal_order_id, "session_id": str(session.id)}


@router.post("/api/checkout/orders/{order_id}/capture")
def capture_order(order_id: str, db: DbSession, paypal: PayPal, submit: Submitter):
    try:
        session = complete_payment(db, _require_paypal(paypal), order_id)
    except CheckoutError as exc:
        return _error(exc)
    if session.status == SessionStatus.PAID:
        submit(session.id)
    return {"status": session.status, "redirect": f"/s/{session.id}"}


@router.get("/checkout/return")
def paypal_return(request: Request, db: DbSession, paypal: PayPal, submit: Submitter, token: str = ""):
    """Return URL for the redirect flow (approval links, e.g. from the checkout agent).
    PayPal appends ?token=<order id>&PayerID=..."""
    session = db.scalar(select(PreviewSession).where(PreviewSession.paypal_order_id == token)) if token else None
    if session is None:
        raise HTTPException(404, "We couldn't find that checkout.")
    try:
        session = complete_payment(db, _require_paypal(paypal), token)
    except CheckoutError as exc:
        log.warning("capture on return failed for %s: %s", token, exc.message)
        return RedirectResponse(f"/s/{session.id}?error={exc.issue or 'capture'}", status_code=303)
    if session.status == SessionStatus.PAID:
        submit(session.id)
    return RedirectResponse(f"/s/{session.id}", status_code=303)


def _load_session(db, session_id: uuid.UUID) -> PreviewSession:
    session = db.get(PreviewSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


PROGRESS_STEPS = ["Payment", "Environment", "Build", "Start", "Live"]


def _progress_step(session: PreviewSession) -> int:
    """Index into PROGRESS_STEPS of the step currently in progress (len = all done)."""
    status = SessionStatus(session.status)
    if status == SessionStatus.PENDING_PAYMENT:
        return 0
    if status in (SessionStatus.LIVE, SessionStatus.EXPIRED):
        return len(PROGRESS_STEPS)
    deploy_statuses = [e.detail.get("status") for e in session.events if e.kind == "deploy_status" and e.detail]
    last = deploy_statuses[-1] if deploy_statuses else None
    if last == "update_in_progress":
        return 3
    if last == "build_in_progress":
        return 2
    return 1


def _session_context(session: PreviewSession, error: str | None = None) -> dict[str, Any]:
    status = SessionStatus(session.status)
    return {
        "s": session,
        "listing": session.listing,
        "status": status,
        "terminal": status in TERMINAL,
        "error": error,
        "steps": PROGRESS_STEPS,
        "step": _progress_step(session),
        "max_attempts": 1 + get_settings().max_heal_attempts,
    }


@router.get("/s/{session_id}")
def session_page(request: Request, session_id: uuid.UUID, db: DbSession, error: str | None = None):
    session = _load_session(db, session_id)
    return templates.TemplateResponse(request, "session.html", _session_context(session, error))


@router.get("/s/{session_id}/status")
def session_status(request: Request, session_id: uuid.UUID, db: DbSession):
    """HTMX-polled fragment: status card + timeline."""
    session = _load_session(db, session_id)
    return templates.TemplateResponse(request, "partials/session_status.html", _session_context(session))
