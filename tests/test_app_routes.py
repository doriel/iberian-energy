"""The doors: who gets in, what the server keeps, and when a session ends.

Driven through the real application rather than by calling the route functions,
because what is being checked is the wiring. A redirect that works when you call
the function and not when a browser asks for the page is the failure this is
here to catch.

Nothing here reaches Databricks or Lakebase. The point where a route would need
either is exactly the point where the session question is already decided, so
the tests stop there, and the one place that does call out is replaced by a
stand in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# importorskip is not enough here. A missing HTTP client makes Starlette raise
# RuntimeError from inside the module rather than ImportError, which
# importorskip does not catch, so the whole suite fails to collect over a test
# dependency. Which client it wants has moved too: current Starlette asks for
# httpx2, Starlette 1.0 and earlier for httpx. Both are in requirements.txt, so
# this should not skip in CI; if it ever does, the reason is printed rather
# than swallowed.
try:
    from fastapi import testclient as fastapi_testclient
except (ImportError, RuntimeError) as exc:  # pragma: no cover - environment
    pytest.skip(
        f"fastapi.testclient is unusable here, so the route tests cannot run: {exc}",
        allow_module_level=True,
    )

import app.main as main  # noqa: E402
from app.main import app  # noqa: E402
from iberian.app.session import SESSIONS, sign  # noqa: E402

REVIEWER = "ta@dataexpert.io"


@pytest.fixture()
def client():
    # follow_redirects off: the redirect itself is the thing under test, and a
    # client that follows it silently turns a failure into a passing test.
    with fastapi_testclient.TestClient(app, follow_redirects=False) as running:
        yield running


@pytest.fixture(autouse=True)
def empty_sessions():
    SESSIONS._sessions.clear()
    yield
    SESSIONS._sessions.clear()


def signed_in(client, email: str = REVIEWER) -> str:
    """Put a live session in place without going near Databricks."""
    session_id = SESSIONS.open(email=email, database_credential="not-a-real-token")
    client.cookies.set("mibel_session", sign(email, session_id))
    return session_id


# --- the public half, which must never ask for anything ------------------------


def test_the_dashboard_needs_no_session(client):
    assert client.get("/").status_code == 200


def test_the_dashboard_links_to_the_workbench(client):
    assert 'href="/workbench"' in client.get("/").text


def test_health_needs_no_session(client):
    assert client.get("/healthz").status_code == 200


# --- the door ------------------------------------------------------------------


def test_the_sign_in_page_is_served(client):
    assert client.get("/signin").status_code == 200


def test_the_sign_in_page_sends_you_to_databricks(client):
    """No name field any more. The identity has to be one somebody verified."""
    body = client.get("/signin").text
    assert 'action="/login"' in body
    assert "Sign in with Databricks" in body


def test_the_sign_in_page_says_what_it_keeps(client):
    """A claim on the page, so it is a claim a test can hold to."""
    assert "does not store it" in client.get("/signin").text


def test_the_workbench_without_a_session_goes_to_sign_in(client):
    response = client.get("/workbench")
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"


def test_the_workbench_with_a_forged_cookie_goes_to_sign_in(client):
    client.cookies.set("mibel_session", f"{REVIEWER}.s1.deadbeef")
    assert client.get("/workbench").status_code == 303


def test_a_signed_cookie_whose_session_is_gone_does_not_open_anything(client):
    """The cookie outlives the credential on purpose, so this path is ordinary.

    Signed, unforged, and still refused, because the store is what decides.
    """
    client.cookies.set("mibel_session", sign(REVIEWER, "a-session-that-ended"))
    assert client.get("/api/session").json()["signed_in"] is False
    assert client.get("/api/episodes").json()["signed_out"] is True


def test_the_workbench_with_a_live_session_is_served(client):
    signed_in(client)
    response = client.get("/workbench")
    assert response.status_code == 200
    assert "MIBEL workbench" in response.text


def test_the_workbench_is_not_cached(client):
    """Otherwise the back button shows the previous person's page after a sign out."""
    signed_in(client)
    assert client.get("/workbench").headers["cache-control"] == "no-store"


# --- what the server reports ---------------------------------------------------


def test_the_session_route_reports_the_verified_email(client):
    signed_in(client)
    assert client.get("/api/session").json() == {"signed_in": True, "email": REVIEWER}


def test_the_session_route_is_honest_when_there_is_none(client):
    assert client.get("/api/session").json() == {"signed_in": False, "email": ""}


def test_signing_out_drops_the_credential_server_side(client):
    """Not just the cookie. Signing out has to end the database access."""
    session_id = signed_in(client)
    assert SESSIONS.get(session_id) is not None

    client.post("/api/signout")
    assert SESSIONS.get(session_id) is None
    assert client.get("/workbench").status_code == 303


# --- the callback, which is where a session is born ----------------------------


class FakeExchange:
    """Stands in for the token endpoint. Records what it was asked."""

    def __init__(self, body):
        self.body = body
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))

        class Response:
            def json(inner):
                return dict(self.body)

        return Response()


def start_flow(client):
    """Begin a sign in so the callback has a state to answer."""
    client.get("/login")
    return next(iter(main._PENDING))


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(main, "HOST", "https://example.cloud.databricks.com")
    monkeypatch.setattr(main, "CLIENT_ID", "client")
    monkeypatch.setattr(main, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(main, "REDIRECT_URI", "http://testserver/callback")
    monkeypatch.setattr(main, "AUTHORIZE_URL", "https://example.cloud.databricks.com/oidc/v1/authorize")
    main._PENDING.clear()
    yield monkeypatch
    main._PENDING.clear()


#: A token whose payload decodes to a subject. The signature is nonsense and is
#: never checked here, which is the documented behaviour of decode_claims.
def token_for(email: str) -> str:
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"sub": email}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_a_successful_callback_opens_a_session_and_redirects(client, configured):
    configured.setattr(
        main.requests, "post", FakeExchange({"access_token": token_for(REVIEWER)})
    )
    configured.setattr(main, "database_credential_for", lambda token: "db-credential")

    state = start_flow(client)
    response = client.get(f"/callback?code=abc&state={state}")

    assert response.status_code == 303
    assert response.headers["location"] == "/workbench"
    assert len(SESSIONS) == 1


def test_the_session_holds_the_database_credential_not_the_platform_token(
    client, configured
):
    """The security claim the sign in page makes, as a test.

    If this ever fails, the page is lying to the people it asks to sign in.
    """
    platform_token = token_for(REVIEWER)
    configured.setattr(main.requests, "post", FakeExchange({"access_token": platform_token}))
    configured.setattr(main, "database_credential_for", lambda token: "db-credential")

    state = start_flow(client)
    client.get(f"/callback?code=abc&state={state}")

    identity = next(iter(SESSIONS._sessions.values()))
    assert identity.email == REVIEWER
    assert identity.database_credential == "db-credential"
    assert platform_token not in vars(identity).values()


def test_the_platform_token_is_what_generates_the_credential(client, configured):
    """And it is the person's token, not the process's."""
    seen = []
    configured.setattr(
        main.requests, "post", FakeExchange({"access_token": token_for(REVIEWER)})
    )
    configured.setattr(
        main, "database_credential_for", lambda token: seen.append(token) or "db"
    )

    state = start_flow(client)
    client.get(f"/callback?code=abc&state={state}")

    assert seen == [token_for(REVIEWER)]


def test_a_token_naming_nobody_is_refused(client, configured):
    """No subject means no identity, and an unattributed judgement is worthless."""
    configured.setattr(main.requests, "post", FakeExchange({"access_token": "header..sig"}))

    state = start_flow(client)
    response = client.get(f"/callback?code=abc&state={state}")

    assert "names nobody" in response.text
    assert len(SESSIONS) == 0


def test_no_session_is_opened_when_lakebase_refuses(client, configured):
    """Signed in to Databricks is not the same as having a database role."""

    def refuse(token):
        raise RuntimeError('role "ta@dataexpert.io" does not exist')

    configured.setattr(
        main.requests, "post", FakeExchange({"access_token": token_for(REVIEWER)})
    )
    configured.setattr(main, "database_credential_for", refuse)

    state = start_flow(client)
    response = client.get(f"/callback?code=abc&state={state}")

    assert response.status_code == 200
    assert "does not exist" in response.text
    assert len(SESSIONS) == 0


def test_a_replayed_state_cannot_open_a_second_session(client, configured):
    configured.setattr(
        main.requests, "post", FakeExchange({"access_token": token_for(REVIEWER)})
    )
    configured.setattr(main, "database_credential_for", lambda token: "db")

    state = start_flow(client)
    client.get(f"/callback?code=abc&state={state}")
    again = client.get(f"/callback?code=abc&state={state}")

    assert "Unknown state" in again.text
    assert len(SESSIONS) == 1