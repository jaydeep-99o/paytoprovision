"""Global daily budgets for paid resources: AI calls (Nebius credits) and deploys (Render
build minutes).

Per-IP rate limits are best-effort: IPs can be rotated or spoofed. These caps are shared by
all visitors and stored in the database (one row per day and kind), so neither a spoofed IP
nor an app restart can get around them. The increment is a single conditional UPDATE, so
concurrent requests can't overshoot the limit.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models import UsageCounter

AI = "ai"
DEPLOY = "deploy"

_session_factory = SessionLocal  # tests point this at their own database


class BudgetExceeded(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"daily {kind} budget used up")


def limit_for(kind: str) -> int:
    s = get_settings()
    return {AI: s.ai_daily_call_limit, DEPLOY: s.deploy_daily_limit}[kind]


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def used(kind: str) -> int:
    with _session_factory() as db:
        return db.scalar(select(UsageCounter.count).where(UsageCounter.day == _today(), UsageCounter.kind == kind)) or 0


def remaining(kind: str) -> int:
    return max(limit_for(kind) - used(kind), 0)


def take(kind: str, amount: int = 1) -> None:
    """Consume `amount` from today's budget or raise BudgetExceeded. A limit of 0 means unlimited."""
    limit = limit_for(kind)
    if limit <= 0:
        return
    day = _today()
    with _session_factory() as db:
        for _ in range(2):
            result = db.execute(
                update(UsageCounter)
                .where(UsageCounter.day == day, UsageCounter.kind == kind, UsageCounter.count + amount <= limit)
                .values(count=UsageCounter.count + amount)
            )
            if result.rowcount:
                db.commit()
                return
            if db.get(UsageCounter, (day, kind)) is not None:
                raise BudgetExceeded(kind)  # today's row exists and is at the limit
            if amount > limit:
                raise BudgetExceeded(kind)
            db.add(UsageCounter(day=day, kind=kind, count=amount))
            try:
                db.commit()
                return
            except IntegrityError:  # another request created today's row first; retry the update
                db.rollback()
        raise BudgetExceeded(kind)
