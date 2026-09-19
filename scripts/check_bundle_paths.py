"""Check that the bundle points at files that exist.

The Job reads its code from the branch, not from the bundle upload, so a
notebook_path that does not resolve is not caught at deploy time. It is caught
four minutes into a run, by which point the ingest task has already called three
public APIs.

This runs offline, with no workspace and no credentials, which matters because
personal access tokens are disabled in the boot camp workspace and CI therefore
cannot talk to Databricks at all.

    python scripts/check_bundle_paths.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

#: How Databricks resolves a notebook path. The file on disk carries .py, the
#: Job configuration does not.
NOTEBOOK_SUFFIXES = (".py", ".ipynb", ".sql", ".scala", ".r")


def resource_files() -> list[Path]:
    return sorted((ROOT / "resources").glob("*.yml"))


def notebook_paths(document: dict) -> list[tuple[str, str]]:
    """Every (task_key, notebook_path) the document declares."""
    out = []
    for job_name, job in (document.get("resources", {}).get("jobs", {}) or {}).items():
        for task in job.get("tasks", []) or []:
            notebook = (task.get("notebook_task") or {}).get("notebook_path")
            if notebook:
                out.append((f"{job_name}.{task['task_key']}", notebook))
    return out


def main() -> int:
    problems: list[str] = []
    checked = 0

    files = resource_files()
    if not files:
        print("No resources/*.yml found.")
        return 1

    for path in files:
        document = yaml.safe_load(path.read_text()) or {}
        for task, notebook in notebook_paths(document):
            checked += 1
            candidates = [ROOT / f"{notebook}{suffix}" for suffix in NOTEBOOK_SUFFIXES]
            if any(candidate.is_file() for candidate in candidates):
                print(f"ok    {task:<32} {notebook}")
            else:
                problems.append(
                    f"{task}: notebook_path '{notebook}' matches no file in the repo"
                )

    # The bundle itself has to parse, and every ${var.x} it uses has to be
    # declared, or deploy substitutes an empty string into a task parameter and
    # the failure shows up as a confusing runtime error rather than a config one.
    bundle = yaml.safe_load((ROOT / "databricks.yml").read_text()) or {}
    declared = set(bundle.get("variables", {}) or {})
    for path in files:
        text = path.read_text()
        for token in set(__import__("re").findall(r"\$\{var\.([A-Za-z0-9_]+)\}", text)):
            if token not in declared:
                problems.append(f"{path.name}: ${{var.{token}}} is not declared in databricks.yml")

    if problems:
        print()
        for problem in problems:
            print(f"FAIL  {problem}")
        return 1

    print(f"\n{checked} notebook paths and every bundle variable resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())