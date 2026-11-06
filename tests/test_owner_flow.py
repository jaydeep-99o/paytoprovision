from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Listing
from app.routes import owner
from app.routes.deps import get_github
from tests.fakes import fake_github

HTML = {"accept": "text/html"}


def analyze(client, url="https://github.com/acme/demo"):
    return client.post("/owner/analyze", data={"repo_url": url}, headers={"HX-Request": "true"})


def edit_url(resp) -> str:
    return resp.headers["HX-Redirect"]


def publish_form(**overrides):
    form = {
        "action": "publish",
        "title": "Demo App",
        "summary": "Says hello.",
        "price_per_hour": "3.50",
        "owner_name": "Ada",
        "owner_paypal_email": "owner@example.com",
        "runtime": "python",
        "build_command": "pip install -r requirements.txt",
        "start_command": "gunicorn app:app --bind 0.0.0.0:$PORT",
        "health_path": "/",
        "env_names": ["GREETING"],
        "env_values": ["hi"],
    }
    form.update(overrides)
    return form


def test_analyze_creates_draft_and_redirects_to_review(client, db_session_factory):
    resp = analyze(client)
    assert resp.status_code == 200 and edit_url(resp).startswith("/owner/listings/")

    page = client.get(edit_url(resp), headers=HTML)
    assert page.status_code == 200
    assert "What the AI found" in page.text and "Flask + gunicorn" in page.text
    assert "gunicorn app:app --bind 0.0.0.0:$PORT" in page.text

    with db_session_factory() as db:
        listing = db.scalar(select(Listing))
        assert listing.status == "draft" and not listing.approved
        assert listing.deploy_config["env_vars"] == {"GREETING": "hello"}
        assert listing.analysis["result"]["stack"] == "Flask + gunicorn"


def test_publish_allowlisted_repo_goes_live_on_home(client):
    url = edit_url(analyze(client))
    resp = client.post(url, data=publish_form(), follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/l/")

    listing_page = client.get(resp.headers["location"], headers=HTML)
    assert "Published! Your listing is live" in listing_page.text and "$3.50" in listing_page.text
    home = client.get("/", headers=HTML)
    assert "Demo App" in home.text and "1 listed" in home.text


def test_publish_requires_payout_email_and_valid_price(client):
    url = edit_url(analyze(client))
    resp = client.post(url, data=publish_form(owner_paypal_email="", price_per_hour="999"))
    assert resp.status_code == 422
    assert "PayPal (sandbox) email" in resp.text and "Price must be between" in resp.text


def test_port_env_var_is_rejected(client):
    url = edit_url(analyze(client))
    resp = client.post(url, data=publish_form(env_names=["PORT"], env_values=["5000"]))
    assert resp.status_code == 422 and "PORT is set by Render" in resp.text


def test_non_allowlisted_repo_awaits_approval(client, db_session_factory):
    app.dependency_overrides[get_github] = lambda: fake_github(full_name="someone/else")
    url = edit_url(analyze(client, "https://github.com/someone/else"))
    resp = client.post(url, data=publish_form(), follow_redirects=False)
    assert resp.status_code == 303

    assert "admin needs to approve" in client.get(resp.headers["location"], headers=HTML).text
    assert "0 listed" in client.get("/", headers=HTML).text
    with db_session_factory() as db:
        listing = db.scalar(select(Listing))
        assert listing.status == "published" and not listing.approved


def test_other_browsers_cannot_edit_or_see_drafts(client):
    url = edit_url(analyze(client))
    stranger = TestClient(app)  # no owner cookie
    assert stranger.get(url, headers=HTML).status_code == 403
    assert stranger.post(url, data=publish_form()).status_code == 403
    slug_page = client.post(url, data=publish_form(action="save"), follow_redirects=False)
    assert slug_page.status_code == 303


def test_bad_repo_url_shows_inline_error(client):
    resp = analyze(client, "https://gitlab.com/acme/demo")
    assert resp.status_code == 200 and "GitHub repo link" in resp.text and "HX-Redirect" not in resp.headers


def test_analysis_is_rate_limited(client):
    owner.analyze_limiter.limit = 2
    try:
        assert "HX-Redirect" in analyze(client).headers
        assert "HX-Redirect" in analyze(client).headers
        resp = analyze(client)
        assert "HX-Redirect" not in resp.headers and "Try again shortly" in resp.text
    finally:
        owner.analyze_limiter.limit = 6


def test_unknown_listing_is_404_page(client):
    resp = client.get("/l/does-not-exist", headers=HTML)
    assert resp.status_code == 404 and "couldn't find" in resp.text
