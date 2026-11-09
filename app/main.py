"""FastAPI entry point: app wiring, owner cookie, error pages, health check."""

import logging
import re
import secrets

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import reaper
from app.config import get_settings
from app.db import DbSession
from app.routes import checkout, internal, owner, pages, webhooks
from app.routes.deps import OWNER_COOKIE
from app.templating import templates

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="PayToProvision", docs_url=None if settings.is_production else "/docs", redoc_url=None)
app.include_router(pages.router)
app.include_router(owner.router)
app.include_router(checkout.router)
app.include_router(webhooks.router)
app.include_router(internal.router)

_OWNER_KEY = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


@app.middleware("http")
async def owner_cookie(request: Request, call_next):
    """Give every browser a random owner key. There are no accounts: listings belong to
    the browser that created them."""
    key = request.cookies.get(OWNER_COOKIE, "")
    is_new = not _OWNER_KEY.match(key)
    if is_new:
        key = secrets.token_urlsafe(32)
    request.state.owner_key = key
    response = await call_next(request)
    if is_new:
        response.set_cookie(
            OWNER_COOKIE,
            key,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=settings.is_production,
        )
    # Piggyback the (throttled) reaper on real traffic; health checks and the cron endpoint don't count.
    if not request.url.path.startswith(("/healthz", "/internal", "/webhooks")):
        reaper.maybe_reap_in_background()
    return response


@app.exception_handler(StarletteHTTPException)
async def html_errors(request: Request, exc: StarletteHTTPException):
    if "text/html" not in request.headers.get("accept", ""):
        return await http_exception_handler(request, exc)
    return templates.TemplateResponse(
        request, "error.html", {"status": exc.status_code, "detail": exc.detail}, status_code=exc.status_code
    )


@app.get("/healthz")
def healthz(db: DbSession):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        log.exception("health check: database unreachable")
        return JSONResponse({"status": "error", "db": "unreachable"}, status_code=503)
    return {"status": "ok", "db": "ok"}
