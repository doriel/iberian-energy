# Databricks notebook source
# MAGIC %md
# MAGIC # Copy the episodes into Lakebase
# MAGIC
# MAGIC Last task of the daily Job, after `publish`.
# MAGIC
# MAGIC The application reads its own database rather than querying a warehouse
# MAGIC on every page load, so `iberian.episodes` holds a small copy of what gold
# MAGIC knows about each episode: enough to list one, describe it, and let
# MAGIC somebody judge its cause. The heavy tables stay in Delta.
# MAGIC
# MAGIC ## Why the application does not read gold directly
# MAGIC
# MAGIC A warehouse query per page load is seconds of latency and a cluster that
# MAGIC has to be awake for a visitor to see anything. The deployed application is
# MAGIC on Render, reachable by anybody, and it cannot depend on a warehouse
# MAGIC being warm at the moment a TA opens it.
# MAGIC
# MAGIC ## Why this table is owned rather than synced
# MAGIC
# MAGIC `episode_labels` has a foreign key to it. A synced table is managed by
# MAGIC Databricks, and pointing a constraint at something another system
# MAGIC rebuilds on its own schedule means the constraint and the sync eventually
# MAGIC disagree about who is in charge. Owning this one keeps the referential
# MAGIC integrity real. It costs an upsert of a few dozen rows a day.
# MAGIC
# MAGIC ## What it does not do
# MAGIC
# MAGIC Delete. An episode that leaves gold, because the window moved, keeps its
# MAGIC row here and keeps its labels. Deleting would cascade to somebody's
# MAGIC judgement, and a judgement is not a derived value: it cannot be rebuilt
# MAGIC by running the pipeline again.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]" pandas
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("project", "doriel-capstone-lakebase", "Lakebase project")
dbutils.widgets.text("branch", "production", "Branch")
dbutils.widgets.text("endpoint_id", "primary", "Endpoint")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
ENDPOINT = (
    f"projects/{dbutils.widgets.get('project')}"
    f"/branches/{dbutils.widgets.get('branch')}"
    f"/endpoints/{dbutils.widgets.get('endpoint_id')}"
)

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
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

import pandas as pd  # noqa: E402

from iberian.agent.batch import episode_key  # noqa: E402
from iberian.analysis.capacity import percentile_function  # noqa: E402
from iberian.app.lakebase import Lakebase, databricks_credentials  # noqa: E402
from iberian.market_time import as_utc  # noqa: E402
from iberian.publish.dashboard import UnityCatalog  # noqa: E402

# COMMAND ----------

# MAGIC %md
# MAGIC ## What gold says
# MAGIC
# MAGIC The capacity percentile is computed with the same function the labelling
# MAGIC sheet uses. It is the number a person decides a cause on, and the
# MAGIC application showing a different one from the sheet would be a
# MAGIC disagreement nobody could explain.

# COMMAND ----------

source = UnityCatalog(spark, CATALOG, SCHEMA)
episodes = as_utc(source.table("gold_split_episodes"), "start_utc", "end_utc")
intervals = as_utc(source.table("gold_interval_premium"), "ts_utc")

if episodes.empty:
    dbutils.notebook.exit("no episodes in gold")

percentile = percentile_function(intervals["capacity_mw"].dropna())

rows = []
for _, episode in episodes.iterrows():
    window = intervals[
        (intervals["ts_utc"] >= episode["start_utc"])
        & (intervals["ts_utc"] < episode["end_utc"])
    ]
    capacity = window["capacity_mw"].dropna()
    lowest = float(capacity.min()) if not capacity.empty else None

    rows.append(
        {
            "episode_key": episode_key(episode),
            "market_day": episode["market_day"],
            "start_utc": pd.Timestamp(episode["start_utc"]).to_pydatetime(),
            "end_utc": pd.Timestamp(episode["end_utc"]).to_pydatetime(),
            "premium_side": episode["premium_side"],
            "peak_abs_spread": round(float(episode["max_abs_spread"]), 2),
            "severity": episode["max_severity"],
            "extra_cost_eur": (
                round(float(episode["extra_cost_eur"]), 2)
                if pd.notna(episode.get("extra_cost_eur"))
                else None
            ),
            "min_capacity_mw": lowest,
            "capacity_percentile": percentile(lowest),
            "share_saturated": (
                round(float(episode["share_saturated"]), 3)
                if pd.notna(episode.get("share_saturated"))
                else None
            ),
        }
    )

print(f"{len(rows)} episode(s) from gold")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Upsert
# MAGIC
# MAGIC One statement per episode rather than one big one. At a few dozen rows
# MAGIC the round trips cost nothing, and a single row that fails its constraints
# MAGIC is reported by key instead of taking the whole batch with it.

# COMMAND ----------

store = Lakebase(
    host=os.environ.get("LAKEBASE_HOST")
    or "ep-dry-river-d16e0gjf.database.us-west-2.cloud.databricks.com",
    user=spark.sql("SELECT current_user()").first()[0],
    credential_factory=databricks_credentials(ENDPOINT),
)

UPSERT = """
INSERT INTO iberian.episodes (
    episode_key, market_day, start_utc, end_utc, premium_side,
    peak_abs_spread, severity, extra_cost_eur, min_capacity_mw,
    capacity_percentile, share_saturated, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (episode_key) DO UPDATE SET
    market_day = EXCLUDED.market_day,
    start_utc = EXCLUDED.start_utc,
    end_utc = EXCLUDED.end_utc,
    premium_side = EXCLUDED.premium_side,
    peak_abs_spread = EXCLUDED.peak_abs_spread,
    severity = EXCLUDED.severity,
    extra_cost_eur = EXCLUDED.extra_cost_eur,
    min_capacity_mw = EXCLUDED.min_capacity_mw,
    capacity_percentile = EXCLUDED.capacity_percentile,
    share_saturated = EXCLUDED.share_saturated,
    updated_at = now()
RETURNING episode_key
"""

written, failed = 0, []
for row in rows:
    try:
        store.execute(
            UPSERT,
            (
                row["episode_key"], row["market_day"], row["start_utc"], row["end_utc"],
                row["premium_side"], row["peak_abs_spread"], row["severity"],
                row["extra_cost_eur"], row["min_capacity_mw"],
                row["capacity_percentile"], row["share_saturated"],
            ),
        )
        written += 1
    except Exception as exc:
        failed.append(f"{row['episode_key']}: {str(exc).splitlines()[0][:150]}")

print(f"{written} written, {len(failed)} failed")
for line in failed:
    print(f"  {line}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read it back
# MAGIC
# MAGIC From Lakebase rather than from the frame above, because what matters is
# MAGIC what the application will read, not what this notebook thought it sent.

# COMMAND ----------

summary = store.query(
    """
    SELECT count(*) AS episodes,
           count(*) FILTER (WHERE capacity_percentile < 25) AS reduced,
           count(*) FILTER (WHERE capacity_percentile >= 25) AS ordinary,
           min(market_day) AS first_day,
           max(market_day) AS last_day
    FROM iberian.episodes
    """
)[0]

labels = store.query(
    "SELECT count(*) AS labels, count(DISTINCT created_by) AS labellers "
    "FROM iberian.episode_labels"
)[0]

print(f"iberian.episodes: {summary['episodes']} rows, "
      f"{summary['first_day']} to {summary['last_day']}")
print(f"  {summary['reduced']} below the quartile, {summary['ordinary']} at an ordinary level")
print(f"iberian.episode_labels: {labels['labels']} label(s) from {labels['labellers']} person(s)")

# COMMAND ----------

message = f"{written} episode(s) in Lakebase"
if failed:
    message += f" | {len(failed)} failed"
dbutils.notebook.exit(message)