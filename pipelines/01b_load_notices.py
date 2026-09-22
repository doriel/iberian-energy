# Databricks notebook source
# MAGIC %md
# MAGIC # Land the A78 notices and sync the vector index
# MAGIC
# MAGIC Runs after `ingest` and beside `transform`, before `explain`.
# MAGIC
# MAGIC The agent already retrieves A78 notices from the transparency platform
# MAGIC every time it explains an episode, and that works. What it does not do is
# MAGIC put the evidence in the lakehouse: the notices live in nobody's table, so
# MAGIC they cannot be queried, joined or seen in lineage, and the retrieval has
# MAGIC nothing to be compared against. This task lands them in
# MAGIC `gold_transmission_notices` and triggers the index sync.
# MAGIC
# MAGIC ## Why the file is 01b
# MAGIC
# MAGIC The notebooks are numbered in run order and this one was inserted between
# MAGIC two that already exist. Renumbering `02_explain_episodes` and
# MAGIC `03_publish_dashboard` would mean renaming files the Job, the bundle and
# MAGIC the docs all point at, a fortnight before the deadline, for a cosmetic
# MAGIC gain. The letter says what happened.
# MAGIC
# MAGIC ## Why the window is the trailing one
# MAGIC
# MAGIC Same reason `ingest` re-fetches three days: ENTSO-E republishes corrected
# MAGIC notices. A republication is a new document with a new publication time, so
# MAGIC it arrives as a new row rather than replacing the old one. That is
# MAGIC deliberate. The old version was the one in force before the correction,
# MAGIC and an episode that happened before the correction must be able to
# MAGIC retrieve it, or the point in time filter is defeated from behind.
# MAGIC
# MAGIC ## Why the MERGE and not an overwrite
# MAGIC
# MAGIC The table accumulates. An overwrite would keep only the trailing window
# MAGIC and quietly shrink the evidence base to three days, which the evaluation
# MAGIC would then report as retrieval getting worse.

# COMMAND ----------

# MAGIC %pip install requests pandas databricks-ai-search
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("endpoint", "zachy_vs", "Vector Search endpoint")
dbutils.widgets.text("days", "3", "Days back to fetch")
dbutils.widgets.text("start_day", "", "First day (YYYY-MM-DD, blank means today)")
dbutils.widgets.dropdown("sync_index", "yes", ["yes", "no"], "Trigger the index sync")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SCOPE = dbutils.widgets.get("secret_scope")
ENDPOINT = dbutils.widgets.get("endpoint")
DAYS = int(dbutils.widgets.get("days"))
START_DAY = dbutils.widgets.get("start_day").strip()
SYNC = dbutils.widgets.get("sync_index") == "yes"

TABLE = f"{CATALOG}.{SCHEMA}.gold_transmission_notices"
INDEX = f"{TABLE}_index"

# COMMAND ----------

os.environ["ENTSOE_SECURITY_TOKEN"] = dbutils.secrets.get(
    scope=SCOPE, key="entsoe_token"
)
print(f"token length: {len(os.environ['ENTSOE_SECURITY_TOKEN'])}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import the project modules
# MAGIC
# MAGIC Same block as the other notebooks, which is the one known to work here.

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

from datetime import date, timedelta  # noqa: E402

from iberian.agent.notices import (  # noqa: E402
    COLUMNS,
    notice_rows,
    spark_schema,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import market_day_window  # noqa: E402
from iberian.parsing.entsoe_outages import parse_outages_response  # noqa: E402

DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fetch the window
# MAGIC
# MAGIC Day by day, on the market day boundary, so the request matches the one
# MAGIC the agent makes for an episode. A notice spanning ten days comes back on
# MAGIC each of them; `notice_rows` de-duplicates by id, so the count is notices
# MAGIC rather than notice-days.
# MAGIC
# MAGIC A day that fails is reported and skipped rather than failing the task.
# MAGIC The notices for that day are still on the platform tomorrow, the trailing
# MAGIC window will ask for them again, and `explain` does not depend on this
# MAGIC table yet.

# COMMAND ----------

last_day = date.fromisoformat(START_DAY) if START_DAY else date.today()
days = [last_day - timedelta(days=offset) for offset in range(DAYS)]

entsoe = EntsoeClient(Settings.from_env().require_entsoe_token())

curves = []
failed: list[str] = []

for day in sorted(days):
    day_start, day_end = market_day_window(day)
    try:
        response = entsoe.transmission_unavailability(
            *DIRECTION, day_start, day_end
        )
    except Exception as exc:
        first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
        failed.append(f"{day}: {first_line[:200]}")
        print(f"  {day}  FAILED, skipped: {first_line[:200]}")
        continue
    day_curves = [] if response.is_empty else parse_outages_response(response)
    curves.extend(day_curves)
    print(f"  {day}  {len(day_curves)} curve(s)")

rows = notice_rows(curves)
print(f"\n{len(rows)} distinct notice(s) from {len(days) - len(failed)} day(s)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Merge them in
# MAGIC
# MAGIC Keyed on `notice_id`, which is document, series and period. A row that is
# MAGIC already there is updated rather than duplicated, because the same notice
# MAGIC arrives again on every day of the trailing window and the values are
# MAGIC identical. A republished notice has a different document mRID, so it is a
# MAGIC new key and a new row, which is the point.

# COMMAND ----------

if rows:
    staged = spark.createDataFrame(rows, schema=spark_schema())
    staged.createOrReplaceTempView("incoming_notices")

    assignments = ", ".join(f"target.{column} = source.{column}" for column in COLUMNS)
    columns = ", ".join(COLUMNS)
    values = ", ".join(f"source.{column}" for column in COLUMNS)

    spark.sql(f"""
        MERGE INTO {TABLE} AS target
        USING incoming_notices AS source
        ON target.notice_id = source.notice_id
        WHEN MATCHED THEN UPDATE SET {assignments}
        WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({values})
    """)
    print(f"{TABLE}: merged {len(rows)} notice(s)")
else:
    # Not an error. A three day window with no transmission outages on the
    # Spain to Portugal border is an ordinary week.
    print(f"{TABLE}: nothing to merge, left alone")

# The setup notebook inserts one placeholder row so the index can be created
# before any real notice exists. It goes as soon as there is something real,
# and the delete is unconditional because it costs nothing when it is gone.
removed = spark.sql(f"SELECT count(*) AS n FROM {TABLE} WHERE notice_id = 'placeholder'")
if removed.first()["n"] and spark.table(TABLE).count() > 1:
    spark.sql(f"DELETE FROM {TABLE} WHERE notice_id = 'placeholder'")
    print(f"{TABLE}: placeholder row removed")

total = spark.table(TABLE).count()
print(f"{TABLE}: {total} row(s)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Sync the index
# MAGIC
# MAGIC The index is TRIGGERED, not continuous, so it sees the new rows only when
# MAGIC somebody asks. This is the ask.
# MAGIC
# MAGIC The sync is started and not waited on. It reads the Change Data Feed of a
# MAGIC table that just gained a handful of rows, and blocking the task on it
# MAGIC would put an embedding job on the critical path of the daily run for no
# MAGIC benefit: nothing downstream reads the index yet, and when something does,
# MAGIC it will read yesterday's notices at worst.

# COMMAND ----------

if not SYNC:
    print("sync_index=no, index left alone")
elif not rows:
    print("nothing merged, sync skipped")
else:
    try:
        from databricks.ai_search.client import AISearchClient as Client
    except ImportError:
        # The SDK was renamed from databricks-vectorsearch. Either works here.
        from databricks.vector_search.client import VectorSearchClient as Client

    try:
        search = Client(disable_notice=True)
    except TypeError:
        search = Client()

    index = search.get_index(endpoint_name=ENDPOINT, index_name=INDEX)
    index.sync()
    status = index.describe().get("status", {})
    print(f"{INDEX}: sync triggered, state {status.get('detailed_state')}")

# COMMAND ----------

summary = f"{len(rows)} notice(s) merged, {total} on file"
if failed:
    summary += f" | {len(failed)} day(s) failed: {'; '.join(failed)}"
dbutils.notebook.exit(summary)