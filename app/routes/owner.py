"""Owner flow: paste a repo -> AI analysis -> review config and price -> publish."""

import logging
import re
import secrets
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.analyzer import analyze_repo
from app.ai.llm import LLMClient, LLMError
from app.config import get_settings
from app.db import DbSession
from app.github_client import GitHubClient, GitHubError, parse_repo_url
from app.models import Listing, ListingStatus, Payout, PreviewSession
from app.payouts import split
from app.ratelimit import RateLimiter
from app.routes.deps import get_github, get_llm, get_owner_key
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/owner")

# Each analysis is a GitHub fetch plus an LLM call; keep anonymous use bounded.
analyze_limiter = RateLimiter(limit=6, window_seconds=600)

MIN_PRICE, MAX_PRICE = Decimal("0.50"), Decimal("50.00")
RUNTIMES = ("python", "node", "docker")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

OwnerKey = Annotated[str, Depends(get_owner_key)]


def _slug_for(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "app"
    return f"{base}-{secrets.token_hex(2)}"


def is_allowlisted(full_name: str) -> bool:
    return full_name.lower() in get_settings().allowed_repo_set


def _owned_listing(db: Session, listing_id: uuid.UUID, owner_key: str) -> Listing:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found")
    if listing.owner_key != owner_key:
        raise HTTPException(403, "This listing belongs to someone else (or was created in another browser).")
    return listing


@router.get("/dashboard")
def dashboard(request: Request, db: DbSession, owner_key: OwnerKey):
    """Sessions, earnings and payouts across this browser's listings."""
    listings = db.scalars(
        select(Listing).where(Listing.owner_key == owner_key).order_by(Listing.created_at.desc())
    ).all()
    ids = [listing.id for listing in listings]
    sessions = (
        db.scalars(
            select(PreviewSession)
            .where(PreviewSession.listing_id.in_(ids), PreviewSession.paid_at.is_not(None))
            .order_by(PreviewSession.created_at.desc())
        ).all()
        if ids
        else []
    )
    payouts = {p.session_id: p for p in db.scalars(select(Payout).where(Payout.listing_id.in_(ids)))} if ids else {}
    paid_out = sum((p.net_amount for p in payouts.values() if p.status == "SUCCESS"), Decimal("0"))
    pending = sum((p.net_amount for p in payouts.values() if p.status != "SUCCESS" and not p.error), Decimal("0"))
    upcoming = sum(
        (split(s.amount)[1] for s in sessions if s.status in ("paid", "deploying", "healing", "live")), Decimal("0")
    )
    per_listing = {
        listing.id: {
            "sold": sum(1 for s in sessions if s.listing_id == listing.id and not s.paypal_refund_id),
            "earned": sum((p.net_amount for p in payouts.values() if p.listing_id == listing.id), Decimal("0")),
        }
        for listing in listings
    }
    return templates.TemplateResponse(
        request,
        "owner_dashboard.html",
        {
            "listings": listings,
            "sessions": sessions[:30],
            "payouts": payouts,
            "per_listing": per_listing,
            "kpis": {
                "paid_out": paid_out,
                "pending": pending + upcoming,
                "sold": sum(1 for s in sessions if not s.paypal_refund_id),
                "refunded": sum(1 for s in sessions if s.paypal_refund_id),
                "live": sum(1 for s in sessions if s.status == "live"),
            },
            "fee_percent": get_settings().platform_fee_percent,
        },
    )


@router.get("/new")
def new_listing(request: Request, db: DbSession, owner_key: OwnerKey):
    mine = db.scalars(select(Listing).where(Listing.owner_key == owner_key).order_by(Listing.created_at.desc())).all()
    return templates.TemplateResponse(request, "owner_new.html", {"my_listings": mine})


@router.post("/analyze")
def analyze(
    request: Request,
    db: DbSession,
    owner_key: OwnerKey,
    repo_url: Annotated[str, Form()],
    github: Annotated[GitHubClient, Depends(get_github)],
    llm: Annotated[LLMClient | None, Depends(get_llm)],
):
    def fail(message: str) -> Response:
        # 200 so HTMX swaps the error into the page.
        return templates.TemplateResponse(request, "partials/analyze_error.html", {"error": message})

    try:
        ref = parse_repo_url(repo_url)
    except GitHubError as exc:
        return fail(str(exc))
    if llm is None:
        return fail("AI analysis isn't configured on this server (NEBIUS_API_KEY missing).")
    client_ip = request.client.host if request.client else "unknown"
    if not analyze_limiter.allow(client_ip):
        return fail("You've analyzed a lot of repos in the last few minutes. Try again shortly.")

    try:
        result = analyze_repo(ref.url + (f"/tree/{ref.branch}" if ref.branch else ""), github=github, llm=llm)
    except GitHubError as exc:
        return fail(str(exc))
    except LLMError:
        log.exception("analysis failed for %s", ref.full_name)
        return fail("The AI couldn't analyze this repo right now. Please try again.")

    a = result.analysis
    listing = Listing(
        slug=_slug_for(ref.name),
        repo_url=result.snapshot.html_url,
        repo_full_name=result.snapshot.full_name,
        branch=result.snapshot.branch,
        title=a.title[:200] or ref.name,
        summary=a.summary,
        owner_paypal_email="",
        price_per_hour=Decimal("2.00"),
        currency=get_settings().currency,
        status=ListingStatus.DRAFT,
        approved=False,
        deploy_config=result.deploy_config(),
        analysis=result.to_record(),
        confidence=a.confidence,
        owner_key=owner_key,
    )
    db.add(listing)
    db.commit()

    target = f"/owner/listings/{listing.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


def _edit_context(listing: Listing, form: dict | None = None, errors: dict | None = None, saved: bool = False):
    cfg = listing.deploy_config or {}
    values = form or {
        "title": listing.title,
        "summary": listing.summary,
        "price_per_hour": f"{listing.price_per_hour:.2f}",
        "owner_name": listing.owner_name,
        "owner_paypal_email": listing.owner_paypal_email,
        "runtime": cfg.get("runtime", "python"),
        "build_command": cfg.get("build_command", ""),
        "start_command": cfg.get("start_command", ""),
        "health_path": cfg.get("health_path", "/"),
        "env": list((cfg.get("env_vars") or {}).items()),
    }
    analysis = listing.analysis or {}
    required = {e["name"] for e in (analysis.get("result") or {}).get("env_vars", []) if e.get("required")}
    return {
        "listing": listing,
        "v": values,
        "errors": errors or {},
        "saved": saved,
        "analysis": analysis,
        "result": analysis.get("result") or {},
        "required_env": required,
        "allowlisted": is_allowlisted(listing.repo_full_name),
        "runtimes": RUNTIMES,
        "max_hours": get_settings().max_session_hours,
    }


@router.get("/listings/{listing_id}")
def edit_listing(request: Request, listing_id: uuid.UUID, db: DbSession, owner_key: OwnerKey, saved: bool = False):
    listing = _owned_listing(db, listing_id, owner_key)
    return templates.TemplateResponse(request, "owner_edit.html", _edit_context(listing, saved=saved))


@router.post("/listings/{listing_id}")
def save_listing(
    request: Request,
    listing_id: uuid.UUID,
    db: DbSession,
    owner_key: OwnerKey,
    action: Annotated[str, Form()] = "save",
    title: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
    price_per_hour: Annotated[str, Form()] = "",
    owner_name: Annotated[str, Form()] = "",
    owner_paypal_email: Annotated[str, Form()] = "",
    runtime: Annotated[str, Form()] = "python",
    build_command: Annotated[str, Form()] = "",
    start_command: Annotated[str, Form()] = "",
    health_path: Annotated[str, Form()] = "/",
    env_names: Annotated[list[str] | None, Form()] = None,
    env_values: Annotated[list[str] | None, Form()] = None,
):
    listing = _owned_listing(db, listing_id, owner_key)
    publishing = action == "publish"
    form = {
        "title": title.strip(),
        "summary": summary.strip(),
        "price_per_hour": price_per_hour.strip(),
        "owner_name": owner_name.strip(),
        "owner_paypal_email": owner_paypal_email.strip(),
        "runtime": runtime,
        "build_command": build_command.strip(),
        "start_command": start_command.strip(),
        "health_path": health_path.strip() or "/",
        "env": [(n.strip(), v) for n, v in zip(env_names or [], env_values or [], strict=False) if n.strip()],
    }

    errors: dict[str, str] = {}
    if not form["title"] or len(form["title"]) > 120:
        errors["title"] = "Give the listing a title (up to 120 characters)."
    if len(form["summary"]) > 1200:
        errors["summary"] = "Keep the summary under 1200 characters."
    try:
        price = Decimal(form["price_per_hour"]).quantize(Decimal("0.01"))
        if not MIN_PRICE <= price <= MAX_PRICE:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errors["price_per_hour"] = f"Price must be between ${MIN_PRICE} and ${MAX_PRICE} per hour."
        price = listing.price_per_hour
    email = form["owner_paypal_email"]
    if email and not _EMAIL.match(email):
        errors["owner_paypal_email"] = "That doesn't look like an email address."
    elif publishing and not email:
        errors["owner_paypal_email"] = "Add the PayPal (sandbox) email where you want to be paid."
    if form["runtime"] not in RUNTIMES:
        errors["runtime"] = "Pick a runtime."
    if form["runtime"] != "docker" and not (form["build_command"] and form["start_command"]):
        errors["commands"] = "Python and Node services need both a build and a start command."
    if not form["health_path"].startswith("/"):
        errors["health_path"] = "Health check path must start with /."
    bad_env = [n for n, _ in form["env"] if not _ENV_NAME.match(n) or n.upper() == "PORT"]
    if bad_env:
        errors["env"] = f"Invalid env var name(s): {', '.join(bad_env)} (PORT is set by Render)."
    elif len(form["env"]) > 25:
        errors["env"] = "Up to 25 env vars."

    if errors:
        context = _edit_context(listing, form=form, errors=errors)
        return templates.TemplateResponse(request, "owner_edit.html", context, status_code=422)

    listing.title = form["title"]
    listing.summary = form["summary"]
    listing.price_per_hour = price
    listing.owner_name = form["owner_name"]
    listing.owner_paypal_email = email
    listing.deploy_config = {
        "runtime": form["runtime"],
        "build_command": form["build_command"],
        "start_command": form["start_command"],
        "health_path": form["health_path"],
        "env_vars": {n: v[:500] for n, v in form["env"]},
    }
    if publishing:
        listing.status = ListingStatus.PUBLISHED
        listing.approved = listing.approved or is_allowlisted(listing.repo_full_name)
    elif action == "unpublish":
        listing.status = ListingStatus.DRAFT
    db.commit()

    if publishing:
        return RedirectResponse(f"/l/{listing.slug}?published=1", status_code=303)
    return RedirectResponse(f"/owner/listings/{listing.id}?saved=1", status_code=303)
