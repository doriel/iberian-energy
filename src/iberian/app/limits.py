"""How much of the model this application will spend, and on whom.

The agent is reachable by anybody with the link, and every question it answers
is paid for. Three things follow, and only the first of them saves money.

**A budget, counted in code.** A question is refused before the model sees it,
so a refusal costs nothing. This is the defence. Everything else is manners.

**A scope, enforced in the prompt.** Telling the model to decline questions
about anything other than this market keeps the product coherent, but the model
has to read the question to decline it, so the call happens either way. Worth
having, worth not mistaking for cost control.

**A ceiling on the answer**, which lives in `assistant.py`: five rounds and 700
tokens. A loop is stopped by the round limit rather than by anybody noticing.

## What this does not do

A person who clears their cookie gets a new session and a new per session
budget. That is why there is a second, process wide limit: it is the one that
holds when the first is walked around, and it is deliberately generous enough
that ordinary use never meets it while a script does within a minute.

Neither limit distinguishes a curious reviewer from an attacker, and neither is
meant to. This is a capstone demonstration with a published link, not a service
with paying users. The threat being defended against is a bored person with a
loop, and the cost of being wrong is an afternoon's credit.

## Why the counters are in memory

Same reason the sessions are: one process. A second instance would give each
visitor two budgets, and whoever adds one has to move this somewhere shared.
Written here because a limit that silently stops limiting is worse than no
limit, since nobody thinks to check it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

#: One person, one sitting. Enough to judge a batch of episodes and ask about
#: each, which is what a reviewer actually does, and far short of a script.
QUESTIONS_PER_SESSION = 25

#: Everybody, rolling hour. The backstop for somebody clearing their cookie.
#: Generous on purpose: three reviewers working through the application at once
#: should never see it.
QUESTIONS_PER_HOUR = 150

WINDOW_SECONDS = 3600


@dataclass
class Verdict:
    """Whether to answer, and what to say when the answer is no.

    The message is written for the person rather than for a log, because it is
    what they read. It says which limit was reached, since "try again later"
    when the real answer is "not in this session" wastes somebody's time.
    """

    allowed: bool
    message: str = ""
    limit: str = ""


@dataclass
class Budget:
    """What the application will spend on the model, counted per session and in total.

    The clock is an argument so a rolling window can be tested without waiting
    an hour, which is the only way a test of a rolling window is worth writing.
    """

    per_session: int = QUESTIONS_PER_SESSION
    per_window: int = QUESTIONS_PER_HOUR
    window_seconds: float = WINDOW_SECONDS
    clock: Callable[[], float] = time.monotonic

    _by_session: dict[str, int] = field(default_factory=dict)
    _recent: deque = field(default_factory=deque)

    def check(self, session_id: str) -> Verdict:
        """Decide without spending anything. Does not count the question."""
        self._forget_old()

        if len(self._recent) >= self._window_limit():
            return Verdict(
                allowed=False,
                limit="hourly",
                message=(
                    "This demonstration has answered as many questions as it "
                    "will in one hour. Everything else on the page still works; "
                    "the agent will answer again shortly."
                ),
            )

        asked = self._by_session.get(session_id, 0)
        if asked >= self.per_session:
            return Verdict(
                allowed=False,
                limit="session",
                message=(
                    f"You have asked the agent {asked} questions in this "
                    "session, which is as many as it answers. Signing in again "
                    "starts a new one, and nothing you have saved is affected."
                ),
            )

        return Verdict(allowed=True)

    def spend(self, session_id: str) -> None:
        """Record a question that was actually sent to the model.

        Separate from `check` on purpose. A question that never reached the
        model, because the session had gone or the endpoint was down, should not
        come out of anybody's budget.
        """
        self._forget_old()
        self._by_session[session_id] = self._by_session.get(session_id, 0) + 1
        self._recent.append(self.clock())

    def remaining(self, session_id: str) -> int:
        """What is left in this session, for the interface to show.

        Shown rather than hidden: a limit somebody meets without warning feels
        like a fault, and one they can see feels like a rule.
        """
        return max(0, self.per_session - self._by_session.get(session_id, 0))

    def _window_limit(self) -> int:
        return self.per_window

    def _forget_old(self) -> None:
        cutoff = self.clock() - self.window_seconds
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()

        # The per session counts are never expired. They are small, a session
        # is a day at most, and forgetting them would hand somebody a fresh
        # budget by waiting rather than by starting a new session.


#: The application's one budget. A module level instance for the same reason the
#: session store is: the route that checks it and the route that spends it are
#: the same process and must see the same counters.
BUDGET = Budget()