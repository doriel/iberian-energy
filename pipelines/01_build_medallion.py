# Databricks notebook source
# MAGIC %md
# MAGIC # MIBEL market intelligence: medallion build
# MAGIC
# MAGIC Ingests Iberian electricity market data into a bronze, silver and gold
# MAGIC lakehouse on Unity Catalog.
# MAGIC
# MAGIC | Layer | What it holds |
# MAGIC |---|---|
# MAGIC | Bronze | API payloads byte for byte in a Volume, nothing interpreted |
# MAGIC | Silver | One Delta table per source, parsed but without business logic |
# MAGIC | Gold | Market splitting episodes, interval premiums, daily profile |
# MAGIC
# MAGIC Three sources, deliberately different in shape: ENTSO-E is XML over an
# MAGIC API, Open-Meteo is columnar JSON, OMIE is delimited files published
# MAGIC daily. Two of them publish the same day-ahead prices independently,
# MAGIC which gives a cross-source data quality check rather than a claim.
# MAGIC
# MAGIC The ingestion and analysis logic lives in `src/iberian/` as plain Python
# MAGIC with no Databricks imports, so the same functions run in a local test
# MAGIC suite in under two seconds and inside this notebook unchanged. This
# MAGIC notebook is an orchestration and persistence layer, not a place where
# MAGIC logic is reimplemented.
# MAGIC
# MAGIC **Prerequisites**: attach this notebook to a cluster, set the widgets at
# MAGIC the top, and store the ENTSO-E token in a secret scope. Writes are
# MAGIC idempotent per market day via Delta `replaceWhere`, so re-running a date
# MAGIC range repairs it rather than duplicating it.

# COMMAND ----------

# MAGIC %pip install requests pandas
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC
# MAGIC Run this cell once to create the widgets, fill them in at the top of the
# MAGIC notebook, then run it again so the values are picked up.
# MAGIC
# MAGIC The token is read from a secret scope rather than a widget so it never
# MAGIC lands in the notebook's saved state or in the repo. Create one with the
# MAGIC CLI if you have not already:
# MAGIC
# MAGIC ```
# MAGIC databricks secrets create-scope mibel
# MAGIC databricks secrets put-secret mibel entsoe_token --string-value "<token>"
# MAGIC ```

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Unity Catalog")
dbutils.widgets.text("schema", "mibel", "Schema")
dbutils.widgets.text("volume", "raw", "Volume for bronze")
dbutils.widgets.text("start_day", "2026-09-01", "First market day (YYYY-MM-DD)")
dbutils.widgets.text("days", "7", "Number of market days")
dbutils.widgets.text("secret_scope", "mibel", "Secret scope")
dbutils.widgets.text("secret_key", "entsoe_token", "Secret key for the ENTSO-E token")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
VOLUME = dbutils.widgets.get("volume").strip()
START_DAY = dbutils.widgets.get("start_day").strip()
DAYS = int(dbutils.widgets.get("days"))

if not CATALOG:
    raise ValueError(
        "Set the catalog widget. Run SHOW CATALOGS to see what you can write to."
    )

ENTSOE_TOKEN = dbutils.secrets.get(
    scope=dbutils.widgets.get("secret_scope"),
    key=dbutils.widgets.get("secret_key"),
)

VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
print(f"Target   {CATALOG}.{SCHEMA}")
print(f"Bronze   {VOLUME_ROOT}")
print(f"Window   {DAYS} market day(s) from {START_DAY}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Catalog objects
# MAGIC
# MAGIC Created if missing so a fresh workspace can run this notebook end to end.

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.{VOLUME}")
spark.sql(f"USE CATALOG {CATALOG}")
spark.sql(f"USE SCHEMA {SCHEMA}")
print(f"Ready: {CATALOG}.{SCHEMA}, volume {VOLUME}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import the project modules
# MAGIC
# MAGIC This notebook sits in `pipelines/` inside the Git folder, so the package
# MAGIC is two levels up under `src/`. Importing rather than copying is what
# MAGIC keeps the logic covered by the test suite: if a function changes, the
# MAGIC tests catch it before this notebook ever runs.

# COMMAND ----------

import os
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

print(f"Repo root: {REPO_ROOT}")
print(f"Source:    {SRC}")

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

# COMMAND ----------

from datetime import date, timedelta  # noqa: E402

import pandas as pd  # noqa: E402

from iberian.analysis.interconnection import build_border_series  # noqa: E402
from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    flag_decoupling,
    infer_step,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.ingestion.border import fetch_border  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.ingestion.omie import OmieClient, parse_marginalpdbc  # noqa: E402
from iberian.ingestion.omie import to_records as omie_to_records  # noqa: E402
from iberian.ingestion.open_meteo import OpenMeteoClient  # noqa: E402
from iberian.ingestion.open_meteo import to_records as weather_to_records  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402
from iberian.parsing.entsoe_prices import (  # noqa: E402
    parse_prices_response,
    to_records,
)
from iberian.pipeline.gold import gold_tables  # noqa: E402

START = date.fromisoformat(START_DAY)
DAY_LIST = [START + timedelta(days=offset) for offset in range(DAYS)]
WANTED = set(DAY_LIST)
WINDOW_START, WINDOW_END = market_day_range(START, DAYS)

print(f"Market days {DAY_LIST[0]} to {DAY_LIST[-1]}")
print(f"UTC window  {WINDOW_START:%Y-%m-%d %H:%M}Z to {WINDOW_END:%Y-%m-%d %H:%M}Z")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Helpers
# MAGIC
# MAGIC Two details that otherwise bite when moving pandas into Delta.
# MAGIC
# MAGIC A column that is entirely null arrives as `object` dtype and Spark
# MAGIC cannot infer a type for it, so the conversion fails on exactly the days
# MAGIC where a source published nothing. Those columns are cast to string.
# MAGIC
# MAGIC Writes use `replaceWhere` on `market_day` rather than a plain append, so
# MAGIC re-running a range replaces those days instead of duplicating them. That
# MAGIC is what makes a backfill safe to repeat.

# COMMAND ----------

def to_spark(frame: pd.DataFrame):
    """Convert a pandas frame to Spark, coercing columns Spark cannot infer."""
    if frame.empty:
        return None

    out = frame.copy()
    for column in out.columns:
        if out[column].dtype == "object" and out[column].isna().all():
            out[column] = out[column].astype("string")
        elif out[column].dtype == "object":
            sample = out[column].dropna().iloc[0]
            if not isinstance(sample, (str, bool, date)):
                out[column] = out[column].astype(str)
    return spark.createDataFrame(out)


def write_table(frame: pd.DataFrame, name: str, comment: str) -> int:
    """Write one table, replacing only the market days in this run."""
    sdf = to_spark(frame)
    if sdf is None:
        print(f"  {name:<32} empty, skipped")
        return 0

    full_name = f"{CATALOG}.{SCHEMA}.{name}"
    days = (
        sorted({str(value) for value in frame["market_day"].dropna().unique()})
        if "market_day" in frame.columns
        else []
    )
    incremental = days and spark.catalog.tableExists(full_name)

    writer = sdf.write.format("delta").mode("overwrite")
    if incremental:
        # replaceWhere and overwriteSchema cannot be combined, so an existing
        # table is replaced day by day and keeps the schema it already has.
        predicate = " OR ".join(f"market_day = '{day}'" for day in days)
        writer = writer.option("replaceWhere", predicate)
    else:
        writer = writer.option("overwriteSchema", "true")

    writer.saveAsTable(full_name)
    escaped = comment.replace("'", "''")
    spark.sql(f"COMMENT ON TABLE {full_name} IS '{escaped}'")
    print(f"  {name:<32} {len(frame):>7} rows")
    return len(frame)


def land_bronze(subpath: str, filename: str, payload: bytes) -> str:
    """Write a raw payload into the Volume, unmodified."""
    target = f"{VOLUME_ROOT}/{subpath}"
    dbutils.fs.mkdirs(target)
    path = f"{target}/{filename}"
    with open(path, "wb") as handle:
        handle.write(payload)
    return path

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bronze and silver: ENTSO-E
# MAGIC
# MAGIC Prices for both bidding zones, plus the scheduled commercial exchanges
# MAGIC and day-ahead transfer capacity that make the saturation test possible.
# MAGIC
# MAGIC Raw payloads land first and parsing happens afterwards, so a parser fix
# MAGIC is a reprocess of what is already stored rather than another call
# MAGIC against a rate limited API.

# COMMAND ----------

client = EntsoeClient(ENTSOE_TOKEN)

price_records = []
for label, eic in (("PT", EIC_PORTUGAL), ("ES", EIC_SPAIN)):
    response = client.day_ahead_prices(eic, WINDOW_START, WINDOW_END)
    land_bronze(
        f"entsoe/day_ahead_prices/zone={label}",
        f"{START:%Y-%m-%d}_{DAYS}d{response.suggested_extension}",
        response.content,
    )
    rows = [
        row
        for row in to_records(parse_prices_response(response, eic))
        if row["market_day"] in WANTED
    ]
    price_records.extend(rows)
    print(f"  entsoe prices {label}: {len(rows)} rows")

prices = pd.DataFrame(price_records)
if prices.empty:
    raise RuntimeError("No prices returned. Check the token and the date range.")

# COMMAND ----------

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

# fetch_border lands the raw payloads itself, so give it a scratch path and
# copy the bytes into the Volume afterwards rather than duplicating its logic.
with tempfile.TemporaryDirectory() as scratch:
    schedules, capacity = fetch_border(
        client, WINDOW_START, WINDOW_END, Path(scratch), START, verbose=True
    )
    for path in Path(scratch).rglob("*"):
        if path.is_file():
            relative = path.relative_to(scratch).parent.as_posix()
            land_bronze(relative, path.name, path.read_bytes())

print(f"  schedules: {len(schedules)} rows")
print(f"  capacity:  {len(capacity)} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bronze and silver: OMIE
# MAGIC
# MAGIC The Iberian market operator publishes the same day-ahead prices as flat
# MAGIC files. A second independent publication of one number is what makes the
# MAGIC quality check downstream meaningful.

# COMMAND ----------

omie_client = OmieClient()
omie_rows = []

for day in DAY_LIST:
    try:
        response = omie_client.day_ahead_prices(day)
    except RuntimeError as exc:
        print(f"  omie {day}: failed, {exc}")
        continue
    if response.looks_empty:
        print(f"  omie {day}: empty")
        continue

    land_bronze(f"omie/file_set={response.file_set}", response.filename, response.content)
    omie_rows.extend(omie_to_records(parse_marginalpdbc(response.text)))

print(f"  omie prices: {len(omie_rows)} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bronze and silver: Open-Meteo
# MAGIC
# MAGIC Weather is the upstream driver. Market data shows that the border was
# MAGIC full and that Portugal paid a premium; it cannot show why Spanish power
# MAGIC was cheap enough to be worth importing. Solar radiation and wind can.
# MAGIC
# MAGIC Locations are chosen for what drives the price rather than for
# MAGIC population, and the reasoning for each sits next to it in
# MAGIC `ingestion/open_meteo.py`.

# COMMAND ----------

import json  # noqa: E402

weather_client = OpenMeteoClient()
weather_rows = []

for location in ("ES_andalusia", "ES_galicia", "PT_lisbon", "PT_alentejo"):
    try:
        points, payload = weather_client.hourly(location, DAY_LIST[0], DAY_LIST[-1])
    except RuntimeError as exc:
        print(f"  weather {location}: failed, {exc}")
        continue
    land_bronze(
        f"open_meteo/location={location}",
        f"{START:%Y-%m-%d}_{DAYS}d.json",
        json.dumps(payload).encode("utf-8"),
    )
    weather_rows.extend(weather_to_records(points))

print(f"  open-meteo: {len(weather_rows)} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write silver

# COMMAND ----------

print("SILVER")
write_table(prices, "silver_entsoe_prices",
            "Day-ahead prices per bidding zone, parsed from ENTSO-E A44")
write_table(schedules, "silver_entsoe_schedules",
            "Scheduled commercial exchanges across the PT/ES border, ENTSO-E A09")
write_table(capacity, "silver_entsoe_capacity",
            "Day-ahead transfer capacity across the PT/ES border, ENTSO-E A61")
write_table(pd.DataFrame(omie_rows), "silver_omie_prices",
            "Day-ahead prices published independently by OMIE, for cross validation")
write_table(pd.DataFrame(weather_rows), "silver_weather",
            "Hourly weather at points chosen for their effect on Iberian prices")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data quality: do two independent publishers agree?
# MAGIC
# MAGIC ENTSO-E and OMIE publish the same settled prices through entirely
# MAGIC separate channels. Agreement is evidence that the parsing and the market
# MAGIC day arithmetic are both correct, since the Iberian market day runs from
# MAGIC local midnight in CET rather than from UTC midnight and a mistake there
# MAGIC would misalign every timestamp. Disagreement is a finding to report, not
# MAGIC a failure to hide, so this measures rather than asserts.

# COMMAND ----------

if omie_rows:
    entsoe_pt = prices[prices["zone_eic"] == EIC_PORTUGAL][["ts_utc", "price_eur_mwh"]]
    omie_frame = pd.DataFrame(omie_rows)[["ts_utc", "price_first_eur_mwh"]]
    check = entsoe_pt.merge(omie_frame, on="ts_utc", how="inner")
    check["difference"] = (
        check["price_eur_mwh"] - check["price_first_eur_mwh"]
    ).abs()

    matched = len(check)
    disagreements = int((check["difference"] > 0.01).sum())
    agreement = 1 - disagreements / matched if matched else 0.0

    print(f"  Intervals matched:  {matched}")
    print(f"  Agreement within 0.01 EUR/MWh: {agreement:.1%}")
    print(f"  Largest difference: {check['difference'].max():.4f} EUR/MWh")

    write_table(
        check.assign(market_day=check["ts_utc"].dt.date),
        "silver_price_source_agreement",
        "Interval level comparison of ENTSO-E against OMIE day-ahead prices",
    )
else:
    print("  OMIE returned nothing for this range, skipping the comparison.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Gold
# MAGIC
# MAGIC Three tables, each serving a named user.
# MAGIC
# MAGIC 1. **A manufacturer scheduling energy intensive equipment** needs to know
# MAGIC    whether one hour is reliably worse than another, which is
# MAGIC    `gold_daily_profile`. One episode tells them nothing; a pattern they
# MAGIC    can plan around every week does.
# MAGIC 2. **A journalist or regulator** needs a defensible figure with a cause
# MAGIC    and a cost, which is `gold_split_episodes`. The cost is the premium
# MAGIC    applied to the energy actually imported during the episode, not to
# MAGIC    national demand, which would overstate it by orders of magnitude.
# MAGIC 3. **A grid analyst** needs utilisation interval by interval, which is
# MAGIC    `gold_interval_premium`.

# COMMAND ----------

flagged = flag_decoupling(build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN))
step = infer_step(flagged)
border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))

tables = gold_tables(flagged, border, pd.DataFrame(weather_rows))

comments = {
    "gold_interval_premium":
        "One row per settlement interval: prices, premium, border utilisation",
    "gold_daily_profile":
        "Probability and size of a price split by hour of day",
    "gold_split_episodes":
        "Contiguous market splitting episodes with duration, cause and cost",
    "gold_weather_context":
        "Interval premiums joined to hourly weather at price relevant locations",
}

print("GOLD")
for name, frame in tables.items():
    write_table(frame, name, comments.get(name, name))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify

# COMMAND ----------

print(f"Settlement interval: {int((step / pd.Timedelta(hours=1)) * 60)} minutes\n")

for row in spark.sql(f"SHOW TABLES IN {CATALOG}.{SCHEMA}").collect():
    name = row["tableName"]
    count = spark.table(f"{CATALOG}.{SCHEMA}.{name}").count()
    print(f"  {name:<34} {count:>7} rows")

# COMMAND ----------

# MAGIC %sql
# MAGIC -- The episodes a journalist would ask about: longest first, with the
# MAGIC -- cost of the energy actually imported while the zones priced apart.
# MAGIC SELECT
# MAGIC   start_utc,
# MAGIC   duration_hours,
# MAGIC   ROUND(peak_spread, 2)     AS peak_premium_eur_mwh,
# MAGIC   premium_side,
# MAGIC   max_severity,
# MAGIC   ROUND(share_saturated, 2) AS share_border_full,
# MAGIC   ROUND(extra_cost_eur, 0)  AS extra_cost_eur
# MAGIC FROM gold_split_episodes
# MAGIC ORDER BY duration_hours DESC, peak_premium_eur_mwh DESC

# COMMAND ----------

# MAGIC %sql
# MAGIC -- The interesting residual: splits the saturation story does not account
# MAGIC -- for. Every row here is a case the current explanation misses, and the
# MAGIC -- pattern in them is where the next result lives.
# MAGIC SELECT
# MAGIC   ts_utc,
# MAGIC   ROUND(premium_eur_mwh, 2) AS premium_eur_mwh,
# MAGIC   ROUND(utilisation, 3)     AS utilisation,
# MAGIC   ROUND(capacity_mw, 0)     AS capacity_mw
# MAGIC FROM gold_interval_premium
# MAGIC WHERE is_decoupled AND NOT COALESCE(is_saturated, false)
# MAGIC ORDER BY ABS(premium_eur_mwh) DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next
# MAGIC
# MAGIC - Run a longer range. Seven days is not enough for the daily profile to
# MAGIC   mean anything; thirty is a minimum and sixty is better. `replaceWhere`
# MAGIC   makes the backfill safe to repeat.
# MAGIC - Schedule this as a job, or lift the same calls into a Lakeflow
# MAGIC   declarative pipeline. The functions do not change either way.
# MAGIC - The weather correlation in `gold_weather_context` is confounded by
# MAGIC   time of day: solar radiation is mostly a function of the clock, so a
# MAGIC   raw correlation against price measures the daily cycle rather than the
# MAGIC   effect of sunshine. Control for hour of day before quoting it.
