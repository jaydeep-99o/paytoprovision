"""Shared Jinja2 environment and template filters."""

from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def money(value: Decimal | float | int | None, currency: str = "USD") -> str:
    if value is None:
        return ""
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{Decimal(value):,.2f}"


templates.env.filters["money"] = money
