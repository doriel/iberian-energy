"""The budget, which is the only guardrail here that actually saves money.

The distinction being tested, and it is the one worth being clear about: a
refusal must happen without the model being called. Everything else about
scoping the agent costs a call either way, because the model has to read a
question to decline it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.app.limits import Budget  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def a_budget(clock=None, **kwargs):
    return Budget(clock=clock or Clock(), **kwargs)


# --- the per session limit ----------------------------------------------------


def test_a_fresh_session_may_ask():
    assert a_budget().check("s1").allowed is True


def test_a_session_is_cut_off_at_its_limit():
    budget = a_budget(per_session=3)
    for _ in range(3):
        assert budget.check("s1").allowed is True
        budget.spend("s1")

    verdict = budget.check("s1")
    assert verdict.allowed is False
    assert verdict.limit == "session"


def test_the_refusal_says_which_limit_and_what_to_do():
    """"Try again later" when the real answer is "not in this session" wastes
    somebody's afternoon."""
    budget = a_budget(per_session=1)
    budget.spend("s1")

    message = budget.check("s1").message
    assert "session" in message.lower()
    assert "signing in again" in message.lower()


def test_one_session_running_out_does_not_affect_another():
    budget = a_budget(per_session=2)
    for _ in range(2):
        budget.spend("s1")

    assert budget.check("s1").allowed is False
    assert budget.check("s2").allowed is True


def test_checking_does_not_spend():
    """Otherwise a page that polls would empty somebody's budget for them."""
    budget = a_budget(per_session=2)
    for _ in range(10):
        budget.check("s1")
    assert budget.remaining("s1") == 2


def test_waiting_does_not_refill_a_session():
    """The per session count never expires, on purpose.

    A budget you can refill by waiting is a budget you refill by waiting.
    """
    clock = Clock()
    budget = a_budget(clock, per_session=1)
    budget.spend("s1")

    clock.advance(60 * 60 * 24 * 7)
    assert budget.check("s1").allowed is False


# --- the process wide limit, which is the backstop -----------------------------


def test_clearing_a_cookie_does_not_buy_an_unlimited_supply():
    """The reason the second limit exists.

    A new session id is one browser action away, so the per session limit alone
    stops nobody. This is what holds.
    """
    budget = a_budget(per_session=1, per_window=5)
    for index in range(5):
        session = f"s{index}"
        assert budget.check(session).allowed is True
        budget.spend(session)

    verdict = budget.check("a-brand-new-session")
    assert verdict.allowed is False
    assert verdict.limit == "hourly"


def test_the_hourly_limit_is_a_rolling_window():
    clock = Clock()
    budget = a_budget(clock, per_window=2, window_seconds=100)
    budget.spend("s1")
    budget.spend("s2")
    assert budget.check("s3").allowed is False

    clock.advance(101)
    assert budget.check("s3").allowed is True


def test_the_hourly_refusal_says_the_rest_of_the_page_still_works():
    """Because it does, and somebody meeting this should not think it is down."""
    budget = a_budget(per_window=1)
    budget.spend("s1")

    message = budget.check("s2").message
    assert "still works" in message.lower()


# --- what the interface is told ------------------------------------------------


def test_remaining_counts_down():
    budget = a_budget(per_session=3)
    assert budget.remaining("s1") == 3
    budget.spend("s1")
    assert budget.remaining("s1") == 2


def test_remaining_never_goes_below_zero():
    budget = a_budget(per_session=1)
    for _ in range(5):
        budget.spend("s1")
    assert budget.remaining("s1") == 0


# --- the defaults, which are a decision and not an accident --------------------


def test_the_defaults_leave_room_for_a_reviewer_and_not_for_a_script():
    from iberian.app import limits

    # Enough to work through a batch of episodes asking about each.
    assert limits.QUESTIONS_PER_SESSION >= 20
    # Several people at once, and nowhere near a loop.
    assert limits.QUESTIONS_PER_HOUR >= limits.QUESTIONS_PER_SESSION * 3
    assert limits.QUESTIONS_PER_HOUR <= 500