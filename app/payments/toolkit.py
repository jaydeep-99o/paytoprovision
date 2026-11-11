"""PayPal Agent Toolkit adapter for the checkout agent.

We use the toolkit's framework-agnostic core (`PayPalAPI.run`) for its `create_order` and
`get_order_details` tools. Its framework adapters (OpenAI Agents SDK, CrewAI, LangChain)
pull in heavy dependencies that don't fit a 512 MB instance, so the package is installed
with `--no-deps` and the tool-calling loop is our own (app/ai/agent.py).
"""

import json
import logging
from decimal import Decimal
from typing import Any, Protocol

from app.config import get_settings

log = logging.getLogger(__name__)


class ToolkitAPI(Protocol):
    def run(self, method: str, params: dict) -> str: ...


def make_toolkit_api() -> ToolkitAPI | None:
    """The toolkit's PayPalAPI in sandbox mode, or None if it isn't installed/configured."""
    s = get_settings()
    if not (s.paypal_client_id and s.paypal_client_secret.get_secret_value()):
        return None
    try:
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context
    except ImportError:
        log.warning("paypal-agent-toolkit not installed; the checkout agent can't create orders")
        return None
    return PayPalAPI(
        client_id=s.paypal_client_id,
        secret=s.paypal_client_secret.get_secret_value(),
        context=Context(sandbox="sandbox" in s.paypal_base_url, source="PAYTOPROVISION"),
    )


def toolkit_create_order(
    api: ToolkitAPI, *, name: str, price_per_hour: Decimal, hours: int, return_url: str, cancel_url: str
) -> dict[str, Any]:
    """Create the order with the toolkit's `create_order` tool. Returns the PayPal order."""
    params = {
        "currency_code": "USD",
        "items": [
            {
                "name": name[:127],
                "description": f"{hours}h private live demo",
                "item_cost": float(price_per_hour),
                "quantity": hours,
                "item_total": float(price_per_hour * hours),
            }
        ],
        "return_url": return_url,
        "cancel_url": cancel_url,
    }
    return json.loads(api.run("create_order", params))


def toolkit_order_status(api: ToolkitAPI, order_id: str) -> str:
    """Order status via the toolkit's `get_order_details` tool."""
    return json.loads(api.run("get_order_details", {"order_id": order_id})).get("status", "UNKNOWN")
