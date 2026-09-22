# Databricks notebook source
# MAGIC %md
# MAGIC # Explain the new episodes
# MAGIC
# MAGIC Third task of the daily Job, between `transform` and `publish`.
# MAGIC
# MAGIC The north star metric is the share of significant price anomalies that
# MAGIC receive a correct, grounded explanation **within fifteen minutes of market
# MAGIC data publication**. Generating the explanations by hand meets that clause
# MAGIC only when a person happens to be at a laptop, which makes the person the
# MAGIC system. This task is what makes the claim true.
# MAGIC
# MAGIC It explains only episodes with no explanation on file, which is two or
# MAGIC three a day. An episode's explanation does not change once its evidence is
# MAGIC published, so re-explaining the other hundred and twenty-three would spend
# MAGIC model calls to reproduce answers that already exist.
# MAGIC
# MAGIC ## Why the Volume rather than the repository
# MAGIC
# MAGIC Within one Job run the Git checkout is fixed at the commit the run started
# MAGIC from. A file this task committed would not be visible to `publish` in the
# MAGIC same run, because `publish` reads the same frozen checkout. The Volume is
# MAGIC where the tasks of this Job already hand things to each other.

# COMMAND ----------

# MAGIC %pip install requests pandas databricks-ai-search
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os

dbutils.widgets.text("catalog", "bootcamp_students", "Catalog")
dbutils.widgets.text("schema", "doriel", "Schema")
dbutils.widgets.text("volume", "raw", "Volume")
dbutils.widgets.text("secret_scope", "iberian", "Secret scope")
dbutils.widgets.text("endpoint", "databricks-claude-haiku-4-5", "Serving endpoint")
dbutils.widgets.text("max_new", "25", "Most episodes to explain in one run")
dbutils.widgets.dropdown("rebuild", "no", ["no", "yes"], "Re-explain everything")
dbutils.widgets.dropdown(
    "notice_retrieval", "direct", ["direct", "vector"], "Where the A78 notices come from"
)
dbutils.widgets.text("vector_endpoint", "zachy_vs", "Vector Search endpoint")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
VOLUME = dbutils.widgets.get("volume")
SCOPE = dbutils.widgets.get("secret_scope")
ENDPOINT = dbutils.widgets.get("endpoint")
MAX_NEW = int(dbutils.widgets.get("max_new"))
REBUILD = dbutils.widgets.get("rebuild") == "yes"
RETRIEVAL = dbutils.widgets.get("notice_retrieval")
VECTOR_ENDPOINT = dbutils.widgets.get("vector_endpoint")

#: Where the explanations live between tasks, and between days.
EXPLANATIONS = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}/agent/explanations.jsonl"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Secrets
# MAGIC
# MAGIC The ENTSO-E token is needed here as well as in `ingest`: the A78 notices
# MAGIC are retrieved per episode rather than landed, because the point in time
# MAGIC filter depends on when the episode started.

# COMMAND ----------

os.environ["ENTSOE_SECURITY_TOKEN"] = dbutils.secrets.get(
    scope=SCOPE, key="entsoe_token"
)
print(f"token length: {len(os.environ['ENTSOE_SECURITY_TOKEN'])}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import the project modules
# MAGIC
# MAGIC Same block as `01_build_medallion`, which is the one known to work here.

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
    raise RuntimeError(
        f"Could not find src/ from {REPO_ROOT}. This notebook expects to live "
        "in pipelines/ inside the repository Git folder."
    )
if SRC not in sys.path:
    sys.path.insert(0, SRC)

print(f"Repo root: {REPO_ROOT}")

import iberian  # noqa: E402

print(f"Package imported from {os.path.dirname(iberian.__file__)}")

from iberian.agent.batch import (  # noqa: E402
    episode_key,
    explain_episodes,
    load_records,
    merge,
    pending,
    sheet_builder,
    write_records,
)
from iberian.agent.explain import databricks_completer  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import as_utc  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_outages_response,
)
from iberian.agent.table import (  # noqa: E402
    column_comments,
    explanation_rows,
    explanations_frame,
    spark_schema,
    summarise_table,
)
from iberian.publish.dashboard import UnityCatalog  # noqa: E402

DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)

#: The agent's output as a table. Written by this task rather than by the
#: declarative pipeline, and that is a deliberate exception to the rule that
#: the pipeline owns every table. The pipeline runs before this task, so a
#: pipeline-owned explanations table would always be a day behind the
#: explanations, which would quietly break the fifteen minute claim. It is
#: also not a transformation of landed data: it is this task's own output.
EXPLANATIONS_TABLE = f"{CATALOG}.{SCHEMA}.gold_episode_explanations"

# COMMAND ----------

# MAGIC %md
# MAGIC ## What still needs explaining

# COMMAND ----------

source = UnityCatalog(spark, CATALOG, SCHEMA)
episodes = as_utc(source.table("gold_split_episodes"), "start_utc", "end_utc")
intervals = as_utc(source.table("gold_interval_premium"), "ts_utc")

if episodes.empty:
    dbutils.notebook.exit("no episodes in gold")

# Worst first. If the cap bites, the episodes that got explained are the ones
# anybody would have looked at first.
episodes = episodes.sort_values("max_abs_spread", ascending=False)

os.makedirs(os.path.dirname(EXPLANATIONS), exist_ok=True)

# What is on file is always read, including on a rebuild. Blanking it here and
# relying on the rebuild to replace everything looks equivalent and is not: the
# cap below can leave a rebuild partway through, and the merge would then write
# only the episodes this run reached and silently delete the rest. `merge` puts
# the fresh records last, so a re-explained episode wins on its key without
# anything else being touched.
already = load_records(EXPLANATIONS)
todo = episodes if REBUILD else pending(episodes, already)

print(f"{len(episodes)} episodes, {len(already)} explained, {len(todo)} pending")

if len(todo) > MAX_NEW:
    # A cap, because the first run after a long gap would otherwise call the
    # model a hundred times inside a task somebody expects to take seconds.
    # The rest are picked up tomorrow, and the count is printed so a backlog is
    # visible rather than silent.
    print(f"capping at {MAX_NEW}; {len(todo) - MAX_NEW} will wait for the next run")
    if REBUILD:
        # A rebuild always starts from the worst episode, so the ones beyond
        # the cap are not picked up by a second rebuild: it would redo the
        # same head. Raise max_new to cover the whole set in one pass.
        print(f"  a capped rebuild does not resume. Set max_new above {len(episodes)}.")
    todo = todo.head(MAX_NEW)

if todo.empty:
    # Not an exit. On a quiet day there is nothing to generate and the table
    # below still has to be written, or a run that adds no episodes would
    # leave the table missing on the first deploy and stale forever after.
    print("nothing new to explain")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Explain them
# MAGIC
# MAGIC Each record is written as it arrives. A batch that fails on episode ten
# MAGIC keeps the first nine, which at one model call each is worth keeping.

# COMMAND ----------

fresh: list[dict] = []
skipped: list[str] = []


def _skip(key, exc):
    # An episode whose evidence could not be fetched gets no record, so it is
    # still pending tomorrow. Treating the failure as "no notices" would
    # publish a false statement that passes verification.
    skipped.append(key)
    print(f"  {key}  SKIPPED, evidence unavailable: {exc}")


if not todo.empty:
    client = EntsoeClient(Settings.from_env().require_entsoe_token())
    complete = databricks_completer(endpoint=ENDPOINT)

    if RETRIEVAL == "vector":
        # Reads the notices from `gold_transmission_notices_index` instead of
        # the transparency platform, and does not call the platform at all.
        #
        # Not the default, and the evaluation is why. The index is only as
        # fresh as its last sync: a notice published between the load task and
        # this one is invisible to it, and that was observed rather than
        # imagined, on an episode whose binding notice was published ninety
        # seconds before the episode began. The north star is an explanation
        # within fifteen minutes of publication, and a retrieval path that can
        # be a day behind does not belong on the critical path of that claim.
        #
        # It stays available because it is what the comparison in
        # `99_evaluate_retrieval` runs against, and because it is the path that
        # survives the platform being down.
        from iberian.agent.retrieval import binding_from_index  # noqa: E402

        try:
            from databricks.ai_search.client import AISearchClient as Client
        except ImportError:
            from databricks.vector_search.client import VectorSearchClient as Client
        try:
            search = Client(disable_notice=True)
        except TypeError:
            search = Client()

        index_name = f"{CATALOG}.{SCHEMA}.gold_transmission_notices_index"
        binding = binding_from_index(
            search.get_index(endpoint_name=VECTOR_ENDPOINT, index_name=index_name)
        )
        print(f"notices from {index_name} on {VECTOR_ENDPOINT}")
    else:
        binding = binding_assets
        print("notices from the ENTSO-E transparency platform")

    build_sheet = sheet_builder(
        client,
        intervals,
        DIRECTION,
        parse_outages_response,
        binding,
        fetch_curves=(RETRIEVAL != "vector"),
    )

    for record in explain_episodes(
        todo,
        build_sheet,
        complete,
        model=ENDPOINT,
        on_each=lambda key, result: print(
            f"  {key}  {'grounded' if result.ok else 'REJECTED'}  "
            f"attempt {result.attempts}"
        ),
        on_skip=_skip,
    ):
        fresh.append(record)
        write_records(EXPLANATIONS, merge(already, fresh))

# COMMAND ----------

grounded = sum(1 for record in fresh if record["grounded"])
total = len(load_records(EXPLANATIONS))

print(f"\n{grounded} of {len(fresh)} new explanations grounded")
print(f"{total} explanations on file at {EXPLANATIONS}")

for record in fresh:
    if not record["grounded"]:
        reason = ", ".join(record["unsupported"]) or "no source named"
        print(f"  rejected {record['episode_key']}: {reason}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The explanations as a table
# MAGIC
# MAGIC The JSONL is the record of work: it is what `publish` reads and what the
# MAGIC evaluation merges into. The table is a materialisation of it, rewritten
# MAGIC in full on every run, so the two cannot drift. At a few hundred rows an
# MAGIC overwrite costs nothing, and it means the file is always the side that
# MAGIC is right.
# MAGIC
# MAGIC Without this the agent's output is the only thing in the system that
# MAGIC cannot be joined to `gold_split_episodes`, queried in SQL, or seen in
# MAGIC lineage.

# COMMAND ----------

everything = load_records(EXPLANATIONS)
rows = explanation_rows(everything)

if rows:
    (
        spark.createDataFrame(rows, schema=spark_schema())
        .write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(EXPLANATIONS_TABLE)
    )

    spark.sql(
        f"COMMENT ON TABLE {EXPLANATIONS_TABLE} IS "
        "'Agent explanations, one row per market splitting episode. Written by "
        "the explain task of the iberian-daily Job, not by the declarative "
        "pipeline. text is empty when grounded is false: an explanation that "
        "failed verification is never shown to a reader.'"
    )
    for column, comment in column_comments().items():
        escaped = comment.replace("'", "''")
        spark.sql(
            f"COMMENT ON COLUMN {EXPLANATIONS_TABLE}.{column} IS '{escaped}'"
        )

    print(f"{EXPLANATIONS_TABLE}: {summarise_table(explanations_frame(everything))}")
else:
    print("No explanations on file, table not written.")

# COMMAND ----------

dbutils.notebook.exit(
    f"{grounded}/{len(fresh)} new grounded | {len(skipped)} skipped, still pending "
    f"| {total} on file"
)