# Databricks notebook source
# MAGIC %md
# MAGIC # Backfill market history
# MAGIC
# MAGIC Run by hand, when you want more history than the daily Job has
# MAGIC accumulated. Say how many days you want in the `days` widget, run it, and
# MAGIC it lands the payloads for that window into the same Volume the Job uses.
# MAGIC It writes no tables.
# MAGIC
# MAGIC ## Why this is separate from `01_build_medallion`
# MAGIC
# MAGIC That notebook is what the Job runs every afternoon, with a three day
# MAGIC trailing window so ENTSO-E's corrections get picked up. Turning its
# MAGIC `days` widget up to 365 would make the Job re-fetch a year every single
# MAGIC day, for data that changed in three of those days. The schedule and the
# MAGIC backfill want opposite things from the same code, so they get separate
# MAGIC notebooks and share the client library underneath.
# MAGIC
# MAGIC ## Nothing downstream has to change
# MAGIC
# MAGIC The layout under the Volume is identical to the Job's, so the Lakeflow
# MAGIC pipeline picks these files up with Auto Loader on its next run, with no
# MAGIC edit to `pipelines/transformations/`. Silver grows, gold is recomputed
# MAGIC from the larger silver, and that is the whole integration.
# MAGIC
# MAGIC Two things make that safe rather than merely convenient:
# MAGIC
# MAGIC **Episode identity survives.** An episode's key is `market_day` plus its
# MAGIC start time, computed from the episode itself in
# MAGIC `iberian.agent.batch.episode_key`. It is not the sequential `episode_id`
# MAGIC the grouping assigns. So adding older history creates new episodes and
# MAGIC renames none of the existing ones. Labels in Lakebase, published
# MAGIC explanations and the evaluation set all keep pointing at the same
# MAGIC episodes they did before.
# MAGIC
# MAGIC **Agent spend stays bounded.** `02_explain_episodes` skips anything
# MAGIC already explained and caps a run at `max_new`, 25 by default. A year of
# MAGIC new episodes does not turn into a year of model calls the next time that
# MAGIC notebook runs.
# MAGIC
# MAGIC ## Chunks, not one big request
# MAGIC
# MAGIC The window is cut into chunks of `chunk_days` and each chunk is one
# MAGIC request per source. Thirty days is the default because it keeps every
# MAGIC response a size the platforms will actually serve, and because a failure
# MAGIC then costs one chunk rather than the year.
# MAGIC
# MAGIC OMIE is the exception: it publishes one file per day, so a chunk there is
# MAGIC a loop of days. That is most of the requests this notebook makes.
# MAGIC
# MAGIC ## Safe to stop and safe to re-run
# MAGIC
# MAGIC Every chunk already on the Volume is skipped, so an interrupted run is
# MAGIC resumed by starting it again. The filenames carry the chunk length, so a
# MAGIC thirty day backfill file never lands on top of a three day Job file.
# MAGIC
# MAGIC That last point is not cosmetic. Auto Loader tracks files by path, so a
# MAGIC file overwritten in place is one the pipeline has already seen and will
# MAGIC not read again. Overwriting is therefore the one way to land data that is
# MAGIC silently never ingested, which is why `refetch` defaults to no.

# COMMAND ----------

# MAGIC %pip install requests pandas
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("volume", "raw", "Volume for bronze")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("days", "365", "Days to fetch, ending at end_date")
dbutils.widgets.text("end_date", "", "Last day, YYYY-MM-DD (blank: yesterday)")
dbutils.widgets.text("chunk_days", "30", "Days per request")
dbutils.widgets.dropdown(
    "sources",
    "all",
    ["all", "entsoe_prices", "entsoe_border", "omie", "open_meteo", "esios"],
    "Which source",
)
dbutils.widgets.dropdown("refetch", "no", ["no", "yes"], "Re-fetch chunks already landed")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
VOLUME = dbutils.widgets.get("volume").strip()
SCOPE = dbutils.widgets.get("secret_scope").strip()
DAYS = int(dbutils.widgets.get("days"))
CHUNK_DAYS = int(dbutils.widgets.get("chunk_days"))
SOURCES = dbutils.widgets.get("sources")
REFETCH = dbutils.widgets.get("refetch") == "yes"

VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

print(f"Landing  {VOLUME_ROOT}")
print(f"Window   {DAYS} days in chunks of {CHUNK_DAYS}")
print(f"Sources  {SOURCES}")

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

# From the secret scope, never a widget. A widget value is saved with the
# notebook state and this repository is public. Print the length, never the
# value: Databricks redacts secrets from output, and that protection is
# defeated by anything as simple as printing the characters one at a time.
os.environ["ENTSOE_SECURITY_TOKEN"] = dbutils.secrets.get(scope=SCOPE, key="entsoe_token")
os.environ["ESIOS_TOKEN"] = dbutils.secrets.get(scope=SCOPE, key="esios_token")

for name in ("ENTSOE_SECURITY_TOKEN", "ESIOS_TOKEN"):
    print(f"  {name}: {len(os.environ.get(name, ''))} characters")

# COMMAND ----------

import io  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.border import fetch_border  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.ingestion.esios import INDICATORS, EsiosClient  # noqa: E402
from iberian.ingestion.omie import OmieClient  # noqa: E402
from iberian.ingestion.open_meteo import LOCATIONS, OpenMeteoClient  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402

if dbutils.widgets.get("end_date").strip():
    LAST_DAY = date.fromisoformat(dbutils.widgets.get("end_date").strip())
else:
    # Yesterday, not today. Day-ahead results for today exist, but the
    # settlement and the corrections do not, and a half published day landed
    # under a thirty day filename is a day nothing will ever correct.
    LAST_DAY = datetime.now(timezone.utc).date() - timedelta(days=1)

FIRST_DAY = LAST_DAY - timedelta(days=DAYS - 1)

#: Oldest first, so an interrupted run has filled in history rather than a hole
#: in the middle. The last chunk is short whenever the window does not divide.
CHUNKS: list[tuple[date, int]] = []
cursor = FIRST_DAY
while cursor <= LAST_DAY:
    length = min(CHUNK_DAYS, (LAST_DAY - cursor).days + 1)
    CHUNKS.append((cursor, length))
    cursor += timedelta(days=length)

print(f"{FIRST_DAY} to {LAST_DAY}, {DAYS} days in {len(CHUNKS)} chunks")
print(f"first chunk {CHUNKS[0][0]} for {CHUNKS[0][1]}d, "
      f"last {CHUNKS[-1][0]} for {CHUNKS[-1][1]}d")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What is already there
# MAGIC
# MAGIC Listed once per folder, up front, rather than checked file by file. A
# MAGIC `ls` per request is several hundred round trips to storage to answer a
# MAGIC question one listing answers.

# COMMAND ----------

landed: list[str] = []
fetched, skipped, failed = 0, 0, []
total_bytes = 0


def existing(folder: str) -> set[str]:
    """Filenames already under one folder of the Volume."""
    try:
        return {entry.name.rstrip("/") for entry in dbutils.fs.ls(f"{VOLUME_ROOT}/{folder}")}
    except Exception:
        return set()


def land(folder: str, filename: str, payload: bytes) -> None:
    global total_bytes
    target = f"{VOLUME_ROOT}/{folder}"
    dbutils.fs.mkdirs(target)
    with open(f"{target}/{filename}", "wb") as handle:
        handle.write(payload)
    landed.append(f"{folder}/{filename}")
    total_bytes += len(payload)


def already(folder: str, prefix: str) -> bool:
    """Whether a chunk landed before, matched on the filename's prefix.

    The extension varies: ENTSO-E answers a large window with a zip rather than
    an XML document, and which one you get is not known before asking.
    """
    if REFETCH:
        return False
    return any(name.startswith(prefix) for name in INVENTORY.get(folder, set()))


def chunk_prefix(start: date, length: int) -> str:
    """`2025-10-01_30d`. The length is in the name on purpose.

    Without it a thirty day backfill file and a three day Job file collide on
    the same path, and an overwrite is a file Auto Loader has already seen and
    will never read again. The data would be on the Volume and absent from
    every table, which is the worst of both.
    """
    return f"{start:%Y-%m-%d}_{length}d"


def land_xml(folder: str, prefix: str, payload: bytes) -> int:
    """Land an ENTSO-E payload as XML documents, expanding a ZIP archive.

    The pipeline reads these folders with `pathGlobFilter "*.xml"`. A `.zip`
    landed there is a file Auto Loader filters out, so the data would sit on
    the Volume and be absent from every table, with nothing anywhere reporting
    a problem.

    ENTSO-E answers with a ZIP once the response holds more than one document.
    A three day window does not reach that, which is why the daily Job has
    never met this and a thirty day backfill meets it every time. Expanding on
    the way in costs nothing and removes the whole class of failure.
    """
    if payload[:2] != b"PK":
        land(folder, f"{prefix}.xml", payload)
        return 1

    written = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = sorted(name for name in archive.namelist() if not name.endswith("/"))
        for index, name in enumerate(names):
            land(folder, f"{prefix}_{index:03d}.xml", archive.read(name))
            written += 1
    return written


#: Polite rather than required. The published limits are well above this rate,
#: and a backfill is exactly the thing that makes somebody look at the logs.
PAUSE_SECONDS = 0.3

FOLDERS = (
    [f"entsoe/day_ahead_prices/zone={z}" for z in ("PT", "ES")]
    + [f"entsoe/crossborder/kind={k}/dir={d}"
       for k in ("A09", "A61") for d in ("ES_to_PT", "PT_to_ES")]
    + [f"open_meteo/location={location}" for location in LOCATIONS]
    + [f"esios/indicator={indicator}" for indicator in INDICATORS.values()]
)

INVENTORY = {folder: existing(folder) for folder in FOLDERS}

# OMIE names its files after the day rather than the chunk, and spreads them
# over file_set folders whose name is not known before fetching. So its
# inventory is one flat set of every filename already there.
OMIE_LANDED: set[str] = set()
try:
    for entry in dbutils.fs.ls(f"{VOLUME_ROOT}/omie"):
        for inner in dbutils.fs.ls(entry.path):
            OMIE_LANDED.add(inner.name)
except Exception:
    pass

print(f"{sum(len(names) for names in INVENTORY.values())} files already in the "
      f"chunked folders")
print(f"{len(OMIE_LANDED)} OMIE files already landed")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fetch
# MAGIC
# MAGIC One section per source so a single failing platform can be re-run on its
# MAGIC own with the `sources` widget, rather than re-running the four that
# MAGIC worked.

# COMMAND ----------

settings = Settings.from_env()
entsoe = EntsoeClient(settings.require_entsoe_token())
omie = OmieClient()
weather = OpenMeteoClient()
esios = EsiosClient(os.environ["ESIOS_TOKEN"])

started = time.time()
wanted = {SOURCES} if SOURCES != "all" else {
    "entsoe_prices", "entsoe_border", "omie", "open_meteo", "esios"
}


def note_failure(label: str, exc: Exception) -> None:
    failed.append(f"{label}: {type(exc).__name__}: {str(exc).splitlines()[0][:140]}")


def progress(index: int) -> None:
    elapsed = (time.time() - started) / 60
    print(
        f"  chunk {index:>2}/{len(CHUNKS)}  {fetched:>4} fetched  "
        f"{skipped:>4} skipped  {len(failed):>3} failed  "
        f"{total_bytes / 1e6:>7.1f} MB  {elapsed:>5.1f} min"
    )

# COMMAND ----------

if "entsoe_prices" in wanted:
    print("ENTSO-E day-ahead prices")
    for index, (start, length) in enumerate(CHUNKS, start=1):
        window_start, window_end = market_day_range(start, length)
        for label, eic in (("PT", EIC_PORTUGAL), ("ES", EIC_SPAIN)):
            folder = f"entsoe/day_ahead_prices/zone={label}"
            prefix = chunk_prefix(start, length)
            if already(folder, prefix):
                skipped += 1
                continue
            try:
                response = entsoe.day_ahead_prices(eic, window_start, window_end)
            except Exception as exc:
                note_failure(f"prices {label} {start}", exc)
                continue
            documents = land_xml(folder, prefix, response.content)
            if documents == 0:
                note_failure(f"prices {label} {start}", ValueError("empty archive"))
            fetched += documents
            time.sleep(PAUSE_SECONDS)
        if index % 4 == 0 or index == len(CHUNKS):
            progress(index)

# COMMAND ----------

if "entsoe_border" in wanted:
    print("ENTSO-E cross-border schedules and capacity")
    for index, (start, length) in enumerate(CHUNKS, start=1):
        window_start, window_end = market_day_range(start, length)
        prefix = chunk_prefix(start, length)

        # fetch_border owns the crossborder layout, so it writes into a scratch
        # directory and the bytes are copied across rather than this notebook
        # holding a second copy of that knowledge. The chunk length is appended
        # on the way, which fetch_border has no reason to know about.
        targets = [f"entsoe/crossborder/kind={k}/dir={d}"
                   for k in ("A09", "A61") for d in ("ES_to_PT", "PT_to_ES")]
        if all(already(folder, prefix) for folder in targets):
            skipped += len(targets)
            continue

        try:
            with tempfile.TemporaryDirectory() as scratch:
                fetch_border(
                    entsoe, window_start, window_end, Path(scratch), start, verbose=False
                )
                for path in sorted(Path(scratch).rglob("*")):
                    if not path.is_file():
                        continue
                    folder = path.relative_to(scratch).parent.as_posix()
                    fetched += land_xml(folder, prefix, path.read_bytes())
        except Exception as exc:
            note_failure(f"border {start}", exc)

        time.sleep(PAUSE_SECONDS)
        if index % 4 == 0 or index == len(CHUNKS):
            progress(index)

# COMMAND ----------

if "omie" in wanted:
    print("OMIE day-ahead files, one request per day")
    empty_days = 0
    for index, (start, length) in enumerate(CHUNKS, start=1):
        for offset in range(length):
            day = start + timedelta(days=offset)
            stamp = f"{day:%Y%m%d}"
            if not REFETCH and any(stamp in name for name in OMIE_LANDED):
                skipped += 1
                continue
            try:
                response = omie.day_ahead_prices(day)
            except Exception as exc:
                note_failure(f"omie {day}", exc)
                continue
            if response.looks_empty:
                empty_days += 1
                continue
            land(f"omie/file_set={response.file_set}", response.filename, response.content)
            OMIE_LANDED.add(response.filename)
            fetched += 1
            time.sleep(PAUSE_SECONDS)
        if index % 2 == 0 or index == len(CHUNKS):
            progress(index)
    print(f"  {empty_days} days OMIE published nothing for")

# COMMAND ----------

if "open_meteo" in wanted:
    print("Open-Meteo hourly weather")
    # The client picks the reanalysis archive over the forecast endpoint for
    # anything older than a week, which is what makes a year of history
    # available at all. Nothing here has to ask for it.
    for index, (start, length) in enumerate(CHUNKS, start=1):
        last = start + timedelta(days=length - 1)
        prefix = chunk_prefix(start, length)
        for location in LOCATIONS:
            folder = f"open_meteo/location={location}"
            if already(folder, prefix):
                skipped += 1
                continue
            try:
                _, payload = weather.hourly(location, start, last)
            except Exception as exc:
                note_failure(f"weather {location} {start}", exc)
                continue
            land(folder, f"{prefix}.json", json.dumps(payload).encode("utf-8"))
            fetched += 1
            time.sleep(PAUSE_SECONDS)
        if index % 4 == 0 or index == len(CHUNKS):
            progress(index)

# COMMAND ----------

if "esios" in wanted:
    print("ESIOS indicators")
    # REE's terms are specific: this token is personal, and anything published
    # from these figures has to be served from your own infrastructure rather
    # than by calling theirs. Landing the payloads is what makes that possible.
    for index, (start, length) in enumerate(CHUNKS, start=1):
        window_start, window_end = market_day_range(start, length)
        prefix = chunk_prefix(start, length)
        for name, indicator_id in INDICATORS.items():
            folder = f"esios/indicator={indicator_id}"
            if already(folder, prefix):
                skipped += 1
                continue
            try:
                response = esios.indicator(indicator_id, window_start, window_end)
            except Exception as exc:
                note_failure(f"esios {name} {start}", exc)
                continue
            land(folder, f"{prefix}.json", response.content)
            fetched += 1
            time.sleep(PAUSE_SECONDS)
        if index % 4 == 0 or index == len(CHUNKS):
            progress(index)

# COMMAND ----------

# MAGIC %md
# MAGIC ## What happened
# MAGIC
# MAGIC Read back from the Volume rather than trusted from the counters above.
# MAGIC The counters say what this run did; the listing says what is there, which
# MAGIC is what the pipeline will read.

# COMMAND ----------

print(f"fetched this run   {fetched:>6}")
print(f"skipped            {skipped:>6}")
print(f"failed             {len(failed):>6}")
print(f"bytes this run     {total_bytes / 1e6:>6.1f} MB")
print()

for folder in ("entsoe/day_ahead_prices", "entsoe/crossborder", "omie",
               "open_meteo", "esios"):
    try:
        count = 0
        for entry in dbutils.fs.ls(f"{VOLUME_ROOT}/{folder}"):
            try:
                count += len(dbutils.fs.ls(entry.path))
            except Exception:
                count += 1
        print(f"  {folder:<28} {count:>5} files")
    except Exception:
        print(f"  {folder:<28} not present")

if failed:
    print(f"\nFailed ({len(failed)}), and these are gaps worth re-running:")
    for line in failed[:15]:
        print(f"  {line}")
    if len(failed) > 15:
        print(f"  ... and {len(failed) - 15} more")
    print("\n  Run this notebook again with the same widgets. Chunks already")
    print("  landed are skipped, so it only retries what is missing. If one")
    print("  platform failed and the others did not, set the sources widget to")
    print("  that one.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next
# MAGIC
# MAGIC Run the `Iberian_01` pipeline. Auto Loader reads only the files it has
# MAGIC not seen, so it parses the backfill into silver and recomputes gold from
# MAGIC the larger silver. Nothing in `pipelines/transformations/` changes.
# MAGIC
# MAGIC Expect `gold_split_episodes` to grow. Existing episodes keep their keys,
# MAGIC so labels, published explanations and the evaluation set are untouched.
# MAGIC New older episodes arrive unexplained, and `02_explain_episodes` will
# MAGIC work through them `max_new` at a time on its own schedule.
# MAGIC
# MAGIC The generation tables already cover a year. Until this notebook has run,
# MAGIC `gold_unit_hourly_output` and `gold_split_episodes` only overlap for the
# MAGIC days the Job has accumulated, and joining a unit's output to an episode
# MAGIC outside that overlap returns nothing rather than saying why.

# COMMAND ----------

message = f"{fetched:,} files landed, {skipped:,} skipped"
if failed:
    message += f" | {len(failed)} failed, re-run to retry"
dbutils.notebook.exit(message)