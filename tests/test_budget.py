import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app import budget
from app.ai.llm import LLMClient
from app.config import get_settings
from app.main import app
from app.models import PreviewSession, SessionStatus
from app.provisioner import run_provisioning
from app.routes.deps import get_llm, get_toolkit
from tests.fakes import GOOD_ANALYSIS, FakeRender, llm_handler


def test_take_until_the_daily_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_daily_call_limit", 3)
    for _ in range(3):
        budget.take(budget.AI)
    assert budget.remaining(budget.AI) == 0
    with pytest.raises(budget.BudgetExceeded):
        budget.take(budget.AI)
    assert budget.used(budget.AI) == 3  # a refused call doesn't count


def test_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(get_settings(), "deploy_daily_limit", 0)
    for _ in range(5):
        budget.take(budget.DEPLOY)
    assert budget.used(budget.DEPLOY) == 0


def budgeted_llm(seen):
    """An LLM client wired to the daily AI budget, like LLMClient.from_settings does."""
    return LLMClient(
        "nb",
        model="m",
        transport=httpx.MockTransport(llm_handler([json.dumps(GOOD_ANALYSIS)], seen)),
        on_call=lambda: budget.take(budget.AI),
    )


def test_exhausted_ai_budget_blocks_analysis_without_calling_the_ai(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_daily_call_limit", 1)
    seen = []
    app.dependency_overrides[get_llm] = lambda: budgeted_llm(seen)
    first = client.post(
        "/owner/analyze", data={"repo_url": "https://github.com/acme/demo"}, headers={"HX-Request": "true"}
    )
    assert "HX-Redirect" in first.headers
    second = client.post(
        "/owner/analyze", data={"repo_url": "https://github.com/acme/demo"}, headers={"HX-Request": "true"}
    )
    assert "HX-Redirect" not in second.headers and "AI budget for today is used up" in second.text
    assert len(seen) == 1  # the second request never reached Nebius


def test_exhausted_ai_budget_stops_the_agent(client, live_listing, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_daily_call_limit", 1)
    budget.take(budget.AI)
    seen = []
    app.dependency_overrides[get_llm] = lambda: budgeted_llm(seen)
    app.dependency_overrides[get_toolkit] = lambda: object()
    resp = client.post("/api/agent/demo-app-1a2b/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 429 and "PayPal button" in resp.json()["error"] and seen == []


def test_no_new_orders_once_deploy_budget_is_spent(client, live_listing, paypal_fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "deploy_daily_limit", 1)
    budget.take(budget.DEPLOY)
    resp = client.post("/api/checkout/demo-app-1a2b/orders", json={"hours": 1})
    assert resp.status_code == 409 and resp.json()["issue"] == "DAILY_LIMIT"
    assert paypal_fake.orders == 0  # PayPal never asked: the buyer is never charged


def test_deploy_budget_race_refunds_instead_of_deploying(
    client, live_listing, db_session_factory, paypal_fake, monkeypatch
):
    monkeypatch.setattr(get_settings(), "deploy_daily_limit", 1)
    budget.take(budget.DEPLOY)  # the last deploy slot went to someone else after this buyer paid
    with db_session_factory() as db:
        s = PreviewSession(
            listing_id=live_listing.id,
            hours=1,
            amount=Decimal("3.50"),
            status=SessionStatus.PAID,
            paypal_order_id="ORDER-1",
            paypal_capture_id="CAP-1",
        )
        db.add(s)
        db.commit()
    render = FakeRender()
    run_provisioning(
        s.id,
        session_factory=db_session_factory,
        render_factory=render.client,
        paypal_factory=paypal_fake.client,
        llm_factory=lambda: None,
        poll_interval=0,
    )
    assert render.created == []
    with db_session_factory() as db:
        assert db.scalar(select(PreviewSession)).status == "failed_refunded"


def test_order_creation_is_rate_limited_per_visitor(client, live_listing, monkeypatch):
    from app.routes import checkout as routes

    monkeypatch.setattr(routes.order_limiter, "limit", 2)
    codes = [client.post("/api/checkout/demo-app-1a2b/orders", json={"hours": 1}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_security_headers(client):
    headers = client.get("/healthz").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
