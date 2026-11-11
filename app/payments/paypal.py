"""PayPal REST client (sandbox): Orders v2, refunds, webhook verification and setup.

Every money-moving call sends a deterministic `PayPal-Request-Id`, so retries (ours,
or a webhook racing the browser) never capture or refund twice. The OAuth token is
cached until shortly before it expires. Credentials never appear in logs or errors.
"""

import logging
import threading
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

# Webhook events PayToProvision subscribes to.
WEBHOOK_EVENTS = [
    "CHECKOUT.ORDER.APPROVED",
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.DENIED",
    "PAYMENT.CAPTURE.REFUNDED",
    "PAYMENT.PAYOUTSBATCH.SUCCESS",
    "PAYMENT.PAYOUTSBATCH.DENIED",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
    "PAYMENT.PAYOUTS-ITEM.FAILED",
]


class PayPalError(Exception):
    def __init__(self, status_code: int, name: str, message: str, *, issue: str | None = None, debug_id: str = ""):
        self.status_code = status_code
        self.name = name
        self.issue = issue
        self.debug_id = debug_id
        super().__init__(f"PayPal {status_code} {name}: {issue or message} (debug_id={debug_id})")


def approve_url(order: dict[str, Any]) -> str | None:
    """The link the buyer opens to approve: `approve` for plain orders, `payer-action` when
    the order carries a payment_source experience context."""
    for link in order.get("links", []):
        if link.get("rel") in ("approve", "payer-action"):
            return link.get("href")
    return None


def capture_details(order: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(capture_id, capture_status, payer_email) from a captured order."""
    payer_email = (order.get("payer") or {}).get("email_address")
    for unit in order.get("purchase_units", []):
        for capture in (unit.get("payments") or {}).get("captures", []):
            return capture.get("id"), capture.get("status"), payer_email
    return None, None, payer_email


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = "https://api-m.sandbox.paypal.com",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not (client_id and client_secret):
            raise PayPalError(0, "CONFIG", "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set")
        self._auth = (client_id, client_secret)
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> "PayPalClient":
        s = settings or get_settings()
        return cls(s.paypal_client_id, s.paypal_client_secret.get_secret_value(), base_url=s.paypal_base_url, **kwargs)

    def __repr__(self) -> str:
        return f"PayPalClient(base_url='{self._http.base_url}')"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PayPalClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- transport

    def _access_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            if force or not self._token or time.monotonic() >= self._token_expires:
                resp = self._http.post("/v1/oauth2/token", auth=self._auth, data={"grant_type": "client_credentials"})
                if resp.status_code != 200:
                    raise PayPalError(resp.status_code, "AUTHENTICATION_FAILURE", "could not get an access token")
                data = resp.json()
                self._token = data["access_token"]
                self._token_expires = time.monotonic() + max(int(data.get("expires_in", 3600)) - 120, 60)
            return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        request_id: str | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        """Retries 5xx/429/network errors only for GETs or calls carrying a PayPal-Request-Id
        (which PayPal deduplicates). Refreshes the token once on 401."""
        safe_to_retry = method == "GET" or request_id is not None
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self._access_token()}", "Prefer": "return=representation"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            try:
                resp = self._http.request(method, path, json=json, headers=headers)
            except httpx.TransportError as exc:
                if safe_to_retry and attempt <= retries:
                    time.sleep(attempt)
                    continue
                raise PayPalError(0, "NETWORK", type(exc).__name__) from exc
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._access_token(force=True)
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and safe_to_retry and attempt <= retries:
                time.sleep(attempt * 2)
                continue
            if resp.status_code >= 400:
                raise self._error(resp)
            return resp.json() if resp.content else {}

    @staticmethod
    def _error(resp: httpx.Response) -> PayPalError:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        details = body.get("details") or [{}]
        return PayPalError(
            resp.status_code,
            body.get("name", "ERROR"),
            body.get("message", resp.text[:200]),
            issue=details[0].get("issue"),
            debug_id=body.get("debug_id", ""),
        )

    # ---------------------------------------------------------------- orders

    def create_order(
        self,
        *,
        amount: Decimal,
        currency: str,
        custom_id: str,
        description: str,
        return_url: str,
        cancel_url: str,
        request_id: str,
        redirect: bool = False,
    ) -> dict[str, Any]:
        """Create a CAPTURE order.

        redirect=False: for the JS SDK buttons (popup). A plain order, so every funding
        source the buttons offer (PayPal, card) works.
        redirect=True: for approval links (e.g. sent by the checkout agent). The order carries
        a PayPal experience context with return/cancel URLs; approve via `approve_url`.
        """
        context = {"brand_name": "PayToProvision", "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW"}
        body: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": custom_id,
                    "custom_id": custom_id,
                    "description": description[:127],
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
        }
        if redirect:
            body["payment_source"] = {
                "paypal": {"experience_context": {**context, "return_url": return_url, "cancel_url": cancel_url}}
            }
        else:
            body["application_context"] = context
        return self._request("POST", "/v2/checkout/orders", json=body, request_id=request_id)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str) -> dict[str, Any]:
        """Capture an approved order. Safe to call twice: a repeat returns the captured order."""
        try:
            return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", request_id=f"capture-{order_id}")
        except PayPalError as exc:
            if exc.issue == "ORDER_ALREADY_CAPTURED":
                return self.get_order(order_id)
            raise

    def refund_capture(self, capture_id: str, *, note: str) -> dict[str, Any]:
        """Full refund of a capture. Idempotent per capture."""
        return self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json={"note_to_payer": note[:255]},
            request_id=f"refund-{capture_id}",
        )

    # ---------------------------------------------------------------- payouts

    def create_payout(
        self, *, sender_batch_id: str, receiver_email: str, amount: Decimal, currency: str, note: str
    ) -> dict[str, Any]:
        """Send money to one PayPal account (the repo owner). `sender_batch_id` must be unique;
        PayPal rejects a reused one, so a retry can never pay twice."""
        body = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": "You earned money from a PayToProvision demo",
                "email_message": note[:255],
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": receiver_email,
                    "amount": {"value": f"{amount:.2f}", "currency": currency},
                    "note": note[:255],
                    "sender_item_id": sender_batch_id,
                }
            ],
        }
        return self._request("POST", "/v1/payments/payouts", json=body, request_id=sender_batch_id)

    def get_payout(self, payout_batch_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/payments/payouts/{payout_batch_id}")

    # ---------------------------------------------------------------- webhooks

    def verify_webhook(self, headers: Mapping[str, str], event: dict[str, Any], webhook_id: str) -> bool:
        """Ask PayPal whether this delivery really came from PayPal for our webhook."""
        h = {k.lower(): v for k, v in headers.items()}
        required = ["paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig"]
        if not webhook_id or any(not h.get(k) for k in required + ["paypal-transmission-time"]):
            return False
        try:
            result = self._request(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                json={
                    "auth_algo": h["paypal-auth-algo"],
                    "cert_url": h["paypal-cert-url"],
                    "transmission_id": h["paypal-transmission-id"],
                    "transmission_sig": h["paypal-transmission-sig"],
                    "transmission_time": h["paypal-transmission-time"],
                    "webhook_id": webhook_id,
                    "webhook_event": event,
                },
            )
        except PayPalError as exc:
            log.warning("webhook verification call failed: %s", exc)
            return False
        return result.get("verification_status") == "SUCCESS"

    def list_webhooks(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/notifications/webhooks").get("webhooks", [])

    def create_webhook(self, url: str, event_types: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/notifications/webhooks",
            json={"url": url, "event_types": [{"name": name} for name in event_types]},
            request_id=f"webhook-{url}",
        )
