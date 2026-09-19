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
# MAGIC If the data has not changed since the last run, nothing is committed. A
# MAGIC daily Job that commits every day whether or not anything happened turns the
# MAGIC history into noise and rebuilds Render for nothing.

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

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SCOPE = dbutils.widgets.get("secret_scope")
REPO = dbutils.widgets.get("repo")
BRANCH = dbutils.widgets.get("branch")
DATA_PATH = dbutils.widgets.get("data_path")
ROOT_OVERRIDE = dbutils.widgets.get("repo_root")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Where the code is
# MAGIC
# MAGIC The Job runs this from the Git checkout, so `src/` and `evaluation/` are
# MAGIC both beside it. Rather than hard coding a path that changes with how the
# MAGIC task is configured, walk up from here until the repository is recognisable.


def repo_root() -> Path:
    if ROOT_OVERRIDE:
        return Path(ROOT_OVERRIDE)
    start = Path(os.getcwd())
    for candidate in (start, *start.parents):
        if (candidate / "src" / "iberian").is_dir() and (candidate / "evaluation").is_dir():
            return candidate
    raise SystemExit(
        f"Could not find the repo root from {start}. Set the repo_root widget."
    )


ROOT = repo_root()
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
print(f"repo root: {ROOT}")

# COMMAND ----------

from export_public_data import UnityCatalog, build, serialise, summarise  # noqa: E402

from iberian.publish.github import publish  # noqa: E402

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build
# MAGIC
# MAGIC The two validation tables are read from the catalog here rather than
# MAGIC recomputed, because the pipeline already owns them.

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
        f"\n\n{chr(10).join(summarise(payload))}"
    ),
    token=token,
    branch=BRANCH,
    author_name="iberian-energy job",
    author_email="doriel3572@gmail.com",
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