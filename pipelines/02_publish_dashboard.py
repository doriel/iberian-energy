# Databricks notebook source
# MAGIC %md
# MAGIC # Publish the dashboard data
# MAGIC
# MAGIC Third task of the daily Job, after `ingest` and `transform`.
# MAGIC
# MAGIC The pipeline has just rebuilt the gold tables. This reads them, builds the
# MAGIC same JSON document `scripts/export_public_data.py` builds on a laptop, and
# MAGIC commits it to the repository. Render is watching the branch, so the commit
# MAGIC is the deploy, and the page is current without anyone opening a terminal.
# MAGIC
# MAGIC It writes no table and reads nothing outside the catalog, so a failure here
# MAGIC leaves the lakehouse exactly as `transform` left it. That is why it is a
# MAGIC third task rather than the tail of the second one.
# MAGIC
# MAGIC If the data has not changed, nothing is committed. A daily Job that commits
# MAGIC every day whether or not anything happened turns the history into noise and
# MAGIC rebuilds Render for nothing.

# COMMAND ----------

import os
import sys
from pathlib import Path

# COMMAND ----------

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("repo", "doriel/iberian-energy", "GitHub owner/repo")
dbutils.widgets.text("branch", "main", "Branch to commit to")
dbutils.widgets.text("data_path", "app/public/data.json", "Path in the repo")
dbutils.widgets.text("repo_root", "", "Repo root (blank = find it)")
dbutils.widgets.dropdown("force", "no", ["no", "yes"], "Commit even if unchanged")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SCOPE = dbutils.widgets.get("secret_scope")
REPO = dbutils.widgets.get("repo")
BRANCH = dbutils.widgets.get("branch")
DATA_PATH = dbutils.widgets.get("data_path")
ROOT_OVERRIDE = dbutils.widgets.get("repo_root")
FORCE = dbutils.widgets.get("force") == "yes"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Find the checkout
# MAGIC
# MAGIC Where a Git sourced notebook runs from is not something to assume, so this
# MAGIC looks for the repository rather than being told, tries more than one
# MAGIC starting point, and prints what it found. When it cannot find it, the error
# MAGIC says where it looked, because the alternative is another twenty minute run
# MAGIC that fails on an import.


def candidates() -> list[Path]:
    """Every plausible starting point, most likely first, without duplicates."""
    found: list[Path] = []
    try:
        context = (
            dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        )
        notebook_path = context.notebookPath().get()
        found.append(Path("/Workspace") / notebook_path.lstrip("/"))
    except Exception as exc:  # the context API is not guaranteed on every runtime
        print(f"notebook context unavailable: {type(exc).__name__}: {exc}")
    found.append(Path(os.getcwd()))
    for entry in sys.path:
        if entry:
            found.append(Path(entry))

    ordered: list[Path] = []
    for path in found:
        for parent in (path, *path.parents):
            if parent not in ordered:
                ordered.append(parent)
    return ordered


def repo_root() -> Path:
    if ROOT_OVERRIDE:
        return Path(ROOT_OVERRIDE)
    looked = candidates()
    for candidate in looked:
        if (candidate / "src" / "iberian" / "publish").is_dir():
            return candidate
    raise SystemExit(
        "Could not find the checkout. Set the repo_root widget to the folder "
        "that contains src/ and evaluation/. Looked in:\n  "
        + "\n  ".join(str(p) for p in looked[:25])
    )


ROOT = repo_root()
print(f"cwd:       {os.getcwd()}")
print(f"repo root: {ROOT}")
print(f"contents:  {sorted(p.name for p in ROOT.iterdir())}")

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

# COMMAND ----------

# Both imports come from the package, so the only path that has to be right is
# the one printed above.
from iberian.publish.dashboard import UnityCatalog, build, serialise, summarise  # noqa: E402
from iberian.publish.github import publish  # noqa: E402

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build
# MAGIC
# MAGIC The two validation tables are read from the catalog here rather than
# MAGIC recomputed, because the pipeline already owns them. If they are missing,
# MAGIC the payload simply has no validation section and the tab that shows it is
# MAGIC empty, so check these lines rather than assuming they appeared.

payload = build(UnityCatalog(spark, CATALOG, SCHEMA), ROOT / "evaluation")
body = serialise(payload)

print(f"{len(body) / 1024:.0f} KB")
for line in summarise(payload):
    print(f"  {line}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Commit
# MAGIC
# MAGIC The token is a fine grained PAT with Contents: Read and write on this
# MAGIC repository only. It comes from the secret scope, never from a widget: a
# MAGIC widget value is saved with the notebook state, and this repository is
# MAGIC public.

token = dbutils.secrets.get(scope=SCOPE, key="github_token")
print(f"token length: {len(token)}")  # never the token itself

result = publish(
    repo=REPO,
    path=DATA_PATH,
    content=body,
    message=(
        f"Publish dashboard data for {payload['coverage']['last_day']}"
        + "\n\n"
        + "\n".join(summarise(payload))
    ),
    token=token,
    branch=BRANCH,
    author_name="iberian-energy job",
    author_email="doriel3572@gmail.com",
    force=FORCE,
)

print(result)
if result.url:
    print(result.url)

# COMMAND ----------

# The Job's run page shows this, so make it say what happened rather than
# leaving someone to read the log.
dbutils.notebook.exit(
    f"{result.status} | {payload['coverage']['days']} days | "
    f"{payload['headline']['episodes']} episodes"
)