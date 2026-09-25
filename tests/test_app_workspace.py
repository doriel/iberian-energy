"""Which identity the application acts as.

One assertion matters here and the rest support it: when the service principal
is configured, nothing in the environment can make the application act as a
person. That is not hypothetical. A CLI profile did exactly that, the workspace
saw `doriel3572@gmail.com` instead of the application, and Lakebase refused the
connection with a message that mentioned neither.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.app import workspace  # noqa: E402

SP = "ac4f110e-0000-0000-0000-000000000000"


@pytest.fixture()
def clean(monkeypatch):
    for name in (
        "DATABRICKS_HOST",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def configure(monkeypatch, **extra):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", SP)
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "not-a-real-secret")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


# --- when the application has its own identity --------------------------------


def test_the_service_principal_is_recognised_as_configured(clean):
    configure(clean)
    assert workspace.service_principal_configured() is True


def test_a_missing_secret_is_not_configured(clean):
    configure(clean)
    clean.delenv("DATABRICKS_CLIENT_SECRET")
    assert workspace.service_principal_configured() is False


def test_a_missing_host_is_not_configured(clean):
    """The host is part of the identity: a client id means nothing without it."""
    configure(clean)
    clean.delenv("DATABRICKS_HOST")
    assert workspace.service_principal_configured() is False


def test_nothing_configured_is_not_configured(clean):
    assert workspace.service_principal_configured() is False


# --- the bug this module exists for -------------------------------------------


def test_a_cli_profile_cannot_take_over_from_the_service_principal(clean):
    """The regression. A profile in the environment used to win.

    Asserted on the settings rather than on a built client, because building
    one fetches a token. The settings are the decision; the SDK does as it is
    told once they are explicit.
    """
    configure(clean, DATABRICKS_CONFIG_PROFILE="DEFAULT")
    settings = workspace.service_principal_settings()

    assert settings["auth_type"] == workspace.SERVICE_PRINCIPAL_AUTH
    assert settings["client_id"] == SP
    assert settings["profile"] is None


def test_the_secret_is_passed_and_not_logged_by_accident(clean):
    """It belongs in the settings and nowhere this module prints."""
    configure(clean)
    settings = workspace.service_principal_settings()

    assert settings["client_secret"] == "not-a-real-secret"
    assert "not-a-real-secret" not in repr(workspace.service_principal_configured())


def test_the_pinned_auth_type_is_the_sdk_s_own_name(clean):
    """A typo here fails at connection time with an unhelpful message."""
    provider = pytest.importorskip("databricks.sdk.credentials_provider")
    assert (
        provider.oauth_service_principal.auth_type() == workspace.SERVICE_PRINCIPAL_AUTH
    )


def test_without_the_variables_it_defers_to_the_sdk(clean):
    """Which is right in a notebook: there the person is the identity."""
    assert workspace.service_principal_settings() is None