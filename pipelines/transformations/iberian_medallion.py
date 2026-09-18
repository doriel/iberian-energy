"""The medallion as a declarative pipeline.

What this file is, and what it deliberately is not.

It is not a second implementation. Every transformation here calls the same
functions the local build calls and the test suite covers. A pipeline that
reimplemented the parsing or the episode grouping would give us two code paths
that drift, and the first symptom of the drift would be a number on a page
that nobody can reproduce locally.

It also does not fetch anything. A declarative pipeline is given data and asked
to derive tables from it. Calling the ENTSO-E API from inside a table would put
a rate limited HTTP request inside a unit of work the platform is entitled to
retry at will. Ingestion stays where it is, in `scripts/build_medallion.py`,
run as the job task ahead of this pipeline, writing raw payloads into the
Volume. This file starts where those bytes land.

The layers, and why each one is the kind of table it is.

Bronze is a streaming table over the Volume, read as `binaryFile` so the
payload is stored exactly as ENTSO-E sent it. Auto Loader tracks which files it
has already seen, which is what makes a backfill safe to re-run. Storing the
bytes rather than the parsed rows is the property that lets a parser fix
reprocess history instead of re-hitting the API, and it is the reason the
sparse Points bug was fixable in an afternoon.

Silver is a streaming table that parses those bytes. The parsing happens in
`mapInPandas`, so it runs on the executors, one document at a time, calling
`parse_day_ahead_prices` unchanged.

Gold is a materialized view rather than a streaming table, and that is not an
oversight. Episode grouping is sequential over a sorted series: whether an
interval extends the current episode or starts a new one depends on the
interval before it, and a gap in the data has to end an episode rather than be
stitched over. That is not expressible as an append-only stream, and forcing it
would produce episode durations that are quietly wrong. A full recompute of
sixty market days is a few thousand rows and takes seconds.

Gold is computed through pandas on the driver for the same reason. The
alternative is rewriting detection, grouping and saturation in PySpark, which
means maintaining a second implementation of logic that is already validated to
0.0015% against REE's published congestion rent. At this volume that trade
buys nothing. If the gold input ever outgrows the driver, the honest fix is to
partition the recompute by market day, which the arithmetic already allows
because episodes never cross the market day boundary.
"""

from __future__ import annotations

import sys

# The module was renamed: `dlt` became `pyspark.pipelines`. The old name still
# works, so falling back keeps this file runnable on a workspace that has not
# picked up the rename yet, rather than failing at import with a message that
# says nothing about which of the two is available here.
try:
    from pyspark import pipelines as dp
except ImportError:  # pragma: no cover - depends on the runtime, not on us
    import dlt as dp  # type: ignore[no-redef]

import pandas as pd
from pyspark.sql import functions as F
from pyspark.sql import types as T

# The Git folder root, so the pipeline imports the same package the tests cover
# instead of a copy pasted into a notebook cell.
sys.path.append("/Workspace/Repos/iberian-energy/src")

from iberian.analysis.interconnection import build_border_series  # noqa: E402
from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    flag_decoupling,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.parsing.entsoe_prices import (  # noqa: E402
    parse_day_ahead_prices,
    parse_quantity_series,
    quantities_to_records,
    to_records,
)
from iberian.pipeline.gold import gold_tables  # noqa: E402

# Set these in the pipeline configuration rather than here, so the same file
# runs against a personal schema and against the shared one without an edit.
CATALOG = spark.conf.get("iberian.catalog", "bootcamp_students")  # noqa: F821
SCHEMA = spark.conf.get("iberian.schema", "doriel")  # noqa: F821
RAW = spark.conf.get(  # noqa: F821
    "iberian.raw_volume", f"/Volumes/{CATALOG}/{SCHEMA}/raw"
)

PRICE_SCHEMA = T.StructType(
    [
        T.StructField("zone_eic", T.StringType()),
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("price_eur_mwh", T.DoubleType()),
        T.StructField("resolution", T.StringType()),
        T.StructField("currency", T.StringType()),
        T.StructField("unit", T.StringType()),
        T.StructField("market", T.StringType()),
        T.StructField("contract_type", T.StringType()),
        T.StructField("series_mrid", T.StringType()),
    ]
)

QUANTITY_SCHEMA = T.StructType(
    [
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("quantity_mw", T.DoubleType()),
        T.StructField("resolution", T.StringType()),
        T.StructField("label", T.StringType()),
        T.StructField("in_domain", T.StringType()),
        T.StructField("out_domain", T.StringType()),
    ]
)


def _landing(folder: str):
    """Auto Loader over one raw folder, keeping the payload byte for byte."""
    return (
        spark.readStream.format("cloudFiles")  # noqa: F821
        .option("cloudFiles.format", "binaryFile")
        .option("cloudFiles.schemaLocation", f"{RAW}/_schemas/{folder}")
        .load(f"{RAW}/{folder}")
        .select(
            F.col("path"),
            F.col("modificationTime").alias("landed_at"),
            F.col("content"),
        )
    )


# --- bronze: the payloads, untouched ----------------------------------------


@dp.table(
    name="bronze_entsoe_prices",
    comment="A44 day-ahead documents exactly as ENTSO-E returned them.",
    table_properties={"quality": "bronze"},
)
def bronze_entsoe_prices():
    # The zone is in the path, not in a column, because the request was made
    # per zone. Losing it here would make the document unparseable later: an
    # A44 payload does not always name the bidding zone in a form we can trust.
    return _landing("entsoe/day_ahead_prices").withColumn(
        "zone_label", F.regexp_extract("path", r"zone=([^/]+)", 1)
    )


@dp.table(
    name="bronze_entsoe_schedules",
    comment="A09 scheduled commercial exchanges, raw.",
    table_properties={"quality": "bronze"},
)
def bronze_entsoe_schedules():
    return _landing("entsoe/scheduled_exchanges")


@dp.table(
    name="bronze_entsoe_capacity",
    comment="A61 day-ahead border capacity, raw.",
    table_properties={"quality": "bronze"},
)
def bronze_entsoe_capacity():
    return _landing("entsoe/day_ahead_capacity")


# --- silver: parsed, one row per settlement interval ------------------------


def _parse_prices(batches):
    """Runs on the executors, one document per row, parser untouched."""
    for batch in batches:
        rows: list[dict] = []
        for _, document in batch.iterrows():
            zone = (
                EIC_PORTUGAL
                if str(document["zone_label"]).upper().startswith("PT")
                else EIC_SPAIN
            )
            body = bytes(document["content"]).decode("utf-8")
            rows.extend(to_records(parse_day_ahead_prices(body, zone)))
        yield pd.DataFrame(rows, columns=PRICE_SCHEMA.fieldNames())


def _parse_quantities(label: str):
    def parse(batches):
        for batch in batches:
            rows: list[dict] = []
            for _, document in batch.iterrows():
                body = bytes(document["content"]).decode("utf-8")
                rows.extend(quantities_to_records(parse_quantity_series(body), label))
            yield pd.DataFrame(rows, columns=QUANTITY_SCHEMA.fieldNames())

    return parse


@dp.table(
    name="silver_entsoe_prices",
    comment="Day-ahead prices per zone and settlement interval.",
    table_properties={"quality": "silver"},
)
# A price is allowed to be negative, and often is when Spanish solar floods the
# market, so no bound is asserted on the value. What is asserted is that the
# fields the join depends on exist: a null here becomes a missing interval,
# and a missing interval silently shortens an episode.
@dp.expect_or_drop("has_timestamp", "ts_utc IS NOT NULL")
@dp.expect_or_drop("has_zone", "zone_eic IS NOT NULL")
@dp.expect("day_ahead_only", "market = 'day_ahead'")
def silver_entsoe_prices():
    return dp.read_stream("bronze_entsoe_prices").mapInPandas(
        _parse_prices, schema=PRICE_SCHEMA
    )


@dp.table(
    name="silver_entsoe_schedules",
    comment="Scheduled exchanges on the ES to PT border.",
    table_properties={"quality": "silver"},
)
@dp.expect_or_drop("has_timestamp", "ts_utc IS NOT NULL")
@dp.expect("flow_is_not_negative", "quantity_mw >= 0")
def silver_entsoe_schedules():
    return dp.read_stream("bronze_entsoe_schedules").mapInPandas(
        _parse_quantities("scheduled_exchange"), schema=QUANTITY_SCHEMA
    )


@dp.table(
    name="silver_entsoe_capacity",
    comment="Day-ahead border capacity on the ES to PT direction.",
    table_properties={"quality": "silver"},
)
@dp.expect_or_drop("has_timestamp", "ts_utc IS NOT NULL")
@dp.expect("capacity_is_not_negative", "quantity_mw >= 0")
def silver_entsoe_capacity():
    return dp.read_stream("bronze_entsoe_capacity").mapInPandas(
        _parse_quantities("day_ahead_capacity"), schema=QUANTITY_SCHEMA
    )


# --- gold: the three persona tables -----------------------------------------


def _build_gold() -> dict[str, pd.DataFrame]:
    """The same call the local build makes, on the same functions."""
    prices = dp.read("silver_entsoe_prices").toPandas()
    schedules = dp.read("silver_entsoe_schedules").toPandas()
    capacity = dp.read("silver_entsoe_capacity").toPandas()

    flagged = flag_decoupling(build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN))
    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))
    return gold_tables(flagged, border)


@dp.materialized_view(
    name="gold_interval_premium",
    comment="Persona 1 and 3: premium and border utilisation per interval.",
    table_properties={"quality": "gold"},
)
def gold_interval_premium():
    return spark.createDataFrame(_build_gold()["gold_interval_premium"])  # noqa: F821


@dp.materialized_view(
    name="gold_daily_profile",
    comment="Persona 1: when a manufacturer should expect to pay the premium.",
    table_properties={"quality": "gold"},
)
def gold_daily_profile():
    return spark.createDataFrame(_build_gold()["gold_daily_profile"])  # noqa: F821


@dp.materialized_view(
    name="gold_split_episodes",
    comment="Persona 2: one row per episode, with duration, cost and cause.",
    table_properties={"quality": "gold"},
)
# An episode that costs nothing is a detection artefact, and one that lasts no
# time is an arithmetic error. Both have happened, and both are cheap to assert.
@dp.expect("episode_has_duration", "duration_hours > 0")
@dp.expect("episode_has_intervals", "intervals > 0")
def gold_split_episodes():
    return spark.createDataFrame(_build_gold()["gold_split_episodes"])  # noqa: F821