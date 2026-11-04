"""FastAPI entry point: pages and health check."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import DbSession
from app.models import Listing, ListingStatus

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

app = FastAPI(title="PayToProvision", docs_url=None if settings.is_production else "/docs", redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: DbSession):
    listings = db.scalars(
        select(Listing)
        .where(Listing.status == ListingStatus.PUBLISHED, Listing.approved.is_(True))
        .order_by(Listing.created_at.desc())
    ).all()
    return templates.TemplateResponse(request, "home.html", {"listings": listings})


@app.get("/healthz")
def healthz(db: DbSession):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        log.exception("health check: database unreachable")
        return JSONResponse({"status": "error", "db": "unreachable"}, status_code=503)
    return {"status": "ok", "db": "ok"}
