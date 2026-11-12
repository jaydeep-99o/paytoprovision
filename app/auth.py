"""Sign in with GitHub: signed cookies and a minimal OAuth client.

Security notes:
- We request no OAuth scopes: the token can only read public profile data. It's used once
  in the callback to identify the user and then discarded; nothing about it is stored.
- The `state` parameter (CSRF protection) lives in a short-lived signed cookie.
- Session cookies are HMAC-SHA256 signed with SECRET_KEY, HttpOnly, SameSite=Lax and
  Secure in production. Tampering or expiry makes them invalid.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings

USER_COOKIE = "ptp_user"
STATE_COOKIE = "ptp_oauth"
USER_MAX_AGE = 30 * 24 * 3600
STATE_MAX_AGE = 10 * 60


class OAuthError(Exception):
    pass


# ---------------------------------------------------------------- signed tokens


def _key() -> bytes:
    return get_settings().secret_key.get_secret_value().encode()


def sign(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps({**payload, "iat": int(time.time())}).encode()).decode().rstrip("=")
    mac = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unsign(token: str | None, *, max_age: int) -> dict[str, Any] | None:
    """The payload of a token we signed, or None if it's missing, forged or expired."""
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    expected = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if not isinstance(payload, dict) or time.time() - payload.get("iat", 0) > max_age:
        return None
    return payload


def safe_next(path: str | None) -> str:
    """Only allow same-site relative redirects (no open redirect)."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/owner/new"


def login_enabled(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.github_client_id and s.github_client_secret.get_secret_value())


# ---------------------------------------------------------------- OAuth client


class GitHubOAuth:
    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_URL = "https://api.github.com/user"

    def __init__(self, client_id: str, client_secret: str, *, transport: httpx.BaseTransport | None = None):
        self.client_id = client_id
        self._secret = client_secret
        self._http = httpx.Client(timeout=20, transport=transport)

    @classmethod
    def from_settings(cls) -> "GitHubOAuth | None":
        s = get_settings()
        return cls(s.github_client_id, s.github_client_secret.get_secret_value()) if login_enabled(s) else None

    def __repr__(self) -> str:
        return "GitHubOAuth()"

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        # No `scope`: public profile only. We never ask for private repos or write access.
        return f"{self.AUTHORIZE_URL}?" + urlencode(
            {"client_id": self.client_id, "redirect_uri": redirect_uri, "state": state, "allow_signup": "true"}
        )

    def identify(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchange the code for a token, read the user's public profile, drop the token."""
        try:
            resp = self._http.post(
                self.TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            token = resp.json().get("access_token") if resp.status_code == 200 else None
            if not token:
                raise OAuthError("GitHub didn't accept the sign-in code.")
            user = self._http.get(
                self.USER_URL, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("Couldn't reach GitHub.") from exc
        if user.status_code != 200:
            raise OAuthError("GitHub didn't return your profile.")
        data = user.json()
        return {
            "id": int(data["id"]),
            "login": data["login"],
            "name": data.get("name") or "",
            "avatar_url": data.get("avatar_url") or "",
        }

    def close(self) -> None:
        self._http.close()
