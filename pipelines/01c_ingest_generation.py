# Databricks notebook source
# MAGIC %md
# MAGIC # Backfill actual generation per generation unit
# MAGIC
# MAGIC Run by hand. Lands one raw XML document per zone per day into the same
# MAGIC Volume the rest of bronze uses, and writes no tables.
# MAGIC
# MAGIC ## Why this is its own notebook and not part of the daily Job
# MAGIC
# MAGIC Because it is a backfill of seven hundred and thirty requests. ENTSO-E
# MAGIC limits [16.1.A] to **one day per request**, so a year is a loop, and a
# MAGIC loop of that length inside a task that runs every afternoon would make
# MAGIC seven hundred requests a day for data that changed in one of them.
# MAGIC
# MAGIC ## Why a year, when the market window is seventy days
# MAGIC
# MAGIC A fact table is allowed more history than the curated window sitting
# MAGIC inside it, and here the extra history does work. To say that a unit was
# MAGIC producing less than it usually does during an episode, you need a
# MAGIC baseline, and a baseline needs a year. Seventy days of summer tells you
# MAGIC nothing about how the fleet behaves in February.
# MAGIC
# MAGIC Measured rather than assumed, by `scripts/probe_generation_units.py`
# MAGIC against a real day: 191 units in Spain at quarter-hourly resolution and
# MAGIC 73 in Portugal at hourly, about 7,800 readings a day, so a year is
# MAGIC roughly 2.9 million rows.
# MAGIC
# MAGIC ## Safe to stop and safe to re-run
# MAGIC
# MAGIC Every day already landed is skipped, so an interrupted run is resumed by
# MAGIC starting it again. That is not a nicety at this length: a cluster that
# MAGIC restarts halfway through should not mean beginning at day one.

# COMMAND ----------

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("volume", "raw", "Volume for bronze")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("days", "365", "Days to fetch, ending yesterday")
dbutils.widgets.text("end_date", "", "Last day, YYYY-MM-DD (blank: yesterday)")
dbutils.widgets.dropdown("refetch", "no", ["no", "yes"], "Re-fetch days already landed")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
VOLUME = dbutils.widgets.get("volume").strip()
SCOPE = dbutils.widgets.get("secret_scope").strip()
DAYS = int(dbutils.widgets.get("days"))
REFETCH = dbutils.widgets.get("refetch") == "yes"

VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
SUBPATH = "entsoe/actual_generation_per_unit"

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

SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

# COMMAND ----------

import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient, EntsoeRequestError  # noqa: E402
from iberian.parsing.entsoe_generation import is_acknowledgement  # noqa: E402

if dbutils.widgets.get("end_date").strip():
    last_day = datetime.strptime(
        dbutils.widgets.get("end_date").strip(), "%Y-%m-%d"
    ).replace(tzinfo=timezone.utc)
else:
    # Yesterday, not today. Generation per unit is published with a lag and
    # today's document is incomplete by definition.
    last_day = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

DAY_LIST = [last_day - timedelta(days=offset) for offset in range(DAYS - 1, -1, -1)]
ZONES = (("ES", EIC_SPAIN), ("PT", EIC_PORTUGAL))

print(f"{DAY_LIST[0]:%Y-%m-%d} to {DAY_LIST[-1]:%Y-%m-%d}, {len(DAY_LIST)} days")
print(f"{len(DAY_LIST) * len(ZONES)} requests at most, into {VOLUME_ROOT}/{SUBPATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What is already there
# MAGIC
# MAGIC Listed once, up front, rather than checked file by file. A `ls` per day
# MAGIC is seven hundred round trips to storage to answer a question one listing
# MAGIC answers.

# COMMAND ----------

for zone, _ in ZONES:
    dbutils.fs.mkdirs(f"{VOLUME_ROOT}/{SUBPATH}/zone={zone}")


def already_landed() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for zone, _ in ZONES:
        try:
            for entry in dbutils.fs.ls(f"{VOLUME_ROOT}/{SUBPATH}/zone={zone}"):
                if entry.size > 0:
                    found.add((zone, entry.name.split(".")[0]))
        except Exception:
            pass
    return found


landed_before = already_landed()
print(f"{len(landed_before)} day-zone pairs already landed")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fetch
# MAGIC
# MAGIC Three outcomes per request, and they are deliberately not collapsed into
# MAGIC two. A day ENTSO-E has no data for is not a day that failed: the first is
# MAGIC an answer, the second is a gap that somebody has to come back to. Landing
# MAGIC an empty file for either would make them indistinguishable afterwards,
# MAGIC and a quiet day and a lost day look identical in a row count.

# COMMAND ----------

# From the secret scope, never a widget. A widget value is saved with the
# notebook state and this repository is public.
os.environ["ENTSOE_SECURITY_TOKEN"] = dbutils.secrets.get(scope=SCOPE, key="entsoe_token")

settings = Settings.from_env()
client = EntsoeClient(settings.require_entsoe_token())
print(f"token: {len(settings.require_entsoe_token())} characters")

#: Polite rather than necessary. ENTSO-E's published limit is well above this
#: rate, and a backfill is exactly the thing that makes somebody look.
PAUSE_SECONDS = 0.3

fetched, skipped, no_data, failed = 0, 0, [], []
total_bytes = 0
started = time.time()

for index, day in enumerate(DAY_LIST, start=1):
    stamp = f"{day:%Y-%m-%d}"
    for zone, eic in ZONES:
        if not REFETCH and (zone, stamp) in landed_before:
            skipped += 1
            continue

        try:
            response = client.actual_generation_per_unit(eic, day, day + timedelta(days=1))
        except EntsoeRequestError as exc:
            failed.append(f"{stamp} {zone}: {exc.reason or exc.status_code}")
            continue
        except Exception as exc:
            failed.append(f"{stamp} {zone}: {type(exc).__name__}: {str(exc)[:120]}")
            continue

        declined, reason = is_acknowledgement(response.body)
        if declined or not response.body.strip():
            no_data.append(f"{stamp} {zone}: {reason or 'empty response'}")
            continue

        path = f"{VOLUME_ROOT}/{SUBPATH}/zone={zone}/{stamp}.xml"
        with open(path, "wb") as handle:
            handle.write(response.content)

        fetched += 1
        total_bytes += len(response.content)
        time.sleep(PAUSE_SECONDS)

    # Every fortnight rather than every day: a progress line per day is seven
    # hundred lines of output nobody reads, and none at all on a run this long
    # looks like it has hung.
    if index % 14 == 0 or index == len(DAY_LIST):
        elapsed = time.time() - started
        print(
            f"  {index:>3}/{len(DAY_LIST)} days  "
            f"{fetched:>4} fetched  {skipped:>4} skipped  "
            f"{len(no_data):>3} no data  {len(failed):>3} failed  "
            f"{total_bytes / 1e6:>7.1f} MB  {elapsed / 60:>5.1f} min"
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## What happened
# MAGIC
# MAGIC Read back from the Volume rather than trusted from the counters above.
# MAGIC The counters say what this run did; the listing says what is there, which
# MAGIC is the thing the next notebook will read.

# COMMAND ----------

landed_after = already_landed()

print(f"fetched this run   {fetched:>6}")
print(f"skipped            {skipped:>6}")
print(f"no data            {len(no_data):>6}")
print(f"failed             {len(failed):>6}")
print(f"bytes this run     {total_bytes / 1e6:>6.1f} MB")
print()

for zone, _ in ZONES:
    days = sorted(stamp for found_zone, stamp in landed_after if found_zone == zone)
    if days:
        print(f"{zone}: {len(days):>3} days on the Volume, {days[0]} to {days[-1]}")
    else:
        print(f"{zone}: nothing landed")

expected = len(DAY_LIST) * len(ZONES)
print(f"\n{len(landed_after)} of {expected} day-zone pairs present")

if no_data:
    print(f"\nNo data ({len(no_data)}), which is an answer rather than a failure:")
    for line in no_data[:10]:
        print(f"  {line}")
    if len(no_data) > 10:
        print(f"  ... and {len(no_data) - 10} more")

if failed:
    print(f"\nFailed ({len(failed)}), and these are gaps worth re-running:")
    for line in failed[:10]:
        print(f"  {line}")
    if len(failed) > 10:
        print(f"  ... and {len(failed) - 10} more")
    print("\n  Run this notebook again. Landed days are skipped, so it will")
    print("  only retry what is missing.")

# COMMAND ----------

message = f"{len(landed_after)} day-zone documents on the Volume"
if failed:
    message += f" | {len(failed)} failed, re-run to retry"
dbutils.notebook.exit(message)