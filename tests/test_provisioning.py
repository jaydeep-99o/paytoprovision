from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from app import reaper
from app.config import get_settings
from app.main import app
from app.models import PreviewSession, SessionStatus
from app.provisioner import preview_name, run_provisioning
from tests.fakes import FakeRender


def paid_session(db_session_factory, listing, **fields) -> PreviewSession:
    with db_session_factory() as db:
        s = PreviewSession(
            listing_id=listing.id,
            hours=2,
            amount=Decimal("7.00"),
            status=SessionStatus.PAID,
            paypal_order_id="ORDER-1",
            paypal_capture_id="CAP-1",
            **fields,
        )
        db.add(s)
        db.commit()
        return s


def provision(db_session_factory, render_fake, paypal_fake, session_id):
    run_provisioning(
        session_id,
        session_factory=db_session_factory,
        render_factory=render_fake.client,
        paypal_factory=paypal_fake.client,
        poll_interval=0,
    )


def test_successful_deploy_goes_live_with_countdown(client, live_listing, db_session_factory, render_fake, paypal_fake):
    s = paid_session(db_session_factory, live_listing)
    provision(db_session_factory, render_fake, paypal_fake, s.id)

    body = render_fake.created[0]
    assert body["name"].startswith("ptp-demo-app") and body["serviceDetails"]["plan"] == "free"
    assert body["serviceDetails"]["envSpecificDetails"]["startCommand"] == "gunicorn app:app --bind 0.0.0.0:$PORT"
    assert body["envVars"] == [{"key": "GREETING", "value": "hi"}]
    with db_session_factory() as db:
        s = db.get(PreviewSession, s.id)
        assert s.status == "live" and s.live_url.endswith(".onrender.com")
        assert s.expires_at - s.live_at == timedelta(hours=2)
        kinds = [e.kind for e in s.events]
        assert kinds[:2] == ["deploying", "service_created"] and kinds[-1] == "live"
        assert "deploy_status" in kinds


def test_failed_deploy_refunds_and_deletes(client, live_listing, db_session_factory, paypal_fake):
    render_fake = FakeRender(deploy_statuses=("build_in_progress", "update_failed"))
    s = paid_session(db_session_factory, live_listing)
    provision(db_session_factory, render_fake, paypal_fake, s.id)

    assert ("POST", "/v2/payments/captures/CAP-1/refund", "refund-CAP-1") in paypal_fake.calls
    assert render_fake.deleted == ["srv-1"]
    with db_session_factory() as db:
        s = db.get(PreviewSession, s.id)
        assert s.status == "failed_refunded" and s.paypal_refund_id == "REF-1" and s.ended_at
        failed = next(e for e in s.events if e.kind == "deploy_failed")
        assert "ModuleNotFoundError" in failed.detail["logs"][0]


def test_refund_failure_is_retried_by_the_reaper(client, live_listing, db_session_factory, paypal_fake, monkeypatch):
    import app.payments.paypal as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    render_fake = FakeRender(deploy_statuses=("build_failed",))
    paypal_fake.refund_fails = True
    s = paid_session(db_session_factory, live_listing)
    provision(db_session_factory, render_fake, paypal_fake, s.id)
    with db_session_factory() as db:
        stuck = db.get(PreviewSession, s.id)
        assert stuck.status == "healing" and stuck.failure_reason  # money not returned yet: keep trying

    paypal_fake.refund_fails = False
    later = datetime.now(UTC) + timedelta(minutes=10)
    summary = reaper.reap(
        session_factory=db_session_factory,
        render_factory=render_fake.client,
        paypal_factory=paypal_fake.client,
        resume=lambda _id: False,
        now=later,
    )
    assert summary["refund_retries"] == 1
    with db_session_factory() as db:
        assert db.get(PreviewSession, s.id).status == "failed_refunded"


def test_provisioning_is_skipped_for_sessions_not_paid(
    client, live_listing, db_session_factory, render_fake, paypal_fake
):
    s = paid_session(db_session_factory, live_listing)
    with db_session_factory() as db:
        db.get(PreviewSession, s.id).status = SessionStatus.LIVE
        db.commit()
    provision(db_session_factory, render_fake, paypal_fake, s.id)
    assert render_fake.created == []


def test_preview_names_use_prefix_and_fit(client, live_listing, db_session_factory):
    s = paid_session(db_session_factory, live_listing)
    with db_session_factory() as db:
        name = preview_name(db.get(PreviewSession, s.id))
    assert name.startswith("ptp-demo-app-1a2b-") and len(name) <= 40


# ---------------------------------------------------------------- reaper


def run_reaper(db_session_factory, render_fake, paypal_fake, now, resumed=None):
    return reaper.reap(
        session_factory=db_session_factory,
        render_factory=render_fake.client,
        paypal_factory=paypal_fake.client,
        resume=lambda sid: (resumed.append(sid) if resumed is not None else None) or True,
        now=now,
    )


def test_reaper_expires_live_sessions_and_deletes_services(
    client, live_listing, db_session_factory, render_fake, paypal_fake
):
    now = datetime.now(UTC)
    s = paid_session(
        db_session_factory,
        live_listing,
        render_service_id="srv-live",
        live_at=now - timedelta(hours=2),
        expires_at=now - timedelta(minutes=1),
    )
    with db_session_factory() as db:
        db.get(PreviewSession, s.id).status = SessionStatus.LIVE
        db.commit()
    summary = run_reaper(db_session_factory, render_fake, paypal_fake, now)
    assert summary["expired"] == 1 and render_fake.deleted == ["srv-live"]
    with db_session_factory() as db:
        assert db.get(PreviewSession, s.id).status == "expired"


def test_reaper_deletes_only_old_orphans_with_our_prefix(
    client, live_listing, db_session_factory, render_fake, paypal_fake
):
    now = datetime.now(UTC)
    old, young = (now - timedelta(hours=1)).isoformat(), (now - timedelta(minutes=5)).isoformat()
    s = paid_session(db_session_factory, live_listing, render_service_id="srv-active")
    render_fake.services = [
        {"id": "srv-orphan", "name": "ptp-old-abc", "type": "web_service", "createdAt": old},
        {"id": "srv-young", "name": "ptp-new-abc", "type": "web_service", "createdAt": young},
        {"id": "srv-active", "name": "ptp-demo-abc", "type": "web_service", "createdAt": old},
        {"id": "srv-main", "name": "paytoprovision", "type": "web_service", "createdAt": old},
    ]
    summary = run_reaper(db_session_factory, render_fake, paypal_fake, now)
    assert summary["orphans_deleted"] == 1 and render_fake.deleted == ["srv-orphan"]
    assert s  # active session's service kept


def test_reaper_refuses_to_sweep_with_unsafe_prefix(client, db_session_factory, render_fake, paypal_fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "preview_name_prefix", "")
    now = datetime.now(UTC)
    render_fake.services = [
        {
            "id": "srv-main",
            "name": "paytoprovision",
            "type": "web_service",
            "createdAt": (now - timedelta(days=1)).isoformat(),
        }
    ]
    run_reaper(db_session_factory, render_fake, paypal_fake, now)
    assert render_fake.deleted == []


def test_reaper_expires_abandoned_checkouts_and_resumes_stuck_ones(
    client, live_listing, db_session_factory, render_fake, paypal_fake
):
    with db_session_factory() as db:
        abandoned = PreviewSession(
            listing_id=live_listing.id, hours=1, amount=Decimal("1"), status=SessionStatus.PENDING_PAYMENT
        )
        db.add(abandoned)
        db.commit()
    stuck = paid_session(db_session_factory, live_listing)
    resumed = []
    summary = run_reaper(db_session_factory, render_fake, paypal_fake, datetime.now(UTC) + timedelta(hours=2), resumed)
    assert summary["abandoned"] == 1 and resumed == [stuck.id]


def test_internal_reap_requires_token(client, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "internal_reap_token", SecretStr("s3cret-token"))
    monkeypatch.setattr(reaper, "reap", lambda: {"expired": 0})
    assert client.post("/internal/reap").status_code == 401
    assert client.post("/internal/reap", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.post("/internal/reap", headers={"Authorization": "Bearer s3cret-token"})
    assert ok.status_code == 200 and ok.json() == {"expired": 0}
    assert TestClient(app)  # app still importable/usable after monkeypatching
