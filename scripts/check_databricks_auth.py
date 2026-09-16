"""Check whether a Databricks service principal can actually get a token.

Four rounds of credentials went by before it became clear that an OAuth app
integration and a service principal are different objects with different flows,
and that `invalid_client` fires during client authentication, before the server
ever looks at the requested scope. This script makes that distinction visible
in one command instead of a curl pasted from memory each time.

    python scripts/check_databricks_auth.py
    python scripts/check_databricks_auth.py --scope sql
    python scripts/check_databricks_auth.py --all-scopes

Reads DATABRICKS_HOST, DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET from
the environment. Nothing is printed that would leak the secret.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone

import requests

TOKEN_PATH = "/oidc/v1/token"
DISCOVERY_PATH = "/oidc/.well-known/oauth-authorization-server"

# The scopes worth trying when you do not yet know what the principal was given.
PROBE_SCOPES = ("postgres", "sql", "unity-catalog", "identity", "all-apis")

# Scopes that only make sense when a human signs in. A credential whose
# assigned scopes look like this is an OAuth app integration, not a service
# principal, and it will never work with client_credentials.
USER_FLOW_SCOPES = {"openid", "email", "profile", "offline_access"}


def settings() -> tuple[str, str, str]:
    host = (os.environ.get("DATABRICKS_HOST") or "").rstrip("/")
    client_id = os.environ.get("DATABRICKS_CLIENT_ID") or ""
    secret = os.environ.get("DATABRICKS_CLIENT_SECRET") or ""

    missing = [
        name
        for name, value in (
            ("DATABRICKS_HOST", host),
            ("DATABRICKS_CLIENT_ID", client_id),
            ("DATABRICKS_CLIENT_SECRET", secret),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing from the environment: "
            + ", ".join(missing)
            + "\nRun: export $(grep -v '^#' .env | xargs)"
        )
    if not host.startswith("https://"):
        raise SystemExit(f"DATABRICKS_HOST should start with https://, got {host!r}")
    return host, client_id, secret


def decode_claims(token: str) -> dict:
    """Read a JWT payload without verifying it.

    The signature is the server's business. What is useful here is which
    identity the token was issued to and which scopes actually came back,
    which is often narrower than what was asked for.
    """
    try:
        payload = token.split(".")[1]
    except IndexError:
        return {}
    padding = "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:
        return {}


def request_token(host: str, client_id: str, secret: str, scope: str) -> tuple[bool, dict]:
    try:
        response = requests.post(
            host + TOKEN_PATH,
            auth=(client_id, secret),
            data={"grant_type": "client_credentials", "scope": scope},
            timeout=30,
        )
    except requests.RequestException as exc:
        # A wrong or unreachable host is a different problem from a rejected
        # credential, and it should not look like one.
        return False, {"error": "unreachable", "error_description": str(exc)[:200]}
    try:
        body = response.json()
    except ValueError:
        body = {"error": "non_json_response", "error_description": response.text[:300]}
    return response.ok and "access_token" in body, body


def explain(body: dict) -> str:
    """Turn the OAuth error into the thing that actually has to change."""
    error = body.get("error", "")
    if error == "unreachable":
        return (
            "Could not reach the token endpoint at all, so this says nothing\n"
            "     about the credentials. Check DATABRICKS_HOST.\n"
            f"     {body.get('error_description', '')}"
        )
    if error == "invalid_client":
        return (
            "Client authentication failed, which happens BEFORE the scope is\n"
            "     read. So the scope is not the cause. Either the secret is wrong\n"
            "     or revoked, or these are not service principal credentials at\n"
            "     all. An OAuth app integration fails exactly like this, because\n"
            "     it is built for the authorization code flow."
        )
    if error == "invalid_scope":
        return "The credentials are valid. This scope is not assigned to them."
    if error == "unauthorized_client":
        return (
            "The principal exists but is not allowed to use client_credentials.\n"
            "     That is the signature of an app integration rather than a\n"
            "     service principal."
        )
    return body.get("error_description") or "No further detail from the server."


def show_discovery(host: str) -> None:
    try:
        response = requests.get(host + DISCOVERY_PATH, timeout=15)
        document = response.json()
    except Exception as exc:
        print(f"  discovery unavailable ({exc}), skipping")
        return

    print(f"  token endpoint:  {document.get('token_endpoint')}")
    grants = document.get("grant_types_supported", [])
    print(f"  client_credentials supported: {'client_credentials' in grants}")
    scopes = document.get("scopes_supported", [])
    interesting = [s for s in PROBE_SCOPES if s in scopes]
    print(f"  relevant scopes advertised: {', '.join(interesting) or 'none'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="postgres")
    parser.add_argument(
        "--all-scopes",
        action="store_true",
        help="try each candidate scope, to separate a scope problem from an auth one",
    )
    parser.add_argument("--discovery", action="store_true", help="print what the workspace advertises")
    args = parser.parse_args()

    host, client_id, secret = settings()

    print(f"Workspace: {host}")
    print(f"Client id: {client_id[:8]}... ({len(client_id)} chars)")
    print(f"Secret:    {secret[:4]}...{secret[-4:]} ({len(secret)} chars)\n")

    if args.discovery:
        print("Workspace OIDC discovery")
        show_discovery(host)
        print()

    scopes = PROBE_SCOPES if args.all_scopes else (args.scope,)
    succeeded: list[tuple[str, dict]] = []

    for scope in scopes:
        ok, body = request_token(host, client_id, secret, scope)
        if ok:
            print(f"  {scope:<16} OK")
            succeeded.append((scope, body))
        else:
            print(f"  {scope:<16} {body.get('error', 'failed')}")

    if not succeeded:
        print()
        _, body = request_token(host, client_id, secret, scopes[0])
        print(f"No token. {explain(body)}")
        return 1

    scope, body = succeeded[0]
    claims = decode_claims(body["access_token"])

    print(f"\nToken issued for scope '{scope}'.")
    if body.get("expires_in"):
        print(f"  expires in: {body['expires_in']}s")
    if claims:
        granted = claims.get("scope", "")
        print(f"  subject:    {claims.get('sub', 'unknown')}")
        print(f"  granted:    {granted or 'not stated'}")
        if claims.get("exp"):
            when = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
            print(f"  valid to:   {when:%Y-%m-%d %H:%M:%S} UTC")

        assigned = set(granted.split())
        if assigned and assigned <= USER_FLOW_SCOPES:
            print(
                "\n  WARNING: every granted scope is a user sign in scope. These\n"
                "  are app integration credentials, and they will not reach data."
            )

    if os.environ.get("PGHOST"):
        print("\nPGHOST is set. Next step is connecting to Lakebase with this")
        print("token as the password. That is a separate grant from getting the")
        print("token, so it can still fail even from here.")
    else:
        print("\nAuth works. To reach data you still need the Lakebase endpoint")
        print("(PGHOST, PGDATABASE, PGUSER) and a Postgres role with grants on")
        print("the gold tables. Getting a token does not imply either.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
