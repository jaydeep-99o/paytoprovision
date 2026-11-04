from decimal import Decimal

import pytest

from app.models import InvalidTransition, PreviewSession, SessionStatus


def make_session(status: SessionStatus = SessionStatus.PENDING_PAYMENT) -> PreviewSession:
    return PreviewSession(status=status, hours=1, amount=Decimal("3.00"))


def test_happy_path_transitions():
    s = make_session()
    for status in (SessionStatus.PAID, SessionStatus.DEPLOYING, SessionStatus.LIVE, SessionStatus.EXPIRED):
        s.transition(status)
    assert s.status == SessionStatus.EXPIRED


def test_healing_loop_then_refund():
    s = make_session(SessionStatus.DEPLOYING)
    s.transition(SessionStatus.HEALING)
    s.transition(SessionStatus.DEPLOYING)
    s.transition(SessionStatus.HEALING)
    s.transition(SessionStatus.FAILED_REFUNDED)
    assert s.status == SessionStatus.FAILED_REFUNDED


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (SessionStatus.PENDING_PAYMENT, SessionStatus.LIVE),
        (SessionStatus.LIVE, SessionStatus.DEPLOYING),
        (SessionStatus.EXPIRED, SessionStatus.LIVE),
        (SessionStatus.FAILED_REFUNDED, SessionStatus.PAID),
    ],
)
def test_invalid_transitions_raise(start, target):
    with pytest.raises(InvalidTransition):
        make_session(start).transition(target)
