"""The routes behind the workbench: episodes, alerts, labels, and the agent.

Split from `main.py` because these need Lakebase and the serving endpoint, and
the dashboard does not. The dashboard is served from a committed JSON file and
must keep working when this half is misconfigured, because it is the page a
visitor lands on and the one that needs nothing to be awake.

**Who the user is.** A name in a signed cookie. It is not authentication and is
not treated as one: it scopes a person's own rows in the interface and separates
one labeller's judgements from another's in the evaluation. Nothing is
authorised by it, and every statement that touches somebody's data filters on it
anyway, so a forged cookie reaches another name's rows and nothing else. Said
plainly here because a reviewer should not have to work out how much this is
trusted.

**Everything returns a shape the page can render.** A route that raised would
give the browser a stack trace and the person a spinner that never stops. The
failures that matter, Lakebase unreachable, the endpoint down, the agent
looping, all arrive as JSON with a message written for a person.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Cookie, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["workbench"])

log = logging.getLogger("workbench")


def _explain(exc: Exception, doing: str) -> str:
    """The full failure to the log, a sentence to the browser.

    Two reasons, and the second is the one that matters once this is deployed.
    A Databricks authentication failure is several lines long and the useful
    part, the command to run, is not the first one, so truncating to one line
    hands somebody the half that cannot help them. And an internal error shown
    in a public page leaks the workspace host, the endpoint names and whatever
    else the traceback mentions.

    So the log gets everything, and the page gets a sentence that says what
    failed and where to look.
    """
    log.error("%s failed\n%s", doing, traceback.format_exc())

    message = str(exc)
    if "databricks-cli" in message or "refresh token" in message:
        return (
            "The server is not signed in to Databricks. Its credentials have "
            "expired or were never set. The server log has the details."
        )
    if "password authentication failed" in message:
        return (
            "The database refused the server's credentials. The Postgres role "
            "may not exist for this identity. The server log has the details."
        )
    if isinstance(exc, RuntimeError) and "LAKEBASE_" in message:
        # Ours, written for a person, so it goes through as it is.
        return message
    return f"Something went wrong while {doing}. The server log has the details."

#: One cookie, signed, carrying the name and the session id together. They were
#: two unsigned cookies and that was worse in a way worth recording: the name
#: could be edited in the browser's console, so a person's rows and the name on
#: them could drift apart without anybody touching the server.
SESSION_COOKIE = "mibel_session"

#: How the visitor names themselves. One cookie, signed, carrying the name and
#: the session id together. They were two unsigned cookies once, and that was
#: worse in a way worth recording: the name could be edited in the browser's
#: console, so a person's rows and the name on them could drift apart without
#: anybody touching the server.
SESSION_COOKIE = "mibel_session"

#: A day, and the server's own secret ends it sooner if the process restarts.
SESSION_MAX_AGE = 86400


# --- configuration -----------------------------------------------------------


class SignedOut(Exception):
    """No valid session. Every route answers this the same way: sign in again."""


def _endpoint() -> str:
    """Which Lakebase endpoint a credential is generated for.

    No defaults. A fallback here would be one person's project name compiled
    into a public repository, and worse, a misconfigured deployment would point
    at it and fail somewhere confusing instead of at start up with a name.
    """
    missing = [
        name
        for name in ("LAKEBASE_PROJECT", "LAKEBASE_BRANCH", "LAKEBASE_ENDPOINT_ID")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            f"Not configured: {', '.join(missing)}. The workbench cannot reach "
            "its database without knowing which endpoint."
        )
    return (
        f"projects/{os.environ['LAKEBASE_PROJECT']}"
        f"/branches/{os.environ['LAKEBASE_BRANCH']}"
        f"/endpoints/{os.environ['LAKEBASE_ENDPOINT_ID']}"
    )


@lru_cache(maxsize=1)
def store():
    """One Lakebase client for the process, because there is one identity.

    The application authenticates as a service principal and connects as that
    principal's Postgres role, so every visitor's work goes through the same
    connection. Who did it is a column, not a credential: `created_by` comes
    from the signed cookie and every statement that touches somebody's data
    filters on it.

    That is a real limitation and it is worth being plain about rather than
    dressing up. The database cannot tell two reviewers apart; the application
    can. A forged cookie reaches another name's rows. Nothing here is protecting
    anything, and the sign in page says so.

    Cached rather than global so the first request pays for the import and a
    misconfigured deployment fails on a request with a readable message instead
    of at import time with a traceback in the deploy log.
    """
    from iberian.app.lakebase import Lakebase, databricks_credentials

    host = os.environ.get("LAKEBASE_HOST")
    user = os.environ.get("LAKEBASE_USER")
    if not host or not user:
        raise RuntimeError(
            "LAKEBASE_HOST and LAKEBASE_USER are not set, so the workbench "
            "cannot reach its database."
        )
    return Lakebase(
        host=host, user=user, credential_factory=databricks_credentials(_endpoint())
    )


@lru_cache(maxsize=1)
def chat():
    from iberian.app.assistant import databricks_chat

    return databricks_chat(
        endpoint=os.environ.get("AGENT_ENDPOINT", "databricks-claude-haiku-4-5")
    )


def actions_for(session: tuple[str, str] | None):
    """The write surface for one named visitor, or SignedOut.

    `created_by` is the name in the signed cookie. Not authenticated, and never
    treated as though it were, but it is the name the person chose and it
    reached the server unaltered.
    """
    from iberian.app.actions import Actions

    if session is None:
        raise SignedOut()
    name, session_id = session
    return Actions(store=store(), session_id=session_id, created_by=name)


# --- the visitor -------------------------------------------------------------


def _session(raw: str | None) -> tuple[str, str] | None:
    """The name and session id in a verified cookie, or None."""
    from iberian.app.session import verify

    return verify(raw)


class Session(BaseModel):
    name: str = Field(default="", max_length=60)


@router.post("/session")
def start_session(body: Session, response: Response) -> dict:
    """Name yourself. No password, because nothing here is protected by one."""
    from iberian.app.session import clean_name, new_session_id, sign

    name = clean_name(body.name)
    session_id = new_session_id()
    # HttpOnly now that it is signed: the page asks `GET /session` for the name
    # instead of reading the cookie, which costs one request and means the only
    # copy of the name the browser can reach is the one the server just sent.
    response.set_cookie(
        SESSION_COOKIE,
        sign(name, session_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"name": name, "session_id": session_id}


@router.get("/session")
def current_session(mibel_session: str | None = Cookie(default=None)) -> dict:
    """Who the server believes you are, which is the only opinion that counts."""
    found = _session(mibel_session)
    if found is None:
        return {"signed_in": False, "name": ""}
    return {"signed_in": True, "name": found[0]}


@router.post("/signout")
def sign_out(response: Response) -> dict:
    """Forget the name. The rows stay, because they are somebody's judgements."""
    response.delete_cookie(SESSION_COOKIE, samesite="lax")
    return {"signed_in": False}


# --- reads -------------------------------------------------------------------


def _guard(work, doing: str = "reading") -> dict:
    """Run a read, and turn any failure into something the page can show.

    An expired or missing session is not an error and is not logged as one. It
    is the ordinary end of an hour's work, and the page answers it by sending
    the person back to sign in.
    """
    started = time.monotonic()
    try:
        return {"ok": True, "rows": work(), "ms": int((time.monotonic() - started) * 1000)}
    except SignedOut:
        return {"ok": False, "rows": [], "signed_out": True,
                "error": "Your session has ended. Sign in again to continue."}
    except Exception as exc:
        return {"ok": False, "rows": [], "error": _explain(exc, doing)}


@router.get("/episodes")
def episodes(
    limit: int = 20,
    unlabelled_only: bool = False,
    mibel_session: str | None = Cookie(default=None),
) -> dict:
    return _guard(
        lambda: actions_for(_session(mibel_session)).list_episodes(
            limit=limit, unlabelled_only=unlabelled_only
        ),
        "listing episodes",
    )


@router.get("/alerts")
def alerts(mibel_session: str | None = Cookie(default=None)) -> dict:
    return _guard(
        lambda: actions_for(_session(mibel_session)).my_alerts(), "reading your alerts"
    )


@router.get("/labels")
def labels(mibel_session: str | None = Cookie(default=None)) -> dict:
    return _guard(
        lambda: actions_for(_session(mibel_session)).my_labels(),
        "reading your judgements",
    )


@router.get("/activity")
def activity(mibel_session: str | None = Cookie(default=None)) -> dict:
    """What the agent has been doing, for the analytics panel.

    Reads from `agent_actions` directly rather than from the Delta side, because
    the point of this panel is what happened a moment ago. The Delta copy, which
    arrives through the change feed, is what the daily aggregates are built from.

    Everybody's activity, not only this session's: the panel is about how the
    agent behaves, and a success rate computed over one person's afternoon says
    less than one computed over everybody who has used it.
    """
    return _guard(
        lambda: _signed_in_store(_session(mibel_session)).query(
            """
            SELECT tool,
                   status,
                   count(*) AS calls,
                   round(avg(latency_ms)) AS avg_ms
            FROM iberian.agent_actions
            WHERE created_at > now() - interval '7 days'
            GROUP BY tool, status
            ORDER BY calls DESC
            """
        ),
        "reading the activity log",
    )


def _signed_in_store(session: tuple[str, str] | None):
    """The database, but only for somebody who named themselves."""
    if session is None:
        raise SignedOut()
    return store()


# --- writes ------------------------------------------------------------------


def _result(result) -> dict:
    return {
        "status": result.status,
        "message": result.message,
        "row": result.row,
        "tool": result.tool,
        "ms": result.latency_ms,
    }


class NewAlert(BaseModel):
    zone: str
    direction: str
    threshold_eur_mwh: float


@router.post("/alerts")
def create_alert(
    body: NewAlert,
    mibel_session: str | None = Cookie(default=None),
) -> dict:
    actions = actions_for(_session(mibel_session))
    return _result(
        actions.create_alert(body.zone, body.direction, body.threshold_eur_mwh)
    )


@router.delete("/alerts/{alert_id}")
def delete_alert(
    alert_id: int,
    confirmed: bool = False,
    mibel_session: str | None = Cookie(default=None),
) -> dict:
    """Refuses without `confirmed`, and says what would go.

    The same two step the agent goes through, on purpose: the button and the
    conversation are the same action with the same safeguard, so a demonstration
    of one is a demonstration of both.
    """
    actions = actions_for(_session(mibel_session))
    return _result(actions.delete_alert(alert_id, confirmed=confirmed))


class NewLabel(BaseModel):
    episode_key: str
    true_cause: str
    confidence: str
    notes: str = ""


@router.post("/labels")
def submit_label(
    body: NewLabel,
    mibel_session: str | None = Cookie(default=None),
) -> dict:
    actions = actions_for(_session(mibel_session))
    return _result(
        actions.submit_episode_label(
            body.episode_key, body.true_cause, body.confidence, body.notes
        )
    )


# --- the agent ---------------------------------------------------------------


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict[str, Any]] = Field(default_factory=list)


#: Enough for a confirmation to have context, short enough that a long session
#: does not send a novel to the model on every turn.
HISTORY_TURNS = 8


@router.post("/ask")
def ask(
    body: Question,
    mibel_session: str | None = Cookie(default=None),
) -> dict:
    """One question, if the budget allows it.

    The order here is the whole point. The session is checked, then the budget,
    and only then is anything sent to the model. A refusal on either costs
    nothing, which is what makes the budget a cost control rather than a
    politeness.
    """
    from iberian.app.assistant import Assistant
    from iberian.app.limits import BUDGET

    session = _session(mibel_session)
    if session is None:
        return {
            "ok": False,
            "signed_out": True,
            "reply": "Your session has ended. Sign in again to continue.",
            "results": [],
        }

    session_id = session[1]
    verdict = BUDGET.check(session_id)
    if not verdict.allowed:
        # Not an error, and not logged as one. The person asked a reasonable
        # question and the answer is that this demonstration has a budget.
        return {
            "ok": False,
            "refused": True,
            "limit": verdict.limit,
            "reply": verdict.message,
            "remaining": BUDGET.remaining(session_id),
            "results": [],
        }

    try:
        assistant = Assistant(actions=actions_for(session), chat=chat())
        turn = assistant.ask(
            body.question,
            history=[
                {"role": message.get("role"), "content": message.get("content", "")}
                for message in body.history[-HISTORY_TURNS:]
                if message.get("role") in {"user", "assistant"}
            ],
        )
        # Spent after the call returned, not before. A question the endpoint
        # never answered, because it was down or the credential was wrong, is
        # not one anybody should be charged for.
        BUDGET.spend(session_id)
    except Exception as exc:
        # The endpoint being down, the credential being wrong, the network. The
        # person gets a sentence; the page keeps its conversation.
        return {
            "ok": False,
            "reply": "I could not reach the model just now. Nothing was changed.",
            "error": _explain(exc, "asking the model"),
            "results": [],
        }

    return {
        "ok": not turn.stopped_early,
        "reply": turn.reply,
        "rounds": turn.rounds,
        "wrote": turn.wrote_anything,
        "remaining": BUDGET.remaining(session_id),
        "results": [
            _result(result) for result in turn.results if hasattr(result, "status")
        ],
    }