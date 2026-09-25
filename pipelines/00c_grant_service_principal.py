# Databricks notebook source
# MAGIC %md
# MAGIC # Give the application its own identity in Lakebase
# MAGIC
# MAGIC Run by hand, from the Git folder, after `00b_apply_lakebase_schema`.
# MAGIC
# MAGIC The deployed application is on Render, outside the workspace, and the only
# MAGIC documented way for something out there to reach Lakebase is a service
# MAGIC principal generating a database credential through the SDK. Being allowed
# MAGIC to authenticate is not the same as being allowed to read a table, and this
# MAGIC notebook is the second half: it creates the Postgres role for that service
# MAGIC principal and applies `sql/002_service_principal_grants.sql`.
# MAGIC
# MAGIC ## Which service principal, and why that one
# MAGIC
# MAGIC Not one created for this project. `service-principals create` is refused
# MAGIC as admin only in this workspace, so there was no creating one. The one
# MAGIC used here came with a Databricks App built earlier in the boot camp, and
# MAGIC its OAuth secret can be minted at workspace level:
# MAGIC
# MAGIC ```
# MAGIC databricks service-principal-secrets-proxy create <numeric id>
# MAGIC ```
# MAGIC
# MAGIC That command is the reason this project needs nothing from an
# MAGIC administrator. Worth knowing if you are reading this wondering why the
# MAGIC principal has an unrelated name.
# MAGIC
# MAGIC ## Why this is a notebook and not three lines typed into one
# MAGIC
# MAGIC Because the grants are part of how the system is provisioned, and the
# MAGIC ones a reviewer should see are the ones that actually ran. Typed into
# MAGIC whatever notebook was open that evening, they exist only in somebody's
# MAGIC memory of having run them.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("principal", "", "Service principal application id")
dbutils.widgets.text("project", "", "Lakebase project")
dbutils.widgets.text("branch", "production", "Branch")
dbutils.widgets.text("endpoint_id", "primary", "Endpoint")
dbutils.widgets.text("sql_file", "sql/002_service_principal_grants.sql", "SQL file")

PRINCIPAL = dbutils.widgets.get("principal").strip()
PROJECT = dbutils.widgets.get("project").strip()
BRANCH = dbutils.widgets.get("branch").strip()
ENDPOINT_ID = dbutils.widgets.get("endpoint_id").strip()
SQL_FILE = dbutils.widgets.get("sql_file").strip()

if not PROJECT:
    raise RuntimeError("Set the project widget to your Lakebase project name.")

ENDPOINT = f"projects/{PROJECT}/branches/{BRANCH}/endpoints/{ENDPOINT_ID}"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check the principal before it reaches a statement
# MAGIC
# MAGIC Postgres cannot parameterise an identifier, so the application id is
# MAGIC substituted into the SQL as text. Anything substituted into SQL as text
# MAGIC gets checked first, and the check here is strict rather than clever: a
# MAGIC service principal application id is a UUID, so anything that is not one
# MAGIC is a mistake worth stopping for.

# COMMAND ----------

import re

UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

if not UUID.match(PRINCIPAL):
    raise RuntimeError(
        f"{PRINCIPAL!r} is not a service principal application id. It should be "
        "a UUID, the second column of `databricks service-principals list`, not "
        "the long numeric id and not the display name."
    )

print(f"principal: {PRINCIPAL}")

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

statements = open(path).read().replace(':"principal"', f'"{PRINCIPAL}"')
if ':"principal"' in statements:
    raise RuntimeError("The placeholder was not fully substituted.")

print(f"{path}: {len(statements.splitlines())} lines")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Connect
# MAGIC
# MAGIC As you, not as the principal. This notebook grants the principal its
# MAGIC access, so it cannot be run by the principal, and the endpoint's own host
# MAGIC rather than the pooled one: the pooler refuses a generated credential,
# MAGIC and this is not the place to find that out again.

# COMMAND ----------

import psycopg  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402

w = WorkspaceClient()
user = w.current_user.me().user_name


def endpoint_host(resource: str) -> str:
    """The read-write host, asked for two ways.

    Same as `00b`. `get_endpoint` is the direct route; listing the branch is
    the one that works if its signature differs from what this was written
    against.
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
# MAGIC ## The role
# MAGIC
# MAGIC `databricks_create_role` is a Lakebase function and lives inside this
# MAGIC database. Running it in the Databricks SQL editor fails with
# MAGIC `UNRESOLVED_ROUTINE`, which reads like a missing function and is really
# MAGIC the wrong engine: that editor speaks to Unity Catalog, not to Postgres.
# MAGIC
# MAGIC Created here rather than in the SQL file because what it does when the
# MAGIC role already exists is not documented. Catching the error and saying so
# MAGIC is honest; a file that only works the first time is not.

# COMMAND ----------

with connection.cursor() as cursor:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (PRINCIPAL,))
    exists = cursor.fetchone() is not None

if exists:
    print(f"role already exists: {PRINCIPAL}")
else:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT databricks_create_role(%s, %s)", (PRINCIPAL, "SERVICE_PRINCIPAL")
            )
        connection.commit()
        print(f"role created: {PRINCIPAL}")
    except Exception as exc:
        connection.rollback()
        # The second argument is the part I am least sure of. If Lakebase wants
        # a different word for this kind of principal, the message says which.
        raise RuntimeError(
            "databricks_create_role failed. If it is complaining about the "
            "principal type, the accepted values are usually named in the "
            f"error.\n\n{exc}"
        ) from exc

# COMMAND ----------

# MAGIC %md
# MAGIC ## The grants, in one transaction
# MAGIC
# MAGIC Not autocommit. A half applied set of grants is an application that can
# MAGIC read but not write, which fails somewhere far from here and looks like a
# MAGIC bug in the code.

# COMMAND ----------

try:
    with connection.cursor() as cursor:
        cursor.execute(statements)
    connection.commit()
    print("granted")
except Exception:
    connection.rollback()
    print("rolled back, nothing changed")
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## What the principal can actually do
# MAGIC
# MAGIC Read back from the catalogue rather than trusted from the file, and per
# MAGIC table, because the failure this is guarding against is one table missing
# MAGIC one privilege. That shows up in the application as a single broken button
# MAGIC and nowhere else.

# COMMAND ----------

with connection.cursor() as cursor:
    cursor.execute(
        """
        SELECT c.relname,
               bool_or(a.privilege_type = 'SELECT') AS can_read,
               bool_or(a.privilege_type = 'INSERT') AS can_insert,
               bool_or(a.privilege_type = 'UPDATE') AS can_update,
               bool_or(a.privilege_type = 'DELETE') AS can_delete
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN information_schema.table_privileges a
               ON a.table_name = c.relname
              AND a.table_schema = n.nspname
              AND a.grantee = %s
        WHERE n.nspname = 'iberian' AND c.relkind = 'r'
        GROUP BY c.relname
        ORDER BY c.relname
        """,
        (PRINCIPAL,),
    )
    rows = cursor.fetchall()

print(f"{'table':<20} {'select':>7} {'insert':>7} {'update':>7} {'delete':>7}")
incomplete = []
for name, read, insert, update, delete in rows:
    print(f"{name:<20} {str(bool(read)):>7} {str(bool(insert)):>7} "
          f"{str(bool(update)):>7} {str(bool(delete)):>7}")
    if not all([read, insert, update, delete]):
        incomplete.append(name)

if incomplete:
    print(f"\n  WARNING: incomplete grants on {', '.join(incomplete)}")
else:
    print(f"\n  {PRINCIPAL} can read and write every table in iberian.")

connection.close()