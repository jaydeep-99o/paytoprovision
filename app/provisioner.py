"""Provisioning: turn a PAID session into a live Render service, healing failed deploys
with AI, or refund it.

`run_provisioning` is re-entrant by state, so the reaper can resume a session whose
worker died (e.g. the app restarted mid-deploy):
  PAID                       -> DEPLOYING, create the service
  DEPLOYING, no service yet  -> create the service
  DEPLOYING, service exists  -> watch its latest deploy
  HEALING                    -> its latest deploy failed: heal it
A failed deploy moves to HEALING: the AI reads the logs and proposes a config-only fix,
which is checked, applied and redeployed (back to DEPLOYING), at most MAX_HEAL_ATTEMPTS
times. The session ends LIVE (with expires_at) or refunded (FAILED_REFUNDED).
"""

import logging
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.ai.healer import check_fix, propose_fix
from app.ai.llm import LLMClient, LLMError
from app.config import get_settings
from app.db import SessionLocal
from app.models import PreviewSession, SessionStatus
from app.payments.paypal import PayPalClient, PayPalError
from app.render_client import RenderAPIError, RenderClient, RenderDeploy

log = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 15 * 60
STARTUP_TIMEOUT_SECONDS = 4 * 60  # built but not healthy after this long = failed
LOG_TAIL_LINES = 40

STATUS_MESSAGES = {
    "created": "Deploy queued on Render",
    "queued": "Deploy queued on Render",
    "build_in_progress": "Building the app (installing dependencies)…",
    "update_in_progress": "Build finished. Starting the app…",
    "pre_deploy_in_progress": "Running pre-deploy step…",
}
FAILURE_TEXT = {
    "build_failed": "the build failed",
    "update_failed": "the app crashed on startup or never became healthy",
    "start_timeout": "the app started but never passed its health check",
    "pre_deploy_failed": "the pre-deploy step failed",
    "canceled": "the deploy was cancelled",
    "deactivated": "the deploy was replaced",
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


def _llm_or_none() -> LLMClient | None:
    try:
        return LLMClient.from_settings()
    except LLMError:  # AI not configured: failed deploys go straight to refund
        return None


def run_provisioning(
    session_id: uuid.UUID,
    *,
    session_factory: sessionmaker = SessionLocal,
    render_factory: Callable[[], RenderClient] = RenderClient.from_settings,
    paypal_factory: Callable[[], PayPalClient] = PayPalClient.from_settings,
    llm_factory: Callable[[], LLMClient | None] | None = None,
    poll_interval: float = 8.0,
) -> None:
    llm_factory = llm_factory or _llm_or_none  # resolved per call so tests can switch the AI off
    with session_factory() as db, render_factory() as render:
        s = db.get(PreviewSession, session_id)
        if s is None or s.status not in (SessionStatus.PAID, SessionStatus.DEPLOYING, SessionStatus.HEALING):
            return
        if s.status == SessionStatus.PAID:
            s.transition(SessionStatus.DEPLOYING)
            s.add_event("deploying", "Creating your private environment on Render")
            db.commit()

        try:
            deploy = _current_deploy(db, s, render, poll_interval)
            while True:
                db.refresh(s)
                if s.status not in (SessionStatus.DEPLOYING, SessionStatus.HEALING):
                    return  # another worker already finished this session
                if deploy.succeeded:
                    go_live(db, s)
                    return

                logs = tail_logs(render, s.render_service_id, deploy)
                text = FAILURE_TEXT.get(deploy.status, deploy.status.replace("_", " "))
                s.add_event(
                    "deploy_failed",
                    f"Attempt {s.deploy_attempts} failed: {text}",
                    {"logs": logs, "status": deploy.status},
                )
                if s.status == SessionStatus.DEPLOYING:
                    s.transition(SessionStatus.HEALING)
                db.commit()

                fixes_tried = max(s.deploy_attempts - 1, 0)
                if fixes_tried >= get_settings().max_heal_attempts:
                    diagnosis = _last_diagnosis(s)
                    reason = f"It still failed after {fixes_tried} AI fixes." + (f" {diagnosis}" if diagnosis else "")
                    fail_and_refund(db, s, reason, render, paypal_factory)
                    return
                deploy = heal_once(db, s, render, llm_factory, deploy, logs, poll_interval)
                if deploy is None:
                    fail_and_refund(db, s, s.failure_reason or "The deploy failed.", render, paypal_factory)
                    return
        except (RenderAPIError, TimeoutError) as exc:
            log.warning("provisioning error for %s: %s", session_id, exc)
            fail_and_refund(db, s, f"Render couldn't deploy the app: {exc}", render, paypal_factory)


def _watch(db: Session, s: PreviewSession, render: RenderClient, deploy_id: str, poll_interval: float) -> RenderDeploy:
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
        max_starting_seconds=STARTUP_TIMEOUT_SECONDS,
    )
    if deploy.is_terminal:
        return deploy
    # Built, but the app never passed its health check: don't let Render retry for 15 minutes.
    try:
        render.cancel_deploy(s.render_service_id, deploy_id)
    except RenderAPIError as exc:
        log.warning("could not cancel stalled deploy %s: %s", deploy_id, exc)
    return deploy.model_copy(update={"status": "start_timeout"})


def _current_deploy(db: Session, s: PreviewSession, render: RenderClient, poll_interval: float) -> RenderDeploy:
    """The deploy to act on: create the service if needed, or pick up where a dead worker stopped."""
    if not s.render_service_id:
        _create_service(db, s, render)
    elif s.status == SessionStatus.HEALING and s.render_deploy_id:
        return render.get_deploy(s.render_service_id, s.render_deploy_id)  # the failed deploy to heal
    else:
        latest = render.get_latest_deploy(s.render_service_id)
        s.render_deploy_id = latest.id if latest else render.trigger_deploy(s.render_service_id).id
        db.commit()
    return _watch(db, s, render, s.render_deploy_id, poll_interval)


def _last_diagnosis(s: PreviewSession) -> str | None:
    for e in reversed(s.events):
        if e.kind == "ai_diagnosis" and e.detail:
            return e.detail.get("root_cause")
    return None


def heal_once(
    db: Session,
    s: PreviewSession,
    render: RenderClient,
    llm_factory: Callable[[], LLMClient | None],
    failed: RenderDeploy,
    logs: list[str],
    poll_interval: float,
) -> RenderDeploy | None:
    """One AI repair attempt: diagnose, check the fix, apply it, redeploy and wait.
    Returns the new deploy's final state, or None (with failure_reason set) if it can't be fixed."""
    llm = llm_factory()
    if llm is None:
        s.failure_reason = "The deploy failed and automatic repair isn't available."
        db.commit()
        return None

    s.add_event("ai_thinking", "AI is reading the logs to find out what went wrong…")
    db.commit()
    current = dict(s.active_config or s.listing.deploy_config or {})
    try:
        with llm:
            plan = propose_fix(
                llm,
                repo=s.listing.repo_full_name,
                config=current,
                deploy_status=failed.status,
                logs=logs,
                attempt=s.deploy_attempts,
                max_attempts=1 + get_settings().max_heal_attempts,
                previous_fixes=[e.message for e in s.events if e.kind == "ai_fix"],
            )
    except LLMError as exc:
        log.warning("healer LLM call failed for %s: %s", s.id, exc)
        s.add_event("ai_gave_up", "The AI repair service didn't respond.")
        s.failure_reason = "The deploy failed and the AI repair service didn't respond."
        db.commit()
        return None

    s.add_event("ai_diagnosis", f"Diagnosis: {plan.root_cause}", {"root_cause": plan.root_cause})
    fix = check_fix(plan, current)
    if fix.problem:
        if plan.fixable:
            message, reason = f"The AI's fix was rejected: {fix.problem}.", f"{plan.root_cause} No safe fix was found."
        else:
            message, reason = (
                plan.fix_explanation,
                f"{plan.root_cause} It can't be fixed without changing the app's code.",
            )
        s.add_event("ai_gave_up", message, {"problem": fix.problem})
        s.failure_reason = reason
        db.commit()
        return None

    new = fix.config
    native_changes = {k: new[k] for k in ("build_command", "start_command", "health_path") if new[k] != current.get(k)}
    if native_changes:
        render.update_service(
            s.render_service_id,
            build_command=native_changes.get("build_command"),
            start_command=native_changes.get("start_command"),
            health_check_path=native_changes.get("health_path"),
        )
    if new["env_vars"] != (current.get("env_vars") or {}):
        render.set_env_vars(s.render_service_id, new["env_vars"])
    deploy = render.trigger_deploy(s.render_service_id)

    s.active_config = new
    s.render_deploy_id = deploy.id
    s.deploy_attempts += 1
    s.add_event(
        "ai_fix",
        f"AI fix: {plan.fix_explanation} Retrying (attempt {s.deploy_attempts})…",
        {"changes": fix.changes, "root_cause": plan.root_cause},
    )
    s.transition(SessionStatus.DEPLOYING)
    db.commit()
    return _watch(db, s, render, deploy.id, poll_interval)


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
    healed = (s.deploy_attempts or 1) > 1
    note = " The AI fix worked." if healed else ""
    s.add_event("live", f"Live at {s.live_url}.{note} Your {s.hours}h countdown has started.")
    if healed:  # tell the owner which config actually works (shown on their listing page)
        changes = next((e.detail.get("changes") for e in reversed(s.events) if e.kind == "ai_fix" and e.detail), [])
        analysis = dict(s.listing.analysis or {})
        analysis["healed_config"] = {"config": s.active_config, "changes": changes, "at": now.isoformat()}
        s.listing.analysis = analysis
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
