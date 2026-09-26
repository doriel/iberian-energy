# Databricks notebook source
# MAGIC %md
# MAGIC # Generation per unit: silver to a gold table for the grid analyst
# MAGIC
# MAGIC Reads `silver_generation_per_unit` and writes `gold_unit_hourly_output`,
# MAGIC one row per unit per production type per hour, with what the unit
# MAGIC produced, what it usually produces at that hour, and the gap between the
# MAGIC two.
# MAGIC
# MAGIC This is the table the third persona works from, and the one that makes
# MAGIC the year of history in bronze pay for itself. Saying "Sines was 280 MW
# MAGIC below its usual output at 19:00" needs a usual, and a usual needs a year.
# MAGIC Seventy days of summer says nothing about February.
# MAGIC
# MAGIC ## The A03 blocks have to be expanded, and why that matters
# MAGIC
# MAGIC The documents publish `curveType` `A03`, a variable sized block: a
# MAGIC published point holds until the next published position, and the last one
# MAGIC holds until the end of the period. A unit that stops at 06:00 and
# MAGIC publishes nothing more does not publish a run of zeros, it publishes one
# MAGIC point and lets it stand.
# MAGIC
# MAGIC So a gold table built without expanding those blocks would simply not
# MAGIC have rows for the hours a plant was down, and `looks_offline` would never
# MAGIC once be true. The signal this table exists for would be missing exactly
# MAGIC when it mattered.
# MAGIC
# MAGIC Expansion needs to know where a block stops, which is why
# MAGIC `period_end_utc` is carried through silver. The two guesses somebody
# MAGIC would otherwise make, one resolution step or the end of the calendar day,
# MAGIC disagree by hours on a day where a unit stopped reporting at noon, and
# MAGIC one of them shows a plant producing through an afternoon it published
# MAGIC nothing for.
# MAGIC
# MAGIC ## How much of this table is a held value rather than a fresh reading
# MAGIC
# MAGIC Measured rather than assumed. Portuguese generation blocks have a median
# MAGIC of 60 minutes but a 95th percentile of 900, and 249,193 blocks cover
# MAGIC roughly 637,000 hours, so the average Portuguese hour carries a value
# MAGIC published about two and a half hours earlier. Spain is denser: a median
# MAGIC of 15 minutes and a 95th percentile of 60.
# MAGIC
# MAGIC That is not an error. With A03 the publisher is asserting the value held.
# MAGIC But "REN says it still held" and "it was measured at 19:00" are different
# MAGIC claims, and an outage attribution built on a figure last published at
# MAGIC dawn is weaker evidence than one built on a reading taken in the hour.
# MAGIC So `published_at_utc` and `source_block_minutes` travel with every row,
# MAGIC and the agent can tell which kind of number it is holding.
# MAGIC
# MAGIC ## Hourly means are time weighted
# MAGIC
# MAGIC Spain publishes quarter hourly and Portugal hourly, and an A03 block can
# MAGIC be any length. A plain average of the readings that fall in an hour would
# MAGIC weight a four hour block the same as a fifteen minute one. Every block is
# MAGIC weighted by the minutes of the hour it actually covers, and
# MAGIC `output_minutes` reports that coverage so a partial hour is visible
# MAGIC rather than looking like a full one.
# MAGIC
# MAGIC ## The judgements live in a tested module
# MAGIC
# MAGIC `iberian.analysis.generation_baseline`, covered by 27 tests. The median,
# MAGIC the deviation, the refusal to give a percentage against a baseline near
# MAGIC zero, and the offline rule are all decisions somebody could disagree
# MAGIC with, which is the argument for having them somewhere a test can reach
# MAGIC rather than inside a notebook cell.

# COMMAND ----------

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("source", "silver_generation_per_unit", "Source table")
dbutils.widgets.text("table", "gold_unit_hourly_output", "Target table")
dbutils.widgets.text("observations", "30", "Observations in the baseline window")
dbutils.widgets.text("offline_output_mw", "1.0", "At or below this, producing nothing")
dbutils.widgets.text("offline_baseline_mw", "10.0", "Baseline below this is not news")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
SOURCE = f"{CATALOG}.{SCHEMA}.{dbutils.widgets.get('source').strip()}"
TARGET = f"{CATALOG}.{SCHEMA}.{dbutils.widgets.get('table').strip()}"

OBSERVATIONS = int(dbutils.widgets.get("observations"))
OFFLINE_OUTPUT_MW = float(dbutils.widgets.get("offline_output_mw"))
OFFLINE_BASELINE_MW = float(dbutils.widgets.get("offline_baseline_mw"))

print(f"{SOURCE}\n  ->  {TARGET}")
print(
    f"baseline: median of the previous {OBSERVATIONS} observations at this local hour"
)

# COMMAND ----------

import os
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

SRC_PATH = os.path.join(REPO_ROOT, "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

# COMMAND ----------

from pyspark.sql import Window  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql import types as T  # noqa: E402

silver = spark.table(SOURCE)

print(f"{silver.count():,} rows in {SOURCE}")

missing_period_end = silver.filter(F.col("period_end_utc").isNull()).count()
print(f"rows with no period end: {missing_period_end:,}")
if missing_period_end:
    print(
        "  These fall back to one resolution step, which is the conservative\n"
        "  reading. Worth looking at if it is more than a handful: it means the\n"
        "  documents stopped carrying timeInterval/end."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Turning points into blocks
# MAGIC
# MAGIC Each reading covers the time from its own timestamp until the next
# MAGIC reading in the same series, or the end of its period if there is no next
# MAGIC one. `lead` runs over the whole year for a series rather than within a
# MAGIC day, so a block at 23:45 joins up with the next day without a special
# MAGIC case. The period end is what stops it running across a day the ingestion
# MAGIC never landed.

# COMMAND ----------

series_window = Window.partitionBy(
    "zone", "unit_eic", "psr_type", "flow_direction"
).orderBy("ts_utc")

# Epoch arithmetic rather than interval expressions: a timestamp plus an
# interval built from a column is the kind of thing whose syntax differs
# between Spark versions, and this is unambiguous everywhere.
one_step_end = F.to_timestamp(
    F.unix_timestamp("ts_utc") + F.coalesce(F.col("resolution_minutes"), F.lit(60)) * 60
)
period_bound = F.coalesce(F.col("period_end_utc"), one_step_end)

blocks = (
    silver.withColumn("next_ts_utc", F.lead("ts_utc").over(series_window))
    .withColumn("period_bound", period_bound)
    .withColumn(
        "block_end_utc",
        F.least(F.coalesce(F.col("next_ts_utc"), F.col("period_bound")), F.col("period_bound")),
    )
    .withColumn(
        "block_minutes",
        (F.unix_timestamp("block_end_utc") - F.unix_timestamp("ts_utc")) / 60.0,
    )
    .filter(F.col("block_end_utc") > F.col("ts_utc"))
)

print(f"{blocks.count():,} blocks with a positive length")

print("\nblock lengths in minutes, which say how sparse the A03 curve really is:")
(
    blocks.groupBy("zone", "flow_direction")
    .agg(
        F.count("*").alias("blocks"),
        F.round(F.expr("percentile_approx(block_minutes, 0.5)"), 1).alias("median"),
        F.round(F.expr("percentile_approx(block_minutes, 0.95)"), 1).alias("p95"),
        F.round(F.expr("percentile_approx(block_minutes, 0.99)"), 1).alias("p99"),
        F.sum((F.col("block_minutes") > 180).cast("int")).alias("over_3h"),
    )
    .orderBy("zone", "flow_direction")
    .show(truncate=False)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Blocks to hours
# MAGIC
# MAGIC A block is cut into the hours it touches and each piece carries the
# MAGIC minutes it covers. A fifteen minute reading contributes fifteen minutes
# MAGIC to one hour; a four hour block contributes sixty minutes to each of four.
# MAGIC The hourly figure is then the mean weighted by those minutes, which is
# MAGIC the average output over the hour rather than the average of whatever
# MAGIC readings happened to land in it.

# COMMAND ----------

hour_end = F.to_timestamp(F.unix_timestamp("hour_utc") + 3600)
overlap_start = F.greatest(F.col("ts_utc"), F.col("hour_utc"))
overlap_end = F.least(F.col("block_end_utc"), hour_end)

pieces = (
    blocks.withColumn(
        "hour_utc",
        F.explode(
            F.expr(
                "sequence(date_trunc('HOUR', ts_utc), "
                "date_trunc('HOUR', block_end_utc - INTERVAL 1 SECOND), "
                "INTERVAL 1 HOUR)"
            )
        ),
    )
    .withColumn(
        "minutes",
        (F.unix_timestamp(overlap_end) - F.unix_timestamp(overlap_start)) / 60.0,
    )
    .filter(F.col("minutes") > 0)
)

generating = F.col("flow_direction") == "generation"
consuming = F.col("flow_direction") == "consumption"

hourly = (
    pieces.groupBy("zone", "unit_eic", "psr_type", "hour_utc")
    .agg(
        F.max("unit_name").alias("unit_name"),
        F.max("psr_label").alias("psr_label"),
        F.sum(F.when(generating, F.col("quantity_mw") * F.col("minutes"))).alias(
            "generation_mw_minutes"
        ),
        F.sum(F.when(generating, F.col("minutes"))).alias("output_minutes"),
        F.sum(F.when(consuming, F.col("quantity_mw") * F.col("minutes"))).alias(
            "consumption_mw_minutes"
        ),
        F.sum(F.when(consuming, F.col("minutes"))).alias("consumption_minutes"),
        F.min(F.when(generating, F.col("ts_utc"))).alias("published_at_utc"),
        F.max(F.when(generating, F.col("block_minutes"))).alias("source_block_minutes"),
    )
    .withColumn(
        "output_mw",
        F.round(F.col("generation_mw_minutes") / F.col("output_minutes"), 3),
    )
    .withColumn(
        "consumption_mw",
        F.round(F.col("consumption_mw_minutes") / F.col("consumption_minutes"), 3),
    )
    .withColumn("output_minutes", F.round(F.col("output_minutes"), 2))
    .withColumn("consumption_minutes", F.round(F.col("consumption_minutes"), 2))
    .withColumn("source_block_minutes", F.round(F.col("source_block_minutes"), 1))
    .drop("generation_mw_minutes", "consumption_mw_minutes")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The local hour, which is what the fleet actually follows
# MAGIC
# MAGIC The key stays UTC, because every other table in this project joins on
# MAGIC UTC and a second time base is how two correct tables disagree. But the
# MAGIC baseline groups by the **local** hour, because a plant's routine follows
# MAGIC the clock on the wall. A year contains two daylight saving changes, and a
# MAGIC baseline built on the UTC hour would compare 19:00 in July against 20:00
# MAGIC local in January, which is a different point in the evening ramp.

# COMMAND ----------

local_time = F.when(
    F.col("zone") == "PT", F.from_utc_timestamp("hour_utc", "Europe/Lisbon")
).otherwise(F.from_utc_timestamp("hour_utc", "Europe/Madrid"))

hourly = (
    hourly.withColumn("local_hour", F.hour(local_time))
    .withColumn("market_day", F.to_date("hour_utc"))
    .withColumn("market_month", F.date_format("hour_utc", "yyyy-MM"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The baseline
# MAGIC
# MAGIC The previous thirty **observations** of this unit at this local hour, not
# MAGIC the previous thirty days. For a unit that reported every day those are
# MAGIC the same thing. For a unit commissioned in March, or one that only runs
# MAGIC in winter, the window reaches further back, and a baseline drawn from
# MAGIC last February is worth knowing about rather than worth hiding. So
# MAGIC `baseline_observations` and `baseline_span_days` come back beside the
# MAGIC number.
# MAGIC
# MAGIC The current row is excluded, so an hour never helps set the baseline it
# MAGIC is being compared against.

# COMMAND ----------

baseline_window = (
    Window.partitionBy("zone", "unit_eic", "psr_type", "local_hour")
    .orderBy("hour_utc")
    .rowsBetween(-OBSERVATIONS, -1)
)

with_history = hourly.withColumn(
    "history", F.collect_list("output_mw").over(baseline_window)
).withColumn(
    "baseline_span_days",
    F.datediff(F.col("market_day"), F.to_date(F.min("hour_utc").over(baseline_window))),
)

# COMMAND ----------

ASSESSMENT = T.StructType(
    [
        T.StructField("baseline_mw", T.DoubleType()),
        T.StructField("baseline_observations", T.IntegerType()),
        T.StructField("deviation_mw", T.DoubleType()),
        T.StructField("deviation_pct", T.DoubleType()),
        T.StructField("looks_offline", T.BooleanType()),
    ]
)

_src = SRC_PATH
_offline_output = OFFLINE_OUTPUT_MW
_offline_baseline = OFFLINE_BASELINE_MW


@F.udf(returnType=ASSESSMENT)
def assess_hour(output_mw, history):
    """Wrapper. No judgement here, on purpose: it all lives in the module."""
    import sys

    if _src not in sys.path:
        sys.path.insert(0, _src)

    from iberian.analysis.generation_baseline import assess

    return assess(output_mw, history, _offline_output, _offline_baseline)

# COMMAND ----------

# MAGIC %md
# MAGIC One unit first, so an import that does not reach the executors fails in
# MAGIC seconds rather than after the whole year has been shuffled.

# COMMAND ----------

probe = (
    with_history.filter(F.col("output_mw").isNotNull())
    .limit(1)
    .select("zone", "unit_eic", "hour_utc", "output_mw", F.size("history").alias("seen"),
            assess_hour("output_mw", "history").alias("assessment"))
    .collect()[0]
)

print(f"{probe['zone']} {probe['unit_eic']} {probe['hour_utc']}")
print(f"  output {probe['output_mw']} MW, {probe['seen']} observations behind it")
print(f"  {probe['assessment'].asDict()}")

# COMMAND ----------

assessed = (
    with_history.withColumn("assessment", assess_hour("output_mw", "history"))
    .select(
        "zone",
        "unit_eic",
        "unit_name",
        "psr_type",
        "psr_label",
        "hour_utc",
        "local_hour",
        "output_mw",
        "output_minutes",
        "consumption_mw",
        "consumption_minutes",
        "published_at_utc",
        "source_block_minutes",
        F.col("assessment.baseline_mw").alias("baseline_mw"),
        F.col("assessment.baseline_observations").alias("baseline_observations"),
        F.col("baseline_span_days"),
        F.round(F.col("assessment.deviation_mw"), 3).alias("deviation_mw"),
        F.round(F.col("assessment.deviation_pct"), 2).alias("deviation_pct"),
        F.col("assessment.looks_offline").alias("looks_offline"),
        "market_day",
        "market_month",
    )
)

(
    assessed.write.mode("overwrite")
    .partitionBy("zone", "market_month")
    .option("overwriteSchema", "true")
    .saveAsTable(TARGET)
)

written = spark.table(TARGET).count()
print(f"{written:,} rows in {TARGET}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Column comments
# MAGIC
# MAGIC The ones that carry a decision. Anything a person querying this table
# MAGIC could reasonably misread is written down where the catalogue will show
# MAGIC it to them.

# COMMAND ----------

COMMENTS = {
    "zone": "Bidding zone, ES or PT.",
    "unit_eic": "EIC code of the generation unit. Not unique on its own: a unit "
                "reports once per production type.",
    "unit_name": "Unit name as published, for reading rather than joining.",
    "psr_type": "ENTSO-E production type code.",
    "psr_label": "The same code in words.",
    "hour_utc": "Start of the hour, UTC. This is the join key: every other table "
                "in this project is on UTC and a second time base is how two "
                "correct tables come to disagree.",
    "local_hour": "Hour of the day in Iberian local time, 0 to 23. What the "
                  "baseline groups by, because a plant's routine follows the clock "
                  "on the wall and a year contains two daylight saving changes.",
    "output_mw": "Mean output over the hour, weighted by the minutes each reading "
                 "actually covers. Generation only. Null means the unit published "
                 "no generation for this hour, which is not the same as zero.",
    "output_minutes": "Minutes of the hour covered by a published generation "
                      "reading, 0 to 60. Below 60 means a partial hour, and "
                      "output_mw is the mean over the part that was published.",
    "consumption_mw": "The same for the unit's own consumption, which arrives as a "
                      "separate positive series. Deliberately a second column and "
                      "never netted off: they are different quantities.",
    "consumption_minutes": "Coverage for consumption_mw.",
    "published_at_utc": "When the generation reading behind output_mw was published. "
                        "With curveType A03 a value holds until the next position, so "
                        "this can be hours before hour_utc. Equal to hour_utc means a "
                        "reading taken in this hour; earlier means a value the "
                        "publisher asserts still held.",
    "source_block_minutes": "How long the longest block behind this hour runs. 15 or "
                            "60 is a fresh reading. 1440 is one point published for a "
                            "whole day. Provenance, not quality: a long block is the "
                            "publisher saying nothing changed. But an outage claim "
                            "built on a value held since dawn is weaker evidence than "
                            "one built on a reading taken in the hour, and the agent "
                            "should be able to tell the difference.",
    "baseline_mw": "Median output of this unit at this local hour over the previous "
                   "30 observations, excluding this one. A median rather than a "
                   "mean because the thing being detected is a unit behaving "
                   "unusually, and a mean is dragged down by exactly those events.",
    "baseline_observations": "How many readings the baseline is made of. Small "
                             "means a thin baseline and a deviation to be careful "
                             "with.",
    "baseline_span_days": "How far back those observations reach. A window of 30 "
                          "observations spanning 200 days is a unit that rarely "
                          "runs, and the baseline is stale rather than wrong.",
    "deviation_mw": "output_mw minus baseline_mw. Negative is below usual.",
    "deviation_pct": "The same as a percentage, null when the baseline is under 1 "
                     "MW. A percentage against a near zero baseline is arithmetic "
                     "rather than information, and somebody sorts by it descending.",
    "looks_offline": "Producing at or below 1 MW when the baseline is at least 10 "
                     "MW. A SIGNAL, not a finding: nothing here knows whether the "
                     "cause was maintenance, a forced outage, no water, or a price "
                     "below marginal cost. Corroborate against an A80 notice before "
                     "calling it an outage.",
    "market_day": "UTC date of hour_utc.",
    "market_month": "Partition column.",
}

for column, comment in COMMENTS.items():
    spark.sql(
        f"ALTER TABLE {TARGET} ALTER COLUMN {column} "
        f"COMMENT '{comment.replace(chr(39), chr(39) * 2)}'"
    )

spark.sql(
    f"COMMENT ON TABLE {TARGET} IS "
    "'Hourly output per generation unit for Spain and Portugal against that "
    "unit''s own recent behaviour at the same local hour. Serves the grid "
    "analyst and supplies the retrieved facts behind an outage explanation. "
    "looks_offline is a signal to corroborate against an A80 notice, not a "
    "finding. consumption_mw is a separate quantity and is never netted against "
    "output_mw.'"
)

print(f"{len(COMMENTS)} column comments set")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What is in there

# COMMAND ----------

gold = spark.table(TARGET)

gold.groupBy("zone").agg(
    F.count("*").alias("rows"),
    F.countDistinct("unit_eic").alias("units"),
    F.countDistinct("market_day").alias("days"),
    F.min("hour_utc").alias("first"),
    F.max("hour_utc").alias("last"),
).show(truncate=False)

print("hour coverage, which says how much of each hour was actually published:")
(
    gold.filter(F.col("output_minutes").isNotNull())
    .withColumn(
        "coverage",
        F.when(F.col("output_minutes") >= 59.9, "full hour")
        .when(F.col("output_minutes") >= 30, "30 to 60 minutes")
        .otherwise("under 30 minutes"),
    )
    .groupBy("zone", "coverage")
    .count()
    .orderBy("zone", "coverage")
    .show(truncate=False)
)

print("how many rows have a baseline behind them:")
(
    gold.withColumn(
        "baseline",
        F.when(F.col("baseline_observations") >= 20, "20 or more observations")
        .when(F.col("baseline_observations") > 0, "1 to 19, thin")
        .otherwise("none yet"),
    )
    .groupBy("baseline")
    .count()
    .orderBy(F.desc("count"))
    .show(truncate=False)
)

print("how fresh the figure behind each hour is:")
(
    gold.filter(F.col("output_mw").isNotNull())
    .withColumn(
        "freshness",
        F.when(F.col("source_block_minutes") <= 60, "published in this hour")
        .when(F.col("source_block_minutes") <= 180, "held up to 3 hours")
        .when(F.col("source_block_minutes") < 1440, "held 3 to 24 hours")
        .otherwise("one point for the whole day"),
    )
    .groupBy("zone", "freshness")
    .count()
    .orderBy("zone", F.desc("count"))
    .show(truncate=False)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### The offline signal, which is the point of the table
# MAGIC
# MAGIC Printed rather than asserted, because the right number is not known in
# MAGIC advance. What would be wrong is an implausible one: a few per cent of
# MAGIC hours is a fleet with plants out, and thirty per cent is a bug.

# COMMAND ----------

offline_rows = gold.filter(F.col("looks_offline")).count()
with_baseline = gold.filter(F.col("baseline_mw").isNotNull()).count()

print(f"hours flagged as looking offline: {offline_rows:,}")
if with_baseline:
    print(f"  {100 * offline_rows / with_baseline:.2f}% of the hours that have a baseline")

print("\nby production type:")
(
    gold.filter(F.col("looks_offline"))
    .groupBy("zone", "psr_label")
    .agg(
        F.count("*").alias("hours"),
        F.countDistinct("unit_eic").alias("units"),
        F.round(F.avg("baseline_mw"), 1).alias("avg_baseline_mw"),
    )
    .orderBy(F.desc("hours"))
    .show(10, truncate=False)
)

print("offline hours by how fresh the zero is, which is what the agent needs:")
(
    gold.filter(F.col("looks_offline"))
    .withColumn(
        "freshness",
        F.when(F.col("source_block_minutes") <= 60, "published in this hour")
        .when(F.col("source_block_minutes") < 1440, "held from earlier")
        .otherwise("one point for the whole day"),
    )
    .groupBy("zone", "freshness")
    .count()
    .orderBy("zone", F.desc("count"))
    .show(truncate=False)
)

print("the largest shortfalls, as a sample somebody can go and check:")
(
    gold.filter(F.col("baseline_mw") > 50)
    .orderBy("deviation_mw")
    .select("zone", "unit_name", "psr_label", "hour_utc", "output_mw", "baseline_mw",
            "deviation_mw", "looks_offline")
    .show(10, truncate=False)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### The checks worth failing on
# MAGIC
# MAGIC These three are asserted rather than printed, because unlike a duplicate
# MAGIC in silver there is no reading of the data under which any of them is
# MAGIC acceptable. A key that repeats, an hour with more than sixty minutes in
# MAGIC it, or a deviation without a baseline are all this notebook being wrong.

# COMMAND ----------

duplicates = (
    gold.groupBy("zone", "unit_eic", "psr_type", "hour_utc")
    .count()
    .filter(F.col("count") > 1)
    .count()
)
print(f"duplicate keys (zone, unit, type, hour): {duplicates:,}")
assert duplicates == 0, "the hourly grain is not unique, which breaks every join"

overfull = gold.filter(
    (F.col("output_minutes") > 60.01) | (F.col("consumption_minutes") > 60.01)
).count()
print(f"hours with more than sixty minutes in them: {overfull:,}")
assert overfull == 0, "blocks are overlapping, so the weighted mean is wrong"

orphan_deviation = gold.filter(
    F.col("deviation_mw").isNotNull() & F.col("baseline_mw").isNull()
).count()
print(f"deviations with no baseline behind them: {orphan_deviation:,}")
assert orphan_deviation == 0, "a deviation from nothing is not a number"

impossible_offline = gold.filter(
    F.col("looks_offline") & (F.col("output_mw") > OFFLINE_OUTPUT_MW)
).count()
print(f"offline flags on units that were producing: {impossible_offline:,}")
assert impossible_offline == 0, "the offline rule did not do what it says"

print("\nAll four hold.")

# COMMAND ----------

message = f"{written:,} rows in {TARGET} | {offline_rows:,} hours look offline"
dbutils.notebook.exit(message)