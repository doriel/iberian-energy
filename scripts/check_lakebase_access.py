# Databricks notebook source
# MAGIC %md
# MAGIC # Can this project use Lakebase, and how much of it?
# MAGIC
# MAGIC Run by hand, in a Databricks notebook. Answers four questions in order,
# MAGIC and each one gates the next:
# MAGIC
# MAGIC 1. Is there a database instance this account can see?
# MAGIC 2. Does a credential come back, and does Postgres accept it?
# MAGIC 3. Which schema can this account write to?
# MAGIC 4. Can it create a table, write to it, and set `REPLICA IDENTITY FULL`?
# MAGIC
# MAGIC The fourth matters as much as the first. Change Data Feed out of Lakebase
# MAGIC needs `REPLICA IDENTITY FULL`, and that requires owning the table. An
# MAGIC account that can create a table but not alter its replica identity can
# MAGIC hold application data and cannot feed the analytics pipeline, which is a
# MAGIC different and much worse situation than having no access at all.
# MAGIC
# MAGIC Nothing is left behind: the scratch table is dropped at the end.
# MAGIC
# MAGIC ## What this does not answer
# MAGIC
# MAGIC Whether the endpoint is reachable from outside the workspace, which is
# MAGIC what the deployed application needs. The documentation does not say either
# MAGIC way. This notebook runs inside Databricks, where reachability is not in
# MAGIC question. The external test has to run from the host the app deploys to.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("instance", "", "Database instance (blank lists them)")
dbutils.widgets.text("schema", "", "Schema to test writes in (blank tries yours)")

INSTANCE = dbutils.widgets.get("instance").strip()
SCHEMA = dbutils.widgets.get("schema").strip()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Which instances exist

# COMMAND ----------

import uuid

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
me = w.current_user.me().user_name
print(f"signed in as {me}\n")

instances = list(w.database.list_database_instances())
if not instances:
    raise SystemExit(
        "No database instances visible to this account. Either none exists in "
        "the workspace, or this account has not been granted access to one. "
        "That is a question for whoever administers the workspace."
    )

for item in instances:
    print(f"  {item.name:<40} state={getattr(item, 'state', '?')}")

# Every instance is tried rather than just the first. Seeing an instance in
# the list means this account may list it, nothing more: the Postgres role is
# a separate grant, and on a shared workspace most of what is listed belongs to
# somebody else. Trying one and stopping reports "no access" when the answer
# might be "not that one".
if not INSTANCE:
    print("\nno instance given, trying each in turn")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. A credential, and a connection
# MAGIC
# MAGIC The token is never printed. Its length is, which is enough to tell an
# MAGIC empty string from a real credential.

# COMMAND ----------

import psycopg  # noqa: E402


def try_connect(name: str):
    """A connection to one instance, or the reason there is not one."""
    instance = w.database.get_database_instance(name=name)
    credential = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()), instance_names=[name]
    )
    host = instance.read_write_dns
    print(f"\n  {name}")
    print(f"    host  {host}")
    print(f"    token {len(credential.token)} characters")
    try:
        conn = psycopg.connect(
            host=host,
            port=5432,
            dbname="databricks_postgres",
            user=me,
            password=credential.token,
            sslmode="require",
            connect_timeout=15,
        )
        conn.autocommit = True
        print("    CONNECTED")
        return conn
    except Exception as exc:
        # The interesting distinction is between being refused a role and not
        # reaching the host at all. The first is a grant, the second is a
        # network, and they are asked of different people.
        first_line = str(exc).splitlines()[0][:150]
        kind = (
            "no Postgres role for this account"
            if "password authentication failed" in str(exc)
            else "did not connect"
        )
        print(f"    {kind}: {first_line}")
        return None


candidates = [INSTANCE] if INSTANCE else [item.name for item in instances]

connection = None
for name in candidates:
    connection = try_connect(name)
    if connection is not None:
        INSTANCE = name
        break

if connection is None:
    raise SystemExit(
        "\nNone of the instances accepted this account. Every one of them "
        "belongs to somebody else until one is created here: an instance is "
        "listable without being usable. Create one, then run this again with "
        "its name in the widget."
    )

with connection.cursor() as cursor:
    cursor.execute("SELECT version(), current_user, current_database()")
    version, user, database = cursor.fetchone()

print(f"\nusing {INSTANCE}")
print(f"  server:   {version.split(',')[0]}")
print(f"  user:     {user}")
print(f"  database: {database}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Where can this account write
# MAGIC
# MAGIC `has_schema_privilege` is asked rather than inferred from the name, because
# MAGIC a schema being visible says nothing about being able to create in it.

# COMMAND ----------

with connection.cursor() as cursor:
    cursor.execute(
        """
        SELECT nspname,
               has_schema_privilege(current_user, nspname, 'CREATE') AS can_create
        FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
        ORDER BY can_create DESC, nspname
        """
    )
    schemas = cursor.fetchall()

for name, can_create in schemas:
    print(f"  {'CREATE' if can_create else '      '}  {name}")

writable = [name for name, can_create in schemas if can_create]

if not SCHEMA:
    # Prefer one that looks like a personal schema over `public`, because
    # `public` being writable is common and says less about real grants.
    personal = [name for name in writable if name not in {"public"}]
    SCHEMA = (personal or writable or [""])[0]

if not SCHEMA:
    raise SystemExit(
        "This account cannot create in any schema. Application tables need a "
        "schema with CREATE, so this is the thing to ask for."
    )

print(f"\ntesting writes in: {SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Create, write, read, and set the replica identity
# MAGIC
# MAGIC Each step reports separately. A failure at the replica identity step with
# MAGIC everything above it passing is the awkward outcome worth knowing about
# MAGIC early: application tables would work and the change feed would not.

# COMMAND ----------

TABLE = f"{SCHEMA}.capstone_access_check"
results: dict[str, str] = {}


def step(name: str, statement: str, args=None) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, args)
            if cursor.description:
                print(f"  {name}: {cursor.fetchall()}")
        results[name] = "ok"
    except Exception as exc:
        results[name] = f"FAILED {type(exc).__name__}: {str(exc).splitlines()[0][:160]}"


step("drop any leftover", f"DROP TABLE IF EXISTS {TABLE}")
step(
    "create table",
    f"""
    CREATE TABLE {TABLE} (
        id          BIGSERIAL PRIMARY KEY,
        created_by  TEXT        NOT NULL,
        note        TEXT        NOT NULL CHECK (length(trim(note)) > 0),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (created_by, note)
    )
    """,
)
step(
    "insert",
    f"INSERT INTO {TABLE} (created_by, note) VALUES (%s, %s) RETURNING id",
    (me, "access check"),
)
step(
    "upsert on the unique constraint",
    f"""
    INSERT INTO {TABLE} (created_by, note) VALUES (%s, %s)
    ON CONFLICT (created_by, note) DO UPDATE SET created_at = now()
    RETURNING id
    """,
    (me, "access check"),
)
step("select", f"SELECT id, created_by, note FROM {TABLE}")
step("check constraint rejects bad input", f"INSERT INTO {TABLE} (created_by, note) VALUES ('x', '   ')")
step("replica identity full", f"ALTER TABLE {TABLE} REPLICA IDENTITY FULL")
step(
    "confirm replica identity",
    f"SELECT relreplident FROM pg_class WHERE oid = '{TABLE}'::regclass",
)
step("drop", f"DROP TABLE IF EXISTS {TABLE}")

# COMMAND ----------

print(f"instance: {INSTANCE}")
print(f"schema:   {SCHEMA}\n")

for name, outcome in results.items():
    print(f"  {name:<34} {outcome}")

# The check constraint step is supposed to fail. Anything else failing is not.
expected_failure = "check constraint rejects bad input"
broken = [
    name
    for name, outcome in results.items()
    if outcome != "ok" and name != expected_failure
]

print()
if results.get(expected_failure, "").startswith("FAILED"):
    print("  the check constraint rejected whitespace, which is correct")
else:
    print("  WARNING: the check constraint did not reject whitespace")

if broken:
    print(f"\n  {len(broken)} step(s) failed: {', '.join(broken)}")
else:
    print("\n  Everything needed for the application tables works here.")
    print("  Still unanswered: whether the endpoint is reachable from the host")
    print("  the application is deployed to. That test cannot run in a notebook.")

connection.close()