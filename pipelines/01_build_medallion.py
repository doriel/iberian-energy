# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC # MIBEL market intelligence: ingestion
# MAGIC
# MAGIC Fetches Iberian electricity market data and lands the payloads, byte for
# MAGIC byte, in a Unity Catalog Volume. It writes no tables.
# MAGIC
# MAGIC That division is deliberate and it changed. This notebook used to build
# MAGIC silver and gold as well, which was fine while it was the only thing
# MAGIC running. It is not any more: the Lakeflow declarative pipeline in
# MAGIC `pipelines/transformations/` now owns every table from bronze onwards,
# MAGIC and a declarative pipeline only manages tables it created. Two writers
# MAGIC on one table name means the pipeline refuses to run, and worse, it means
# MAGIC two implementations of the same transformation drifting apart until a
# MAGIC number on a page cannot be reproduced.
# MAGIC
# MAGIC So the split is:
# MAGIC
# MAGIC | Here | In the pipeline |
# MAGIC |---|---|
# MAGIC | Call the APIs | Read the Volume with Auto Loader |
# MAGIC | Land the bytes unmodified | Parse into silver, build gold |
# MAGIC | Nothing else | Everything else |
# MAGIC
# MAGIC Landing the raw bytes rather than parsed rows is what makes a parser fix
# MAGIC a reprocess of stored payloads instead of another call against a rate
# MAGIC limited API. It is also what let sixty market days be rebuilt from disk
# MAGIC without asking ENTSO-E for any of it again.
# MAGIC
# MAGIC **Run order**: this notebook, then the declarative pipeline. As a Job,
# MAGIC two tasks with the pipeline depending on this one.
# MAGIC
# MAGIC The ingestion and parsing logic lives in `src/iberian/` as plain Python
# MAGIC with no Databricks imports, so the same functions run in a local test
# MAGIC suite in under two seconds and here unchanged. This notebook is
# MAGIC orchestration, not a place where logic is reimplemented.

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
# MAGIC Tokens come from a secret scope, never from a widget. A widget's value is
# MAGIC saved with the notebook state, and this repository is public. Create the
# MAGIC scope once with the CLI:
# MAGIC
# MAGIC ```
# MAGIC databricks secrets create-scope iberian
# MAGIC databricks secrets put-secret iberian entsoe_token
# MAGIC databricks secrets put-secret iberian esios_token
# MAGIC ```

# COMMAND ----------

# Widgets are recreated from scratch on every run. Without removeAll, a widget
# that already exists keeps whatever value it had, so changing a default below
# would silently have no effect.
dbutils.widgets.removeAll()

dbutils.widgets.text("catalog", "bootcamp_students", "Unity Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("volume", "raw", "Volume for bronze")
dbutils.widgets.text("start_day", "", "First market day (YYYY-MM-DD, blank = recent)")
dbutils.widgets.text("days", "3", "Number of market days")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
VOLUME = dbutils.widgets.get("volume").strip()
START_DAY = dbutils.widgets.get("start_day").strip()
DAYS = int(dbutils.widgets.get("days"))
SCOPE = dbutils.widgets.get("secret_scope").strip()

if not CATALOG:
    raise ValueError(
        "Set the catalog widget. Run SHOW CATALOGS to see what you can write to."
    )

VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
print(f"Target   {CATALOG}.{SCHEMA}")
print(f"Landing  {VOLUME_ROOT}")
print(f"Window   {DAYS} market day(s) from "
      f"{START_DAY or 'the most recent, ending today'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Credentials
# MAGIC
# MAGIC The modules under `src/iberian/` read their tokens from the environment,
# MAGIC and that is all they know. What differs between a laptop and this
# MAGIC workspace is only who puts the values there: a gitignored `.env` locally,
# MAGIC a secret scope here. Setting the environment at this boundary means there
# MAGIC is one authentication path to maintain rather than two.
# MAGIC
# MAGIC Never print a secret. Databricks tries to redact them from cell output,
# MAGIC but that protection is defeated by anything as simple as printing the
# MAGIC characters one at a time. Print the length if you need to check it is set.

# COMMAND ----------

import os

os.environ["ENTSOE_SECURITY_TOKEN"] = dbutils.secrets.get(scope=SCOPE, key="entsoe_token")
os.environ["ESIOS_TOKEN"] = dbutils.secrets.get(scope=SCOPE, key="esios_token")

for name in ("ENTSOE_SECURITY_TOKEN", "ESIOS_TOKEN"):
    value = os.environ.get(name, "")
    print(f"  {name:<24} {'set, ' + str(len(value)) + ' characters' if value else 'MISSING'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Catalog objects
# MAGIC
# MAGIC Only the Volume is created here. The schema has to exist for the Volume
# MAGIC to live in it, but no table is created: those belong to the pipeline.

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.{VOLUME}")
print(f"Ready: volume {VOLUME} in {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import the project modules
# MAGIC
# MAGIC This notebook sits in `pipelines/` inside the Git folder, so the package
# MAGIC is one level up under `src/`. Importing rather than copying is what keeps
# MAGIC the logic covered by the test suite: if a function changes, the tests
# MAGIC catch it before this notebook ever runs.

# COMMAND ----------

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

import json  # noqa: E402
import tempfile  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.border import fetch_border  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.ingestion.esios import INDICATORS, EsiosClient  # noqa: E402
from iberian.ingestion.omie import OmieClient  # noqa: E402
from iberian.ingestion.open_meteo import LOCATIONS, OpenMeteoClient  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402

# A blank start day means "the last DAYS market days, ending today". That is
# what a scheduled run needs, and it keeps the schedule out of the Job
# definition: the Job passes no date at all and the window follows the calendar.
# Naming an explicit day is still how a backfill is done.
#
# The default window is three days rather than one on purpose. ENTSO-E
# republishes corrected documents, and a trailing window re-fetches the last few
# days so a correction is picked up. Landing the same day twice is safe: the
# pipeline keeps the later publication.
START = date.fromisoformat(START_DAY) if START_DAY else (
    date.today() - timedelta(days=DAYS - 1)
)
DAY_LIST = [START + timedelta(days=offset) for offset in range(DAYS)]
WINDOW_START, WINDOW_END = market_day_range(START, DAYS)

print(f"Market days {DAY_LIST[0]} to {DAY_LIST[-1]}")
print(f"UTC window  {WINDOW_START:%Y-%m-%d %H:%M}Z to {WINDOW_END:%Y-%m-%d %H:%M}Z")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Landing
# MAGIC
# MAGIC One helper, and it does one thing. The layout under the Volume is the
# MAGIC same as the local `data/raw/`, which is what allows a local backfill to
# MAGIC be copied up and read by the pipeline without translation.
# MAGIC
# MAGIC Landing the same window twice is safe. The pipeline resolves overlapping
# MAGIC documents by publication time, treating the later one as a correction,
# MAGIC which is how the transparency platform itself treats a republication.

# COMMAND ----------

landed: list[str] = []


def land(subpath: str, filename: str, payload: bytes) -> str:
    """Write a raw payload into the Volume, unmodified."""
    target = f"{VOLUME_ROOT}/{subpath}"
    dbutils.fs.mkdirs(target)
    path = f"{target}/{filename}"
    with open(path, "wb") as handle:
        handle.write(payload)
    landed.append(path)
    print(f"  {len(payload):>9,} bytes  {subpath}/{filename}")
    return path

# COMMAND ----------

# MAGIC %md
# MAGIC ## ENTSO-E: prices, schedules and capacity
# MAGIC
# MAGIC Prices for both bidding zones, plus the scheduled commercial exchanges
# MAGIC and day-ahead transfer capacity that make the saturation test possible.
# MAGIC Both directions of the border are fetched, because a net flow is one
# MAGIC side minus the other and the platform publishes one direction per
# MAGIC request.

# COMMAND ----------

settings = Settings.from_env()
client = EntsoeClient(settings.require_entsoe_token())

print("ENTSO-E day-ahead prices")
for label, eic in (("PT", EIC_PORTUGAL), ("ES", EIC_SPAIN)):
    response = client.day_ahead_prices(eic, WINDOW_START, WINDOW_END)
    land(
        f"entsoe/day_ahead_prices/zone={label}",
        f"{START:%Y-%m-%d}_{DAYS}d{response.suggested_extension}",
        response.content,
    )

# COMMAND ----------

print("ENTSO-E cross-border series")

# fetch_border lands the raw payloads itself under the layout the pipeline
# expects, so give it a scratch directory and copy the bytes into the Volume
# rather than duplicating its knowledge of that layout here.
with tempfile.TemporaryDirectory() as scratch:
    fetch_border(client, WINDOW_START, WINDOW_END, Path(scratch), START, verbose=False)
    for path in sorted(Path(scratch).rglob("*")):
        if path.is_file():
            land(path.relative_to(scratch).parent.as_posix(), path.name, path.read_bytes())

# COMMAND ----------

# MAGIC %md
# MAGIC ## OMIE
# MAGIC
# MAGIC The Iberian market operator publishes the same day-ahead prices as flat
# MAGIC files. A second independent publication of one number is what makes a
# MAGIC quality check meaningful rather than self referential.

# COMMAND ----------

print("OMIE day-ahead files")
omie_client = OmieClient()

for day in DAY_LIST:
    try:
        response = omie_client.day_ahead_prices(day)
    except RuntimeError as exc:
        print(f"  {day}: failed, {exc}")
        continue
    if response.looks_empty:
        print(f"  {day}: empty")
        continue
    land(f"omie/file_set={response.file_set}", response.filename, response.content)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Open-Meteo
# MAGIC
# MAGIC Weather is the upstream driver. Market data shows that the border was
# MAGIC full and that Portugal paid a premium; it cannot show why Spanish power
# MAGIC was cheap enough to be worth importing. Solar radiation and wind can.
# MAGIC
# MAGIC Locations are chosen for what drives the price rather than for
# MAGIC population, and the reasoning for each sits next to it in
# MAGIC `ingestion/open_meteo.py`.

# COMMAND ----------

print("Open-Meteo hourly weather")
weather_client = OpenMeteoClient()

for location in LOCATIONS:
    try:
        _, payload = weather_client.hourly(location, DAY_LIST[0], DAY_LIST[-1])
    except RuntimeError as exc:
        print(f"  {location}: failed, {exc}")
        continue
    land(
        f"open_meteo/location={location}",
        f"{START:%Y-%m-%d}_{DAYS}d.json",
        json.dumps(payload).encode("utf-8"),
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## REE / ESIOS
# MAGIC
# MAGIC Congestion rent on the Portuguese border, which is the independent check
# MAGIC on this project's cost figure, plus the demand forecast and the demand
# MAGIC that actually happened.
# MAGIC
# MAGIC REE's terms are specific: the token is personal, and anything published
# MAGIC has to be served from your own infrastructure rather than by calling
# MAGIC theirs. Landing the payloads here is what makes that possible.

# COMMAND ----------

print("ESIOS indicators")
esios_client = EsiosClient(os.environ["ESIOS_TOKEN"])

for name, indicator_id in INDICATORS.items():
    try:
        response = esios_client.indicator(indicator_id, WINDOW_START, WINDOW_END)
    except RuntimeError as exc:
        print(f"  {name} ({indicator_id}): failed, {exc}")
        continue
    land(
        f"esios/indicator={indicator_id}",
        f"{START:%Y-%m-%d}_{DAYS}d.json",
        response.content,
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## What landed

# COMMAND ----------

print(f"{len(landed)} file(s) landed under {VOLUME_ROOT}\n")

for folder in ("entsoe/day_ahead_prices", "entsoe/crossborder", "omie",
               "open_meteo", "esios"):
    try:
        entries = dbutils.fs.ls(f"{VOLUME_ROOT}/{folder}")
        print(f"  {folder:<28} {len(entries)} entries")
    except Exception:
        print(f"  {folder:<28} not present")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next
# MAGIC
# MAGIC Run the declarative pipeline, the one the `transform` task of the
# MAGIC `iberian-daily` Job triggers. It is a separate object in the workspace,
# MAGIC listed under Jobs & Pipelines, and the bundle references it by id as
# MAGIC `pipeline_id`. Auto Loader picks up only the files it has
# MAGIC not seen, parses them into silver and rebuilds gold.
# MAGIC
# MAGIC As a scheduled Job this notebook is task one and the pipeline is task
# MAGIC two, depending on it. Day-ahead results are published in the early
# MAGIC afternoon local time, so a run after that picks up the following market
# MAGIC day.
# MAGIC
# MAGIC Two sources are landed here but not yet read by the pipeline: OMIE and
# MAGIC Open-Meteo. Their bronze and silver tables, and the weather context gold
# MAGIC table, still need adding to `pipelines/transformations/`. Until then the
# MAGIC cross source price check and the weather analysis run locally only, and
# MAGIC saying so is better than leaving someone to discover it.