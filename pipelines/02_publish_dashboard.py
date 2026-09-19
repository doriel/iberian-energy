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
# MAGIC
# MAGIC The setup below is deliberately identical to `01_build_medallion`. That
# MAGIC notebook runs as the first task of this same Job and imports the same
# MAGIC package, so whatever it does about dependencies and the checkout path is
# MAGIC known to work here. Inventing a second mechanism cost two failed runs.

# COMMAND ----------

# MAGIC %pip install requests pandas
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC
# MAGIC The GitHub token comes from the secret scope, never from a widget. A
# MAGIC widget's value is saved with the notebook state, and this repository is
# MAGIC public.
# MAGIC
# MAGIC ```
# MAGIC databricks secrets put-secret iberian github_token
# MAGIC ```

# COMMAND ----------

import os

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("repo", "doriel/iberian-energy", "GitHub owner/repo")
dbutils.widgets.text("branch", "main", "Branch to commit to")
dbutils.widgets.text("data_path", "app/public/data.json", "Path in the repo")
dbutils.widgets.dropdown("force", "no", ["no", "yes"], "Commit even if unchanged")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SCOPE = dbutils.widgets.get("secret_scope")
REPO = dbutils.widgets.get("repo")
BRANCH = dbutils.widgets.get("branch")
DATA_PATH = dbutils.widgets.get("data_path")
FORCE = dbutils.widgets.get("force") == "yes"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import the project modules
# MAGIC
# MAGIC This notebook sits in `pipelines/` inside the Git folder, so the package
# MAGIC is one level up under `src/`. Same block as `01_build_medallion`, and the
# MAGIC import happens in this cell so a path that does not work fails here rather
# MAGIC than one cell later with less to go on.

# COMMAND ----------

import sys

# Inside a Git folder the working directory is the notebook's own directory,
# so the repo root is one level up. Falling back to the notebook path keeps
# this working if the notebook is run from somewhere that does not set cwd.
REPO_ROOT = os.path.abspath(os.path.join(os.getcwd(), ".."))
if not os.path.isdir(os.path.join(REPO_ROOT, "src")):
    notebook_path = (
        dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        .notebookPath().get()
    )
    REPO_ROOT = os.path.abspath(
        os.path.join("/Workspace", os.path.dirname(notebook_path).lstrip("/"), "..")
    )

SRC = os.path.join(REPO_ROOT, "src")
if not os.path.isdir(SRC):
    raise RuntimeError(
        f"Could not find src/ from {REPO_ROOT}. This notebook expects to live "
        "in pipelines/ inside the repository Git folder."
    )

if SRC not in sys.path:
    sys.path.insert(0, SRC)

print(f"Working directory: {os.getcwd()}")
print(f"Repo root:         {REPO_ROOT}")
print(f"Source:            {SRC}")
print(f"Repo contents:     {sorted(os.listdir(REPO_ROOT))}")

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

from iberian.publish.dashboard import UnityCatalog, build, serialise, summarise  # noqa: E402
from iberian.publish.github import publish  # noqa: E402

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build
# MAGIC
# MAGIC The two validation tables are read from the catalog here rather than
# MAGIC recomputed, because the pipeline already owns them. If they are missing,
# MAGIC the payload simply has no validation section and the tab that shows it is
# MAGIC empty, so read these lines rather than assuming they appeared.

# COMMAND ----------

EVALUATION = os.path.join(REPO_ROOT, "evaluation")

payload = build(UnityCatalog(spark, CATALOG, SCHEMA), EVALUATION)
body = serialise(payload)

print(f"{len(body) / 1024:.0f} KB")
for line in summarise(payload):
    print(f"  {line}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Commit
# MAGIC
# MAGIC The token is a fine grained PAT with Contents: Read and write on this
# MAGIC repository only.

# COMMAND ----------

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