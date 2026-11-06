import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base, get_db, make_engine
from app.main import app
from app.routes import owner
from app.routes.deps import get_github, get_llm
from tests.fakes import fake_github, fake_llm


@pytest.fixture
def db_session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def client(db_session_factory, monkeypatch):
    """App client with a fresh DB and fake GitHub/LLM. Allowlists acme/demo."""

    def _db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_github] = lambda: fake_github()
    app.dependency_overrides[get_llm] = lambda: fake_llm()
    monkeypatch.setattr(get_settings(), "allowed_repos", "acme/demo")
    owner.analyze_limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
