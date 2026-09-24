"""The credential lifecycle, which is the part that fails an hour after deploy.

Everything else in `lakebase.py` is a thin wrapper over the driver. The reason
this file exists is the sixty minute expiry: a credential fetched once and kept
works perfectly until it does not, at a moment nobody is watching, and the
symptom is an application that reads fine and silently stops writing.

No network, no database, no SDK. The class takes its connector and its
credential factory as arguments precisely so this runs in milliseconds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.app.lakebase import CREDENTIAL_TTL_SECONDS, Lakebase  # noqa: E402


class FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description
        self.executed: list[tuple] = []

    def execute(self, statement, args=()):
        self.executed.append((statement, tuple(args)))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, rows=(), description=None):
        self.rows = list(rows)
        self.description = description
        self.closed = False
        self.cursors: list[FakeCursor] = []

    def cursor(self):
        cursor = FakeCursor(self.rows, self.description)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class Clock:
    """A clock the test moves by hand, so expiry is exercised without waiting."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(connect=None, tokens=None, clock=None):
    issued = list(tokens or ["token-1", "token-2", "token-3"])
    handed_out: list[str] = []

    def factory() -> str:
        token = issued.pop(0)
        handed_out.append(token)
        return token

    store = Lakebase(
        host="endpoint.example",
        user="someone@example.com",
        credential_factory=factory,
        connect=connect or (lambda host, user, password, dbname: FakeConnection()),
        clock=clock or Clock(),
    )
    return store, handed_out


# --- the credential ----------------------------------------------------------


def test_the_credential_is_fetched_once_and_reused():
    store, handed_out = build()
    for _ in range(5):
        store.credential()
    assert handed_out == ["token-1"], "one credential should serve many calls"


def test_the_credential_is_refetched_after_the_ttl():
    clock = Clock()
    store, handed_out = build(clock=clock)

    store.credential()
    clock.advance(CREDENTIAL_TTL_SECONDS - 1)
    store.credential()
    assert handed_out == ["token-1"], "still inside the window"

    clock.advance(2)
    store.credential()
    assert handed_out == ["token-1", "token-2"]


def test_the_ttl_leaves_room_before_the_real_expiry():
    """Sixty minutes is what Databricks grants. This must be comfortably less.

    Not a style preference: a request that begins at minute fifty nine with a
    credential the code still considers valid fails against a server that has
    already expired it.
    """
    assert CREDENTIAL_TTL_SECONDS < 60 * 60
    assert 60 * 60 - CREDENTIAL_TTL_SECONDS >= 10 * 60


# --- the retry, which is the whole point -------------------------------------


def test_a_refused_credential_is_retried_once_with_a_fresh_one():
    attempts: list[str] = []

    def connect(host, user, password, dbname):
        attempts.append(password)
        if len(attempts) == 1:
            raise RuntimeError(
                'connection failed: ERROR:  password authentication failed for user "x"'
            )
        return FakeConnection()

    store, handed_out = build(connect=connect)
    with store.connection() as handle:
        assert isinstance(handle, FakeConnection)

    assert attempts == ["token-1", "token-2"], "the retry used a new credential"
    assert handed_out == ["token-1", "token-2"]


def test_a_refused_credential_is_not_retried_twice():
    """A credential refused twice is configuration, and configuration does not
    improve by being asked again. Failing fast keeps the error legible."""
    attempts: list[str] = []

    def connect(host, user, password, dbname):
        attempts.append(password)
        raise RuntimeError("password authentication failed")

    store, _ = build(connect=connect)
    with pytest.raises(RuntimeError):
        with store.connection():
            pass

    assert len(attempts) == 2


def test_a_failure_that_is_not_about_credentials_is_raised_immediately():
    # Retrying a name that does not resolve, or a port that is closed, wastes a
    # second and buries the real message under a second identical one.
    attempts: list[str] = []

    def connect(host, user, password, dbname):
        attempts.append(password)
        raise OSError("could not translate host name to address")

    store, _ = build(connect=connect)
    with pytest.raises(OSError):
        with store.connection():
            pass

    assert len(attempts) == 1


# --- connections are not left open -------------------------------------------


def test_the_connection_is_closed_even_when_the_body_raises():
    handle = FakeConnection()
    store, _ = build(connect=lambda *a: handle)

    with pytest.raises(ValueError):
        with store.connection():
            raise ValueError("something in the caller")

    assert handle.closed, "a leaked connection holds a server side session open"


def test_a_connection_that_will_not_close_does_not_mask_the_real_result():
    class Stubborn(FakeConnection):
        def close(self):
            raise RuntimeError("already gone")

    store, _ = build(connect=lambda *a: Stubborn())
    with store.connection() as handle:
        assert isinstance(handle, Stubborn)


# --- rows --------------------------------------------------------------------


def test_rows_come_back_as_dictionaries():
    handle = FakeConnection(
        rows=[(1, "someone", "saturation_planned")],
        description=[("id",), ("created_by",), ("true_cause",)],
    )
    store, _ = build(connect=lambda *a: handle)

    assert store.query("SELECT ...") == [
        {"id": 1, "created_by": "someone", "true_cause": "saturation_planned"}
    ]


def test_a_statement_that_returns_nothing_is_an_empty_list_not_an_error():
    store, _ = build(connect=lambda *a: FakeConnection(rows=[], description=None))
    assert store.query("DELETE FROM ...") == []
    assert store.execute("DELETE FROM ...") is None


def test_execute_returns_the_stored_row_rather_than_what_was_asked_for():
    """Every write ends in RETURNING, so the interface shows what was stored.

    The two differ more often than it looks: a default fills in, a trigger sets
    updated_at, an upsert lands on a row that already existed.
    """
    handle = FakeConnection(
        rows=[(7, True)], description=[("id",), ("active",)]
    )
    store, _ = build(connect=lambda *a: handle)

    assert store.execute("INSERT ... RETURNING id, active") == {"id": 7, "active": True}


def test_arguments_are_passed_to_the_driver_rather_than_formatted_in():
    handle = FakeConnection(rows=[], description=None)
    store, _ = build(connect=lambda *a: handle)

    store.query("SELECT * FROM t WHERE k = %s", ("'; DROP TABLE t; --",))

    statement, args = handle.cursors[0].executed[0]
    assert "%s" in statement, "the value must not be interpolated into the SQL"
    assert args == ("'; DROP TABLE t; --",)