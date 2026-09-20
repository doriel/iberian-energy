# Databricks notebook source
# MAGIC %md
# MAGIC # Explain the new episodes
# MAGIC
# MAGIC Third task of the daily Job, between `transform` and `publish`.
# MAGIC
# MAGIC The north star metric is the share of significant price anomalies that
# MAGIC receive a correct, grounded explanation **within fifteen minutes of market
# MAGIC data publication**. Generating the explanations by hand meets that clause
# MAGIC only when a person happens to be at a laptop, which makes the person the
# MAGIC system. This task is what makes the claim true.
# MAGIC
# MAGIC It explains only episodes with no explanation on file, which is two or
# MAGIC three a day. An episode's explanation does not change once its evidence is
# MAGIC published, so re-explaining the other hundred and twenty-three would spend
# MAGIC model calls to reproduce answers that already exist.
# MAGIC
# MAGIC ## Why the Volume rather than the repository
# MAGIC
# MAGIC Within one Job run the Git checkout is fixed at the commit the run started
# MAGIC from. A file this task committed would not be visible to `publish` in the
# MAGIC same run, because `publish` reads the same frozen checkout. The Volume is
# MAGIC where the tasks of this Job already hand things to each other.

# COMMAND ----------

# MAGIC %pip install requests pandas
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("volume", "raw", "Volume")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("endpoint", "databricks-claude-haiku-4-5", "Serving endpoint")
dbutils.widgets.text("max_new", "25", "Most episodes to explain in one run")
dbutils.widgets.dropdown("rebuild", "no", ["no", "yes"], "Re-explain everything")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
VOLUME = dbutils.widgets.get("volume")
SCOPE = dbutils.widgets.get("secret_scope")
ENDPOINT = dbutils.widgets.get("endpoint")
MAX_NEW = int(dbutils.widgets.get("max_new"))
REBUILD = dbutils.widgets.get("rebuild") == "yes"

#: Where the explanations live between tasks, and between days.
EXPLANATIONS = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}/agent/explanations.jsonl"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Secrets
# MAGIC
# MAGIC The ENTSO-E token is needed here as well as in `ingest`: the A78 notices
# MAGIC are retrieved per episode rather than landed, because the point in time
# MAGIC filter depends on when the episode started.

# COMMAND ----------

os.environ["ENTSOE_SECURITY_TOKEN"] = dbutils.secrets.get(
    scope=SCOPE, key="entsoe_token"
)
print(f"token length: {len(os.environ['ENTSOE_SECURITY_TOKEN'])}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import the project modules
# MAGIC
# MAGIC Same block as `01_build_medallion`, which is the one known to work here.

# COMMAND ----------

import sys

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

print(f"Repo root: {REPO_ROOT}")

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

from iberian.agent.batch import (  # noqa: E402
    episode_key,
    explain_episodes,
    load_records,
    merge,
    pending,
    sheet_builder,
    write_records,
)
from iberian.agent.explain import databricks_completer  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import as_utc  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_outages_response,
)
from iberian.publish.dashboard import UnityCatalog  # noqa: E402

DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)

# COMMAND ----------

# MAGIC %md
# MAGIC ## What still needs explaining

# COMMAND ----------

source = UnityCatalog(spark, CATALOG, SCHEMA)
episodes = as_utc(source.table("gold_split_episodes"), "start_utc", "end_utc")
intervals = as_utc(source.table("gold_interval_premium"), "ts_utc")

if episodes.empty:
    dbutils.notebook.exit("no episodes in gold")

# Worst first. If the cap bites, the episodes that got explained are the ones
# anybody would have looked at first.
episodes = episodes.sort_values("max_abs_spread", ascending=False)

os.makedirs(os.path.dirname(EXPLANATIONS), exist_ok=True)
already = [] if REBUILD else load_records(EXPLANATIONS)
todo = episodes if REBUILD else pending(episodes, already)

print(f"{len(episodes)} episodes, {len(already)} explained, {len(todo)} pending")

if len(todo) > MAX_NEW:
    # A cap, because the first run after a long gap would otherwise call the
    # model a hundred times inside a task somebody expects to take seconds.
    # The rest are picked up tomorrow, and the count is printed so a backlog is
    # visible rather than silent.
    print(f"capping at {MAX_NEW}; {len(todo) - MAX_NEW} will wait for the next run")
    todo = todo.head(MAX_NEW)

if todo.empty:
    dbutils.notebook.exit(f"nothing new | {len(already)} explanations on file")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Explain them
# MAGIC
# MAGIC Each record is written as it arrives. A batch that fails on episode ten
# MAGIC keeps the first nine, which at one model call each is worth keeping.

# COMMAND ----------

client = EntsoeClient(Settings.from_env().require_entsoe_token())
complete = databricks_completer(endpoint=ENDPOINT)
build_sheet = sheet_builder(
    client, intervals, DIRECTION, parse_outages_response, binding_assets
)

fresh: list[dict] = []
for record in explain_episodes(
    todo,
    build_sheet,
    complete,
    model=ENDPOINT,
    on_each=lambda key, result: print(
        f"  {key}  {'grounded' if result.ok else 'REJECTED'}  "
        f"attempt {result.attempts}"
    ),
):
    fresh.append(record)
    write_records(EXPLANATIONS, merge(already, fresh))

# COMMAND ----------

grounded = sum(1 for record in fresh if record["grounded"])
total = len(load_records(EXPLANATIONS))

print(f"\n{grounded} of {len(fresh)} new explanations grounded")
print(f"{total} explanations on file at {EXPLANATIONS}")

for record in fresh:
    if not record["grounded"]:
        reason = ", ".join(record["unsupported"]) or "no source named"
        print(f"  rejected {record['episode_key']}: {reason}")

# COMMAND ----------

dbutils.notebook.exit(
    f"{grounded}/{len(fresh)} new grounded | {total} on file"
)