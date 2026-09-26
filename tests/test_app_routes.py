"""The doors: which pages are behind a name, and what happens without one.

Driven through the real application rather than by calling the route functions,
because what is being checked is the wiring. A redirect that works when you call
the function and not when a browser asks for the page is the failure this is
here to catch.

Lakebase is never reached. These tests stop at the point where a route would
need it, which is exactly where the session question is already decided.
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
from iberian.app.session import sign  # noqa: E402


@pytest.fixture()
def client():
    # follow_redirects off: the redirect itself is the thing under test, and a
    # client that follows it silently turns a failure into a passing test.
    with fastapi_testclient.TestClient(app, follow_redirects=False) as running:
        yield running


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


def test_the_sign_in_page_says_it_is_not_a_login(client):
    """A claim on the page, so it is a claim a test can hold to."""
    assert "not a login" in client.get("/signin").text.lower()


def test_the_workbench_without_a_session_goes_to_sign_in(client):
    response = client.get("/workbench")
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"


def test_the_workbench_with_a_forged_cookie_goes_to_sign_in(client):
    client.cookies.set("mibel_session", "Ana.s1.deadbeef")
    assert client.get("/workbench").status_code == 303


def test_the_workbench_with_a_valid_session_is_served(client):
    client.cookies.set("mibel_session", sign("Ana", "s1"))
    response = client.get("/workbench")
    assert response.status_code == 200
    assert "MIBEL workbench" in response.text


def test_the_workbench_is_not_cached(client):
    """Otherwise the back button shows the previous person's page after a sign out."""
    client.cookies.set("mibel_session", sign("Ana", "s1"))
    assert client.get("/workbench").headers["cache-control"] == "no-store"


# --- starting and ending a session ---------------------------------------------


def test_starting_a_session_sets_a_signed_http_only_cookie(client):
    response = client.post("/api/session", json={"name": "Ana"})
    assert response.status_code == 200
    assert response.json()["name"] == "Ana"

    header = response.headers["set-cookie"]
    assert "mibel_session=" in header
    assert "HttpOnly" in header
    # The name must not be sitting in the cookie in the clear, or the signature
    # is decoration.
    assert "=Ana" not in header


def test_the_session_route_reports_the_name_the_server_verified(client):
    client.post("/api/session", json={"name": "Ana Ferreira"})
    assert client.get("/api/session").json() == {"signed_in": True, "name": "Ana Ferreira"}


def test_the_session_route_is_honest_when_there_is_none(client):
    assert client.get("/api/session").json() == {"signed_in": False, "name": ""}


def test_an_empty_name_becomes_the_anonymous_one(client):
    assert client.post("/api/session", json={"name": "   "}).json()["name"] == "guest"


def test_two_sessions_for_the_same_name_are_different_sessions(client):
    first = client.post("/api/session", json={"name": "Ana"}).json()["session_id"]
    second = client.post("/api/session", json={"name": "Ana"}).json()["session_id"]
    assert first != second


def test_signing_out_clears_the_cookie_and_the_workbench_closes(client):
    client.post("/api/session", json={"name": "Ana"})
    assert client.get("/workbench").status_code == 200

    client.post("/api/signout")
    assert client.get("/api/session").json()["signed_in"] is False
    assert client.get("/workbench").status_code == 303


# --- no name, no writes --------------------------------------------------------


def test_a_read_without_a_session_says_so_rather_than_answering(client):
    """The failure that matters is the quiet one.

    Before this, a request with no cookie was served as "guest", so a visitor
    whose session had gone filed their judgements under a name they never chose
    and nothing said anything. Refusing is the point.
    """
    payload = client.get("/api/episodes").json()
    assert payload["ok"] is False
    assert payload["signed_out"] is True
    assert payload["rows"] == []


def test_every_read_answers_the_same_way(client):
    for route in ["/api/episodes", "/api/alerts", "/api/labels", "/api/activity"]:
        assert client.get(route).json()["signed_out"] is True, route


def _oauth(monkeypatch, **values):
    """Force the OAuth settings, so neither branch depends on a developer's .env.

    The first version of this test asserted whichever page happened to render,
    which passed in CI with nothing configured and failed on a machine that had
    sourced its .env. A test whose result depends on the environment is telling
    you about the environment, not the code.
    """
    for name in ("HOST", "CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.setattr(main, name, values.get(name, ""))


def test_the_diagnostic_names_what_is_missing_when_it_is_not_configured(
    client, monkeypatch
):
    """Nothing sits behind this flow, so an unconfigured deployment is ordinary.

    What it must not do is pretend to work, or fall over and take the page down.
    """
    _oauth(monkeypatch)
    response = client.get("/auth")

    assert response.status_code == 200
    assert "APP_OAUTH_CLIENT_ID" in response.text
    assert "/login" not in response.text


def test_the_diagnostic_offers_the_flow_once_it_is_configured(client, monkeypatch):
    _oauth(
        monkeypatch,
        HOST="https://example.cloud.databricks.com",
        CLIENT_ID="client",
        CLIENT_SECRET="secret",
        REDIRECT_URI="http://testserver/callback",
    )
    response = client.get("/auth")

    assert response.status_code == 200
    assert "/login" in response.text
    # And it must not leak the secret onto a page anybody can open.
    assert "secret" not in response.text


# --- the budget, through the route ---------------------------------------------


def test_the_agent_refuses_over_budget_without_calling_the_model(client, monkeypatch):
    """The assertion that makes this a cost control rather than a politeness.

    If the model is reached before the budget is checked, the refusal has
    already been paid for and the limit is decoration.
    """
    import app.api as api
    from iberian.app.limits import Budget

    called = []
    monkeypatch.setattr(api, "chat", lambda: (lambda *a, **k: called.append(1) or {}))
    monkeypatch.setattr("iberian.app.limits.BUDGET", Budget(per_session=0))

    client.post("/api/session", json={"name": "Ana"})
    payload = client.post("/api/ask", json={"question": "hello"}).json()

    assert payload["refused"] is True
    assert payload["limit"] == "session"
    assert called == [], "the model was called for a question that was refused"


def test_asking_without_a_session_is_a_sign_out_not_a_refusal(client):
    """Two different answers, and the page does different things with them."""
    payload = client.post("/api/ask", json={"question": "hello"}).json()

    assert payload["signed_out"] is True
    assert payload.get("refused") is None


def test_a_question_longer_than_the_cap_is_rejected_before_anything(client):
    """Pydantic, not us, but worth a test: it is the cheapest limit there is."""
    client.post("/api/session", json={"name": "Ana"})
    response = client.post("/api/ask", json={"question": "x" * 5000})

    assert response.status_code == 422