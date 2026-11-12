import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app import auth
from app.config import get_settings
from app.github_client import GitHubClient
from app.main import app
from app.models import Listing, ListingStatus, User
from app.routes import auth as auth_routes
from app.routes.deps import get_github

HTML = {"accept": "text/html"}
PROFILE = {
    "id": 4242,
    "login": "octo-dev",
    "name": "Octo Dev",
    "avatar_url": "https://avatars.githubusercontent.com/u/4242?v=4",
}


def oauth_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/login/oauth/access_token":
        code = dict(x.split("=") for x in request.content.decode().split("&"))["code"]
        return httpx.Response(
            200, json={"access_token": "gho_x"} if code == "good" else {"error": "bad_verification_code"}
        )
    if request.url.path == "/user":
        return httpx.Response(200, json=PROFILE)
    return httpx.Response(404)


@pytest.fixture
def oauth(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_client_id", "cid")
    monkeypatch.setattr(get_settings(), "github_client_secret", SecretStr("csecret"))
    app.dependency_overrides[auth_routes.get_github_oauth] = lambda: auth.GitHubOAuth(
        "cid", "csecret", transport=httpx.MockTransport(oauth_handler)
    )


def start_login(client, next_path="/owner/new"):
    resp = client.get(f"/auth/github/login?next={next_path}", follow_redirects=False)
    assert resp.status_code == 303
    return parse_qs(urlparse(resp.headers["location"]).query)


# ---------------------------------------------------------------- signed tokens


def test_signed_tokens_reject_tampering_and_expiry(monkeypatch):
    token = auth.sign({"uid": "1", "login": "a"})
    assert auth.unsign(token, max_age=60)["login"] == "a"
    body, mac = token.rsplit(".", 1)
    assert auth.unsign(body + "." + "0" * len(mac), max_age=60) is None
    assert auth.unsign(body[:-2] + "xx." + mac, max_age=60) is None
    later = time.time() + 3600
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert auth.unsign(token, max_age=60) is None


@pytest.mark.parametrize("bad", ["//evil.com", "https://evil.com", "/\\evil.com", "", None])
def test_no_open_redirects(bad):
    assert auth.safe_next(bad) == "/owner/new"


# ---------------------------------------------------------------- flow


def test_login_hidden_and_disabled_without_oauth_app(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_client_id", "")
    monkeypatch.setattr(get_settings(), "github_client_secret", SecretStr(""))
    assert client.get("/auth/github/login", follow_redirects=False).status_code == 404
    assert "Sign in with GitHub" not in client.get("/owner/new", headers=HTML).text


def test_login_requests_no_scopes(client, oauth):
    params = start_login(client)
    assert params["client_id"] == ["cid"] and "scope" not in params
    assert params["redirect_uri"][0].endswith("/auth/github/callback")


def test_callback_rejects_wrong_state(client, oauth):
    start_login(client)
    resp = client.get("/auth/github/callback?code=good&state=forged", headers=HTML, follow_redirects=False)
    assert resp.status_code == 400


def test_callback_rejects_bad_code(client, oauth):
    state = start_login(client)["state"][0]
    assert client.get(f"/auth/github/callback?code=bad&state={state}", follow_redirects=False).status_code == 400


def test_sign_in_creates_user_and_keeps_this_browsers_listings(client, oauth, db_session_factory):
    client.get("/", headers=HTML)  # get an owner cookie
    owner_key = client.cookies.get("ptp_owner")
    state = start_login(client, "/owner/dashboard")["state"][0]
    resp = client.get(f"/auth/github/callback?code=good&state={state}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/owner/dashboard"
    with db_session_factory() as db:
        user = db.scalar(select(User))
        assert user.login == "octo-dev" and user.owner_key == owner_key
    page = client.get("/owner/dashboard", headers=HTML).text
    assert "@octo-dev" in page and "Sign out" in page


def test_listings_follow_the_account_to_a_new_device(client, oauth, db_session_factory):
    with db_session_factory() as db:
        db.add(User(github_id=PROFILE["id"], login="octo-dev", owner_key="k" * 43))
        db.commit()
    new_device = TestClient(app)
    new_device.get("/", headers=HTML)
    device_key = new_device.cookies.get("ptp_owner")
    with db_session_factory() as db:  # something listed on the new device before signing in
        db.add(
            Listing(
                slug="d-1",
                repo_url="https://github.com/a/b",
                repo_full_name="a/b",
                title="B",
                owner_paypal_email="",
                price_per_hour=Decimal("1"),
                status=ListingStatus.DRAFT,
                owner_key=device_key,
            )
        )
        db.commit()
    state = start_login(new_device)["state"][0]
    new_device.get(f"/auth/github/callback?code=good&state={state}", follow_redirects=False)
    assert new_device.cookies.get("ptp_owner") == "k" * 43
    with db_session_factory() as db:
        assert db.scalar(select(Listing)).owner_key == "k" * 43


def test_logout_drops_session_and_ownership(client, oauth):
    state = start_login(client)["state"][0]
    client.get(f"/auth/github/callback?code=good&state={state}", follow_redirects=False)
    key = client.cookies.get("ptp_owner")
    client.post("/auth/logout", follow_redirects=False)
    assert client.cookies.get("ptp_user") is None and client.cookies.get("ptp_owner") != key


def test_repo_picker_lists_public_repos(client, oauth):
    def handler(request):
        assert request.url.path == "/users/octo-dev/repos"
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "octo-dev/todo",
                    "name": "todo",
                    "html_url": "https://github.com/octo-dev/todo",
                    "description": "A todo app",
                    "language": "Python",
                    "stargazers_count": 7,
                    "fork": False,
                },
                {
                    "full_name": "octo-dev/old",
                    "name": "old",
                    "html_url": "https://github.com/octo-dev/old",
                    "archived": True,
                },
            ],
        )

    app.dependency_overrides[get_github] = lambda: GitHubClient("t", transport=httpx.MockTransport(handler))
    from app.routes import owner

    owner._repo_cache.clear()
    state = start_login(client)["state"][0]
    client.get(f"/auth/github/callback?code=good&state={state}", follow_redirects=False)
    page = client.get("/owner/new", headers=HTML).text
    assert "Your public repos" in page and 'data-repo="https://github.com/octo-dev/todo"' in page
    assert "octo-dev/old" not in page  # archived repos hidden
