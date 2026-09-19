"""Commit a built file to GitHub over the contents API.

The dashboard is served by Render from this repository, so the way to make the
deployed page current is to commit the data file. A Databricks Job cannot push
with git, it has no checkout and no ssh key, but it can make one authenticated
HTTP request, and the contents API turns a file plus its current sha into a
commit on the branch.

Render is watching the branch, so the commit is also the deploy. Nothing here
talks to Render, which is the point: one trigger, and no second credential.

    publish(
        repo="doriel/iberian-energy",
        path="app/public/data.json",
        content=serialise(payload),
        message="Publish dashboard data",
        token=...,
    )

The token is a fine grained personal access token with Contents: Read and write
on this repository and nothing else. It goes in the Databricks secret scope, not
in a widget and not in the notebook, because a widget value is saved with the
notebook state and this repository is public.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import requests

API = "https://api.github.com"
TIMEOUT = 30

#: Keys that change on every run whether or not the data did. Comparing without
#: them is what stops a daily Job from producing a daily empty commit.
VOLATILE = ("generated_at",)


@dataclass(frozen=True)
class Result:
    status: str  # "created", "updated" or "unchanged"
    sha: str | None
    url: str | None

    def __str__(self) -> str:
        return f"{self.status}" + (f" {self.sha[:8]}" if self.sha else "")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch(repo: str, path: str, branch: str, token: str) -> tuple[str | None, bytes]:
    """The file's current sha and bytes, or (None, b"") if it is not there yet."""
    response = requests.get(
        f"{API}/repos/{repo}/contents/{path}",
        headers=_headers(token),
        params={"ref": branch},
        timeout=TIMEOUT,
    )
    if response.status_code == 404:
        return None, b""
    response.raise_for_status()
    body = response.json()
    if body.get("encoding") != "base64":
        # Files over 1 MB come back without content and need the blobs API. The
        # payload is well under that, so this is a signal that something else
        # is at this path rather than a case worth handling.
        raise RuntimeError(
            f"{path} did not come back base64 encoded; it may be too large or a directory"
        )
    return body["sha"], base64.b64decode(body["content"])


def same_data(left: bytes, right: bytes, volatile: tuple[str, ...] = VOLATILE) -> bool:
    """Whether two payloads differ in anything except their timestamps.

    Falls back to comparing bytes if either side is not the JSON object this
    expects, so a corrupt remote file always counts as different and gets
    replaced rather than silently kept.
    """
    try:
        a, b = json.loads(left), json.loads(right)
    except (ValueError, TypeError):
        return left == right
    if not isinstance(a, dict) or not isinstance(b, dict):
        return left == right
    for key in volatile:
        a.pop(key, None)
        b.pop(key, None)
    return a == b


def publish(
    *,
    repo: str,
    path: str,
    content: bytes,
    message: str,
    token: str,
    branch: str = "main",
    author_name: str | None = None,
    author_email: str | None = None,
    force: bool = False,
) -> Result:
    """Create or update `path` on `branch`, skipping a no-op commit.

    `force` writes even when the data is unchanged, which is what you want when
    testing the credential and never on a schedule.
    """
    sha, existing = fetch(repo, path, branch, token)

    if sha and not force and same_data(existing, content):
        return Result("unchanged", sha, None)

    payload: dict = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if sha:
        # Without this GitHub rejects the write rather than overwriting, which
        # is the behaviour to want: a stale sha means someone else committed.
        payload["sha"] = sha
    if author_name and author_email:
        author = {"name": author_name, "email": author_email}
        payload["committer"] = author
        payload["author"] = author

    response = requests.put(
        f"{API}/repos/{repo}/contents/{path}",
        headers=_headers(token),
        json=payload,
        timeout=TIMEOUT,
    )
    if response.status_code == 409:
        raise RuntimeError(
            f"{path} changed on {branch} while this ran. Re-run; the next attempt "
            "reads the new sha."
        )
    response.raise_for_status()

    body = response.json()
    return Result(
        "updated" if sha else "created",
        body["content"]["sha"],
        body["commit"]["html_url"],
    )