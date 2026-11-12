import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models import PreviewSession, SessionStatus

HTML = {"accept": "text/html"}


def create(client, hours=2, slug="demo-app-1a2b"):
    return client.post(f"/api/checkout/{slug}/orders", json={"hours": hours})


def test_create_order_makes_pending_session_with_quote(client, live_listing, db_session_factory, paypal_fake):
    resp = create(client, hours=2)
    assert resp.status_code == 200 and resp.json()["id"] == "ORDER-1"
    with db_session_factory() as db:
        s = db.scalar(select(PreviewSession))
        assert s.status == "pending_payment" and s.amount == Decimal("7.00") and s.paypal_order_id == "ORDER-1"
    assert paypal_fake.bodies[0]["purchase_units"][0]["custom_id"] == str(s.id)


def test_capture_marks_paid_and_starts_provisioning(client, live_listing, db_session_factory, submitted):
    order_id = create(client).json()["id"]
    resp = client.post(f"/api/checkout/orders/{order_id}/capture")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "paid" and body["redirect"].startswith("/s/")
    with db_session_factory() as db:
        s = db.scalar(select(PreviewSession))
        assert s.paypal_capture_id == f"CAP-{order_id}" and s.buyer_email == "buyer@example.com" and s.paid_at
        assert [e.kind for e in s.events] == ["checkout", "paid"]
    assert submitted == [s.id]


def test_second_capture_is_a_no_op(client, live_listing, paypal_fake, submitted):
    order_id = create(client).json()["id"]
    client.post(f"/api/checkout/orders/{order_id}/capture")
    with_capture = len([c for c in paypal_fake.calls if c[1].endswith("/capture")])
    client.post(f"/api/checkout/orders/{order_id}/capture")
    assert len([c for c in paypal_fake.calls if c[1].endswith("/capture")]) == with_capture == 1


def test_declined_card_asks_buyer_to_retry(client, live_listing, paypal_fake, submitted):
    order_id = create(client).json()["id"]
    paypal_fake.capture_error = "INSTRUMENT_DECLINED"
    resp = client.post(f"/api/checkout/orders/{order_id}/capture")
    assert resp.status_code == 422 and resp.json()["issue"] == "INSTRUMENT_DECLINED"
    assert submitted == []


def test_hours_are_bounded(client, live_listing):
    assert create(client, hours=3).status_code == 400
    assert create(client, hours=0).status_code == 400


def test_capacity_is_enforced_before_taking_money(client, live_listing, db_session_factory, paypal_fake):
    with db_session_factory() as db:
        for _ in range(3):
            db.add(PreviewSession(listing_id=live_listing.id, hours=1, amount=Decimal("1"), status=SessionStatus.LIVE))
        db.commit()
    resp = create(client)
    assert resp.status_code == 409 and "not been charged" in resp.json()["error"]
    assert paypal_fake.orders == 0


def test_unpublished_listing_cannot_be_bought(client, live_listing, db_session_factory):
    from app.models import Listing

    with db_session_factory() as db:
        db.get(Listing, live_listing.id).approved = False
        db.commit()
    assert create(client).status_code == 404


def test_return_url_captures_and_redirects(client, live_listing, submitted):
    order_id = create(client).json()["id"]
    resp = client.get(f"/checkout/return?token={order_id}&PayerID=ABC", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/s/")
    assert len(submitted) == 1


def test_return_url_capture_failure_shows_session_not_500(client, live_listing, paypal_fake, submitted):
    order_id = create(client).json()["id"]
    paypal_fake.capture_error = "SOMETHING_UNEXPECTED"
    resp = client.get(f"/checkout/return?token={order_id}&PayerID=ABC", follow_redirects=False)
    assert resp.status_code == 303 and "error=" in resp.headers["location"]
    assert submitted == []


def test_session_page_and_status_fragment(client, live_listing):
    order_id = create(client).json()["id"]
    redirect = client.post(f"/api/checkout/orders/{order_id}/capture").json()["redirect"]
    page = client.get(redirect, headers=HTML)
    assert page.status_code == 200 and "Starting your environment" in page.text and "Payment of $7.00" in page.text
    fragment = client.get(redirect + "/status")
    assert 'hx-trigger="every 3s"' in fragment.text and "<html" not in fragment.text


def test_unknown_session_is_404(client):
    assert client.get(f"/s/{uuid.uuid4()}", headers=HTML).status_code == 404
