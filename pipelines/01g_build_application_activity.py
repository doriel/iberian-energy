# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %md
# MAGIC # Application activity, from the Lakebase change feed
# MAGIC
# MAGIC Reads the `lb_*_history` tables the Lakebase change data feed writes and
# MAGIC builds `silver_application_events` and `gold_application_activity`.
# MAGIC
# MAGIC This is the analytics half of the reverse flow, and what it measures is
# MAGIC **the product**, not the market. Every other table in this project is
# MAGIC about Iberian electricity. This one is about whether the agent is being
# MAGIC used, whether its tools work, who is writing what, and whether the ground
# MAGIC truth is growing. It is the only table that can answer "is this thing any
# MAGIC good in practice", and it can answer it because the application's own
# MAGIC writes come back into the lakehouse rather than staying in Postgres.
# MAGIC
# MAGIC ## What is incremental here, and what is not
# MAGIC
# MAGIC Worth being precise, because "incremental" is claimed more often than it
# MAGIC is true.
# MAGIC
# MAGIC **The feed is read incrementally, by high-water mark.** Each history
# MAGIC table is append-only and carries `_sort_by`, a key the feed guarantees to
# MAGIC be monotonic. A run reads the rows above the highest `sort_by` already in
# MAGIC silver for that source and no others.
# MAGIC
# MAGIC The first version of this used a streaming query per table with
# MAGIC `foreachBatch` and a checkpoint, which is what the Databricks
# MAGIC documentation shows. It killed the Spark Connect session on serverless
# MAGIC compute: `INTERNAL_ERROR: Spark session is no longer usable`, on the
# MAGIC first table, taking the other two with it. Rather than fight that, the
# MAGIC state moved from a checkpoint directory into the target table itself.
# MAGIC
# MAGIC That is not a downgrade. A watermark stored in the table is visible to
# MAGIC anyone who queries it, survives a workspace that loses a Volume, and
# MAGIC needs no explanation of where the position is kept. The streaming version
# MAGIC bought automatic state management for state that is one number.
# MAGIC
# MAGIC **The write into silver is idempotent.** The merge is keyed on
# MAGIC `(source_table, pg_lsn, sort_by, change_type)`, which identifies one
# MAGIC change: an LSN and a sort key name one row in the write-ahead log, and
# MAGIC the change type separates the two halves of an update. A run interrupted
# MAGIC halfway and started again inserts nothing twice.
# MAGIC
# MAGIC **The gold aggregate is recomputed in full, and that is deliberate.**
# MAGIC Silver holds a few thousand rows. An additive merge over a grain that
# MAGIC includes a date would be more code, more ways to be wrong, and would save
# MAGIC a second. The incrementality that is worth having is not re-reading the
# MAGIC change feed, and that is the part that is incremental. Saying so is
# MAGIC better than implying an efficiency nobody measured.
# MAGIC
# MAGIC ## The tables are discovered rather than listed
# MAGIC
# MAGIC The change feed is configured per schema, so a Postgres table added
# MAGIC tomorrow appears as `lb_<name>_history` without anybody editing anything.
# MAGIC This notebook finds them by pattern for the same reason: a hardcoded list
# MAGIC would quietly stop covering the schema the day it grew.

# COMMAND ----------

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.dropdown("reread", "no", ["no", "yes"], "Ignore the watermark")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
REREAD = dbutils.widgets.get("reread") == "yes"

EVENTS = f"{CATALOG}.{SCHEMA}.silver_application_events"
ACTIVITY = f"{CATALOG}.{SCHEMA}.gold_application_activity"

print(f"{EVENTS}\n{ACTIVITY}")
if REREAD:
    print("\nreread: every history row is read again, watermark ignored.")
    print("Safe, because the merge is keyed on the change itself and inserts")
    print("nothing that is already there.")

# COMMAND ----------

from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql import types as T  # noqa: E402

#: The system columns the change data feed adds to every history table.
#: Named here because the notebook refers to them repeatedly and because a
#: reader meeting `_sort_by` for the first time deserves to be told what it is.
FEED_COLUMNS = {
    "_pg_change_type": "insert, delete, update_preimage or update_postimage",
    "_pg_lsn": "Postgres log sequence number, the position in the write-ahead log",
    "_pg_xid": "Postgres transaction id",
    "_timestamp": "when the change was processed into Delta",
    "_sort_by": "monotonic ordering key within the feed",
}

history = [
    row.tableName
    for row in spark.sql(f"SHOW TABLES IN {CATALOG}.{SCHEMA}").collect()
    if row.tableName.startswith("lb_") and row.tableName.endswith("_history")
]

print(f"{len(history)} history table(s):")
for name in sorted(history):
    print(f"  {name}")

if not history:
    raise RuntimeError(
        "No lb_*_history tables. Either the change data feed is not configured, "
        "or every table in the Postgres schema was empty when it looked, which "
        "leaves them in CDF_STATE_SKIPPED. `00d_enable_lakebase_cdf` reports "
        "both."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## One shape for four different tables
# MAGIC
# MAGIC `agent_actions` carries a tool, a status and a latency. `alerts` carries a
# MAGIC threshold. `episode_labels` carries a cause. They have almost nothing in
# MAGIC common except that somebody did something at a time, and that is exactly
# MAGIC what this table is about, so they are normalised down to that and the
# MAGIC specifics are left in the history tables where they came from.
# MAGIC
# MAGIC A column that a source does not have arrives as null rather than as a
# MAGIC failure, so a table added to the Postgres schema later flows through
# MAGIC without an edit here.

# COMMAND ----------


def optional(frame, name: str, kind=T.StringType()):
    """The column if the source has it, a typed null if it does not."""
    return (
        F.col(name).cast(kind) if name in frame.columns else F.lit(None).cast(kind)
    )


def normalise(frame, table_name: str):
    """One history table, reduced to who did what, when, and how it went."""
    source = table_name[len("lb_"):-len("_history")]

    return frame.select(
        F.lit(source).alias("source_table"),
        F.col("_pg_change_type").alias("change_type"),
        F.col("_timestamp").alias("occurred_at"),
        F.to_date("_timestamp").alias("activity_date"),
        optional(frame, "created_by").alias("created_by"),
        optional(frame, "session_id").alias("session_id"),
        optional(frame, "tool").alias("tool"),
        optional(frame, "status").alias("status"),
        optional(frame, "latency_ms", T.LongType()).alias("latency_ms"),
        # Whatever identifies the thing that was written. `episode_key` for a
        # label or a note, the surrogate id for an alert or an action. Kept as
        # text because the point is to count distinct things, not to join.
        F.coalesce(
            optional(frame, "episode_key"),
            optional(frame, "id"),
        ).alias("target_key"),
        F.col("_pg_lsn").cast("long").alias("pg_lsn"),
        F.col("_sort_by").cast("long").alias("sort_by"),
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The silver table
# MAGIC
# MAGIC Created before the streams start rather than by the first write, so its
# MAGIC schema is stated in one place and a merge has something to merge into.

# COMMAND ----------

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {EVENTS} (
        source_table   STRING  COMMENT 'Postgres table the change came from.',
        change_type    STRING  COMMENT 'insert, delete, update_preimage or update_postimage. An update produces two rows, before and after.',
        occurred_at    TIMESTAMP COMMENT 'When the change was processed into Delta, not when the user acted. The two are seconds apart.',
        activity_date  DATE    COMMENT 'Partition and grouping key.',
        created_by     STRING  COMMENT 'The display name the visitor signed in with. Not a Databricks identity: the application holds the database credential and the visitor holds a name.',
        session_id     STRING  COMMENT 'Agent actions only.',
        tool           STRING  COMMENT 'Agent actions only: which tool was called.',
        status         STRING  COMMENT 'Agent actions only: ok, rejected or error. rejected is a write the validation refused and is not a failure of the system.',
        latency_ms     BIGINT  COMMENT 'Agent actions only.',
        target_key     STRING  COMMENT 'What was written to, where the source names one.',
        pg_lsn         BIGINT  COMMENT 'Position in the Postgres write-ahead log.',
        sort_by        BIGINT  COMMENT 'Monotonic ordering key from the change feed.'
    )
    USING DELTA
    PARTITIONED BY (activity_date)
    COMMENT 'Every change the application and the agent made in Lakebase, normalised to one shape. Built from the Lakebase change data feed, which is why it exists at all: without the feed these writes would live only in Postgres, unqueryable from the lakehouse and invisible in lineage.'
    """
)

print(f"{EVENTS} ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read the feed
# MAGIC
# MAGIC One pass per history table, each starting above the highest `sort_by`
# MAGIC already in silver for that source. A table that fails is reported and
# MAGIC skipped rather than taking the others with it: a table still
# MAGIC snapshotting is the ordinary case and not a reason to lose the run.

# COMMAND ----------


def watermark(source: str) -> int:
    """The highest change already recorded for one source, or -1 for none.

    Read from the target table rather than from a checkpoint. The position is
    then a column somebody can query, which is worth more than the automatic
    state management it replaces.
    """
    found = spark.sql(
        f"SELECT max(sort_by) AS high FROM {EVENTS} WHERE source_table = '{source}'"
    ).collect()
    high = found[0]["high"] if found else None
    return -1 if high is None else int(high)


#: Keyed on the change itself. An LSN and a sort key name one row in the
#: write-ahead log; the change type separates the two halves of an update,
#: which share everything else.
MERGE = f"""
MERGE INTO {EVENTS} AS target
USING incoming_events AS source
   ON target.source_table = source.source_table
  AND target.pg_lsn      = source.pg_lsn
  AND target.sort_by     = source.sort_by
  AND target.change_type = source.change_type
WHEN NOT MATCHED THEN INSERT *
"""

processed = {}
failures = {}

for table_name in sorted(history):
    source = table_name[len("lb_"):-len("_history")]
    full = f"{CATALOG}.{SCHEMA}.{table_name}"
    try:
        high = -1 if REREAD else watermark(source)
        frame = spark.table(full).filter(F.col("_sort_by") > F.lit(high))
        incoming = normalise(frame, table_name)

        found = incoming.count()
        processed[table_name] = found
        if found:
            # A temporary view and a SQL MERGE rather than the DeltaTable
            # Python API. Both work, and SQL is the one that behaves the same
            # on every compute this might run on, which after the streaming
            # attempt is a property worth paying a little verbosity for.
            incoming.createOrReplaceTempView("incoming_events")
            spark.sql(MERGE)

        print(f"  {table_name:<34} from sort_by > {high:<14} {found:>8,} new row(s)")
    except Exception as exc:
        failures[table_name] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
        print(f"  {table_name:<34} FAILED, skipped")

if failures:
    print("\nFailed:")
    for name, reason in failures.items():
        print(f"  {name}: {reason}")

print(f"\n{sum(processed.values()):,} new event(s) this run")
print(f"{spark.table(EVENTS).count():,} event(s) in {EVENTS}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Where each source stands
# MAGIC
# MAGIC The watermark, after the run. This is the whole of the incremental
# MAGIC state, and it is four numbers in a table rather than a directory nobody
# MAGIC can read.

# COMMAND ----------

(
    spark.table(EVENTS)
    .groupBy("source_table")
    .agg(
        F.count("*").alias("events"),
        F.max("sort_by").alias("watermark"),
        F.min("occurred_at").alias("first"),
        F.max("occurred_at").alias("last"),
    )
    .orderBy("source_table")
    .show(truncate=False)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The gold table
# MAGIC
# MAGIC One row per day per source table per change type per tool per status.
# MAGIC Wide enough that the three questions this exists to answer are each a
# MAGIC filter away, narrow enough that nobody has to pivot anything.
# MAGIC
# MAGIC Latency is carried as a sum and a count rather than as an average, so an
# MAGIC average over any subset of rows is still right. An average of averages
# MAGIC is the kind of number that looks fine and is not.

# COMMAND ----------

events = spark.table(EVENTS)

activity = (
    events.groupBy(
        "activity_date", "source_table", "change_type", "tool", "status"
    )
    .agg(
        F.count("*").alias("events"),
        F.countDistinct("created_by").alias("authors"),
        F.countDistinct("session_id").alias("sessions"),
        F.countDistinct("target_key").alias("targets"),
        F.sum("latency_ms").alias("latency_ms_total"),
        F.count("latency_ms").alias("latency_ms_count"),
        F.min("occurred_at").alias("first_at"),
        F.max("occurred_at").alias("last_at"),
    )
    .withColumn(
        "avg_latency_ms",
        F.round(F.col("latency_ms_total") / F.col("latency_ms_count"), 1),
    )
)

(
    activity.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .partitionBy("activity_date")
    .saveAsTable(ACTIVITY)
)

written = spark.table(ACTIVITY).count()
print(f"{written:,} rows in {ACTIVITY}")

# COMMAND ----------

COMMENTS = {
    "activity_date": "The day the change reached Delta, UTC.",
    "source_table": "Postgres table the change came from, without the lb_ and "
                    "_history the feed wraps it in.",
    "change_type": "insert, delete, update_preimage or update_postimage. An "
                   "update is two rows: counting both as activity would double "
                   "every edit, so filter to update_postimage when counting "
                   "edits.",
    "tool": "Which agent tool was called. Null for a write that did not come "
            "through the agent.",
    "status": "ok, rejected or error. rejected is a write the validation "
              "refused, which is the system working rather than failing, and "
              "the distinction is what makes a tool success rate mean anything.",
    "events": "Changes at this grain.",
    "authors": "Distinct display names. The application holds the database "
               "credential; the visitor holds a name.",
    "sessions": "Distinct agent sessions. Null-heavy outside agent_actions.",
    "targets": "Distinct things written to, where the source names one.",
    "latency_ms_total": "Sum, not an average, so an average over any subset of "
                        "these rows is still correct.",
    "latency_ms_count": "How many of the events carried a latency at all.",
    "avg_latency_ms": "The average at this grain. Derived from the two columns "
                      "above rather than stored independently of them.",
    "first_at": "Earliest change at this grain.",
    "last_at": "Latest change at this grain.",
}

for column, comment in COMMENTS.items():
    spark.sql(
        f"ALTER TABLE {ACTIVITY} ALTER COLUMN {column} "
        f"COMMENT '{comment.replace(chr(39), chr(39) * 2)}'"
    )

spark.sql(
    f"COMMENT ON TABLE {ACTIVITY} IS "
    "'What the application and the agent actually did, built from the Lakebase "
    "change data feed. The only table in this project that measures the product "
    "rather than the market. Its existence depends on the reverse flow: these "
    "writes happen in Postgres, and without the feed they would never be "
    "queryable from the lakehouse or visible in lineage.'"
)

print(f"{len(COMMENTS)} column comments set")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What it says
# MAGIC
# MAGIC Read back from the table. Small numbers at this stage are expected and
# MAGIC honest: the application has had a handful of real users and one of them
# MAGIC is a probe row from `00d`.

# COMMAND ----------

gold = spark.table(ACTIVITY)

print("activity by source table:")
(
    gold.groupBy("source_table")
    .agg(
        F.sum("events").alias("events"),
        F.countDistinct("activity_date").alias("days"),
        F.min("first_at").alias("first"),
        F.max("last_at").alias("last"),
    )
    .orderBy(F.desc("events"))
    .show(truncate=False)
)

print("agent tools, and whether they worked:")
(
    gold.filter(F.col("tool").isNotNull())
    .groupBy("tool")
    .agg(
        F.sum("events").alias("calls"),
        F.sum(F.when(F.col("status") == "ok", F.col("events")).otherwise(0)).alias("ok"),
        F.sum(F.when(F.col("status") == "rejected", F.col("events")).otherwise(0))
        .alias("rejected"),
        F.sum(F.when(F.col("status") == "error", F.col("events")).otherwise(0))
        .alias("error"),
        F.round(
            F.sum("latency_ms_total") / F.greatest(F.sum("latency_ms_count"), F.lit(1)),
            1,
        ).alias("avg_ms"),
    )
    .withColumn(
        # Deliberately not counting a rejection against the tool. A refused
        # write is the validation doing its job, and a success rate that
        # punishes it would push towards accepting bad input.
        "success_pct",
        F.round(100.0 * F.col("ok") / F.greatest(F.col("calls") - F.col("rejected"), F.lit(1)), 1),
    )
    .orderBy(F.desc("calls"))
    .show(truncate=False)
)

print("writes by table and change type:")
(
    gold.groupBy("source_table", "change_type")
    .agg(F.sum("events").alias("events"), F.max("authors").alias("authors"))
    .orderBy("source_table", "change_type")
    .show(truncate=False)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### The loop, closed
# MAGIC
# MAGIC Labels submitted in the browser are the evaluation's ground truth. This
# MAGIC counts them, and counts how often two people labelled the same episode
# MAGIC differently, which is worth reporting rather than hiding: an evaluation
# MAGIC against ground truth that nobody ever disagreed about has not been
# MAGIC tested.

# COMMAND ----------

labels = events.filter(
    (F.col("source_table") == "episode_labels")
    & (F.col("change_type").isin("insert", "update_postimage"))
)

count = labels.count()
print(f"{count:,} label event(s) in the feed")

if count:
    by_episode = (
        labels.groupBy("target_key")
        .agg(F.countDistinct("created_by").alias("authors"))
    )
    print(f"  {by_episode.count():,} distinct episode(s) labelled")
    print(f"  {by_episode.filter(F.col('authors') > 1).count():,} with more than one author")
    labels.groupBy("created_by").count().orderBy(F.desc("count")).show(truncate=False)

# COMMAND ----------

message = (
    f"{sum(processed.values()):,} new event(s), {written:,} rows in {ACTIVITY}"
)
if failures:
    message += f" | {len(failures)} history table(s) failed"
dbutils.notebook.exit(message)