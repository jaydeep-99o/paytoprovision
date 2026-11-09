"""Reaper: keeps Render and the database in line with session lifecycles.

Runs (a) in the background after app requests, at most once a minute, and (b) from the
GitHub Actions cron via POST /internal/reap, which also wakes the free instance.

Each run:
  1. LIVE sessions past expires_at  -> delete the service, EXPIRED
  2. checkouts never paid after 1h  -> EXPIRED
  3. stuck PAID/DEPLOYING sessions  -> resume provisioning (the worker died)
  4. failed sessions not yet refunded -> retry the refund
  5. orphan `ptp-` services with no active session, older than 30 min -> delete
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import provisioner
from app.config import get_settings
from app.db import SessionLocal
from app.models import ACTIVE_SESSION_STATUSES, PreviewSession, SessionStatus
from app.payments.paypal import PayPalClient
from app.render_client import RenderAPIError, RenderClient

log = logging.getLogger(__name__)

ABANDONED_CHECKOUT_AFTER = timedelta(hours=1)
STUCK_AFTER = timedelta(minutes=3)
ORPHAN_GRACE = timedelta(minutes=30)
AUTO_INTERVAL_SECONDS = 60

_run_lock = threading.Lock()
_last_auto_run = 0.0
AUTO_ENABLED = True  # tests switch this off


def safe_prefix(prefix: str) -> bool:
    """The orphan sweep deletes every service with this prefix, so insist on a specific one."""
    return len(prefix) >= 4 and prefix.endswith("-")


def maybe_reap_in_background() -> None:
    """Throttled fire-and-forget run, triggered by app traffic."""
    global _last_auto_run
    now = time.monotonic()
    if not AUTO_ENABLED or now - _last_auto_run < AUTO_INTERVAL_SECONDS or _run_lock.locked():
        return
    _last_auto_run = now
    threading.Thread(target=_safe_reap, name="reaper", daemon=True).start()


def _safe_reap() -> None:
    try:
        reap()
    except Exception:
        log.exception("background reaper run failed")


def reap(
    *,
    session_factory: sessionmaker = SessionLocal,
    render_factory: Callable[[], RenderClient] = RenderClient.from_settings,
    paypal_factory: Callable[[], PayPalClient] = PayPalClient.from_settings,
    resume: Callable[[Any], bool] = provisioner.submit_provisioning,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not _run_lock.acquire(blocking=False):
        return {"skipped": "another reaper run is in progress"}
    try:
        return _reap(session_factory, render_factory, paypal_factory, resume, now or datetime.now(UTC))
    finally:
        _run_lock.release()


def _reap(session_factory, render_factory, paypal_factory, resume, now) -> dict[str, Any]:
    summary = {"expired": 0, "abandoned": 0, "resumed": 0, "refund_retries": 0, "orphans_deleted": 0, "errors": 0}
    with session_factory() as db, render_factory() as render:
        # 1. expire live sessions
        for s in db.scalars(
            select(PreviewSession).where(PreviewSession.status == SessionStatus.LIVE, PreviewSession.expires_at <= now)
        ).all():
            try:
                if s.render_service_id:
                    render.delete_service(s.render_service_id)
            except RenderAPIError as exc:
                log.warning("reaper: could not delete %s: %s", s.render_service_id, exc)
                summary["errors"] += 1
                continue
            s.transition(SessionStatus.EXPIRED)
            s.ended_at = now
            s.add_event("expired", "Time's up. The environment was deleted.")
            summary["expired"] += 1
        db.commit()

        # 2. abandoned checkouts
        for s in db.scalars(
            select(PreviewSession).where(
                PreviewSession.status == SessionStatus.PENDING_PAYMENT,
                PreviewSession.paypal_capture_id.is_(None),
                PreviewSession.created_at <= now - ABANDONED_CHECKOUT_AFTER,
            )
        ).all():
            s.transition(SessionStatus.EXPIRED)
            s.ended_at = now
            s.add_event("abandoned", "Checkout was never completed.")
            summary["abandoned"] += 1
        db.commit()

        # 3 + 4. stuck provisioning and pending refunds
        for s in db.scalars(
            select(PreviewSession).where(
                PreviewSession.status.in_([SessionStatus.PAID, SessionStatus.DEPLOYING, SessionStatus.HEALING]),
                PreviewSession.updated_at <= now - STUCK_AFTER,
            )
        ).all():
            if provisioner.is_in_flight(s.id):
                continue
            if s.failure_reason:  # provisioning gave up but the refund didn't go through
                summary["refund_retries"] += 1
                provisioner.fail_and_refund(db, s, s.failure_reason, render, paypal_factory)
            elif resume(s.id):
                summary["resumed"] += 1

        # 5. orphans
        active_ids = set(
            db.scalars(
                select(PreviewSession.render_service_id).where(
                    PreviewSession.status.in_(ACTIVE_SESSION_STATUSES), PreviewSession.render_service_id.is_not(None)
                )
            )
        )
        prefix = get_settings().preview_name_prefix
        try:
            services = render.list_services(name_prefix=prefix) if safe_prefix(prefix) else []
        except RenderAPIError as exc:
            log.warning("reaper: could not list services: %s", exc)
            summary["errors"] += 1
            services = []
        for svc in services:
            old_enough = svc.created_at is not None and svc.created_at <= now - ORPHAN_GRACE
            if svc.id not in active_ids and old_enough:
                try:
                    render.delete_service(svc.id)
                    summary["orphans_deleted"] += 1
                    log.info("reaper: deleted orphan service %s (%s)", svc.name, svc.id)
                except RenderAPIError as exc:
                    log.warning("reaper: could not delete orphan %s: %s", svc.id, exc)
                    summary["errors"] += 1
    if any(summary.values()):
        log.info("reaper run: %s", summary)
    return summary
