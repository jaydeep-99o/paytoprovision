"""Provisioning: turn a PAID session into a live Render service, or refund it.

`run_provisioning` is re-entrant by state, so the reaper can resume a session whose
worker died (e.g. the app restarted mid-deploy):
  PAID                       -> DEPLOYING, create the service
  DEPLOYING, no service yet  -> create the service
  DEPLOYING, service exists  -> watch its latest deploy
then LIVE (with expires_at) or, on failure, refund -> FAILED_REFUNDED.
"""

import logging
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.models import PreviewSession, SessionStatus
from app.payments.paypal import PayPalClient, PayPalError
from app.render_client import RenderAPIError, RenderClient, RenderDeploy

log = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 15 * 60
LOG_TAIL_LINES = 40

STATUS_MESSAGES = {
    "created": "Deploy queued on Render",
    "queued": "Deploy queued on Render",
    "build_in_progress": "Building the app (installing dependencies)…",
    "update_in_progress": "Build finished. Starting the app…",
    "pre_deploy_in_progress": "Running pre-deploy step…",
}

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="provision")
_in_flight: set[uuid.UUID] = set()
_in_flight_lock = threading.Lock()


def preview_name(session: PreviewSession) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", session.listing.slug.lower()).strip("-")[:24].strip("-")
    return f"{get_settings().preview_name_prefix}{base}-{session.id.hex[:6]}"


def is_in_flight(session_id: uuid.UUID) -> bool:
    with _in_flight_lock:
        return session_id in _in_flight


def submit_provisioning(session_id: uuid.UUID, **kwargs) -> bool:
    """Run provisioning in the background. Returns False if it's already running for this session."""
    with _in_flight_lock:
        if session_id in _in_flight:
            return False
        _in_flight.add(session_id)

    def job() -> None:
        try:
            run_provisioning(session_id, **kwargs)
        except Exception:
            log.exception("provisioning crashed for session %s", session_id)
        finally:
            with _in_flight_lock:
                _in_flight.discard(session_id)

    _executor.submit(job)
    return True


def run_provisioning(
    session_id: uuid.UUID,
    *,
    session_factory: sessionmaker = SessionLocal,
    render_factory: Callable[[], RenderClient] = RenderClient.from_settings,
    paypal_factory: Callable[[], PayPalClient] = PayPalClient.from_settings,
    poll_interval: float = 8.0,
) -> None:
    with session_factory() as db, render_factory() as render:
        s = db.get(PreviewSession, session_id)
        if s is None or s.status not in (SessionStatus.PAID, SessionStatus.DEPLOYING):
            return
        if s.status == SessionStatus.PAID:
            s.transition(SessionStatus.DEPLOYING)
            s.add_event("deploying", "Creating your private environment on Render")
            db.commit()

        try:
            if not s.render_service_id:
                _create_service(db, s, render)
                deploy_id = s.render_deploy_id
            else:
                latest = render.get_latest_deploy(s.render_service_id)
                deploy_id = latest.id if latest else render.trigger_deploy(s.render_service_id).id
                s.render_deploy_id = deploy_id
                db.commit()

            def on_status(d: RenderDeploy) -> None:
                if d.status in STATUS_MESSAGES:
                    s.add_event("deploy_status", STATUS_MESSAGES[d.status], {"status": d.status})
                    db.commit()

            deploy = render.wait_for_deploy(
                s.render_service_id,
                deploy_id,
                timeout=DEPLOY_TIMEOUT_SECONDS,
                poll_interval=poll_interval,
                on_status=on_status,
            )
        except (RenderAPIError, TimeoutError) as exc:
            log.warning("provisioning error for %s: %s", session_id, exc)
            fail_and_refund(db, s, f"Render couldn't deploy the app: {exc}", render, paypal_factory)
            return

        db.refresh(s)
        if s.status != SessionStatus.DEPLOYING:  # another worker already finished this session
            return
        if deploy.succeeded:
            go_live(db, s)
        else:
            logs = tail_logs(render, s.render_service_id, deploy)
            s.add_event("deploy_failed", f"Deploy failed ({deploy.status.replace('_', ' ')})", {"logs": logs})
            db.commit()
            fail_and_refund(db, s, f"The deploy ended as {deploy.status}.", render, paypal_factory)


def _create_service(db: Session, s: PreviewSession, render: RenderClient) -> None:
    listing = s.listing
    cfg = dict(listing.deploy_config or {})
    s.active_config = cfg
    name = preview_name(s)
    created = render.create_web_service(
        name=name,
        repo_url=listing.repo_url,
        branch=listing.branch,
        runtime=cfg.get("runtime", "python"),
        build_command=cfg.get("build_command") or None,
        start_command=cfg.get("start_command") or None,
        env_vars=cfg.get("env_vars") or {},
        health_check_path=cfg.get("health_path") or None,
        plan=get_settings().preview_plan,
    )
    s.render_service_id = created.service.id
    s.render_service_name = name
    s.render_deploy_id = created.deploy_id
    s.live_url = created.service.url
    s.deploy_attempts = (s.deploy_attempts or 0) + 1
    s.add_event("service_created", f"Render service {name} created")
    db.commit()
    if not s.render_deploy_id:
        s.render_deploy_id = render.trigger_deploy(s.render_service_id).id
        db.commit()


def go_live(db: Session, s: PreviewSession) -> None:
    now = datetime.now(UTC)
    s.transition(SessionStatus.LIVE)
    s.live_at = now
    s.expires_at = now + timedelta(hours=s.hours)
    s.add_event("live", f"Live at {s.live_url}. Your {s.hours}h countdown has started.")
    db.commit()


def tail_logs(render: RenderClient, service_id: str, deploy: RenderDeploy) -> list[str]:
    """Last build + app log lines for a deploy (for the timeline and, in Phase 4, the AI healer)."""
    lines = []
    for log_type in ("build", "app"):
        try:
            lines += render.get_logs(service_id, log_type=log_type, since=deploy.created_at, max_lines=LOG_TAIL_LINES)
        except RenderAPIError as exc:
            log.warning("could not fetch %s logs: %s", log_type, exc)
    lines.sort(key=lambda line: line.timestamp)
    return [line.message for line in lines][-LOG_TAIL_LINES:]


def fail_and_refund(
    db: Session,
    s: PreviewSession,
    reason: str,
    render: RenderClient,
    paypal_factory: Callable[[], PayPalClient],
) -> None:
    """Refund the buyer, delete the half-made service, mark FAILED_REFUNDED. If the refund
    call fails, the session keeps its state and failure_reason so the reaper retries."""
    s.failure_reason = reason
    db.commit()
    if s.paypal_capture_id and not s.paypal_refund_id:
        try:
            with paypal_factory() as paypal:
                refund = paypal.refund_capture(
                    s.paypal_capture_id, note=f"PayToProvision couldn't start {s.listing.title}: full refund."
                )
        except PayPalError as exc:
            log.error("refund failed for session %s: %s", s.id, exc)
            s.add_event("refund_retry", "Couldn't reach PayPal to refund yet. Retrying automatically.")
            db.commit()
            return
        s.paypal_refund_id = refund.get("id")
    _delete_service(s, render)
    s.transition(SessionStatus.FAILED_REFUNDED)
    s.ended_at = datetime.now(UTC)
    s.add_event("refunded", f"Refunded ${s.amount} to your PayPal account. Sorry about that!")
    db.commit()


def _delete_service(s: PreviewSession, render: RenderClient) -> None:
    if s.render_service_id:
        try:
            render.delete_service(s.render_service_id)
        except RenderAPIError as exc:  # the reaper's orphan sweep will catch it
            log.warning("could not delete %s: %s", s.render_service_id, exc)
