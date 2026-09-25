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

#: Sessions are opened by the Databricks callback in `app/main.py`, not here.
#: This module reads them and ends them.


# --- configuration -----------------------------------------------------------


class SignedOut(Exception):
    """No live session. Every route answers this the same way: sign in again."""


def store_for(identity):
    """A Lakebase client bound to one signed in person.

    Not cached, and not shared. Each session has its own credential and its own
    Postgres role, so a client cached for the process would hand one reviewer's
    connection to the next one and file their work under the wrong name.

    The credential factory closes over a credential that cannot be refreshed:
    this application threw away the token that would mint another. When it
    expires the session is already gone, because the store expires it five
    minutes earlier, and the person is sent back to sign in.
    """
    from iberian.app.lakebase import Lakebase

    host = os.environ.get("LAKEBASE_HOST")
    if not host:
        raise RuntimeError(
            "LAKEBASE_HOST is not set, so the workbench cannot reach its database."
        )
    return Lakebase(
        host=host,
        user=identity.email,
        credential_factory=lambda: identity.database_credential,
    )


@lru_cache(maxsize=1)
def chat():
    from iberian.app.assistant import databricks_chat

    return databricks_chat(
        endpoint=os.environ.get("AGENT_ENDPOINT", "databricks-claude-haiku-4-5")
    )


def actions_for(session_id: str):
    """The write surface for one live session, or SignedOut.

    `created_by` is the email Databricks verified, never anything the browser
    sent. That is what makes a stored judgement attributable.
    """
    from iberian.app.actions import Actions
    from iberian.app.session import SESSIONS

    identity = SESSIONS.get(session_id)
    if identity is None:
        raise SignedOut()
    return Actions(
        store=store_for(identity), session_id=session_id, created_by=identity.email
    )


# --- the visitor -------------------------------------------------------------


def _session_id(raw: str | None) -> str | None:
    """The session id in a verified cookie, or None.

    The email in the cookie is deliberately ignored here. It is signed, so it
    is not forged, but the store holds the authoritative copy and reading it
    from one place means the two can never disagree.
    """
    from iberian.app.session import verify

    found = verify(raw)
    return found[1] if found else None


@router.get("/session")
def current_session(mibel_session: str | None = Cookie(default=None)) -> dict:
    """Who the server believes you are, which is the only opinion that counts."""
    from iberian.app.session import SESSIONS

    identity = SESSIONS.get(_session_id(mibel_session))
    if identity is None:
        return {"signed_in": False, "email": ""}
    return {"signed_in": True, "email": identity.email}


@router.post("/signout")
def sign_out(response: Response, mibel_session: str | None = Cookie(default=None)) -> dict:
    """End the session and drop the credential with it.

    The credential is discarded server side rather than only cleared from the
    browser, so signing out actually ends the database access instead of hiding
    the way back to it.
    """
    from iberian.app.session import SESSIONS

    SESSIONS.close(_session_id(mibel_session))
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
        lambda: actions_for(_session_id(mibel_session)).list_episodes(
            limit=limit, unlabelled_only=unlabelled_only
        ),
        "listing episodes",
    )


@router.get("/alerts")
def alerts(mibel_session: str | None = Cookie(default=None)) -> dict:
    return _guard(
        lambda: actions_for(_session_id(mibel_session)).my_alerts(), "reading your alerts"
    )


@router.get("/labels")
def labels(mibel_session: str | None = Cookie(default=None)) -> dict:
    return _guard(
        lambda: actions_for(_session_id(mibel_session)).my_labels(),
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
        lambda: _signed_in_store(_session_id(mibel_session)).query(
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


def _signed_in_store(session_id: str | None):
    """The database, as the signed in person, or SignedOut."""
    from iberian.app.session import SESSIONS

    identity = SESSIONS.get(session_id)
    if identity is None:
        raise SignedOut()
    return store_for(identity)


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
    actions = actions_for(_session_id(mibel_session))
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
    actions = actions_for(_session_id(mibel_session))
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
    actions = actions_for(_session_id(mibel_session))
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
    from iberian.app.assistant import Assistant

    try:
        assistant = Assistant(actions=actions_for(_session_id(mibel_session)), chat=chat())
        turn = assistant.ask(
            body.question,
            history=[
                {"role": message.get("role"), "content": message.get("content", "")}
                for message in body.history[-HISTORY_TURNS:]
                if message.get("role") in {"user", "assistant"}
            ],
        )
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
        # Only the action results, not the rows a read returned: the page
        # already has the rows from its own endpoints, and shipping them twice
        # invites the two copies to disagree.
        "results": [
            _result(result) for result in turn.results if hasattr(result, "status")
        ],
    }