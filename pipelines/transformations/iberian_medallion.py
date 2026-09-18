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

Gold runs the pandas functions through `applyInPandas` rather than collecting
to the driver. The alternative is rewriting detection, grouping and saturation
in PySpark, which means maintaining a second implementation of logic already
validated to 0.0015% against REE's published congestion rent, and at this
volume that trade buys nothing.

One difference between here and the local build is worth stating, because it
caused a real failure rather than a theoretical one. The local build parses the
responses it just requested. The pipeline reads a landing zone that keeps every
file it was ever given, so overlapping backfills deliver the same settlement
interval twice and the duplicate guard in `build_spread_series` refuses to pick
one. `pipeline.dedupe` resolves it the way the transparency platform does: the
later publication supersedes the earlier one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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

# The repo root, so the pipeline imports the same package the tests cover
# instead of a copy pasted into a notebook cell. Derived from this file's own
# location rather than written out, because the absolute path depends on who
# cloned the Git folder and where, and a hardcoded one is wrong for everybody
# except the person who typed it.
try:
    sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))
except NameError:  # __file__ is not defined in every execution context
    sys.path.append(spark.conf.get("iberian.src_path"))  # noqa: F821

from iberian.analysis.interconnection import build_border_series  # noqa: E402
from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    flag_decoupling,
)
from iberian.analysis.validation import (  # noqa: E402
    AGREEMENT_COLUMNS,
    COST_COLUMNS,
    cost_validation,
    price_source_agreement,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.market_time import as_utc  # noqa: E402
from iberian.ingestion.esios import (  # noqa: E402
    GEO_PENINSULA,
    IndicatorResponse,
    congestion_rent_by_day,
)
from iberian.ingestion.esios import to_records as esios_to_records  # noqa: E402
from iberian.ingestion.omie import parse_marginalpdbc  # noqa: E402
from iberian.ingestion.omie import to_records as omie_to_records  # noqa: E402
from iberian.ingestion.open_meteo import LOCATIONS, parse_hourly  # noqa: E402
from iberian.ingestion.open_meteo import to_records as weather_to_records  # noqa: E402
from iberian.parsing.entsoe_prices import (  # noqa: E402
    parse_day_ahead_prices,
    parse_quantity_series,
    quantities_to_records,
    to_records,
)
from iberian.pipeline.dedupe import (  # noqa: E402
    PRICE_KEYS,
    QUANTITY_KEYS,
    latest_per_key,
)
from iberian.pipeline.gold import gold_tables, gold_weather_context  # noqa: E402

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
        # Carried through from the file's modification time in the Volume, so
        # that a republished document can be told from the one it supersedes.
        T.StructField("landed_at", T.TimestampType()),
    ]
)

QUANTITY_SCHEMA = T.StructType(
    [
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("quantity_mw", T.DoubleType()),
        T.StructField("resolution", T.StringType()),
        # `quantities_to_records` calls this `series_kind`, and declaring it
        # as "label" quietly produced a column of nulls.
        T.StructField("series_kind", T.StringType()),
        T.StructField("in_domain", T.StringType()),
        T.StructField("out_domain", T.StringType()),
        T.StructField("landed_at", T.TimestampType()),
    ]
)


def _landing(folder: str, name: str, pattern: str = "*.xml"):
    """Auto Loader over one raw folder, keeping the payload byte for byte.

    `name` is only the checkpoint's own directory. Deriving it from the folder
    would put an `=` from a partition path into the schema location, which is
    legal and unreadable.

    `pattern` matters more than it looks. The landing zone holds the request
    metadata beside the response it describes, and a `_request.json` handed to
    an XML parser fails in a way that reads like a corrupt document rather than
    a file that was never meant to be parsed.
    """
    return (
        spark.readStream.format("cloudFiles")  # noqa: F821
        .option("cloudFiles.format", "binaryFile")
        .option("cloudFiles.schemaLocation", f"{RAW}/_schemas/{name}")
        .option("pathGlobFilter", pattern)
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
    return _landing("entsoe/day_ahead_prices", "prices").withColumn(
        "zone_label", F.regexp_extract("path", r"zone=([^/]+)", 1)
    )


@dp.table(
    name="bronze_entsoe_schedules",
    comment="A09 scheduled commercial exchanges, raw.",
    table_properties={"quality": "bronze"},
)
def bronze_entsoe_schedules():
    # Both directions live under this prefix, as `dir=ES_to_PT` and
    # `dir=PT_to_ES`, and both are needed: a net flow is one side minus the
    # other. The direction is read from the document rather than the path,
    # because the document is what a reader could check.
    return _landing("entsoe/crossborder/kind=A09", "schedules")


@dp.table(
    name="bronze_entsoe_capacity",
    comment="A61 day-ahead border capacity, raw.",
    table_properties={"quality": "bronze"},
)
def bronze_entsoe_capacity():
    return _landing("entsoe/crossborder/kind=A61", "capacity")


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
            landed = document["landed_at"]
            rows.extend(
                {**row, "landed_at": landed}
                for row in to_records(parse_day_ahead_prices(body, zone))
            )
        yield pd.DataFrame(rows, columns=PRICE_SCHEMA.fieldNames())


def _parse_quantities(label: str):
    def parse(batches):
        for batch in batches:
            rows: list[dict] = []
            for _, document in batch.iterrows():
                body = bytes(document["content"]).decode("utf-8")
                landed = document["landed_at"]
                rows.extend(
                    {**row, "landed_at": landed}
                    for row in quantities_to_records(parse_quantity_series(body), label)
                )
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
        _parse_quantities("forecasted_capacity"), schema=QUANTITY_SCHEMA
    )


# --- gold: the three persona tables -----------------------------------------
#
# Everything below is lazy on purpose, and the first version of this file was
# not. Calling `.toPandas()` inside a table function runs while the graph is
# being built, before the upstream tables exist, so gold was computed from
# whatever the previous run had left behind. On an empty workspace that raises;
# on a populated one it silently produces yesterday's answer, which is worse.
#
# `applyInPandas` keeps the same pandas functions and makes them part of the
# plan rather than a side effect of defining it. The grouping key is a constant
# so the whole series arrives as one frame: episode grouping is sequential, and
# an episode that runs past local midnight has to stay one episode. The natural
# key if this ever needs to scale is `market_day`, at the cost of splitting
# those crossing episodes in two, which at 60 days and a few thousand rows buys
# nothing today.

INTERVAL_SCHEMA = T.StructType(
    [
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("price_pt_eur_mwh", T.DoubleType()),
        T.StructField("price_es_eur_mwh", T.DoubleType()),
        T.StructField("premium_eur_mwh", T.DoubleType()),
        T.StructField("abs_premium_eur_mwh", T.DoubleType()),
        T.StructField("is_decoupled", T.BooleanType()),
        T.StructField("premium_side", T.StringType()),
        T.StructField("severity", T.StringType()),
        T.StructField("net_flow_mw", T.DoubleType()),
        T.StructField("capacity_mw", T.DoubleType()),
        T.StructField("utilisation", T.DoubleType()),
        T.StructField("is_saturated", T.BooleanType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("hour_of_day_utc", T.IntegerType()),
    ]
)

PROFILE_SCHEMA = T.StructType(
    [
        T.StructField("hour_of_day_utc", T.IntegerType()),
        T.StructField("intervals", T.LongType()),
        T.StructField("decoupled_intervals", T.LongType()),
        T.StructField("mean_premium_eur_mwh", T.DoubleType()),
        T.StructField("worst_premium_eur_mwh", T.DoubleType()),
        T.StructField("split_probability", T.DoubleType()),
        T.StructField("mean_utilisation", T.DoubleType()),
        T.StructField("mean_capacity_mw", T.DoubleType()),
    ]
)

EPISODE_SCHEMA = T.StructType(
    [
        T.StructField("episode_id", T.LongType()),
        T.StructField("start_utc", T.TimestampType()),
        T.StructField("end_utc", T.TimestampType()),
        T.StructField("intervals", T.LongType()),
        T.StructField("duration_hours", T.DoubleType()),
        T.StructField("mean_spread", T.DoubleType()),
        T.StructField("max_abs_spread", T.DoubleType()),
        T.StructField("peak_spread", T.DoubleType()),
        T.StructField("premium_side", T.StringType()),
        T.StructField("max_severity", T.StringType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("extra_cost_eur", T.DoubleType()),
        T.StructField("share_saturated", T.DoubleType()),
        T.StructField("explained_by_saturation", T.BooleanType()),
    ]
)


def _events():
    """The three silver tables as one long frame.

    `applyInPandas` takes one input, and gold needs three. Stacking them with a
    `kind` column and splitting them back inside the function keeps the plan
    lazy without inventing a second way to build gold.
    """
    prices = dp.read("silver_entsoe_prices").select(
        F.lit("price").alias("kind"),
        F.col("ts_utc"),
        F.col("zone_eic"),
        F.col("price_eur_mwh").alias("value"),
        F.col("resolution"),
        F.lit(None).cast("string").alias("in_domain"),
        F.lit(None).cast("string").alias("out_domain"),
        F.col("market"),
        F.col("landed_at"),
    )

    def quantities(table: str, kind: str):
        return dp.read(table).select(
            F.lit(kind).alias("kind"),
            F.col("ts_utc"),
            F.lit(None).cast("string").alias("zone_eic"),
            F.col("quantity_mw").alias("value"),
            F.col("resolution"),
            F.col("in_domain"),
            F.col("out_domain"),
            F.lit(None).cast("string").alias("market"),
            F.col("landed_at"),
        )

    return (
        prices.unionByName(quantities("silver_entsoe_schedules", "schedule"))
        .unionByName(quantities("silver_entsoe_capacity", "capacity"))
    )


def _rebuild(events: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split the stacked frame back apart and call the tested functions."""
    # Spark returns timestamps with no timezone, and the market day is found by
    # converting to CET, which a naive timestamp cannot do. Restoring UTC here,
    # at the one boundary where the data crosses out of Spark, keeps every
    # function downstream working on the same instants the local build sees.
    events = as_utc(events, "ts_utc", "landed_at")

    prices = events[events["kind"] == "price"].rename(
        columns={"value": "price_eur_mwh"}
    )
    schedules = events[events["kind"] == "schedule"].rename(
        columns={"value": "quantity_mw"}
    )
    capacity = events[events["kind"] == "capacity"].rename(
        columns={"value": "quantity_mw"}
    )

    # The landing zone keeps every file it was ever given, so overlapping
    # backfills put the same interval in two documents. The local build never
    # sees this because it parses the responses it just asked for. Later
    # publication wins, which is how a correction is meant to be read.
    prices = latest_per_key(prices, PRICE_KEYS)
    schedules = latest_per_key(schedules, QUANTITY_KEYS)
    capacity = latest_per_key(capacity, QUANTITY_KEYS)

    flagged = flag_decoupling(build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN))
    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))
    return gold_tables(flagged, border)


def _one_table(name: str, schema: T.StructType):
    """Build every gold table and return the one asked for, with its columns."""

    def build(events: pd.DataFrame) -> pd.DataFrame:
        frame = _rebuild(events).get(name, pd.DataFrame())
        if frame.empty:
            return pd.DataFrame(columns=schema.fieldNames())
        return frame[schema.fieldNames()]

    return build


def _gold(name: str, schema: T.StructType):
    return _events().groupBy(F.lit(1).alias("all")).applyInPandas(
        _one_table(name, schema), schema=schema
    )


@dp.materialized_view(
    name="gold_interval_premium",
    comment="Persona 1 and 3: premium and border utilisation per interval.",
    table_properties={"quality": "gold"},
)
def gold_interval_premium():
    return _gold("gold_interval_premium", INTERVAL_SCHEMA)


@dp.materialized_view(
    name="gold_daily_profile",
    comment="Persona 1: when a manufacturer should expect to pay the premium.",
    table_properties={"quality": "gold"},
)
def gold_daily_profile():
    return _gold("gold_daily_profile", PROFILE_SCHEMA)


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
    return _gold("gold_split_episodes", EPISODE_SCHEMA)


# --- the other three sources ------------------------------------------------
#
# ENTSO-E alone would make a working platform and a weaker one. OMIE publishes
# the same day-ahead prices through a separate channel, which is what turns
# "the prices are right" from an assertion into a measurement. ESIOS publishes
# the congestion rent this project's cost figure is checked against. Open-Meteo
# carries the reason Spanish power was cheap enough to be worth importing,
# which no market document contains.
#
# They also differ in shape on purpose: XML over an API, columnar JSON,
# delimited files published daily, each with its own idea of a timestamp.

OMIE_SCHEMA = T.StructType(
    [
        T.StructField("source", T.StringType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("period", T.IntegerType()),
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("price_first_eur_mwh", T.DoubleType()),
        T.StructField("price_second_eur_mwh", T.DoubleType()),
        T.StructField("landed_at", T.TimestampType()),
    ]
)

WEATHER_VARIABLES = ("shortwave_radiation_wm2", "wind_speed_100m_kmh", "temperature_c")

WEATHER_SCHEMA = T.StructType(
    [
        T.StructField("source", T.StringType()),
        T.StructField("location", T.StringType()),
        T.StructField("zone", T.StringType()),
        T.StructField("latitude", T.DoubleType()),
        T.StructField("longitude", T.DoubleType()),
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("temperature_c", T.DoubleType()),
        T.StructField("wind_speed_100m_kmh", T.DoubleType()),
        T.StructField("shortwave_radiation_wm2", T.DoubleType()),
        T.StructField("cloud_cover_pct", T.DoubleType()),
        T.StructField("landed_at", T.TimestampType()),
    ]
)

ESIOS_SCHEMA = T.StructType(
    [
        T.StructField("indicator_id", T.IntegerType()),
        T.StructField("indicator", T.StringType()),
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("value", T.DoubleType()),
        T.StructField("geo_id", T.IntegerType()),
        T.StructField("geo_name", T.StringType()),
        T.StructField("resolution", T.StringType()),
        T.StructField("landed_at", T.TimestampType()),
    ]
)

#: Keys for resolving a re-landed file. OMIE republishes a corrected day under
#: the same name, and the weather archive supersedes the forecast for the same
#: hour, so both need the same treatment the ENTSO-E documents get.
OMIE_KEYS = ("ts_utc",)
WEATHER_KEYS = ("location", "ts_utc")
ESIOS_KEYS = ("indicator_id", "ts_utc", "geo_id")


# --- bronze -----------------------------------------------------------------


@dp.table(
    name="bronze_omie",
    comment="OMIE day-ahead files exactly as published.",
    table_properties={"quality": "bronze"},
)
def bronze_omie():
    # No extension to filter on: OMIE names its files marginalpdbc_20260719.1,
    # where the suffix is a version rather than a type.
    return _landing("omie", "omie", pattern="*")


@dp.table(
    name="bronze_open_meteo",
    comment="Open-Meteo hourly payloads, one per location and window.",
    table_properties={"quality": "bronze"},
)
def bronze_open_meteo():
    # Same guard as the ESIOS catalogue: anything under this prefix without a
    # `location=` folder is not an observation from a place.
    return (
        _landing("open_meteo", "open_meteo", pattern="*.json")
        .withColumn("location", F.regexp_extract("path", r"location=([^/]+)", 1))
        .where(F.col("location") != "")
    )


@dp.table(
    name="bronze_esios",
    comment="REE ESIOS indicator payloads, one per indicator and window.",
    table_properties={"quality": "bronze"},
)
def bronze_esios():
    # The indicator is in the path because the payload names it by id and the
    # id alone says nothing to a reader of the table.
    #
    # The landing zone also holds `indicators.json`, the catalogue of every
    # indicator ESIOS publishes, which is a reference document rather than a
    # measurement and has no `indicator=` folder. Dropping it by the absence of
    # that folder is deliberate: with ANSI mode on, casting its empty match to
    # an integer fails the whole stream, which is the right behaviour and the
    # reason this is a filter rather than a silent null.
    return (
        _landing("esios", "esios", pattern="*.json")
        .withColumn("indicator_id", F.regexp_extract("path", r"indicator=(\d+)", 1))
        .where(F.col("indicator_id") != "")
        .withColumn("indicator_id", F.col("indicator_id").cast("int"))
    )


# --- silver -----------------------------------------------------------------


def _parse_omie(batches):
    for batch in batches:
        rows: list[dict] = []
        for _, document in batch.iterrows():
            text = bytes(document["content"]).decode("latin-1")
            landed = document["landed_at"]
            rows.extend(
                {**row, "landed_at": landed}
                for row in omie_to_records(parse_marginalpdbc(text))
            )
        yield pd.DataFrame(rows, columns=OMIE_SCHEMA.fieldNames())


def _parse_weather(batches):
    for batch in batches:
        rows: list[dict] = []
        for _, document in batch.iterrows():
            location = document["location"]
            if location not in LOCATIONS:
                continue
            latitude, longitude, _ = LOCATIONS[location]
            payload = json.loads(bytes(document["content"]).decode("utf-8"))
            landed = document["landed_at"]
            points = parse_hourly(payload, location, latitude, longitude)
            rows.extend(
                {**row, "landed_at": landed} for row in weather_to_records(points)
            )
        yield pd.DataFrame(rows, columns=WEATHER_SCHEMA.fieldNames())


def _parse_esios(batches):
    for batch in batches:
        rows: list[dict] = []
        for _, document in batch.iterrows():
            response = IndicatorResponse(
                indicator_id=int(document["indicator_id"]),
                content=bytes(document["content"]),
            )
            landed = document["landed_at"]
            rows.extend(
                {**row, "landed_at": landed}
                for row in esios_to_records(response, geo_id=GEO_PENINSULA)
            )
        yield pd.DataFrame(rows, columns=ESIOS_SCHEMA.fieldNames())


@dp.table(
    name="silver_omie_prices",
    comment="Day-ahead prices as published by OMIE, for cross source validation.",
    table_properties={"quality": "silver"},
)
@dp.expect_or_drop("has_timestamp", "ts_utc IS NOT NULL")
@dp.expect("period_is_within_a_day", "period BETWEEN 1 AND 100")
def silver_omie_prices():
    return dp.read_stream("bronze_omie").mapInPandas(_parse_omie, schema=OMIE_SCHEMA)


@dp.table(
    name="silver_weather",
    comment="Hourly weather at locations chosen for their effect on price.",
    table_properties={"quality": "silver"},
)
@dp.expect_or_drop("has_timestamp", "ts_utc IS NOT NULL")
@dp.expect_or_drop("has_location", "location IS NOT NULL")
# Radiation at night is zero, not missing, and a negative value would mean the
# parser lined the arrays up wrongly rather than that the sun misbehaved.
@dp.expect("radiation_is_not_negative", "shortwave_radiation_wm2 >= 0")
def silver_weather():
    return dp.read_stream("bronze_open_meteo").mapInPandas(
        _parse_weather, schema=WEATHER_SCHEMA
    )


@dp.table(
    name="silver_esios_indicators",
    comment="REE ESIOS indicators: congestion rent, demand forecast and actual.",
    table_properties={"quality": "silver"},
)
@dp.expect_or_drop("has_timestamp", "ts_utc IS NOT NULL")
@dp.expect("peninsula_only", f"geo_id = {GEO_PENINSULA}")
def silver_esios_indicators():
    return dp.read_stream("bronze_esios").mapInPandas(
        _parse_esios, schema=ESIOS_SCHEMA
    )


# --- gold: context and the two validations ----------------------------------
#
# These take two inputs rather than one, so they use `cogroup` instead of the
# stacking trick above. The key is a constant for the same reason: the work is
# a few thousand rows and correctness across the whole series matters more than
# parallelism that buys nothing.

WEATHER_CONTEXT_SCHEMA = T.StructType(
    list(INTERVAL_SCHEMA.fields)
    + [
        T.StructField(f"{variable}__{location}", T.DoubleType())
        for variable in WEATHER_VARIABLES
        for location in sorted(LOCATIONS)
    ]
    + [T.StructField("weather_resolution", T.StringType())]
)

AGREEMENT_SCHEMA = T.StructType(
    [
        T.StructField("ts_utc", T.TimestampType()),
        T.StructField("market_day", T.DateType()),
        T.StructField("price_pt_entsoe", T.DoubleType()),
        T.StructField("price_pt_omie", T.DoubleType()),
        T.StructField("price_es_entsoe", T.DoubleType()),
        T.StructField("price_es_omie", T.DoubleType()),
        T.StructField("pt_difference", T.DoubleType()),
        T.StructField("es_difference", T.DoubleType()),
        T.StructField("agrees", T.BooleanType()),
    ]
)

COST_SCHEMA = T.StructType(
    [
        T.StructField("market_day", T.DateType()),
        T.StructField("episodes", T.IntegerType()),
        T.StructField("our_cost_eur", T.DoubleType()),
        T.StructField("congestion_rent_eur", T.DoubleType()),
        T.StructField("difference_eur", T.DoubleType()),
        T.StructField("difference_pct", T.DoubleType()),
    ]
)


def _conform(frame: pd.DataFrame, schema: T.StructType) -> pd.DataFrame:
    """Give Spark exactly the columns it was promised, in order.

    A location that published nothing leaves its pivoted columns absent rather
    than null, and a schema mismatch inside `applyInPandas` surfaces as an
    arrow conversion error that names none of this.
    """
    out = frame.copy() if not frame.empty else pd.DataFrame(columns=schema.fieldNames())
    for field in schema.fields:
        if field.name not in out.columns:
            out[field.name] = None
    return out[schema.fieldNames()]


def _pair(left_name: str, right_name: str, function, schema: T.StructType):
    """Cogroup two tables under one key and hand both frames to a function."""
    left = dp.read(left_name).groupBy(F.lit(1).alias("all"))
    right = dp.read(right_name).groupBy(F.lit(1).alias("all"))
    return left.cogroup(right).applyInPandas(function, schema=schema)


@dp.materialized_view(
    name="gold_weather_context",
    comment="Persona 2 and 3: interval premiums beside the weather that drove them.",
    table_properties={"quality": "gold"},
)
def gold_weather_context_table():
    def build(intervals: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
        intervals = as_utc(intervals, "ts_utc")
        weather = latest_per_key(as_utc(weather, "ts_utc", "landed_at"), WEATHER_KEYS)
        return _conform(
            gold_weather_context(intervals, weather), WEATHER_CONTEXT_SCHEMA
        )

    return _pair(
        "gold_interval_premium", "silver_weather", build, WEATHER_CONTEXT_SCHEMA
    )


@dp.materialized_view(
    name="gold_price_source_agreement",
    comment="Do ENTSO-E and OMIE publish the same day-ahead price, interval by interval.",
    table_properties={"quality": "gold"},
)
# The check is worth nothing if a disagreement passes quietly. This does not
# fail the table, because a disagreement is a finding to report rather than a
# reason to withhold the data, but it does surface in the pipeline's metrics.
@dp.expect("sources_agree", "agrees")
def gold_price_source_agreement():
    def build(prices: pd.DataFrame, omie: pd.DataFrame) -> pd.DataFrame:
        prices = latest_per_key(as_utc(prices, "ts_utc", "landed_at"), PRICE_KEYS)
        omie = latest_per_key(as_utc(omie, "ts_utc", "landed_at"), OMIE_KEYS)
        if prices.empty or omie.empty:
            return _conform(pd.DataFrame(), AGREEMENT_SCHEMA)
        spread = build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN)
        return _conform(price_source_agreement(spread, omie), AGREEMENT_SCHEMA)

    return _pair(
        "silver_entsoe_prices", "silver_omie_prices", build, AGREEMENT_SCHEMA
    )


@dp.materialized_view(
    name="gold_cost_validation",
    comment="This project's extra import cost against REE's published congestion rent.",
    table_properties={"quality": "gold"},
)
def gold_cost_validation():
    def build(episodes: pd.DataFrame, esios: pd.DataFrame) -> pd.DataFrame:
        esios = latest_per_key(as_utc(esios, "ts_utc", "landed_at"), ESIOS_KEYS)
        rent = congestion_rent_by_day(esios) if not esios.empty else pd.DataFrame()
        return _conform(
            cost_validation(as_utc(episodes, "start_utc", "end_utc"), rent),
            COST_SCHEMA,
        )

    return _pair(
        "gold_split_episodes", "silver_esios_indicators", build, COST_SCHEMA
    )