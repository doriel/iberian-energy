# Databricks notebook source
# MAGIC %md
# MAGIC # Generation per unit: bronze XML to a silver table
# MAGIC
# MAGIC Reads the 728 documents `01c_ingest_generation` landed and writes
# MAGIC `silver_generation_per_unit`, one row per unit per production type per
# MAGIC direction per interval. About 2.5 million rows over a year.
# MAGIC
# MAGIC ## Direction is part of the key
# MAGIC
# MAGIC A unit publishes generation and consumption as two separate TimeSeries,
# MAGIC both positive, both `businessType` `A01`, distinguished only by whether
# MAGIC the document carries `inBiddingZone_Domain` or `outBiddingZone_Domain`.
# MAGIC The first build of this table did not read that element and reported
# MAGIC 39,061 duplicate keys as a result. They were not duplicates. They were
# MAGIC Aguieira pumping and Aguieira generating in the same hour, and Lares and
# MAGIC Carrapatelo doing the same with their own station consumption.
# MAGIC
# MAGIC The consequence for anybody querying this table: **`quantity_mw` is
# MAGIC always positive and a consumption row is not a negative generation row.**
# MAGIC An aggregate over output has to filter `flow_direction = 'generation'` or
# MAGIC it adds consumption to production and reports a larger fleet than exists.
# MAGIC
# MAGIC ## Why this is a Spark job and not the pattern used elsewhere
# MAGIC
# MAGIC The declarative pipeline pulls raw payloads to the driver with
# MAGIC `.toPandas()` and parses them there. That is a reasonable choice at six
# MAGIC megabytes and the wrong one at 579: the driver would hold every document
# MAGIC in memory and parse them one after another, on one core.
# MAGIC
# MAGIC Here the documents are read as a DataFrame, parsed by a UDF that runs on
# MAGIC the executors, and exploded into rows. The parallelism is over files, so
# MAGIC 728 parses happen across the cluster instead of in a loop.
# MAGIC
# MAGIC ## The parser is the tested one
# MAGIC
# MAGIC `iberian.parsing.entsoe_generation`, the same module 22 tests cover. The
# MAGIC UDF is a wrapper around it and contains no parsing logic of its own,
# MAGIC because logic that lives in a notebook is logic no test ever sees. That
# MAGIC matters more than it sounds: the awkward parts here, sparse positions and
# MAGIC two zones publishing at different resolutions, are exactly the parts a
# MAGIC reimplementation would get subtly wrong.
# MAGIC
# MAGIC ## One file first
# MAGIC
# MAGIC Whether a module on the Workspace can be imported inside a UDF depends on
# MAGIC the runtime and the access mode, and the documentation is not explicit for
# MAGIC every combination. So the job parses a single document and prints the row
# MAGIC count before it touches the rest. If the import does not reach the
# MAGIC executors, that fails in seconds rather than after twenty minutes of
# MAGIC otherwise wasted work.

# COMMAND ----------

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("volume", "raw", "Volume for bronze")
dbutils.widgets.text("table", "silver_generation_per_unit", "Target table")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
VOLUME = dbutils.widgets.get("volume").strip()
TABLE = dbutils.widgets.get("table").strip()

SOURCE = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}/entsoe/actual_generation_per_unit"
TARGET = f"{CATALOG}.{SCHEMA}.{TABLE}"

print(f"{SOURCE}\n  ->  {TARGET}")

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

# MAGIC %md
# MAGIC ## Reading the documents
# MAGIC
# MAGIC `wholetext` so each file is one row rather than one row per line: an XML
# MAGIC document split across lines is not parseable a line at a time.
# MAGIC
# MAGIC The zone and the day come from the file path rather than from the
# MAGIC document. The path is what the ingestion controlled and therefore knows
# MAGIC to be right, and reading them out of the XML would be a second source of
# MAGIC truth that can disagree with the first.

# COMMAND ----------

from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql import types as T  # noqa: E402

# `wholetext` is a parameter here and NOT `.option("wholetext", "true")`.
# `DataFrameReader.text()` takes `wholetext=False` as its default and sets it on
# the way through, so an option set beforehand is silently overridden and every
# line of the file becomes a row. The parser then meets an XML declaration on
# its own and fails somewhere that says nothing about the reader.
documents = (
    spark.read.text(SOURCE, wholetext=True, recursiveFileLookup=True)
    .select(
        F.col("value").alias("xml"),
        F.col("_metadata.file_path").alias("file_path"),
    )
    .withColumn("zone", F.regexp_extract("file_path", r"zone=([A-Z]{2})", 1))
    .withColumn(
        "market_day",
        F.to_date(F.regexp_extract("file_path", r"(\d{4}-\d{2}-\d{2})\.xml$", 1)),
    )
)

count = documents.count()
print(f"{count} documents")

print("\nby zone:")
documents.groupBy("zone").agg(
    F.count("*").alias("documents"),
    F.min("market_day").alias("first"),
    F.max("market_day").alias("last"),
).show(truncate=False)

# A path that did not yield a zone or a day means the layout changed, and every
# row from that file would be filed under an empty zone without complaint.
unlabelled = documents.filter((F.col("zone") == "") | F.col("market_day").isNull())
if unlabelled.count():
    unlabelled.select("file_path").show(5, truncate=False)
    raise RuntimeError("Some documents have no zone or day in their path.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The parser, as a UDF
# MAGIC
# MAGIC `SRC_PATH` is captured as a plain string and re-added to `sys.path` inside
# MAGIC the function. A path appended on the driver is not necessarily on the
# MAGIC executors, and an import that works interactively and not under Spark is
# MAGIC a confusing half hour.

# COMMAND ----------

POINT = T.StructType(
    [
        T.StructField("unit_eic", T.StringType()),
        T.StructField("unit_name", T.StringType()),
        T.StructField("psr_type", T.StringType()),
        T.StructField("psr_label", T.StringType()),
        T.StructField("flow_direction", T.StringType()),
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("resolution_minutes", T.IntegerType()),
        T.StructField("quantity_mw", T.DoubleType()),
        T.StructField("position", T.IntegerType()),
        T.StructField("curve_type", T.StringType()),
    ]
)

_src = SRC_PATH


@F.udf(returnType=T.ArrayType(POINT))
def parse_generation(xml, zone):
    """Wrapper. No parsing logic here, on purpose."""
    import sys

    if _src not in sys.path:
        sys.path.insert(0, _src)

    from iberian.parsing.entsoe_generation import generation_rows

    return generation_rows(xml or "", zone or "")

# COMMAND ----------

# MAGIC %md
# MAGIC One document first, to prove the import reaches the executors.

# COMMAND ----------

probe = (
    documents.limit(1)
    .select("zone", "market_day", parse_generation("xml", "zone").alias("points"))
    .select("zone", "market_day", F.size("points").alias("rows"))
    .collect()[0]
)

print(f"{probe['zone']} {probe['market_day']}: {probe['rows']:,} rows from one document")

if probe["rows"] == 0:
    raise RuntimeError(
        "The parser returned nothing for a document that has bytes. Either the "
        "import did not reach the executors, or the document shape has changed."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Parse and explode
# MAGIC
# MAGIC Repartitioned before the UDF. Without it the work is spread over however
# MAGIC many partitions the reader chose, which for 728 whole-text files can be
# MAGIC a handful of large ones, leaving most of the cluster idle while a few
# MAGIC tasks parse a hundred documents each.

# COMMAND ----------

PARTITIONS = 64

rows = (
    documents.repartition(PARTITIONS)
    .select(
        "zone",
        "market_day",
        F.explode(parse_generation("xml", "zone")).alias("point"),
    )
    .select(
        "zone",
        "market_day",
        F.col("point.unit_eic").alias("unit_eic"),
        F.col("point.unit_name").alias("unit_name"),
        F.col("point.psr_type").alias("psr_type"),
        F.col("point.psr_label").alias("psr_label"),
        F.col("point.flow_direction").alias("flow_direction"),
        F.col("point.ts_utc").alias("ts_utc"),
        F.col("point.resolution_minutes").alias("resolution_minutes"),
        F.col("point.quantity_mw").alias("quantity_mw"),
        F.col("point.position").alias("position"),
        F.col("point.curve_type").alias("curve_type"),
    )
    # A partition column, so a year of data does not have to be read to answer a
    # question about one month. Month rather than day: 728 daily partitions of
    # four thousand rows each is the small files problem by construction.
    .withColumn("market_month", F.date_format("market_day", "yyyy-MM"))
)

# COMMAND ----------

(
    rows.write.mode("overwrite")
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
# MAGIC The ones that carry a decision rather than a definition. `quantity_mw`
# MAGIC and `resolution_minutes` both encode a choice somebody querying this table
# MAGIC needs to know about, and a comment in the catalogue reaches them where a
# MAGIC comment in this notebook does not.

# COMMAND ----------

COMMENTS = {
    "zone": "Bidding zone the control area belongs to, from the ingestion path.",
    "unit_eic": "EIC code of the generation unit. Not unique on its own: a unit "
                "reports once per production type and once per direction.",
    "unit_name": "Unit name as published. Free text, and not stable enough to key on.",
    "psr_type": "ENTSO-E production type code, B01 to B25.",
    "psr_label": "The same code in words. An unrecognised code is carried through "
                 "as itself rather than dropped.",
    "flow_direction": "generation or consumption, from inBiddingZone_Domain against "
                      "outBiddingZone_Domain on the TimeSeries. Part of the key: a "
                      "unit publishes both for the same interval, and not only "
                      "pumped storage. Filter on this before summing output.",
    "ts_utc": "Start of the interval, UTC. Derived from the period start plus "
              "position times resolution.",
    "resolution_minutes": "Length of the interval. Spain publishes 15, Portugal 60. "
                          "Nothing here resamples either: upsampling the Portuguese "
                          "hour would invent three readings nobody measured.",
    "quantity_mw": "As published, and always positive. A consumption reading is NOT "
                   "a negative generation reading: it arrives in its own series and "
                   "is left positive, so SUM(quantity_mw) without a flow_direction "
                   "filter adds consumption to production.",
    "position": "Position within the period, one based. Kept because gaps are not "
                "filled and this is what shows one.",
    "curve_type": "ENTSO-E curve type, A03 here: a variable sized block, where a "
                  "published point holds until the next position. Nothing here "
                  "expands those blocks, so carrying a value forward is a decision "
                  "for whoever aggregates, made in the open.",
    "market_day": "The day the document covers, from the ingestion path.",
    "market_month": "Partition column.",
}

for column, comment in COMMENTS.items():
    spark.sql(
        f"ALTER TABLE {TARGET} ALTER COLUMN {column} COMMENT '{comment.replace(chr(39), chr(39) * 2)}'"
    )

spark.sql(
    f"COMMENT ON TABLE {TARGET} IS "
    "'Actual generation per generation unit [16.1.A] for Spain and Portugal, "
    "one row per unit per production type per direction per interval. A year of "
    "history, deliberately deeper than the seventy day market window, because "
    "saying a unit produced less than usual needs a baseline. Read flow_direction "
    "before aggregating: consumption rows are positive and a sum that ignores "
    "them reports a larger fleet than exists.'"
)

print(f"{len(COMMENTS)} column comments set")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What is in there
# MAGIC
# MAGIC Read back from the table rather than trusted from the counters. Every
# MAGIC number below is one somebody could be asked to defend.

# COMMAND ----------

table = spark.table(TARGET)

summary = table.groupBy("zone").agg(
    F.count("*").alias("rows"),
    F.countDistinct("unit_eic").alias("units"),
    F.countDistinct("market_day").alias("days"),
    F.min("ts_utc").alias("first"),
    F.max("ts_utc").alias("last"),
)
summary.show(truncate=False)

print("resolutions, which differ by zone on purpose:")
table.groupBy("zone", "resolution_minutes").count().orderBy("zone").show()

print("direction, the column this build exists to add:")
table.groupBy("zone", "flow_direction").count().orderBy("zone", "flow_direction").show()

print("which production types publish consumption, and how much of their output it is:")
(
    table.groupBy("psr_label")
    .agg(
        F.count("*").alias("rows"),
        F.sum((F.col("flow_direction") == "consumption").cast("int")).alias("consuming"),
    )
    .withColumn("pct", F.round(100 * F.col("consuming") / F.col("rows"), 1))
    .orderBy(F.desc("consuming"))
    .show(10, truncate=False)
)

print("production types by output, generation only:")
(
    table.filter(F.col("flow_direction") == "generation")
    .groupBy("psr_label")
    .agg(F.count("*").alias("rows"), F.round(F.avg("quantity_mw"), 1).alias("avg_mw"))
    .orderBy(F.desc("rows"))
    .show(10, truncate=False)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### The checks worth failing on
# MAGIC
# MAGIC Reported rather than asserted where the right answer is not obvious. A
# MAGIC duplicate key here would mean a unit reported twice for the same
# MAGIC interval, production type and direction, which is possible in a
# MAGIC republication and is not something to crash on without looking.
# MAGIC
# MAGIC The previous build reported 39,061 of these and every one of them was
# MAGIC the parser's fault rather than the data's. That is the argument for
# MAGIC reporting a count here instead of a silent `dropDuplicates`: a
# MAGIC deduplication would have made the number zero and thrown away half of
# MAGIC what pumped storage does on the way.

# COMMAND ----------

nulls = table.select(
    [
        F.sum(F.col(column).isNull().cast("int")).alias(column)
        for column in [
            "zone", "unit_eic", "ts_utc", "quantity_mw", "resolution_minutes",
            "flow_direction",
        ]
    ]
).collect()[0].asDict()

print("nulls by column:")
for column, found in nulls.items():
    flag = "  <-- unexpected" if found and column != "resolution_minutes" else ""
    print(f"  {column:<20} {found:>10,}{flag}")

# A direction outside the two the parser knows about would mean the document
# shape moved, and it would be sitting in a key column.
strays = table.filter(~F.col("flow_direction").isin("generation", "consumption")).count()
print(f"\nrows with an unrecognised direction: {strays:,}")

negatives = table.filter(F.col("quantity_mw") < 0).count()
print(f"rows with a negative quantity: {negatives:,}")
if negatives:
    print("  Consumption has its own positive series, so these are something else.")
    (
        table.filter(F.col("quantity_mw") < 0)
        .groupBy("zone", "psr_label", "flow_direction")
        .agg(F.count("*").alias("rows"), F.min("quantity_mw").alias("lowest"))
        .orderBy(F.desc("rows"))
        .show(5, truncate=False)
    )

duplicates = (
    table.groupBy("zone", "unit_eic", "psr_type", "flow_direction", "ts_utc")
    .count()
    .filter(F.col("count") > 1)
)
duplicate_count = duplicates.count()
print(f"\nduplicate keys (zone, unit, type, direction, timestamp): {duplicate_count:,}")
if duplicate_count:
    duplicates.orderBy(F.desc("count")).show(5, truncate=False)
    print("  Worth looking at before the gold layer aggregates over them.")
else:
    print("  None. The direction was the missing dimension.")

expected_days = table.select(F.countDistinct("market_day")).collect()[0][0]
gaps = (
    table.select("zone", "market_day").distinct()
    .groupBy("zone").agg(F.countDistinct("market_day").alias("days"))
)
print(f"\ndistinct days overall: {expected_days}")
gaps.show()

# COMMAND ----------

message = f"{written:,} rows in {TARGET}"
if duplicate_count:
    message += f" | {duplicate_count:,} duplicate keys to look at"
dbutils.notebook.exit(message)