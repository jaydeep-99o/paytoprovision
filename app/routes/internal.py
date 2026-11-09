"""Internal endpoints for automation (GitHub Actions cron)."""

import hmac

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from app import reaper
from app.config import get_settings

router = APIRouter(prefix="/internal")


@router.post("/reap")
async def reap(authorization: str = Header(default="")):
    expected = get_settings().internal_reap_token.get_secret_value()
    token = authorization.removeprefix("Bearer ").strip()
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "invalid token")
    return await run_in_threadpool(reaper.reap)
