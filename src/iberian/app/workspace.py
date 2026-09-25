"""Which identity this process acts as, decided here rather than by the SDK.

The SDK resolves credentials from the environment on its own, and that
resolution is not the one this application wants. With `DATABRICKS_CLIENT_ID`
and `DATABRICKS_CLIENT_SECRET` set for a service principal, and a CLI profile
also present, it picked the profile: `auth_type` came back as `databricks-cli`
and the workspace saw a person rather than the application.

That failure is quiet in the worst way. Generating a database credential
succeeds, because the person is allowed to do that, and the connection is then
refused with `OAuth: User is not authorized`, because the credential belongs to
one identity and the Postgres role name to another. The error names neither, and
nothing in it suggests looking at which profile happened to be in the shell.

So this module builds a client from an explicit `Config`, pinned to
`oauth-m2m`, whenever the service principal's credentials are present. Nothing
ambient is consulted and a profile in the environment cannot win.

Two callers, and both must agree: `lakebase.py`, which generates database
credentials, and `assistant.py`, which calls the serving endpoint. An
application acting as two identities depending on which module you are in is a
bug waiting for a deadline.

Without those variables it falls back to the SDK's own resolution, which is
correct in a notebook: there the identity is the person running it, and that is
who the Lakebase role belongs to.
"""

from __future__ import annotations

import os

#: The SDK's name for service principal OAuth. Pinned rather than left to be
#: inferred, so a different credential in the environment cannot change it.
SERVICE_PRINCIPAL_AUTH = "oauth-m2m"


def service_principal_configured() -> bool:
    return bool(
        os.environ.get("DATABRICKS_CLIENT_ID")
        and os.environ.get("DATABRICKS_CLIENT_SECRET")
        and os.environ.get("DATABRICKS_HOST")
    )


def service_principal_settings() -> dict | None:
    """Exactly what the SDK should be told, or None to let it decide.

    Separated from building the client because constructing a `Config` fetches
    a token, which makes it useless in a test. The decision is the part worth
    testing, and it is all here, in a dictionary, with no network anywhere near
    it.
    """
    if not service_principal_configured():
        return None
    return {
        "host": os.environ["DATABRICKS_HOST"],
        "client_id": os.environ["DATABRICKS_CLIENT_ID"],
        "client_secret": os.environ["DATABRICKS_CLIENT_SECRET"],
        "auth_type": SERVICE_PRINCIPAL_AUTH,
        # Explicitly nothing. A profile in the environment is what caused this
        # module to exist, and passing None is what stops Config reading
        # DATABRICKS_CONFIG_PROFILE for itself.
        "profile": None,
    }


def workspace_client():
    """A client for whoever this process should be.

    Imported late so this module stays importable, and testable, without the
    SDK installed.
    """
    from databricks.sdk import WorkspaceClient

    settings = service_principal_settings()
    if settings is None:
        # A notebook, or a developer's machine before the application variables
        # are set. The SDK's own resolution is right there.
        return WorkspaceClient()

    from databricks.sdk.core import Config

    return WorkspaceClient(config=Config(**settings))


def acting_as(client) -> str:
    """Who the workspace thinks this is. For start up logs and diagnostics.

    Worth printing once on a deployment. The difference between the application
    and the person who deployed it is invisible until something is refused.
    """
    try:
        return client.current_user.me().user_name or "unknown"
    except Exception as exc:  # pragma: no cover - diagnostic only
        return f"unknown ({type(exc).__name__})"