"""Check that every configured credential works, without printing any secret.

    python -m scripts.check_env

Each check is skipped (not failed) when its env vars aren't set yet, so you can run
this after every phase as you add accounts.
"""

import sys
from collections.abc import Callable

import httpx
from sqlalchemy import create_engine, text

from app.config import Settings, get_settings
from app.render_client import RenderClient

OK, SKIP, FAIL = "OK  ", "SKIP", "FAIL"


def check_database(s: Settings) -> tuple[str, str]:
    url = s.sqlalchemy_url
    kind = "SQLite (local dev)" if url.startswith("sqlite") else "Postgres"
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:  # noqa: BLE001
                version = None
    finally:
        engine.dispose()
    migrated = f"migrated to {version}" if version else "NOT migrated: run `alembic upgrade head`"
    return OK, f"{kind}, {migrated}"


def check_render(s: Settings) -> tuple[str, str]:
    if not s.render_api_key.get_secret_value():
        return SKIP, "RENDER_API_KEY not set"
    with RenderClient.from_settings(s, max_retries=1) as render:
        owners = render.list_owners()
        names = ", ".join(f"{o['name']} ({o['id']})" for o in owners)
        if s.render_owner_id and s.render_owner_id not in {o["id"] for o in owners}:
            return FAIL, f"RENDER_OWNER_ID not among this key's workspaces: {names}"
        return OK, f"workspaces: {names}"


def check_github(s: Settings) -> tuple[str, str]:
    if not s.github_token.get_secret_value():
        return SKIP, "GITHUB_TOKEN not set (anonymous limit is 60 req/h)"
    resp = httpx.get(
        "https://api.github.com/rate_limit",
        headers={
            "Authorization": f"Bearer {s.github_token.get_secret_value()}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    core = resp.json()["resources"]["core"]
    return OK, f"rate limit {core['remaining']}/{core['limit']}"


def check_nebius(s: Settings) -> tuple[str, str]:
    if not s.nebius_api_key.get_secret_value():
        return SKIP, "NEBIUS_API_KEY not set"
    resp = httpx.get(
        s.nebius_base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {s.nebius_api_key.get_secret_value()}"},
        timeout=20,
    )
    resp.raise_for_status()
    ids = [m["id"] for m in resp.json().get("data", [])]
    if s.nebius_model in ids:
        return OK, f"model {s.nebius_model} available"
    nemotron = [i for i in ids if "nemotron" in i.lower()]
    return FAIL, f"NEBIUS_MODEL {s.nebius_model!r} not found. Nemotron models on this key: {nemotron or 'none'}"


def check_paypal(s: Settings) -> tuple[str, str]:
    if not (s.paypal_client_id and s.paypal_client_secret.get_secret_value()):
        return SKIP, "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set"
    if "sandbox" not in s.paypal_base_url:
        return FAIL, f"PAYPAL_BASE_URL {s.paypal_base_url} is not the sandbox; this project is sandbox-only"
    resp = httpx.post(
        s.paypal_base_url.rstrip("/") + "/v1/oauth2/token",
        auth=(s.paypal_client_id, s.paypal_client_secret.get_secret_value()),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    resp.raise_for_status()
    token = resp.json()
    webhook = "PAYPAL_WEBHOOK_ID set" if s.paypal_webhook_id else "PAYPAL_WEBHOOK_ID not set yet (Phase 3)"
    return OK, f"sandbox token issued (expires in {token.get('expires_in')}s); {webhook}"


CHECKS: list[tuple[str, Callable[[Settings], tuple[str, str]]]] = [
    ("Database", check_database),
    ("Render", check_render),
    ("GitHub", check_github),
    ("Nebius", check_nebius),
    ("PayPal", check_paypal),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # workspace names may contain non-ASCII
    settings = get_settings()
    failed = False
    for label, check in CHECKS:
        try:
            status, detail = check(settings)
        except httpx.HTTPStatusError as exc:
            status, detail = FAIL, f"HTTP {exc.response.status_code} from {exc.request.url.host}"
        except Exception as exc:  # noqa: BLE001
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        failed |= status == FAIL
        print(f"[{status}] {label:<9} {detail}")

    guardrails = settings.allowed_repo_set
    print(f"\nAllowlisted repos: {', '.join(sorted(guardrails)) if guardrails else 'none (ALLOWED_REPOS empty)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
