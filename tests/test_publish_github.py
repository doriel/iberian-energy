"""The parts of the GitHub publish that are worth getting wrong only once.

No network. `publish` is exercised against a fake session so the sha handling
and the skip decision are tested, since both are only observable in production
on the day they misbehave.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.publish import github  # noqa: E402


def payload(days: int, generated: str) -> bytes:
    return json.dumps(
        {"generated_at": generated, "coverage": {"days": days}}
    ).encode("utf-8")


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGitHub:
    """Just enough of the contents API to drive the two code paths."""

    def __init__(self, existing: bytes | None) -> None:
        self.existing = existing
        self.puts: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        if self.existing is None:
            return FakeResponse(404)
        return FakeResponse(
            200,
            {
                "sha": "oldsha",
                "encoding": "base64",
                "content": base64.b64encode(self.existing).decode("ascii"),
            },
        )

    def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append(json)
        return FakeResponse(
            200,
            {
                "content": {"sha": "newsha"},
                "commit": {"html_url": "https://github.test/commit/newsha"},
            },
        )


@pytest.fixture
def fake(monkeypatch):
    def install(existing):
        client = FakeGitHub(existing)
        monkeypatch.setattr(github.requests, "get", client.get)
        monkeypatch.setattr(github.requests, "put", client.put)
        return client

    return install


# --- same_data --------------------------------------------------------------


def test_a_new_timestamp_alone_is_not_a_change():
    assert github.same_data(payload(60, "2026-09-19T06:00:00Z"), payload(60, "2026-09-20T06:00:00Z"))


def test_a_new_market_day_is_a_change():
    assert not github.same_data(
        payload(60, "2026-09-19T06:00:00Z"), payload(61, "2026-09-19T06:00:00Z")
    )


def test_a_remote_file_that_is_not_json_counts_as_different():
    assert not github.same_data(b"<html>oops</html>", payload(60, "x"))


# --- publish ----------------------------------------------------------------


def test_a_missing_file_is_created_without_a_sha(fake):
    client = fake(None)
    result = github.publish(
        repo="o/r", path="p.json", content=payload(60, "a"), message="m", token="t"
    )
    assert result.status == "created"
    assert "sha" not in client.puts[0]


def test_an_existing_file_is_updated_with_its_sha(fake):
    client = fake(payload(59, "a"))
    result = github.publish(
        repo="o/r", path="p.json", content=payload(60, "b"), message="m", token="t"
    )
    assert result.status == "updated"
    assert client.puts[0]["sha"] == "oldsha"


def test_unchanged_data_does_not_commit(fake):
    client = fake(payload(60, "yesterday"))
    result = github.publish(
        repo="o/r", path="p.json", content=payload(60, "today"), message="m", token="t"
    )
    assert result.status == "unchanged"
    assert client.puts == []


def test_force_commits_unchanged_data(fake):
    client = fake(payload(60, "yesterday"))
    result = github.publish(
        repo="o/r",
        path="p.json",
        content=payload(60, "today"),
        message="m",
        token="t",
        force=True,
    )
    assert result.status == "updated"
    assert len(client.puts) == 1


def test_the_branch_is_passed_through(fake):
    client = fake(payload(59, "a"))
    github.publish(
        repo="o/r",
        path="p.json",
        content=payload(60, "b"),
        message="m",
        token="t",
        branch="publish",
    )
    assert client.puts[0]["branch"] == "publish"