"""Create (or find) the PayPal sandbox webhook for this app and print its ID.

    python -m scripts.paypal_webhook https://paytoprovision.onrender.com

Put the printed ID in PAYPAL_WEBHOOK_ID (on Render, and in .env if you test webhooks locally
through a tunnel). Re-running is safe: an existing webhook for the same URL is reused.
"""

import sys

from app.payments.paypal import WEBHOOK_EVENTS, PayPalClient


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        print(__doc__)
        return 2
    url = sys.argv[1].rstrip("/") + "/webhooks/paypal"
    with PayPalClient.from_settings() as paypal:
        existing = next((w for w in paypal.list_webhooks() if w.get("url") == url), None)
        if existing:
            print(f"Webhook already exists for {url}")
            webhook = existing
        else:
            webhook = paypal.create_webhook(url, WEBHOOK_EVENTS)
            print(f"Created webhook for {url}")
    print("Events:", ", ".join(e["name"] for e in webhook.get("event_types", [])))
    print(f"PAYPAL_WEBHOOK_ID={webhook['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
