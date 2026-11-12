"""Sign in with GitHub."""

import hmac
import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from app.auth import (
    STATE_COOKIE,
    STATE_MAX_AGE,
    USER_COOKIE,
    USER_MAX_AGE,
    GitHubOAuth,
    OAuthError,
    safe_next,
    sign,
    unsign,
)
from app.config import get_settings
from app.db import DbSession
from app.models import Listing, User
from app.routes.deps import OWNER_COOKIE, get_owner_key

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")


def get_github_oauth() -> GitHubOAuth | None:
    return GitHubOAuth.from_settings()


OAuth = Annotated[GitHubOAuth | None, Depends(get_github_oauth)]


def _callback_url() -> str:
    return get_settings().app_base_url.rstrip("/") + "/auth/github/callback"


def _cookie(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, httponly=True, samesite="lax", secure=get_settings().is_production
    )


@router.get("/github/login")
def github_login(oauth: OAuth, next: str = "/owner/new"):
    if oauth is None:
        raise HTTPException(404, "GitHub sign-in isn't configured on this server.")
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(oauth.authorize_url(state=state, redirect_uri=_callback_url()), status_code=303)
    _cookie(response, STATE_COOKIE, sign({"state": state, "next": safe_next(next)}), STATE_MAX_AGE)
    return response


@router.get("/github/callback")
def github_callback(
    request: Request,
    db: DbSession,
    oauth: OAuth,
    owner_key: Annotated[str, Depends(get_owner_key)],
    code: str = "",
    state: str = "",
):
    if oauth is None:
        raise HTTPException(404, "GitHub sign-in isn't configured on this server.")
    saved = unsign(request.cookies.get(STATE_COOKIE), max_age=STATE_MAX_AGE)
    if not saved or not state or not hmac.compare_digest(saved.get("state", ""), state):
        raise HTTPException(400, "Sign-in expired or was tampered with. Please try again.")
    try:
        profile = oauth.identify(code=code, redirect_uri=_callback_url())
    except OAuthError as exc:
        raise HTTPException(400, str(exc)) from exc

    user = db.scalar(select(User).where(User.github_id == profile["id"]))
    if user is None:
        # First sign-in: this browser's listings become the account's listings.
        user = User(github_id=profile["id"], owner_key=owner_key)
        db.add(user)
    elif user.owner_key != owner_key:
        # Signing in on another device: bring along anything listed here before signing in.
        db.execute(update(Listing).where(Listing.owner_key == owner_key).values(owner_key=user.owner_key))
    user.login, user.name, user.avatar_url = profile["login"], profile["name"], profile["avatar_url"]
    user.last_login_at = datetime.now(UTC)
    db.commit()
    log.info("github sign-in: %s", user.login)

    response = RedirectResponse(saved.get("next", "/owner/new"), status_code=303)
    _cookie(response, OWNER_COOKIE, user.owner_key, 365 * 24 * 3600)
    _cookie(
        response, USER_COOKIE, sign({"uid": str(user.id), "login": user.login, "avatar": user.avatar_url}), USER_MAX_AGE
    )
    response.delete_cookie(STATE_COOKIE)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(USER_COOKIE)
    # A fresh owner key: this browser no longer controls the account's listings.
    _cookie(response, OWNER_COOKIE, secrets.token_urlsafe(32), 365 * 24 * 3600)
    return response


def current_user(request: Request) -> dict | None:
    """The signed-in user's {uid, login, avatar}, from the signed cookie (no DB hit)."""
    return unsign(request.cookies.get(USER_COOKIE), max_age=USER_MAX_AGE)
