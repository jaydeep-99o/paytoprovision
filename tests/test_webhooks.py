from sqlalchemy import func, select

from app.config import get_settings
from app.models import PreviewSession, WebhookEvent

SIGNED = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "PAYPAL-TRANSMISSION-ID": "tx123",
    "PAYPAL-TRANSMISSION-SIG": "sig",
    "PAYPAL-TRANSMISSION-TIME": "2026-11-06T10:00:00Z",
}


def approved_event(order_id, event_id="WH-EVT-1"):
    return {"id": event_id, "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": order_id, "status": "APPROVED"}}


def new_order(client):
    return client.post("/api/checkout/demo-app-1a2b/orders", json={"hours": 1}).json()["id"]


def test_order_approved_webhook_captures_when_the_browser_never_did(
    client, live_listing, submitted, db_session_factory
):
    order_id = new_order(client)
    resp = client.post("/webhooks/paypal", json=approved_event(order_id), headers=SIGNED)
    assert resp.status_code == 200 and "paid" in resp.json()["outcome"]
    with db_session_factory() as db:
        assert db.scalar(select(PreviewSession)).status == "paid"
    assert len(submitted) == 1


def test_duplicate_delivery_is_processed_once(client, live_listing, paypal_fake, submitted, db_session_factory):
    order_id = new_order(client)
    event = approved_event(order_id)
    assert client.post("/webhooks/paypal", json=event, headers=SIGNED).json()["status"] == "ok"
    assert client.post("/webhooks/paypal", json=event, headers=SIGNED).json()["status"] == "duplicate"
    assert len([c for c in paypal_fake.calls if c[1].endswith("/capture")]) == 1
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1


def test_bad_signature_is_rejected(client, live_listing, paypal_fake, db_session_factory):
    paypal_fake.verify_ok = False
    resp = client.post("/webhooks/paypal", json=approved_event("ORDER-X"), headers=SIGNED)
    assert resp.status_code == 400
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_missing_signature_headers_are_rejected(client, live_listing):
    assert client.post("/webhooks/paypal", json=approved_event("ORDER-X")).status_code == 400


def test_unconfigured_webhook_id_returns_503(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "paypal_webhook_id", "")
    assert client.post("/webhooks/paypal", json=approved_event("O"), headers=SIGNED).status_code == 503


def test_capture_completed_marks_pending_capture_as_paid(
    client, live_listing, paypal_fake, submitted, db_session_factory
):
    order_id = new_order(client)
    paypal_fake.capture_status = "PENDING"
    client.post(f"/api/checkout/orders/{order_id}/capture")
    assert submitted == []
    event = {
        "id": "WH-EVT-2",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": f"CAP-{order_id}",
            "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }
    assert client.post("/webhooks/paypal", json=event, headers=SIGNED).json()["outcome"] == "marked paid"
    with db_session_factory() as db:
        assert db.scalar(select(PreviewSession)).status == "paid"
    assert len(submitted) == 1


def test_unknown_event_types_are_acknowledged(client):
    event = {"id": "WH-EVT-3", "event_type": "BILLING.PLAN.CREATED", "resource": {}}
    assert client.post("/webhooks/paypal", json=event, headers=SIGNED).json()["outcome"] == "ignored"
