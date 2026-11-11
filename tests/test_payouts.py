from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app import reaper
from app.models import Payout, PreviewSession, SessionStatus
from app.payouts import split
from tests.test_webhooks import SIGNED

HTML = {"accept": "text/html"}


def ended_session(db_session_factory, listing, **fields):
    now = datetime.now(UTC)
    values = dict(
        listing_id=listing.id,
        hours=2,
        amount=Decimal("7.00"),
        status=SessionStatus.LIVE,
        paypal_order_id="ORDER-1",
        paypal_capture_id="CAP-1",
        render_service_id="srv-live",
        paid_at=now - timedelta(hours=2),
        live_at=now - timedelta(hours=2),
        expires_at=now - timedelta(minutes=1),
    )
    values.update(fields)
    with db_session_factory() as db:
        s = PreviewSession(**values)
        db.add(s)
        db.commit()
        return s


def run_reaper(db_session_factory, render_fake, paypal_fake):
    return reaper.reap(
        session_factory=db_session_factory,
        render_factory=render_fake.client,
        paypal_factory=paypal_fake.client,
        resume=lambda _id: False,
        now=datetime.now(UTC),
    )


def test_fee_split():
    assert split(Decimal("7.00")) == (Decimal("1.40"), Decimal("5.60"))
    assert split(Decimal("2.50")) == (Decimal("0.50"), Decimal("2.00"))


def test_owner_is_paid_80_percent_once_when_session_ends(
    client, live_listing, db_session_factory, render_fake, paypal_fake
):
    s = ended_session(db_session_factory, live_listing)
    summary = run_reaper(db_session_factory, render_fake, paypal_fake)
    assert summary["expired"] == 1 and summary["payouts"] == 1

    body = next(b for b in paypal_fake.bodies if "sender_batch_header" in b)
    item = body["items"][0]
    assert item["receiver"] == "owner@example.com" and item["amount"] == {"value": "5.60", "currency": "USD"}
    assert body["sender_batch_header"]["sender_batch_id"] == f"ptp-{s.id.hex}"

    run_reaper(db_session_factory, render_fake, paypal_fake)  # second run: polls status, never pays again
    assert len([c for c in paypal_fake.calls if c[0] == "POST" and c[1] == "/v1/payments/payouts"]) == 1
    with db_session_factory() as db:
        payout = db.scalar(select(Payout))
        assert (payout.gross_amount, payout.fee_amount, payout.net_amount) == (
            Decimal("7.00"),
            Decimal("1.40"),
            Decimal("5.60"),
        )
        assert payout.status == "SUCCESS" and payout.paypal_item_id == "ITEM-1"
        assert db.get(PreviewSession, s.id).events[-1].kind == "payout"


def test_refunded_or_never_live_sessions_are_not_paid_out(
    client, live_listing, db_session_factory, render_fake, paypal_fake
):
    ended_session(
        db_session_factory, live_listing, status=SessionStatus.FAILED_REFUNDED, paypal_refund_id="REF-1", live_at=None
    )
    ended_session(
        db_session_factory, live_listing, status=SessionStatus.EXPIRED, live_at=None, paypal_order_id="ORDER-2"
    )
    run_reaper(db_session_factory, render_fake, paypal_fake)
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Payout)) == 0


def test_payout_webhook_marks_success(client, live_listing, db_session_factory, render_fake, paypal_fake):
    ended_session(db_session_factory, live_listing)
    run_reaper(db_session_factory, render_fake, paypal_fake)
    event = {
        "id": "WH-PAYOUT-1",
        "event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
        "resource": {"batch_header": {"payout_batch_id": "BATCH-1", "batch_status": "SUCCESS"}},
    }
    assert client.post("/webhooks/paypal", json=event, headers=SIGNED).json()["outcome"] == "payout SUCCESS"


def test_dashboard_shows_earnings(client, db_session_factory, render_fake, paypal_fake):
    url = client.post(
        "/owner/analyze", data={"repo_url": "https://github.com/acme/demo"}, headers={"HX-Request": "true"}
    ).headers["HX-Redirect"]
    client.post(
        url,
        data={
            "action": "publish",
            "title": "Demo App",
            "summary": "x",
            "price_per_hour": "3.50",
            "owner_paypal_email": "owner@example.com",
            "runtime": "python",
            "build_command": "pip install -r requirements.txt",
            "start_command": "gunicorn app:app",
            "health_path": "/",
        },
    )
    from app.models import Listing

    with db_session_factory() as db:
        listing = db.scalar(select(Listing))
    ended_session(db_session_factory, listing)
    run_reaper(db_session_factory, render_fake, paypal_fake)
    run_reaper(db_session_factory, render_fake, paypal_fake)  # payout -> SUCCESS

    page = client.get("/owner/dashboard", headers=HTML)
    assert page.status_code == 200
    assert "Owner dashboard" in page.text and "$5.60 paid" in page.text and "Demo App" in page.text
