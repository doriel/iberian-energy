# Databricks notebook source
# MAGIC %md
# MAGIC # Turn on the Lakebase change data feed
# MAGIC
# MAGIC Run once, by hand. Points the change data feed of the `iberian` Postgres
# MAGIC schema at Unity Catalog, so every insert, update and delete the
# MAGIC application and the agent make in Lakebase lands in Delta as
# MAGIC `lb_<table>_history`.
# MAGIC
# MAGIC This is the reverse half of the architecture. Gold already flows forward
# MAGIC into Lakebase for the app to read; this is what carries the app's own
# MAGIC writes back, and it is what makes the usage analytics and the GenAI
# MAGIC evaluation dataset come from the product rather than from a script.
# MAGIC
# MAGIC ## Checked against the documentation on 27 September 2026
# MAGIC
# MAGIC Databricks product surfaces in this area move quickly, so what follows was
# MAGIC read rather than remembered, and the date is here so a reader knows how
# MAGIC stale it might be.
# MAGIC
# MAGIC - The feature is **Public Preview**, not generally available.
# MAGIC - It works at the **schema** level, not the table level. One configuration
# MAGIC   covers every table in the Postgres schema, including tables added later.
# MAGIC - A configuration is **immutable once created**. To change the
# MAGIC   destination you delete it and make another.
# MAGIC - The SDK call is `w.postgres.create_cdf_config`, and its `parent` is
# MAGIC   `projects/{project}/branches/{branch}/databases/{database}`, one segment
# MAGIC   deeper than the parent the role calls take.
# MAGIC - Changes are flushed roughly every 15 seconds, read off the Postgres
# MAGIC   write-ahead log.
# MAGIC
# MAGIC ## Two things that stop this working, and both are checked below
# MAGIC
# MAGIC **The destination catalog needs an external storage location.** A catalog
# MAGIC on the metastore's default storage is documented as unsupported. This is
# MAGIC the one that can end the exercise, so it is checked first and the notebook
# MAGIC says so plainly rather than failing later inside an operation.
# MAGIC
# MAGIC **An empty table is skipped** until it holds at least one row. A table
# MAGIC with no rows comes back as `CDF_STATE_SKIPPED`, which reads like a fault
# MAGIC and is not one. Put a row in it and it starts on its own.
# MAGIC
# MAGIC `REPLICA IDENTITY FULL` is the third requirement and it is already in
# MAGIC `sql/001_application_tables.sql`, applied when the schema was created.
# MAGIC It is verified here anyway, because a table added since then would not
# MAGIC have it and the feed would carry updates with no before-image.

# COMMAND ----------

# MAGIC %pip install databricks-sdk --upgrade "psycopg[binary]"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.text("project", "", "Lakebase project")
dbutils.widgets.text("branch", "production", "Branch")
dbutils.widgets.text("database", "databricks_postgres", "Postgres database")
dbutils.widgets.text("postgres_schema", "iberian", "Postgres schema to capture")
dbutils.widgets.text("catalog", "bootcamp_students", "Destination catalog")
dbutils.widgets.text("schema", "doriel", "Destination schema")
dbutils.widgets.text("endpoint_id", "primary", "Endpoint")
dbutils.widgets.text("host", "", "Endpoint host (blank: LAKEBASE_HOST)")
dbutils.widgets.dropdown("apply", "no", ["no", "yes"], "Create the configuration")

import os as _os

PROJECT = dbutils.widgets.get("project").strip()
ENDPOINT_ID = dbutils.widgets.get("endpoint_id").strip()
# Not defaulted to the real host. This repository is public, and a hostname is
# not a secret but it is infrastructure nobody outside needs to be handed.
HOST = dbutils.widgets.get("host").strip() or _os.environ.get("LAKEBASE_HOST", "")
BRANCH = dbutils.widgets.get("branch").strip()
DATABASE = dbutils.widgets.get("database").strip()
POSTGRES_SCHEMA = dbutils.widgets.get("postgres_schema").strip()
CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
APPLY = dbutils.widgets.get("apply") == "yes"

if not PROJECT:
    raise ValueError(
        "Set the project widget. The cell below lists the projects this "
        "identity can see, so run it first with the widget blank if you do "
        "not know the name."
    )

BRANCH_PARENT = f"projects/{PROJECT}/branches/{BRANCH}"
DATABASE_PARENT = f"{BRANCH_PARENT}/databases/{DATABASE}"

print(f"source      {DATABASE_PARENT}, schema {POSTGRES_SCHEMA}")
print(f"destination {CATALOG}.{SCHEMA}")
print(f"apply       {APPLY}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What this identity can see
# MAGIC
# MAGIC Printed before anything is created, because the `parent` for a change
# MAGIC data feed is a four segment resource name and a typo in any segment fails
# MAGIC with a not-found that says nothing about which segment was wrong.

# COMMAND ----------

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.postgres import CdfConfig  # noqa: E402

w = WorkspaceClient()

print("projects:")
for project in w.postgres.list_projects():
    print(f"  {project.name}")

print(f"\nbranches under projects/{PROJECT}:")
for branch in w.postgres.list_branches(parent=f"projects/{PROJECT}"):
    print(f"  {branch.name}")

print(f"\ndatabases under {BRANCH_PARENT}:")
for database in w.postgres.list_databases(parent=BRANCH_PARENT):
    print(f"  {database.name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check one: the destination catalog's storage
# MAGIC
# MAGIC The documentation is explicit that the destination must be a catalog with
# MAGIC an external storage location rather than the metastore default. If this
# MAGIC check says otherwise, stop here: the fix is a catalog, not a parameter,
# MAGIC and on a shared boot camp metastore it may not be yours to make.

# COMMAND ----------

catalog_info = w.catalogs.get(CATALOG)

print(f"catalog        {catalog_info.name}")
print(f"type           {catalog_info.catalog_type}")
print(f"storage root   {catalog_info.storage_root or '(metastore default)'}")
print(f"storage loc    {catalog_info.storage_location or '(none)'}")

if catalog_info.storage_root:
    print(
        "\n  The catalog has a storage root of its own rather than the "
        "metastore default, which is what the documentation asks for. Measured "
        "on 27 September 2026 for bootcamp_students: a MANAGED_CATALOG with an "
        "S3 root. That is the check that could have ended this, and it passed."
    )
else:
    print(
        "\n  No explicit storage root. The documentation lists an externally "
        "located catalog as a requirement, so the create below may be refused.\n"
        "  Worth trying anyway: a refusal costs a minute and tells us more than "
        "a guess does. If it is refused, the honest answer is that this "
        "workspace's catalog does not support the preview, which is a finding "
        "for the documentation rather than a defect to work around."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check two: replica identity, and which tables have rows
# MAGIC
# MAGIC Read from Postgres itself rather than from the DDL file, because what
# MAGIC matters is the state of the database and not the state of the repository.
# MAGIC A table created by hand since the schema was applied is exactly the case
# MAGIC this catches.

# COMMAND ----------

import os  # noqa: E402
import sys  # noqa: E402

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

from iberian.app.lakebase import Lakebase, databricks_credentials  # noqa: E402

if not HOST:
    raise ValueError(
        "No endpoint host. Fill the host widget, or set LAKEBASE_HOST. Use the "
        "endpoint's own host and not the -pooler one: the pooler refuses the "
        "generated credential with 'SASL authentication failed', cached or "
        "fresh, which cost an afternoon to find the first time."
    )

ENDPOINT = f"{BRANCH_PARENT}/endpoints/{ENDPOINT_ID}"

store = Lakebase(
    host=HOST,
    user=spark.sql("SELECT current_user()").first()[0],
    credential_factory=databricks_credentials(ENDPOINT),
)

#: `relreplident` is a single character: `d` is the default, the primary key
#: only, and `f` is FULL. Anything but `f` means an update arrives in the feed
#: without its before-image, so `update_preimage` rows would be empty and any
#: analysis of what changed would be reading nulls.
REPLICA_IDENTITY = """
SELECT c.relname AS table_name,
       c.relreplident AS replica_identity,
       c.reltuples::bigint AS estimated_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind = 'r'
ORDER BY c.relname
"""

rows = store.query(REPLICA_IDENTITY, (POSTGRES_SCHEMA,))

print(f"{'table':<28} {'replica identity':<18} {'rows (estimated)':>16}")
print("-" * 64)
wrong = []
for row in rows:
    identity = {"d": "default", "f": "FULL", "n": "nothing", "i": "index"}.get(
        row["replica_identity"], row["replica_identity"]
    )
    flag = "" if row["replica_identity"] == "f" else "   <-- not FULL"
    if row["replica_identity"] != "f":
        wrong.append(row["table_name"])
    print(f"{row['table_name']:<28} {identity:<18} {row['estimated_rows']:>16,}{flag}")

if wrong:
    print("\n  Fix these before creating the configuration:")
    for name in wrong:
        print(f"    ALTER TABLE {POSTGRES_SCHEMA}.{name} REPLICA IDENTITY FULL;")

# COMMAND ----------

# MAGIC %md
# MAGIC ### The real row counts
# MAGIC
# MAGIC `reltuples` above is the planner's estimate and is -1 on a table that has
# MAGIC never been analysed, which is most of them here. An empty table is
# MAGIC skipped by the feed rather than failed, so it is worth knowing which are
# MAGIC empty before reading the states below and worrying about them.

# COMMAND ----------

for row in rows:
    name = row["table_name"]
    found = store.query(f'SELECT count(*) AS n FROM {POSTGRES_SCHEMA}."{name}"')
    count = found[0]["n"]
    note = "  will be skipped until it has a row" if count == 0 else ""
    print(f"  {name:<28} {count:>8,}{note}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What is configured already
# MAGIC
# MAGIC A configuration is immutable and there is one per Postgres schema per
# MAGIC database, so creating a second one for the same schema is not how you
# MAGIC change the destination. Listing first means the re-run of this notebook
# MAGIC reports rather than fails.

# COMMAND ----------

existing = list(w.postgres.list_cdf_configs(parent=DATABASE_PARENT))

if existing:
    for config in existing:
        print(f"  {config.name}")
        print(f"      postgres schema {config.postgres_schema}")
        print(f"      destination     {config.catalog}.{config.schema}")
        print(f"      created         {config.create_time}")
else:
    print("  none")

already = next(
    (c for c in existing if c.postgres_schema == POSTGRES_SCHEMA), None
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create it
# MAGIC
# MAGIC Guarded by the `apply` widget so the checks above can be read on their
# MAGIC own. Set it to `yes` and run this cell once everything above looks right.

# COMMAND ----------

if already is not None:
    print(f"Already configured: {already.name}")
    print(f"  -> {already.catalog}.{already.schema}")
    print("  Immutable. To point it somewhere else, delete it first with")
    print(f"  w.postgres.delete_cdf_config(name={already.name!r})")
elif not APPLY:
    print("apply is 'no'. Nothing created.")
    print("Read the checks above, then set the widget to 'yes' and run this cell.")
else:
    operation = w.postgres.create_cdf_config(
        parent=DATABASE_PARENT,
        cdf_config=CdfConfig(
            catalog=CATALOG,
            schema=SCHEMA,
            postgres_schema=POSTGRES_SCHEMA,
        ),
        # Defaults to the Postgres schema name when omitted. Stated anyway, so
        # the resource name in the console is one somebody can recognise.
        cdf_config_id=POSTGRES_SCHEMA,
    )
    print(f"created: {operation}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Where it got to
# MAGIC
# MAGIC The initial snapshot starts on its own. `SNAPSHOTTING` means it is
# MAGIC copying what is already there, `STREAMING` means it is following the
# MAGIC write-ahead log, and `SKIPPED` means the table was empty when it looked.
# MAGIC
# MAGIC Re-run this cell rather than waiting inside it: a table of a few hundred
# MAGIC rows takes seconds, and a notebook that blocks on a remote state machine
# MAGIC is a notebook that hangs when the state machine does.

# COMMAND ----------

import time  # noqa: E402

time.sleep(5)

statuses = list(w.postgres.list_cdf_statuses(parent=DATABASE_PARENT))

if not statuses:
    print("No statuses yet. Give it a few seconds and run this cell again.")
else:
    print(f"{'postgres table':<34} {'state':<22} {'unity catalog table'}")
    print("-" * 100)
    for status in sorted(statuses, key=lambda s: s.postgres_table or ""):
        state = (status.state.name if status.state else "unknown").replace(
            "CDF_STATE_", ""
        )
        print(f"{status.postgres_table or '':<34} {state:<22} {status.uc_table or ''}")
        if status.status_detail:
            print(f"    {status.status_detail}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prove it end to end
# MAGIC
# MAGIC A configuration that exists is not evidence. A row written in Postgres
# MAGIC and read back out of Delta is. This writes one row, waits, and reads it
# MAGIC from the other side.

# COMMAND ----------

PROBE_SESSION = "cdf-probe"

store.execute(
    """
    INSERT INTO iberian.agent_actions
        (session_id, created_by, tool, arguments, status, target_table, latency_ms)
    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
    """,
    (PROBE_SESSION, "00d_enable_lakebase_cdf", "cdf_probe",
     '{"note": "written to prove the change data feed reaches Delta"}',
     "ok", "agent_actions", 0),
)
print("one row written to iberian.agent_actions")

# COMMAND ----------

HISTORY = f"{CATALOG}.{SCHEMA}.lb_agent_actions_history"

for attempt in range(1, 13):
    time.sleep(10)
    try:
        found = spark.sql(
            f"SELECT count(*) AS n FROM {HISTORY} WHERE session_id = '{PROBE_SESSION}'"
        ).collect()[0]["n"]
    except Exception as exc:
        print(f"  {attempt * 10:>3}s  {HISTORY} not there yet ({type(exc).__name__})")
        continue
    print(f"  {attempt * 10:>3}s  {found} matching row(s) in {HISTORY}")
    if found:
        break
else:
    print(
        "\n  Two minutes and nothing arrived. The feed flushes about every 15 "
        "seconds, so this is worth looking at rather than waiting out. Check "
        "the state above and `SELECT * FROM wal2delta.tables` in the Lakebase "
        "SQL editor."
    )

# COMMAND ----------

try:
    spark.sql(
        f"""
        SELECT _pg_change_type, _timestamp, session_id, tool, status
        FROM {HISTORY}
        WHERE session_id = '{PROBE_SESSION}'
        ORDER BY _sort_by
        """
    ).show(truncate=False)
except Exception as exc:
    print(f"{HISTORY} is not readable yet: {exc}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next
# MAGIC
# MAGIC `01g_build_application_activity` reads these history tables and builds
# MAGIC `gold_application_activity`. It is incremental on purpose: the change
# MAGIC feed is credited for being a feed, and a nightly full rebuild off it
# MAGIC would be using it as a snapshot.
# MAGIC
# MAGIC The probe row above stays. It is one row in an audit log, it is labelled
# MAGIC as a probe, and deleting it would remove the evidence that this worked.

# COMMAND ----------

message = f"{len(statuses)} table(s) in the feed" if statuses else "no statuses yet"
dbutils.notebook.exit(message)