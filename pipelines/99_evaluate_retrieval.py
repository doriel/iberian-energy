# Databricks notebook source
# MAGIC %md
# MAGIC # Does the vector index find the same evidence, and does the filter work?
# MAGIC
# MAGIC Run by hand, not part of the daily Job. The 99 says so: everything below
# MAGIC 10 is the daily run or the setup it needs.
# MAGIC
# MAGIC Two questions, and the second one is the one that matters.
# MAGIC
# MAGIC **Agreement.** For every episode that has an explanation, both paths are
# MAGIC asked which asset was tightest and at what capacity. The direct path reads
# MAGIC the notices straight from the transparency platform; the vector path reads
# MAGIC them from the index. A disagreement is a real finding, because the
# MAGIC explanation a reader sees is built from exactly these two values.
# MAGIC
# MAGIC **Leakage.** The same retrieval is run twice, once with the publication
# MAGIC filter and once without, and both are checked for notices published after
# MAGIC the episode began. With the filter the count must be zero. Without it, the
# MAGIC count is the size of the problem the filter solves, and reporting it is
# MAGIC what turns "we handled point in time correctness" from a claim into a
# MAGIC measurement.
# MAGIC
# MAGIC A number that is zero because nothing was tested is worthless, so the
# MAGIC unfiltered count is printed next to the filtered one. If both are zero,
# MAGIC the honest reading is that this window contains no republished notices,
# MAGIC not that the filter was proved.
# MAGIC
# MAGIC ## What this does not measure
# MAGIC
# MAGIC Whether the retrieved asset is the *right* cause. That is the labelled
# MAGIC evaluation set, and it is a different question with a human in it. This
# MAGIC notebook only asks whether two mechanisms agree and whether one of them
# MAGIC can see the future.

# COMMAND ----------

# MAGIC %pip install requests pandas databricks-ai-search==0.78
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("endpoint", "zachy_vs", "Vector Search endpoint")
dbutils.widgets.text("max_episodes", "0", "Cap (0 means all)")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SCOPE = dbutils.widgets.get("secret_scope")
ENDPOINT = dbutils.widgets.get("endpoint")
MAX_EPISODES = int(dbutils.widgets.get("max_episodes"))

TABLE = f"{CATALOG}.{SCHEMA}.gold_transmission_notices"
INDEX = f"{TABLE}_index"
RESULTS = f"{CATALOG}.{SCHEMA}.gold_retrieval_evaluation"

# COMMAND ----------

os.environ["ENTSOE_SECURITY_TOKEN"] = dbutils.secrets.get(
    scope=SCOPE, key="entsoe_token"
)
print(f"token length: {len(os.environ['ENTSOE_SECURITY_TOKEN'])}")

# COMMAND ----------

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
if not os.path.isdir(SRC):
    raise RuntimeError(f"Could not find src/ from {REPO_ROOT}.")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

import pandas as pd  # noqa: E402

from iberian.agent.batch import episode_key  # noqa: E402
from iberian.agent.retrieval import comparable, vector_assets  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import as_utc, market_day_window  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_outages_response,
)
from iberian.publish.dashboard import UnityCatalog  # noqa: E402

DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The episodes
# MAGIC
# MAGIC The ones that have an explanation, because those are the ones whose
# MAGIC evidence a reader has already been shown. Worst first, so a cap keeps the
# MAGIC episodes anybody would look at first.

# COMMAND ----------

source = UnityCatalog(spark, CATALOG, SCHEMA)
episodes = as_utc(source.table("gold_split_episodes"), "start_utc", "end_utc")
explained = source.table("gold_episode_explanations")

keys = set(explained["episode_key"]) if not explained.empty else set()
episodes = episodes.sort_values("max_abs_spread", ascending=False)

if keys:
    episodes = episodes[episodes.apply(episode_key, axis=1).isin(keys)]
    print(f"{len(episodes)} episodes with an explanation on file")
else:
    print(f"no explanations on file, evaluating all {len(episodes)} episodes")

if MAX_EPISODES:
    episodes = episodes.head(MAX_EPISODES)
    print(f"capped at {MAX_EPISODES}")

if episodes.empty:
    dbutils.notebook.exit("no episodes to evaluate")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Both paths, episode by episode
# MAGIC
# MAGIC The day's notices are fetched once and reused, the same way the agent
# MAGIC does it, so this compares retrieval rather than fetching.

# COMMAND ----------

try:
    from databricks.ai_search.client import AISearchClient as Client
except ImportError:
    from databricks.vector_search.client import VectorSearchClient as Client

try:
    search = Client(disable_notice=True)
except TypeError:
    search = Client()

index = search.get_index(endpoint_name=ENDPOINT, index_name=INDEX)
entsoe = EntsoeClient(Settings.from_env().require_entsoe_token())

curves_by_day: dict = {}
records: list[dict] = []
fetch_failures: list[str] = []


def leaked(assets, start) -> int:
    """Retrieved notices that were published at or after the episode began."""
    return sum(
        1
        for row in assets
        if row.get("published_at") is not None and row["published_at"] >= start
    )


for _, episode in episodes.iterrows():
    key = episode_key(episode)
    start = pd.Timestamp(episode["start_utc"]).to_pydatetime()
    end = pd.Timestamp(episode["end_utc"]).to_pydatetime()
    day = episode["market_day"]

    if day not in curves_by_day:
        day_start, day_end = market_day_window(day)
        try:
            response = entsoe.transmission_unavailability(
                *DIRECTION, day_start, day_end
            )
        except Exception as exc:
            first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
            fetch_failures.append(f"{day}: {first_line[:120]}")
            print(f"  {key}  SKIPPED, {first_line[:120]}")
            continue
        curves_by_day[day] = [] if response.is_empty else parse_outages_response(response)

    from_api = binding_assets(
        curves_by_day[day], start, end, published_before=start, direction=DIRECTION
    )
    from_index = vector_assets(
        index, start, end, published_before=start, direction=DIRECTION
    )
    # The same query with the guarantee removed. This is the control: without a
    # number here, a zero above says nothing.
    unfiltered = vector_assets(index, start, end, direction=DIRECTION)

    agree = bool(from_api) == bool(from_index) and (
        not from_api or comparable(from_api[0]) == comparable(from_index[0])
    )

    records.append(
        {
            "episode_key": key,
            "market_day": day,
            "start_utc": start,
            "direct_asset": from_api[0]["asset"] if from_api else None,
            "direct_available_mw": from_api[0]["available_mw"] if from_api else None,
            "direct_notices": len(from_api),
            "vector_asset": from_index[0]["asset"] if from_index else None,
            "vector_available_mw": from_index[0]["available_mw"] if from_index else None,
            "vector_notices": len(from_index),
            "vector_notice_id": from_index[0].get("notice_id") if from_index else None,
            "agree": agree,
            "leaked_with_filter": leaked(from_index, start),
            "leaked_without_filter": leaked(unfiltered, start),
        }
    )

    if not agree:
        print(
            f"  {key}  DISAGREE  direct {records[-1]['direct_asset']} "
            f"{records[-1]['direct_available_mw']}  vector "
            f"{records[-1]['vector_asset']} {records[-1]['vector_available_mw']}"
        )

print(f"\n{len(records)} episodes evaluated, {len(fetch_failures)} days unavailable")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The headline

# COMMAND ----------

frame = pd.DataFrame(records)

if frame.empty:
    dbutils.notebook.exit("nothing evaluated, every day failed to fetch")

agreed = int(frame["agree"].sum())
with_filter = int(frame["leaked_with_filter"].sum())
without_filter = int(frame["leaked_without_filter"].sum())
episodes_exposed = int((frame["leaked_without_filter"] > 0).sum())

print(f"agreement on the tightest asset: {agreed}/{len(frame)}")
print(f"future notices retrieved WITH the filter:    {with_filter}")
print(f"future notices retrieved WITHOUT the filter: {without_filter}")
print(f"  across {episodes_exposed} of {len(frame)} episodes")

if with_filter:
    print("\nFAIL: the publication filter let a future notice through.")
elif not without_filter:
    print(
        "\nInconclusive: no notice in this window was published after an episode, "
        "so the filter was never given anything to exclude."
    )
else:
    print("\nPASS: every future notice was excluded, and there were some to exclude.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Keep it
# MAGIC
# MAGIC In a table rather than in this notebook's output, because a number quoted
# MAGIC in a presentation should be one somebody else can re-run the query for.

# COMMAND ----------

(
    spark.createDataFrame(frame)
    .write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(RESULTS)
)

spark.sql(
    f"COMMENT ON TABLE {RESULTS} IS "
    "'Vector retrieval against direct retrieval, one row per explained episode. "
    "agree compares the tightest asset and its available capacity. "
    "leaked_with_filter must be zero: it counts notices published at or after "
    "the episode began that the point in time filter let through. "
    "leaked_without_filter is the same count with the filter removed, and is the "
    "control that makes the zero meaningful. Written by "
    "pipelines/99_evaluate_retrieval, run by hand.'"
)

print(f"{RESULTS}: {len(frame)} row(s)")

# COMMAND ----------

dbutils.notebook.exit(
    f"agreement {agreed}/{len(frame)} | leaked with filter {with_filter} | "
    f"leaked without filter {without_filter}"
)