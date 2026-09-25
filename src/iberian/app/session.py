"""Who the visitor is, proved by Databricks, and what the server keeps about it.

The workspace refuses to let this project create a service principal, so the
deployed application has no identity of its own and cannot reach Lakebase by
itself. It therefore acts as whoever signs in: the person authenticates against
Databricks, and their own permissions are what the database sees. Everyone who
needs this application has an account in that workspace, so the limitation costs
nothing here and buys a verified `created_by` on every row, which matters because
those rows are evaluation ground truth.

Two pieces, and the split is the security design:

**A signed cookie** carrying the email and a session id, and nothing else. It
proves the browser holds a session this server issued. Signing is what stops the
email being edited in the console, which would otherwise put somebody else's
name on a judgement without touching the server.

**A store, in memory, holding one database credential per session.** The
platform token that comes back from the OAuth exchange carries the person's full
workspace permissions. This application does not keep it. It is used once, in the
callback, to generate a Lakebase credential, and then dropped. What remains can
open a Postgres connection as that person and do nothing else, so an attacker who
reads this process's memory gets database access rather than a workspace.

The cost of that choice, stated rather than discovered: Lakebase credentials last
sixty minutes and this application cannot mint a new one without the platform
token it threw away. So a session ends after an hour and the person signs in
again. For a reviewer working through a handful of episodes that is a click, and
it is a great deal easier to defend than storing workspace tokens on a public
host.

**The signing secret is generated at start up**, so restarting the server ends
every session. On a host that sleeps when idle or restarts on deploy, that means
signing in again when it wakes. Deliberate: the alternative is a secret that
outlives the process and a promise the interface cannot keep.

The store is a dictionary, which is correct for exactly one process. If this
ever runs behind more than one instance, a visitor's requests will land on an
instance that does not know them and they will be bounced to sign in at random.
Whoever adds a second instance has to move this to a shared store first.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable

#: New on every start, held only here. Restarting ends every session, which is
#: the point rather than a side effect.
SECRET = secrets.token_bytes(32)

#: Long enough that two visitors never collide, short enough to read in a log.
SESSION_ID_BYTES = 9

#: A name is a label on a row, not a document. Anything longer is a mistake or
#: an attempt to see what breaks.
MAX_NAME = 60

SEPARATOR = "."


def clean_name(raw: str | None) -> str:
    """A display name, or the anonymous one. Never empty, never enormous.

    Control characters go because this name is rendered in a page and written
    to a log, and a newline in either is somebody else's problem later.
    """
    text = "".join(character for character in (raw or "") if character.isprintable())
    return text.strip()[:MAX_NAME] or "guest"


def new_session_id() -> str:
    return secrets.token_urlsafe(SESSION_ID_BYTES)


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _decode(text: str) -> str:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding).decode("utf-8")


def sign(name: str, session_id: str) -> str:
    """The cookie value: the name, the session, and a signature over both.

    The name is base64 encoded rather than written plainly so that a separator
    inside somebody's name cannot shift where the fields begin.
    """
    payload = f"{_encode(name)}{SEPARATOR}{session_id}"
    signature = hmac.new(SECRET, payload.encode("ascii"), sha256).hexdigest()[:32]
    return f"{payload}{SEPARATOR}{signature}"


def verify(raw: str | None) -> tuple[str, str] | None:
    """The name and session in a cookie, or None if it is not ours.

    None covers every way this fails, and they are all the same answer to the
    caller: no cookie, a cookie from before the last restart, a cookie somebody
    edited, a cookie that is not three fields. The route sends the visitor to
    the sign in page and nothing here has to explain which it was.
    """
    if not raw:
        return None
    parts = raw.split(SEPARATOR)
    if len(parts) != 3:
        return None
    encoded_name, session_id, signature = parts

    payload = f"{encoded_name}{SEPARATOR}{session_id}"
    expected = hmac.new(SECRET, payload.encode("ascii"), sha256).hexdigest()[:32]
    # compare_digest rather than ==, so the comparison does not leak how much of
    # the signature was right. Cheap, and the habit is worth more than the case.
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        name = _decode(encoded_name)
    except Exception:
        return None
    if not name or not session_id:
        return None
    return name, session_id


# --- what the server keeps for a signed in person -----------------------------

#: Lakebase credentials are documented as lasting sixty minutes. Sessions are
#: held five minutes short of that, so a request that arrives just before the
#: line does not start work with a credential that expires mid connection.
CREDENTIAL_LIFETIME_SECONDS = 55 * 60

#: A ceiling on concurrent sessions. Not a security control, a memory one: this
#: runs on a small instance and an open sign in page is an open invitation.
MAX_SESSIONS = 200


@dataclass
class Identity:
    """One signed in person, and the one credential the server kept.

    `email` is what Databricks said, not what anybody typed, and it is both the
    name shown in the interface and the Postgres role the connection opens as.
    Those being the same string is not a coincidence to be tidied away later:
    it is what makes a row's `created_by` mean something.
    """

    email: str
    database_credential: str
    issued_at: float

    def expired(self, now: float, lifetime: float = CREDENTIAL_LIFETIME_SECONDS) -> bool:
        return now - self.issued_at >= lifetime


class Sessions:
    """Live sessions, by session id. One process, one dictionary.

    The clock is an argument so the expiry can be tested without sleeping for
    an hour, which is the only way a test of an expiry is worth writing.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        lifetime: float = CREDENTIAL_LIFETIME_SECONDS,
        limit: int = MAX_SESSIONS,
    ) -> None:
        self._sessions: dict[str, Identity] = {}
        self._clock = clock
        self._lifetime = lifetime
        self._limit = limit

    def open(self, email: str, database_credential: str) -> str:
        """Record a signed in person and return the id that names the session."""
        self._evict()
        session_id = new_session_id()
        self._sessions[session_id] = Identity(
            email=email,
            database_credential=database_credential,
            issued_at=self._clock(),
        )
        return session_id

    def get(self, session_id: str | None) -> Identity | None:
        """The live session, or None. An expired one is removed on the way out.

        None is the only failure this returns, because every caller does the
        same thing with it: send the person to sign in. Distinguishing "never
        existed" from "expired" would let a caller tell an attacker which
        session ids are real.
        """
        if not session_id:
            return None
        identity = self._sessions.get(session_id)
        if identity is None:
            return None
        if identity.expired(self._clock(), self._lifetime):
            self._sessions.pop(session_id, None)
            return None
        return identity

    def close(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def _evict(self) -> None:
        """Drop what has expired, and if still over the limit, the oldest.

        Expiry first, so a burst of sign ins does not throw out live sessions
        while dead ones sit in the dictionary holding their place.
        """
        now = self._clock()
        for session_id, identity in list(self._sessions.items()):
            if identity.expired(now, self._lifetime):
                del self._sessions[session_id]

        while len(self._sessions) >= self._limit:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].issued_at)
            del self._sessions[oldest]

    def __len__(self) -> int:
        return len(self._sessions)


#: The application's one store. A module level instance rather than something
#: passed around, because it has to be the same object for the callback that
#: writes it and the routes that read it, and those live in different modules.
SESSIONS = Sessions()