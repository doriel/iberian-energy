# Databricks notebook source
# MAGIC %md
# MAGIC # Give the application its own identity in Lakebase
# MAGIC
# MAGIC Run by hand, from the Git folder, after `00b_apply_lakebase_schema`.
# MAGIC
# MAGIC The deployed application runs on Render, outside the workspace, and the
# MAGIC only documented way for something out there to reach Lakebase is a service
# MAGIC principal generating a database credential through the SDK. Being allowed
# MAGIC to authenticate is not the same as being allowed to read a table, and this
# MAGIC notebook is both halves: it creates the Postgres role for that service
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
# MAGIC That command is why this project needs nothing from an administrator.
# MAGIC Worth knowing if you are reading this wondering why the principal has a
# MAGIC name that has nothing to do with electricity.
# MAGIC
# MAGIC ## Why the role is created through the SDK and not in SQL
# MAGIC
# MAGIC Because `databricks_create_role` does not exist in this database. That
# MAGIC function belongs to the older Lakebase generation, the one built on
# MAGIC database instances. This project is the newer kind, with projects,
# MAGIC branches and endpoints, where roles are resources managed through
# MAGIC `w.postgres.create_role` and are not SQL at all. Written down because the
# MAGIC documentation for the older generation is easy to find, reads as though it
# MAGIC applies, and fails with `UNRESOLVED_ROUTINE` in the Databricks SQL editor
# MAGIC and `function does not exist` in Postgres, neither of which points at the
# MAGIC real problem.
# MAGIC
# MAGIC The grants stay in SQL, because those are ordinary Postgres.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("principal", "", "Service principal application id")
dbutils.widgets.text("role_id", "mibel-workbench", "Role id")
dbutils.widgets.text("project", "", "Lakebase project")
dbutils.widgets.text("branch", "production", "Branch")
dbutils.widgets.text("endpoint_id", "primary", "Endpoint")
dbutils.widgets.text("sql_file", "sql/002_service_principal_grants.sql", "SQL file")

PRINCIPAL = dbutils.widgets.get("principal").strip()
ROLE_ID = dbutils.widgets.get("role_id").strip()
PROJECT = dbutils.widgets.get("project").strip()
BRANCH = dbutils.widgets.get("branch").strip()
ENDPOINT_ID = dbutils.widgets.get("endpoint_id").strip()
SQL_FILE = dbutils.widgets.get("sql_file").strip()

if not PROJECT:
    raise RuntimeError("Set the project widget to your Lakebase project name.")

PARENT = f"projects/{PROJECT}/branches/{BRANCH}"
ENDPOINT = f"{PARENT}/endpoints/{ENDPOINT_ID}"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check both identifiers before they reach anything
# MAGIC
# MAGIC The application id ends up in a GRANT, and Postgres cannot parameterise
# MAGIC an identifier, so it is substituted as text and therefore checked first.
# MAGIC The role id has its own rule from the API: 4 to 63 characters, lowercase
# MAGIC letters, digits and hyphens. Both checks are here rather than discovered
# MAGIC halfway through, when a role exists and the grants do not.

# COMMAND ----------

import re

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ROLE_ID_RULE = re.compile(r"^[a-z0-9][a-z0-9-]{2,61}[a-z0-9]$")

if not UUID.match(PRINCIPAL):
    raise RuntimeError(
        f"{PRINCIPAL!r} is not a service principal application id. It should be "
        "a lowercase UUID, the second column of `databricks service-principals "
        "list`, not the long numeric id and not the display name."
    )
if not ROLE_ID_RULE.match(ROLE_ID):
    raise RuntimeError(
        f"{ROLE_ID!r} is not a usable role id. The API wants 4 to 63 characters, "
        "lowercase letters, digits and hyphens, starting and ending with a "
        "letter or digit."
    )

print(f"principal: {PRINCIPAL}")
print(f"role id:   {ROLE_ID}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The role
# MAGIC
# MAGIC Three deliberate choices in the spec.
# MAGIC
# MAGIC **`membership_roles` is empty.** A role's memberships are replaced rather
# MAGIC than merged, and `DATABRICKS_SUPERUSER` is a membership. The application
# MAGIC creates alerts and records judgements; it has no business being superuser,
# MAGIC and handing that to a process reachable from the public internet would be
# MAGIC the worst decision in this repository.
# MAGIC
# MAGIC **No attributes.** No `createdb`, no `createrole`, no `bypassrls`. Row
# MAGIC level security is not in use today, and a role that can bypass it is a
# MAGIC role somebody has to remember to revisit if it ever is.
# MAGIC
# MAGIC **`replace_existing` is never set.** With it, this spec would overwrite
# MAGIC whatever the role already has, and an existing role's superuser membership
# MAGIC would be silently cleared by the empty list above. So the role is created
# MAGIC only when it is absent, and an existing one is left exactly as it is.

# COMMAND ----------

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service import postgres  # noqa: E402

w = WorkspaceClient()

existing = {role.role_id: role for role in w.postgres.list_roles(parent=PARENT)}
print(f"{len(existing)} role(s) in {PARENT}:")
for role_id, role in sorted(existing.items()):
    status = role.status
    print(f"  {role_id:<24} {status.postgres_role:<44} {status.identity_type}")

# COMMAND ----------

if ROLE_ID in existing:
    role = existing[ROLE_ID]
    print(f"role already exists, left alone: {ROLE_ID}")
else:
    operation = w.postgres.create_role(
        parent=PARENT,
        role_id=ROLE_ID,
        role=postgres.Role(
            spec=postgres.RoleRoleSpec(
                identity_type=postgres.RoleIdentityType.SERVICE_PRINCIPAL,
                auth_method=postgres.RoleAuthMethod.LAKEBASE_OAUTH_V1,
                postgres_role=PRINCIPAL,
                membership_roles=[],
                attributes=postgres.RoleAttributes(
                    bypassrls=False, createdb=False, createrole=False
                ),
            )
        ),
    )
    role = operation.wait()
    print(f"role created: {ROLE_ID}")

# The name the application connects as, which is not necessarily the role id.
# For a user role these differ: the id is the local part, the postgres role is
# the whole email. Read it back rather than assumed, because this string is
# both the GRANT target below and LAKEBASE_USER in the deployment.
PG_ROLE = role.status.postgres_role
print(f"\npostgres role: {PG_ROLE}")
print(f"identity:      {role.status.identity_type}")
print(f"auth method:   {role.status.auth_method}")
print(f"memberships:   {role.status.membership_roles or 'none'}")

if role.status.membership_roles:
    print("\n  NOTE: this role has memberships. Check they are intended.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The grants
# MAGIC
# MAGIC Applied as you, not as the principal: this is the notebook that gives the
# MAGIC principal its access, so it cannot be run by it.
# MAGIC
# MAGIC The endpoint's own host rather than the pooled one. The pooler refuses a
# MAGIC generated credential with `SASL authentication failed`, and this is not
# MAGIC the place to find that out a second time.

# COMMAND ----------

import os  # noqa: E402
import sys  # noqa: E402

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

if '"' in PG_ROLE:
    raise RuntimeError(f"{PG_ROLE!r} contains a quote and cannot be a safe identifier.")

statements = open(path).read().replace(':"principal"', f'"{PG_ROLE}"')
if ':"principal"' in statements:
    raise RuntimeError("The placeholder was not fully substituted.")

print(f"{path}: {len(statements.splitlines())} lines, granting to {PG_ROLE}")

# COMMAND ----------

import psycopg  # noqa: E402


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
        wanted = resource.rsplit("/", 1)[-1]
        found = next(
            item
            for item in w.postgres.list_endpoints(parent=PARENT)
            if item.endpoint_id == wanted
        )
    return found.status.hosts.host


user = w.current_user.me().user_name
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
# MAGIC In one transaction. A half applied set of grants is an application that
# MAGIC can read but not write, which fails far from here and looks like a bug in
# MAGIC the code rather than a missing privilege.

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
# MAGIC table, because the failure worth catching is one table missing one
# MAGIC privilege. In the application that shows up as a single broken button and
# MAGIC nowhere else.

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
        (PG_ROLE,),
    )
    rows = cursor.fetchall()

print(f"{'table':<20} {'select':>7} {'insert':>7} {'update':>7} {'delete':>7}")
incomplete = []
for name, read, insert, update, delete in rows:
    print(f"{name:<20} {str(bool(read)):>7} {str(bool(insert)):>7} "
          f"{str(bool(update)):>7} {str(bool(delete)):>7}")
    if not all([read, insert, update, delete]):
        incomplete.append(name)

connection.close()

if incomplete:
    print(f"\n  WARNING: incomplete grants on {', '.join(incomplete)}")
else:
    print(f"\n  {PG_ROLE} can read and write every table in iberian.")

print("\nFor the deployment:")
print(f"  LAKEBASE_USER={PG_ROLE}")
print(f"  DATABRICKS_CLIENT_ID={PRINCIPAL}")
print("  DATABRICKS_CLIENT_SECRET=<the secret from service-principal-secrets-proxy>")