import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import reaper
from app.config import get_settings
from app.db import Base, get_db, make_engine
from app.main import app
from app.models import Listing, ListingStatus
from app.routes import owner
from app.routes.deps import get_github, get_llm, get_paypal, get_provision_submitter
from tests.fakes import FakePayPal, FakeRender, fake_github, fake_llm

# Never let tests start real background reaper runs against Render.
reaper.AUTO_ENABLED = False


@pytest.fixture(autouse=True)
def no_real_ai(monkeypatch):
    """Tests that don't pass an llm_factory get "AI not configured", never the real Nebius API."""
    from app import provisioner

    monkeypatch.setattr(provisioner, "_llm_or_none", lambda: None)


@pytest.fixture
def db_session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def paypal_fake():
    return FakePayPal()


@pytest.fixture
def render_fake():
    return FakeRender()


@pytest.fixture
def submitted():
    """Session IDs handed to background provisioning (stubbed out in route tests)."""
    return []


@pytest.fixture
def client(db_session_factory, paypal_fake, submitted, monkeypatch):
    """App client with a fresh DB and fake GitHub/LLM/PayPal. Allowlists acme/demo."""

    def _db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    def _submit(session_id):
        submitted.append(session_id)
        return True

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_github] = lambda: fake_github()
    app.dependency_overrides[get_llm] = lambda: fake_llm()
    app.dependency_overrides[get_paypal] = paypal_fake.client
    app.dependency_overrides[get_provision_submitter] = lambda: _submit
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_repos", "acme/demo")
    monkeypatch.setattr(settings, "paypal_webhook_id", "WH-TEST")
    monkeypatch.setattr(settings, "max_concurrent_previews", 3)
    monkeypatch.setattr(settings, "preview_name_prefix", "ptp-")
    owner.analyze_limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def live_listing(db_session_factory) -> Listing:
    with db_session_factory() as db:
        listing = Listing(
            id=uuid.uuid4(),
            slug="demo-app-1a2b",
            repo_url="https://github.com/acme/demo",
            repo_full_name="acme/demo",
            branch="main",
            title="Demo App",
            summary="Says hello.",
            owner_paypal_email="owner@example.com",
            price_per_hour=Decimal("3.50"),
            status=ListingStatus.PUBLISHED,
            approved=True,
            deploy_config={
                "runtime": "python",
                "build_command": "pip install -r requirements.txt",
                "start_command": "gunicorn app:app --bind 0.0.0.0:$PORT",
                "health_path": "/",
                "env_vars": {"GREETING": "hi"},
            },
        )
        db.add(listing)
        db.commit()
        return listing
