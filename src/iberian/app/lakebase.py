"""Talking to Lakebase from a process that does not run inside Databricks.

The application is deployed on Render, so it authenticates as a service
principal and reaches the endpoint's public ingress. That works because the
endpoint's DNS is split horizon: inside the workspace the name resolves to a
private address over PrivateLink, and from the internet to a public load
balancer. Worth writing down, because the private address is what a notebook
sees and it looks alarming if you meet it first.

**Credentials last sixty minutes.** That single fact shapes this file. A
credential fetched at start up and kept in a module variable works for an hour
and then the application stops writing, at a moment nobody is watching. So the
credential is cached with an expiry well inside the real one, and any connection
that is refused for authentication is retried once with a fresh credential
before the failure is believed.

**No connection pool, deliberately.** A pool is the right answer for an
application under load, and this one serves a handful of clicks an hour. A pool
whose connections outlive the credential that opened them is a subtle bug, and
adding one to avoid a few hundred milliseconds of connection setup would buy
latency nobody can perceive at the cost of a failure mode that appears an hour
after deployment. If the traffic ever justifies it, the endpoint already
publishes a pooled host, which is what that host is for.

Nothing here knows about episodes, alerts or labels. It opens connections, runs
statements and records what happened. The operations live next door.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence

#: How long a credential is trusted for, against a real life of sixty minutes.
#: The margin covers a request that starts just before expiry and a clock that
#: disagrees with Databricks by a few minutes.
CREDENTIAL_TTL_SECONDS = 40 * 60

#: What a refused authentication looks like, whatever the driver wraps it in.
#: Matched on the message rather than the exception type because the type
#: differs between psycopg versions and the message has been stable.
_AUTH_FAILURE_MARKERS = (
    "password authentication failed",
    "authentication failed",
    "token is expired",
    "invalid token",
)


def _looks_like_auth_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _AUTH_FAILURE_MARKERS)


def databricks_credentials(endpoint: str) -> Callable[[], str]:
    """A credential factory backed by the SDK, as the deployed app uses.

    Imported inside the function so this module stays importable, and testable,
    without the SDK installed. The test suite must not need a workspace.

    The SDK picks up `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID` and
    `DATABRICKS_CLIENT_SECRET` from the environment on its own. Those are its
    own reserved names and they are the right ones here: this is the service
    principal the application acts as. They are not the same as the
    `APP_OAUTH_*` pair, which is the separate flow for signing a person in, and
    the two must not be given the same values.
    """

    def factory() -> str:
        from databricks.sdk import WorkspaceClient

        workspace = WorkspaceClient()
        return workspace.postgres.generate_database_credential(endpoint=endpoint).token

    return factory


def psycopg_connect(host: str, user: str, password: str, dbname: str):
    """The real connector. Also imported late, for the same reason."""
    import psycopg

    return psycopg.connect(
        host=host,
        port=5432,
        dbname=dbname,
        user=user,
        password=password,
        sslmode="require",
        connect_timeout=15,
        autocommit=True,
    )


class Lakebase:
    """Connections to one Lakebase endpoint, with the credential handled.

    `credential_factory` and `connect` are arguments rather than imports so a
    test can drive every path in this class, including the expiry and the retry,
    without a network or a database. That is not decoration: the retry on a
    refused credential is the one piece of this file that will run in anger, an
    hour after a deployment, and it needs to be covered by a test that runs in
    milliseconds.
    """

    def __init__(
        self,
        host: str,
        user: str,
        credential_factory: Callable[[], str],
        dbname: str = "databricks_postgres",
        connect: Callable[..., Any] = psycopg_connect,
        ttl_seconds: int = CREDENTIAL_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.user = user
        self.dbname = dbname
        self._credential_factory = credential_factory
        self._connect = connect
        self._ttl = ttl_seconds
        self._clock = clock
        self._token: str | None = None
        self._fetched_at = 0.0

    # --- credentials ---------------------------------------------------------

    def credential(self, force: bool = False) -> str:
        if force or self._token is None or self._clock() - self._fetched_at >= self._ttl:
            self._token = self._credential_factory()
            self._fetched_at = self._clock()
        return self._token

    # --- connections ---------------------------------------------------------

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """One connection, closed afterwards whatever happened.

        A refused authentication is retried once with a fresh credential. Once
        and no more: a credential that is refused twice is a configuration
        problem, and retrying a configuration problem in a loop turns a clear
        failure into a slow one.
        """
        try:
            handle = self._connect(self.host, self.user, self.credential(), self.dbname)
        except Exception as exc:
            if not _looks_like_auth_failure(exc):
                raise
            handle = self._connect(
                self.host, self.user, self.credential(force=True), self.dbname
            )
        try:
            yield handle
        finally:
            try:
                handle.close()
            except Exception:
                # A connection that will not close is already gone. Raising here
                # would replace whatever the caller was doing with a message
                # about tidying up.
                pass

    # --- statements ----------------------------------------------------------

    def query(self, statement: str, args: Sequence[Any] = ()) -> list[dict]:
        """Rows as dictionaries, because a tuple of nine values is unreadable."""
        with self.connection() as handle:
            with handle.cursor() as cursor:
                cursor.execute(statement, args)
                if cursor.description is None:
                    return []
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def execute(self, statement: str, args: Sequence[Any] = ()) -> dict | None:
        """A write. Returns the affected row when the statement returns one.

        Every write in this application ends in `RETURNING`, so the interface
        can show what was actually stored rather than echoing what was asked
        for. The two differ more often than it seems: a default fills in, a
        trigger sets a timestamp, an upsert lands on an existing row.
        """
        rows = self.query(statement, args)
        return rows[0] if rows else None