-- What the deployed application is allowed to do in the database.
--
-- The application runs on Render, outside the workspace, and Databricks
-- documents exactly one way for something out there to reach Lakebase: a
-- service principal generating a database credential through the SDK. There is
-- no static Postgres password to fall back on.
--
-- Creating a service principal is refused as admin only in this workspace. The
-- one used here was not created for this. It came with a Databricks App built
-- earlier in the boot camp, and a secret for it can be minted at workspace
-- level with `databricks service-principal-secrets-proxy create`. So the
-- application has an identity of its own without anybody's permissions being
-- changed, which is the whole reason this file exists rather than a request to
-- an administrator.
--
-- Applied by `pipelines/00c_grant_service_principal.py`. The role itself is
-- created there, in Python, because `databricks_create_role` is a Lakebase
-- function whose behaviour when the role already exists is not documented, and
-- an error caught with a readable message beats a file that only works once.
--
-- `:"principal"` is the service principal's application id. Postgres cannot
-- parameterise an identifier, so the notebook substitutes it textually after
-- checking it is a UUID. Everything below is idempotent: a GRANT already held
-- is a no op, so running this again after adding a table is the normal way to
-- use it.

GRANT CONNECT ON DATABASE databricks_postgres TO :"principal";
GRANT USAGE ON SCHEMA iberian TO :"principal";

-- Read and write, not owner. The application creates alerts, records
-- judgements and appends to the audit log. It has no business dropping a table
-- or altering a column, and that difference is one somebody will be glad of on
-- the day a bug writes the wrong statement.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA iberian TO :"principal";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA iberian TO :"principal";

-- And on tables added later. Without this, a table created next week is
-- invisible to the application until somebody remembers to run the grants
-- again, which they will not, and the failure will look like a bug in the app
-- rather than a missing grant.
ALTER DEFAULT PRIVILEGES IN SCHEMA iberian
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"principal";
ALTER DEFAULT PRIVILEGES IN SCHEMA iberian
    GRANT USAGE, SELECT ON SEQUENCES TO :"principal";