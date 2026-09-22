# Databricks notebook source
# MAGIC %md
# MAGIC # Set up the notice table and its vector index
# MAGIC
# MAGIC Run once, by hand, from the Git folder. Safe to run again: the table is
# MAGIC created only if missing, the placeholder row only goes into an empty
# MAGIC table, and the index is created only if it does not exist.
# MAGIC
# MAGIC Not part of the daily Job. The Job fills the table and triggers a sync;
# MAGIC this notebook is what makes the table and the index exist in the first
# MAGIC place, so that neither lives only in somebody's scratch notebook.
# MAGIC
# MAGIC ## Why the table is not owned by the declarative pipeline
# MAGIC
# MAGIC A Delta Sync index needs a source table with Change Data Feed enabled.
# MAGIC Whether a pipeline-managed table can serve as that source was not tested,
# MAGIC and a Job task writing a plain Delta table is known to work: it is the
# MAGIC same arrangement as `gold_episode_explanations`.
# MAGIC
# MAGIC ## Why the dates are numbers
# MAGIC
# MAGIC The point in time filter is `published_epoch <= episode start - 1`. The
# MAGIC filtering guide documents `<=` for numeric columns and only `>` for
# MAGIC timestamps, and a smoke test on this endpoint confirmed that the numeric
# MAGIC filter excludes a notice published after the episode. Without the filter,
# MAGIC that future notice was the top result.
# MAGIC
# MAGIC ## Why the index is created early
# MAGIC
# MAGIC The shared endpoint has a limit of 50 indexes and was full once already.
# MAGIC The index is created with its final name and schema before the table has
# MAGIC real rows, so the slot is held. A placeholder row goes in because it is
# MAGIC not known whether an index can be created from an empty table; the task
# MAGIC that loads the real notices deletes it.

# COMMAND ----------

# MAGIC %pip install databricks-ai-search
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("endpoint", "zachy_vs", "Vector Search endpoint")
dbutils.widgets.text("embedding_model", "databricks-gte-large-en", "Embedding model")
dbutils.widgets.dropdown("recreate_index", "no", ["no", "yes"], "Recreate the index")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
ENDPOINT = dbutils.widgets.get("endpoint")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
RECREATE = dbutils.widgets.get("recreate_index") == "yes"

TABLE = f"{CATALOG}.{SCHEMA}.gold_transmission_notices"
INDEX = f"{TABLE}_index"

# COMMAND ----------

# MAGIC %md
# MAGIC ## The table
# MAGIC
# MAGIC The schema is fixed here because the index is fixed to it. Adding a column
# MAGIC later means recreating the index.

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  notice_id          STRING  NOT NULL COMMENT 'A78 document mRID and time series mRID, one row per notice',
  text               STRING  COMMENT 'The notice in prose, which is what gets embedded',
  published_epoch    BIGINT  COMMENT 'Publication time, seconds since epoch UTC. The point in time filter',
  outage_start_epoch BIGINT  COMMENT 'Start of the unavailability, seconds since epoch UTC',
  outage_end_epoch   BIGINT  COMMENT 'End of the unavailability, seconds since epoch UTC',
  out_domain         STRING  COMMENT 'EIC of the exporting side',
  in_domain          STRING  COMMENT 'EIC of the importing side',
  asset              STRING  COMMENT 'Asset name, null when the operator did not publish one',
  asset_named        BOOLEAN COMMENT 'False when the notice carries no asset block at all',
  status             STRING  COMMENT 'planned or unplanned',
  business_type      STRING  COMMENT 'A53 planned maintenance or A54 unplanned outage',
  min_available_mw   DOUBLE  COMMENT 'Lowest capacity that remains AVAILABLE on the asset across the whole notice',
  breakpoints_json   STRING  COMMENT 'The availability step function as JSON, [[epoch, mw], ...]. The lowest capacity inside an episode window is computed from this, not from min_available_mw',
  published_at       TIMESTAMP,
  outage_start       TIMESTAMP,
  outage_end         TIMESTAMP
)
COMMENT 'ENTSO-E A78 transmission unavailability notices, the source of the notice vector index. Written by a Job task, not by the declarative pipeline.'
TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")

# Added after the index existed. An existing table keeps its rows and gains the
# column; the index has to be recreated to see it, which is what `recreate_index`
# is for.
existing = {field.name for field in spark.table(TABLE).schema.fields}
if "breakpoints_json" not in existing:
    spark.sql(f"ALTER TABLE {TABLE} ADD COLUMN breakpoints_json STRING")
    spark.sql(
        f"ALTER TABLE {TABLE} ALTER COLUMN breakpoints_json COMMENT "
        "'The availability step function as JSON, [[epoch, mw], ...]'"
    )
    print(f"{TABLE}: breakpoints_json added")

rows = spark.table(TABLE).count()
if rows == 0:
    # Dates at zero, so the overlap filter can never return it for a real
    # episode even before the loading task deletes it.
    spark.sql(f"""
        INSERT INTO {TABLE} (notice_id, text, published_epoch, outage_start_epoch, outage_end_epoch)
        VALUES ('placeholder', 'Placeholder row, replaced by the real A78 notices.', 0, 0, 0)
    """)
    print(f"{TABLE}: created, placeholder row inserted")
else:
    print(f"{TABLE}: already has {rows} row(s), left alone")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The index

# COMMAND ----------

import time

try:
    from databricks.ai_search.client import AISearchClient as Client
except ImportError:
    # The SDK was renamed from databricks-vectorsearch. Either works here.
    from databricks.vector_search.client import VectorSearchClient as Client

try:
    client = Client(disable_notice=True)
except TypeError:
    client = Client()

def create_index():
    """Create, retrying while the old index is still being deleted.

    Deletion is asynchronous and the API refuses any operation on an index in
    PENDING_DELETE. Whether `get_index` keeps succeeding during that window was
    not verified, so the wait is driven by the create call itself, which is the
    operation that actually has to succeed.
    """
    for attempt in range(60):
        try:
            return client.create_delta_sync_index(
                endpoint_name=ENDPOINT,
                source_table_name=TABLE,
                index_name=INDEX,
                pipeline_type="TRIGGERED",
                primary_key="notice_id",
                embedding_source_column="text",
                embedding_model_endpoint_name=EMBEDDING_MODEL,
            )
        except Exception as exc:
            if "pending deletion" not in str(exc).lower():
                raise
            if attempt == 0:
                print(f"{INDEX}: still being deleted, waiting")
            time.sleep(10)
    raise RuntimeError(
        f"{INDEX} was still pending deletion after 10 minutes. "
        "Wait and run this notebook again with recreate_index=yes."
    )


if RECREATE:
    # Delete and create in the same cell, deliberately. The endpoint has a
    # limit of 50 indexes and has been full once, so the slot should not be
    # left free for longer than this takes.
    #
    # Deletion is asynchronous. Creating straight after the delete call fails
    # with "currently pending deletion", so wait for it to actually disappear.
    try:
        client.delete_index(endpoint_name=ENDPOINT, index_name=INDEX)
        print(f"{INDEX}: delete requested")
    except Exception as exc:
        print(f"{INDEX}: nothing to delete ({type(exc).__name__})")

try:
    if RECREATE:
        raise LookupError("recreate requested")
    index = client.get_index(endpoint_name=ENDPOINT, index_name=INDEX)
    print(f"{INDEX}: already exists, left alone")
except Exception:
    index = create_index()
    print(f"{INDEX}: created on {ENDPOINT}")

for _ in range(60):
    status = index.describe().get("status", {})
    print(status.get("detailed_state"), status.get("ready"))
    if status.get("ready"):
        break
    time.sleep(20)

# COMMAND ----------

dbutils.notebook.exit(f"{TABLE} and {INDEX} ready on {ENDPOINT}")