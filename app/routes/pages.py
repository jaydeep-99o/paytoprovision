"""Public pages: home and listing detail."""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.config import get_settings
from app.db import DbSession
from app.models import Listing, ListingStatus
from app.routes.owner import OwnerKey
from app.templating import templates

router = APIRouter()


@router.get("/")
def home(request: Request, db: DbSession):
    listings = db.scalars(
        select(Listing)
        .where(Listing.status == ListingStatus.PUBLISHED, Listing.approved.is_(True))
        .order_by(Listing.created_at.desc())
    ).all()
    return templates.TemplateResponse(request, "home.html", {"listings": listings})


@router.get("/l/{slug}")
def listing_page(request: Request, slug: str, db: DbSession, owner_key: OwnerKey, published: bool = False):
    listing = db.scalar(select(Listing).where(Listing.slug == slug))
    is_owner = listing is not None and listing.owner_key == owner_key
    if listing is None or not (listing.is_live or is_owner):
        raise HTTPException(404, "Listing not found")
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "listing.html",
        {
            "listing": listing,
            "is_owner": is_owner,
            "just_published": published,
            "hours_options": list(range(1, settings.max_session_hours + 1)),
            "result": (listing.analysis or {}).get("result") or {},
        },
    )
