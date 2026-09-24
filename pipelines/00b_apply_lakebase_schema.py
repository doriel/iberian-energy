# Databricks notebook source
# MAGIC %md
# MAGIC # Apply the Lakebase application schema
# MAGIC
# MAGIC Run by hand, from the Git folder. Reads `sql/001_application_tables.sql`
# MAGIC and applies it, so the schema has one definition rather than one in a file
# MAGIC and another in whatever was typed into a notebook that afternoon.
# MAGIC
# MAGIC Safe to run again. Every statement in the file is idempotent, which is
# MAGIC what lets the file be the schema rather than a migration.
# MAGIC
# MAGIC ## Why the credential is generated here and not stored
# MAGIC
# MAGIC Lakebase credentials last sixty minutes. A stored one is a support ticket
# MAGIC waiting to happen, so every connection asks for a fresh one. The same rule
# MAGIC applies in the application, where it matters more.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("project", "doriel-capstone-lakebase", "Lakebase project")
dbutils.widgets.text("branch", "production", "Branch")
dbutils.widgets.text("endpoint_id", "primary", "Endpoint")
dbutils.widgets.text("sql_file", "sql/001_application_tables.sql", "SQL file")

PROJECT = dbutils.widgets.get("project")
BRANCH = dbutils.widgets.get("branch")
ENDPOINT_ID = dbutils.widgets.get("endpoint_id")
SQL_FILE = dbutils.widgets.get("sql_file")

ENDPOINT = f"projects/{PROJECT}/branches/{BRANCH}/endpoints/{ENDPOINT_ID}"

# COMMAND ----------

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.getcwd(), ".."))
if not os.path.isdir(os.path.join(REPO_ROOT, "sql")):
    notebook_path = (
        dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        .notebookPath().get()
    )
    REPO_ROOT = os.path.abspath(
        os.path.join("/Workspace", os.path.dirname(notebook_path).lstrip("/"), "..")
    )

path = os.path.join(REPO_ROOT, SQL_FILE)
if not os.path.isfile(path):
    raise RuntimeError(f"{path} not found. This notebook runs from the Git folder.")

statements = open(path).read()
print(f"{path}: {len(statements.splitlines())} lines")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Connect
# MAGIC
# MAGIC The endpoint's own host rather than the pooled one. A pooler is right for
# MAGIC an application holding many short connections and wrong for DDL, which
# MAGIC wants one session that owns what it creates.

# COMMAND ----------

import psycopg  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402

w = WorkspaceClient()
user = w.current_user.me().user_name

def endpoint_host(resource: str) -> str:
    """The read-write host, asked for two ways.

    `get_endpoint` is the direct route; listing the branch is the one that
    works if its signature differs from what this was written against. The
    pooled host beside it is for the application, not for DDL.
    """
    try:
        found = w.postgres.get_endpoint(name=resource)
    except Exception as exc:
        print(f"  get_endpoint: {type(exc).__name__}, listing the branch instead")
        parent = resource.rsplit("/endpoints/", 1)[0]
        wanted = resource.rsplit("/", 1)[-1]
        found = next(
            item
            for item in w.postgres.list_endpoints(parent=parent)
            if item.endpoint_id == wanted
        )
    return found.status.hosts.host


host = endpoint_host(ENDPOINT)
credential = w.postgres.generate_database_credential(endpoint=ENDPOINT)

print(f"user:  {user}")
print(f"host:  {host}")
print(f"token: {len(credential.token)} characters")

connection = psycopg.connect(
    host=host,
    port=5432,
    dbname="databricks_postgres",
    user=user,
    password=credential.token,
    sslmode="require",
    connect_timeout=15,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Apply, in one transaction
# MAGIC
# MAGIC Not autocommit. A file that fails halfway should leave nothing behind,
# MAGIC because a half applied schema is harder to reason about than no schema.

# COMMAND ----------

try:
    with connection.cursor() as cursor:
        cursor.execute(statements)
    connection.commit()
    print("applied")
except Exception:
    connection.rollback()
    print("rolled back, nothing changed")
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## What exists now
# MAGIC
# MAGIC Read back from the catalogue rather than trusted from the file. The
# MAGIC replica identity in particular: `f` is full, `d` is the default, and the
# MAGIC default carries only the primary key on an update, which would leave the
# MAGIC analytics pipeline blind to what changed.

# COMMAND ----------

with connection.cursor() as cursor:
    cursor.execute(
        """
        SELECT c.relname,
               c.relreplident,
               (SELECT count(*) FROM pg_index i WHERE i.indrelid = c.oid) AS indexes,
               (SELECT count(*) FROM pg_constraint k WHERE k.conrelid = c.oid) AS constraints
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'iberian' AND c.relkind = 'r'
        ORDER BY c.relname
        """
    )
    rows = cursor.fetchall()

print(f"{'table':<20} {'replica':<9} {'indexes':>8} {'constraints':>12}")
for name, identity, indexes, constraints in rows:
    flag = "FULL" if identity == "f" else f"NOT FULL ({identity})"
    print(f"{name:<20} {flag:<9} {indexes:>8} {constraints:>12}")

not_full = [name for name, identity, _, _ in rows if identity != "f" and name != "episodes"]
if not_full:
    print(f"\n  WARNING: no change feed from {', '.join(not_full)}")
else:
    print("\n  Every table the application writes to can feed the change feed.")

connection.close()