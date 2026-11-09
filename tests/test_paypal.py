from decimal import Decimal

import httpx
import pytest

from app.payments.paypal import PayPalClient, PayPalError, approve_url, capture_details
from tests.fakes import FakePayPal


def order_kwargs(**overrides):
    kwargs = dict(
        amount=Decimal("7"),
        currency="USD",
        custom_id="sess-1",
        description="Demo, 2h",
        return_url="https://app/checkout/return",
        cancel_url="https://app/l/x",
        request_id="order-sess-1",
    )
    kwargs.update(overrides)
    return kwargs


def test_token_is_fetched_once_and_reused():
    fake = FakePayPal()
    client = fake.client()
    client.get_order("O-1")
    client.get_order("O-2")
    assert [c for c in fake.calls if c[1] == "/v1/oauth2/token"] == [("POST", "/v1/oauth2/token", None)]


def test_button_orders_are_plain_and_redirect_orders_carry_return_urls():
    fake = FakePayPal()
    client = fake.client()
    client.create_order(**order_kwargs())
    client.create_order(**order_kwargs(redirect=True))
    plain, redirect = fake.bodies
    assert plain["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "7.00"}
    assert plain["purchase_units"][0]["custom_id"] == "sess-1"
    assert plain["application_context"]["shipping_preference"] == "NO_SHIPPING"
    assert "payment_source" not in plain
    ctx = redirect["payment_source"]["paypal"]["experience_context"]
    assert ctx["return_url"] == "https://app/checkout/return" and ctx["user_action"] == "PAY_NOW"
    assert fake.calls[-1][2] == "order-sess-1"  # PayPal-Request-Id


def test_capture_is_idempotent_per_order():
    fake = FakePayPal()
    order = fake.client().capture_order("O-9")
    assert ("POST", "/v2/checkout/orders/O-9/capture", "capture-O-9") in fake.calls
    assert capture_details(order) == ("CAP-O-9", "COMPLETED", "buyer@example.com")


def test_already_captured_returns_the_order():
    fake = FakePayPal()
    fake.capture_error = "ORDER_ALREADY_CAPTURED"
    order = fake.client().capture_order("O-9")
    assert order["status"] == "COMPLETED" and fake.calls[-1][0] == "GET"


def test_declined_capture_raises_with_issue():
    fake = FakePayPal()
    fake.capture_error = "INSTRUMENT_DECLINED"
    with pytest.raises(PayPalError) as exc:
        fake.client().capture_order("O-9")
    assert exc.value.issue == "INSTRUMENT_DECLINED" and exc.value.status_code == 422


def test_refund_uses_idempotency_key_and_retries_server_errors(monkeypatch):
    import app.payments.paypal as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)  # don't actually wait between retries
    attempts = []

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        attempts.append(request.headers["PayPal-Request-Id"])
        if len(attempts) == 1:
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"})
        return httpx.Response(201, json={"id": "REF-1", "status": "COMPLETED"})

    client = PayPalClient("id", "secret", transport=httpx.MockTransport(handler))
    assert client.refund_capture("CAP-1", note="sorry")["id"] == "REF-1"
    assert attempts == ["refund-CAP-1", "refund-CAP-1"]


def test_401_refreshes_token_once():
    tokens = iter(["old", "new"])
    seen = []

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
        return httpx.Response(200, json={"id": "O-1"})

    client = PayPalClient("id", "secret", transport=httpx.MockTransport(handler))
    assert client.get_order("O-1") == {"id": "O-1"}
    assert seen == ["Bearer old", "Bearer new"]


def test_verify_webhook_needs_all_transmission_headers():
    fake = FakePayPal()
    client = fake.client()
    headers = {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert",
        "PAYPAL-TRANSMISSION-ID": "abc123",
        "PAYPAL-TRANSMISSION-SIG": "sig",
        "PAYPAL-TRANSMISSION-TIME": "2026-11-06T10:00:00Z",
    }
    assert client.verify_webhook(headers, {"id": "WH-1"}, "WH-ID") is True
    assert fake.bodies[-1]["transmission_id"] == "abc123" and fake.bodies[-1]["webhook_id"] == "WH-ID"
    assert (
        client.verify_webhook({k: v for k, v in headers.items() if k != "PAYPAL-TRANSMISSION-SIG"}, {}, "WH") is False
    )
    fake.verify_ok = False
    assert client.verify_webhook(headers, {"id": "WH-1"}, "WH-ID") is False


def test_approve_url_handles_both_link_kinds():
    assert approve_url({"links": [{"rel": "approve", "href": "a"}]}) == "a"
    assert approve_url({"links": [{"rel": "payer-action", "href": "b"}]}) == "b"
    assert approve_url({"links": []}) is None


def test_missing_credentials_is_an_error():
    with pytest.raises(PayPalError):
        PayPalClient("", "")
