"""Checkout agent chat endpoint (JSON, called from the listing page)."""

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.ai.agent import CheckoutAgent
from app.ai.llm import LLMClient, LLMError
from app.budget import BudgetExceeded
from app.db import DbSession
from app.models import Listing
from app.ratelimit import RateLimiter
from app.routes.deps import get_llm, get_toolkit

log = logging.getLogger(__name__)
router = APIRouter()

chat_limiter = RateLimiter(limit=30, window_seconds=600)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]  # the client can't inject system or tool messages
    content: str = Field(max_length=2000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=30)
    order_id: str | None = Field(default=None, max_length=64)


@router.post("/api/agent/{slug}/chat")
def chat(
    slug: str,
    body: ChatRequest,
    request: Request,
    db: DbSession,
    llm: Annotated[LLMClient | None, Depends(get_llm)],
    toolkit: Annotated[Any, Depends(get_toolkit)],
):
    listing = db.scalar(select(Listing).where(Listing.slug == slug))
    if listing is None or not listing.is_live:
        raise HTTPException(404, "Listing not found")
    if llm is None or toolkit is None:
        return JSONResponse({"error": "The checkout agent isn't available right now. Use the PayPal button."}, 503)
    if body.messages[-1].role != "user":
        raise HTTPException(400, "The last message must come from the user.")
    if not chat_limiter.allow(request.client.host if request.client else "unknown"):
        return JSONResponse({"error": "You're sending messages quickly. Wait a moment and try again."}, 429)

    agent = CheckoutAgent(db, listing, llm, toolkit)
    try:
        result = agent.respond([m.model_dump() for m in body.messages], order_id=body.order_id)
    except BudgetExceeded:
        return JSONResponse(
            {"error": "The checkout agent reached today's demo limit. You can still pay with the PayPal button."}, 429
        )
    except LLMError:
        log.exception("checkout agent failed")
        return JSONResponse({"error": "The agent had a hiccup. Try again, or use the PayPal button."}, 502)
    return {"reply": result.reply, "order": result.order, "tools_used": result.tools_used}
