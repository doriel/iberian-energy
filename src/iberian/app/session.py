"""Who the visitor says they are, in a cookie the server can check.

This is not authentication and the interface says so out loud. There is no
password, anybody may enter any name, and nothing is authorised by it. What it
is for: keeping one person's alerts and one person's judgements apart from
everybody else's, so that two reviewers labelling the same episode produce two
rows instead of overwriting each other.

So why sign it at all, if a forged cookie only reaches another name's rows?

**Because the name has to survive the round trip unchanged.** An unsigned cookie
is a string the browser hands back, and a typo, a proxy, or a curious visitor
editing it in the console all produce a `created_by` that no longer matches
anything. Signing makes a tampered cookie fail cleanly at the door instead of
quietly opening a third identity halfway through a session.

**And because the secret is generated at start up, on purpose.** It lives in this
process and nowhere else, so restarting the server invalidates every session that
existed before it. That is the behaviour the interface promises: stop the app,
start it again, and you are asked who you are. A secret read from the environment
would persist across restarts and quietly break that promise.

The consequence is worth stating rather than discovering: on a host that sleeps
when idle or restarts on deploy, every visitor is asked their name again when it
wakes. For a page whose sign in is one text field, that is a fair trade for the
sessions actually ending when the process does.
"""

from __future__ import annotations

import base64
import hmac
import secrets
from hashlib import sha256

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