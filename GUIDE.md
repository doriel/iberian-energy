# How to run this

Two environments, and the same code in both. Locally the medallion is parquet
on disk, on Databricks it is Delta in Unity Catalog built by a Lakeflow
declarative pipeline. The ingestion, parsing and analysis functions are the
same objects in both cases, which is what makes the numbers comparable.

Start here for the local loop, which is where development happens, then
[Databricks](#on-databricks) for the platform.

- [Where everything lives](#where-everything-lives)
- [The mental model](#the-mental-model)
- [Locally](#locally)
- [The dashboard and the workbench](#the-dashboard-and-the-workbench)
- [On Databricks](#on-databricks)
- [The Lakebase half](#the-lakebase-half)
- [The backfills](#the-backfills)
- [The notice index, and the two retrieval paths](#the-notice-index-and-the-two-retrieval-paths)
- [The daily Job](#the-daily-job)
- [Continuous integration](#continuous-integration)
- [Tracing and experiments](#tracing-and-experiments)
- [The evaluation set](#the-evaluation-set)
- [Running the agent](#running-the-agent)
- [Things worth breaking on purpose](#things-worth-breaking-on-purpose)
- [Changing the analysis](#changing-the-analysis)

## Where everything lives

Five places, and which one a file belongs in is decided by what it is allowed to
import rather than by what it is about.

| Folder | What it is | May import |
|---|---|---|
| `src/iberian/` | the library | standard library, pandas, requests. No Databricks, no Spark |
| `src/iberian/app/` | what the web service needs | the Databricks SDK and psycopg. Nothing from the rest of the library |
| `pipelines/` | notebooks and the declarative pipeline | `src/iberian/`, plus Spark and dbutils |
| `scripts/` | entry points you run at a terminal | `src/iberian/` |
| `app/` | the web service | `src/iberian/app/` only |

That rule is what keeps the test suite fast. The moment a Databricks import
appears under `src/iberian/` outside `app/`, every test needs a cluster to run.

`src/iberian/app/` is separated in the other direction and for the mirror image
of the reason. The web service installs `requirements-app.txt`, which is much
shorter than `requirements.txt`, and an import reaching from there into the
analysis modules would drag pandas into a deploy that has no use for it. The
first sign would be a cold start getting slower.

```
src/iberian/
  config.py  market_time.py        settings, and the Iberian market day
  ingestion/                       API clients, one per source
  parsing/                         payloads to tidy rows
  analysis/                        pure functions on dataframes
  pipeline/                        gold table builders, dedupe
  agent/
    facts.py                       retrieved evidence, as sourced facts
    verify.py                      rejects any figure that was not retrieved
    explain.py                     the generate, check, retry once loop
    retrieval.py                   the same evidence, out of the vector index
    notices.py                     A78 notices as rows of the notice table
    tracing.py                     MLflow's decorator, or a no-op without it
    experiment.py                  an evaluation run, as an MLflow run
    batch.py                       which episodes need explaining, and merging
    table.py                       the explanations as a typed, commented table
  app/
    lakebase.py                    a fresh 60 minute credential per connection
    workspace.py                   which identity this process acts as
    session.py                     who the visitor says they are, in a cookie
    actions.py                     the writes the agent may make, validated
    assistant.py                   the tool calling agent behind the workbench
    limits.py                      what this application will spend, and on whom
  publish/
    dashboard.py                   gold tables to the published JSON
    github.py                      commit a built file over the contents API

agents/
  mibel_agent.py                   the explanation agent as a ResponsesAgent

pipelines/
  00_setup_notice_index.py         the notice table and its vector index
  00b_apply_lakebase_schema.py     sql/001 applied to Lakebase
  00c_grant_service_principal.py   the application's Postgres role and grants
  00d_enable_lakebase_cdf.py       the change data feed, immutable once created
  01_build_medallion.py            ingest task: APIs to the Volume
  01b_load_notices.py              load task: A78 notices, and the index sync
  01c_ingest_generation.py         by hand: a year of A73 documents
  01d_build_generation_silver.py   by hand: silver_generation_per_unit
  01e_build_generation_gold.py     by hand: gold_unit_hourly_output
  01f_backfill_market_history.py   by hand: N days of market history, resumable
  01g_build_application_activity.py  by hand: lb_*_history to silver to gold
  02_explain_episodes.py           explain task: new episodes to the Volume
  03_publish_dashboard.py          publish task: gold to a commit
  04_sync_episodes_to_lakebase.py  gold_split_episodes to iberian.episodes
  99_evaluate_retrieval.py         direct against vector, and the leak measured
  transformations/                 the declarative pipeline

app/
  main.py                          FastAPI: the dashboard and the OAuth flow
  api.py                           the workbench routes
  public/index.html                the dashboard
  public/workbench.html            the workbench
  public/signin.html               the name prompt, which is not authentication
  public/data.json                 written by the Job, not by hand

sql/                               the Lakebase schema and its grants
images_readme/                     the figures in README.md, with their sources
scripts/                           every entry point, see the sections below
tests/                             synthetic data, no network, no credentials
resources/iberian_job.yml          the daily Job
databricks.yml                     the Asset Bundle
.github/workflows/ci.yml           tests and checks on every push
```

### The two MLflow files, and why they are library code

`agent/tracing.py` and `agent/experiment.py` sit under `src/iberian/` rather
than in `scripts/`, which looks wrong at first because MLflow is a platform
thing.

They are there because the agent uses them. `facts.py`, `verify.py` and
`explain.py` are decorated by `tracing.trace`, so it has to be importable
wherever they are. `experiment.py` is used by `scripts/explain_episodes.py` and
could have lived there, but then a notebook that wanted to record a run would
have to import from a scripts directory, which is the shape that cost three
failed Job runs already.

Neither breaks the no-platform-imports rule, because neither imports MLflow at
module level. They try, and carry on without it. Run the suite on a machine that
has never installed MLflow and it passes; install it and three extra tests start
running against the real decorator.

---

## The mental model

Six layers, and each one is worth poking at separately.

**Raw** holds API payloads byte for byte, exactly as they arrived. Nothing is
interpreted. If a parser turns out to be wrong, this is what gets reprocessed
instead of re-hitting a rate limited API. Locally it is `data/raw/`, on
Databricks a Unity Catalog Volume with the same directory layout, which is why
a local backfill can be copied up and read without translation.

**Bronze** exists only on Databricks: a streaming table over the Volume, read as
`binaryFile`, so the payload is stored exactly as it arrived and Auto Loader
tracks which files it has already seen.

**Silver** is those payloads parsed into tidy rows, one table per source, no
business logic applied.

**Gold** is the persona tables, the two validations, the notices, the agent's
explanations and the application activity.

**The analysis modules** (`src/iberian/analysis/`) are pure functions on
dataframes. No network, no credentials, no Databricks.

**The agent** (`src/iberian/agent/`) assembles retrieved facts, asks a model to
phrase them, and rejects the answer if it contains a figure that was not
retrieved. Only the model call touches a network.

There is also **Lakebase**, which is not a layer in the medallion and should not
be thought of as one. It is the operational database the application reads and
writes, fed from gold and read back into Delta by the change data feed. See
[The Lakebase half](#the-lakebase-half).

---

## Locally

```bash
cd ~/repos/iberian-energy
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
```

### Start here

```bash
python -m pytest tests/ -q          # 541 tests, no network, about 15 seconds
python scripts/run_market_splitting.py --demo   # whole pipeline, synthetic data
python scripts/explore.py tables    # what the last run produced
```

The demo plants two known splits in a synthetic week, so the detection is
verifiable by eye before trusting it on real data.

### Building the medallion

```bash
# One market day, cheap
python scripts/build_medallion.py --start 2026-09-03 --days 1

# A month, for a daily profile that actually means something
python scripts/build_medallion.py --start 2026-08-01 --days 31

# Recompute gold from the silver already on disk, no API calls at all
python scripts/build_medallion.py --from-silver

# Skip a source to see the pipeline degrade gracefully
python scripts/build_medallion.py --start 2026-09-03 --days 1 --skip-weather
```

`--from-silver` is the one to use after changing anything in `analysis/` or
`pipeline/gold.py`. It runs the same `build_gold()` the full pipeline runs, so
there is no second code path that can drift from the first.

Seven days is not enough to tell a manufacturer when to run equipment. Thirty
is a minimum, sixty is better. That run costs nothing but time.

### Exploring what it built

```bash
python scripts/explore.py profile              # persona 1: when does PT pay?
python scripts/explore.py episodes             # persona 2: duration, cause, cost
python scripts/explore.py day --date 2026-09-03    # just the splits that day
python scripts/explore.py day --date 2026-09-03 --all   # every interval
python scripts/explore.py weather              # does solar move the ES price?
python scripts/explore.py compare              # ENTSO-E against OMIE
```

`explore.py weather` reports two numbers per location: the raw correlation and
the same correlation computed within each hour of the day. They disagree badly,
and the second is the one to believe. See [the weather result](#the-weather-result-and-why-the-obvious-version-was-wrong).

### Investigating one number

These take a real question and answer it with sourced figures.

```bash
# Why did Portugal pay 28 EUR/MWh more at this moment?
python scripts/explain_interval.py --at 2026-09-03T18:00

# Show the leak the point in time filter prevents
python scripts/explain_interval.py --at 2026-09-03T18:00 --no-point-in-time

# The capacity curve, hour by hour, with splits marked
python scripts/show_capacity.py --start 2026-09-03 --days 2

# Prices side by side around an episode, from already landed XML
python scripts/show_window.py --from 2026-09-03T16:30 --to 2026-09-03T19:00

# Does a full border explain the splits, across a range?
python scripts/analyse_saturation.py --start 2026-09-01 --days 7 --show-splits

# The cost figure against REE's published congestion rent
python scripts/check_congestion_rent.py --start 2026-07-01 --days 60
```

The last one is now also a gold table, `gold_cost_validation`. The script
remains useful for a quick answer without a cluster.

### Poking at the tables directly

```python
import pandas as pd
pd.set_option("display.width", 200)

g = pd.read_parquet("data/lakehouse/gold/gold_interval_premium.parquet")

g[g.is_decoupled][["ts_utc", "premium_eur_mwh", "utilisation"]]
g.groupby("market_day").is_decoupled.sum()
g.groupby("hour_of_day_utc").premium_eur_mwh.mean().sort_values()

# Splits that saturation does NOT explain, which are the interesting ones
g[(g.is_decoupled) & (~g.is_saturated.fillna(False))]
```

That last query is the one to keep an eye on. Every row in it is a split the
current story does not account for, and the pattern in them is where the next
real result lives.

---

## The dashboard and the workbench

Two pages, and the split between them is deliberate.

**The dashboard**, at `/`, is a single HTML file and a single JSON file. It
needs no warehouse, no token, no database and no call to REE at request time. It
is the page a visitor lands on, and it keeps working when everything else is
misconfigured or asleep.

**The workbench**, at `/workbench`, is the same data with an agent that can act.
It needs Lakebase and a serving endpoint. When either is down the dashboard is
unaffected, which is the whole reason the routes are in two files.

```bash
pip install -r requirements-app.txt
python scripts/export_public_data.py
python -m uvicorn app.main:app --reload --port 8000
```

Then open `http://127.0.0.1:8000`.

`python -m uvicorn` rather than `uvicorn`. If the system package manager has
also installed uvicorn, the bare command runs the system Python and cannot see
anything in the virtual environment, which surfaces as `No module named
'fastapi'` while fastapi is plainly installed.

| Route | What it is |
|---|---|
| `/` | the dashboard, from the committed JSON |
| `/data.json` | the published gold data the page reads |
| `/workbench` | the agent and the labelling interface |
| `/signin` | a name prompt, which is not authentication |
| `/auth` | Databricks sign in status, and the start of the OAuth flow |
| `/healthz` | liveness, and whether the data file is actually present |

`/healthz` reports the data file on purpose. A health check that only proves the
process is up reports green while the page renders empty.

### The workbench API

| Route | What it does |
|---|---|
| `POST /api/session` | set the visitor's name in a signed cookie |
| `GET /api/episodes` | the episode list, from Lakebase |
| `GET /api/alerts` `POST` `DELETE /api/alerts/{id}` | the manufacturer's write action |
| `GET /api/labels` `POST /api/labels` | the analyst's write action, and the ground truth |
| `GET /api/activity` | what has been done, for the person doing it |
| `POST /api/ask` | the agent, with tools |

Every route returns a shape the page can render. A route that raised would give
the browser a stack trace and the person a spinner that never stops, so the
failures that matter, Lakebase unreachable, the endpoint down, the agent
looping, all arrive as JSON with a message written for a person. The full
failure goes to the log.

### The name is not authentication, and the interface says so

There is no password. Anybody may type any name, and nothing is authorised by
it. What it is for is keeping one person's alerts and one person's judgements
apart from everybody else's, so that two reviewers labelling the same episode
produce two rows rather than overwriting each other. Every statement that
touches somebody's data filters on the name anyway, so a forged cookie reaches
another name's rows and nothing else.

This is stated plainly in the code and on the page, because a reviewer should
not have to work out how much it is trusted.

### What the agent may do

Seven tools, and the validation is in the tool rather than in the prompt.

| Tool | Writes |
|---|---|
| `list_episodes` | no |
| `my_alerts` | no |
| `my_labels` | no |
| `create_alert` | yes |
| `update_alert` | yes |
| `delete_alert` | yes |
| `submit_episode_label` | yes |

The three write tools exist together on purpose: an alert is the one object that
exercises create, update and delete, which is what makes the change feed
downstream show all four `_pg_change_type` values rather than only inserts.

Every call, successful or not, is recorded in `iberian.agent_actions` with its
arguments, its status and its latency. `ok`, `rejected` and `error` are three
separate states: a write the validation refused is the system working, a write
that blew up is not, and a tool success rate that mixes them tells you nothing
about either.

### What it will spend

`src/iberian/app/limits.py`. The agent is reachable by anybody with the link and
every question is paid for, so there is a budget counted in code: 25 questions
per session and 150 per rolling hour across everybody. A question is refused
before the model sees it, with a message that says so.

### Where the data comes from

`scripts/export_public_data.py` reads the local Parquet build. The Job runs the
same builder against the Delta tables, through `iberian.publish.dashboard`, and
commits the result. Both produce the same document; only the source differs.

One difference between the two paths is deliberate. The local build does not
write the two validation tables, so on that path they are recomputed from silver
and from the stored ESIOS payloads. On Databricks the pipeline already owns them
as tables and they are read rather than recomputed. Either way the numbers come
out of the same functions in `iberian.analysis.validation`.

**The Job owns `app/public/data.json`.** Run the export locally to look at the
page, but discard it before committing, or a stale local build overwrites what
the Job published:

```bash
git checkout app/public/data.json
```

### On Render

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements-app.txt` |
| Start command | `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/healthz` |

`$PORT` and `--host 0.0.0.0` are both required: Render assigns the port and
stops a service that is not listening on it, and without the host it accepts
connections only from inside the container.

Environment variables:

| Variable | For |
|---|---|
| `DATABRICKS_HOST` | the workspace |
| `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` | the service principal the application acts as |
| `APP_OAUTH_CLIENT_ID`, `APP_OAUTH_CLIENT_SECRET` | the user facing OAuth flow at `/auth` |
| `DATABRICKS_REDIRECT_URI`, `DATABRICKS_SCOPES` | the same flow |
| `LAKEBASE_HOST`, `LAKEBASE_USER` | the database endpoint and the service principal's Postgres role |
| `AGENT_ENDPOINT` | the serving endpoint the workbench agent calls |

The two OAuth pairs must never share values. `DATABRICKS_CLIENT_ID` and
`DATABRICKS_CLIENT_SECRET` are reserved names the SDK reads for machine to
machine auth, which is exactly what the application wants them for; the pair the
human sign in uses has to be called something else or the SDK picks the wrong
one and fails with `invalid_client`.

**Do not set `DATABRICKS_CONFIG_PROFILE` on Render.** There is no CLI profile
there, and the SDK will prefer it over the service principal and fail.

The free instance sleeps when idle. A sign in started before it restarted fails
with "unknown state", because the pending flow is held in memory; `/callback`
says so rather than blaming the user.

---

## On Databricks

Five pieces in the daily path, in this order: a notebook that fetches and lands,
a notebook that loads the notices and syncs the index, a pipeline that
transforms, a notebook that explains, and a notebook that publishes. The first
writes no tables, the pipeline makes no HTTP requests, the explain task calls a
model and writes neither tables nor payloads, and the publish task makes one
authenticated HTTP request and nothing else. None of those is an accident.

A declarative pipeline is given data and asked to derive tables from it. Putting
a rate limited API call inside a unit of work the platform is entitled to retry
would be a mistake. And a pipeline only manages tables it created, so a notebook
writing the same names both breaks the pipeline and gives two implementations of
one transformation.

### One time setup

**Secrets.** Every token lives in a scope, never in a widget: a widget's value
is saved with the notebook state and this repository is public.

```bash
databricks secrets create-scope iberian
databricks secrets put-secret iberian entsoe_token
databricks secrets put-secret iberian esios_token
databricks secrets put-secret iberian github_token
```

Run each without flags and it opens an editor to paste into, which keeps the
value out of the shell history. `github_token` is a fine grained personal access
token scoped to this repository alone, with **Contents: Read and write** and
nothing else. GitHub adds **Metadata: Read-only** by itself, which is required
for any repository permission.

**Git folder.** Clone the repository into the workspace. The notebooks and the
pipeline both import `src/iberian/` from it rather than carrying copies.

**The pipeline.** In **Jobs & Pipelines**, create an ETL pipeline with:

| Setting | Value |
|---|---|
| Source code | `pipelines/transformations` inside the Git folder |
| Default catalog and schema | where the tables should land |
| Configuration | `iberian.catalog`, `iberian.schema`, `iberian.raw_volume`, `iberian.src_path` |
| Compute | serverless |

`iberian.src_path` is the absolute path to `src/` in the Git folder. The file
tries `__file__` first and falls back to this, because `__file__` is not defined
in every execution context.

Keep `spark.sql.ansi.enabled` at `true`. It is what turned a stray reference
file in the landing zone into a visible error rather than a silent null.

**The notice index.** Run `pipelines/00_setup_notice_index.py` once. It creates
`gold_transmission_notices` and its Delta Sync vector index. Notice that this
table is created by a notebook and not by the pipeline, and see
[the notice index section](#the-notice-index-and-the-two-retrieval-paths) for
why.

**Lakebase.** Run `00b`, then `00c`, then `00d`, in that order, once each. See
[The Lakebase half](#the-lakebase-half).

### Running it

**1. Ingestion.** Open `pipelines/01_build_medallion.py`, set the widgets and
**Run all**.

```
catalog       bootcamp_students
schema        doriel
volume        raw
start_day     <blank means ending today>
days          3
secret_scope  iberian
```

It fetches ENTSO-E prices for both zones, the cross-border schedules and
capacity in both directions, the OMIE files, Open-Meteo for six locations and
four ESIOS indicators, and lands every payload in the Volume. Landing the same
window twice is safe. For more than a few days, use
[the backfill notebook](#the-backfills) instead of turning `days` up here.

**2. The notices.** Open `pipelines/01b_load_notices.py` and **Run all**. It
fetches the A78 transmission unavailability notices for the same trailing
window, merges them into `gold_transmission_notices` on `notice_id`, and syncs
the vector index.

**3. The pipeline.** **Dry run** first: it validates the code and the dependency
graph in seconds without writing anything, which catches an import or a schema
mistake before a cluster spends minutes on it. Then **Run pipeline**.

Auto Loader reads only the files it has not seen, so a daily run costs seconds.
Gold is recomputed in full, which at this size also costs seconds.

**4. Explanations.** Open `pipelines/02_explain_episodes.py` and **Run all**. It
reads `gold_split_episodes`, works out which episodes have no explanation on
file, and calls the serving endpoint for those. Worst first, so if the cap bites
the explained ones are the ones anybody would have looked at first. Set the
`rebuild` widget to `yes` only to re-explain everything, which costs one model
call per episode.

It then writes `gold_episode_explanations`, overwritten in full from the JSONL
on every run. The file is the record of work and the table is a materialisation
of it, so if the two ever disagree the file is right. Every column carries a
comment, because a gold table a journalist or a grader is expected to query
should explain itself:

```sql
SELECT e.market_day, e.peak_spread, x.text
FROM   bootcamp_students.doriel.gold_split_episodes  e
JOIN   bootcamp_students.doriel.gold_episode_explanations x
  ON   x.episode_key = concat(e.market_day, 'T', date_format(e.start_utc, 'HHmm'))
WHERE  x.grounded
ORDER  BY e.peak_spread DESC
LIMIT  10;
```

**5. Publishing.** Open `pipelines/03_publish_dashboard.py` and **Run all**. It
reads the gold tables, builds the same JSON the local export builds, and commits
it to the branch. Render watches the branch, so the commit is the deploy.

It prints where it found the checkout before it imports anything, which is the
first thing to read if it fails:

```
Working directory: /Workspace/Repos/.internal/<id>_commits/<sha>/pipelines
Repo root:         /Workspace/Repos/.internal/<id>_commits/<sha>
```

If the data has not changed it prints `unchanged` and commits nothing. Set the
`force` widget to `yes` to commit anyway, which is worth doing once to prove the
token works and never on a schedule.

**6. The Lakebase copy.** Open `pipelines/04_sync_episodes_to_lakebase.py` and
**Run all**, so the workbench sees the new episodes.

### The tables

| Layer | Tables |
|---|---|
| Bronze | `bronze_entsoe_prices`, `bronze_entsoe_schedules`, `bronze_entsoe_capacity`, `bronze_omie`, `bronze_open_meteo`, `bronze_esios` |
| Silver | `silver_entsoe_prices`, `silver_entsoe_schedules`, `silver_entsoe_capacity`, `silver_omie_prices`, `silver_weather`, `silver_esios_indicators` |
| Gold | `gold_interval_premium`, `gold_daily_profile`, `gold_split_episodes`, `gold_weather_context`, `gold_price_source_agreement`, `gold_cost_validation` |

Those eighteen are owned by the declarative pipeline. Five more are written by
notebooks, and each exception has a reason:

| Table | Written by | Why not the pipeline |
|---|---|---|
| `gold_transmission_notices` | `01b_load_notices` | the notices come from an HTTP call, and an API call does not belong in a unit of work the platform may retry |
| `silver_generation_per_unit` | `01d` | a one off backfill of a year, not a daily derivation |
| `gold_unit_hourly_output` | `01e` | same |
| `gold_episode_explanations` | `02_explain_episodes` | the pipeline runs before the explanations exist, so a pipeline owned copy would always be a day behind |
| `silver_application_events`, `gold_application_activity` | `01g` | their source is the Lakebase change feed, which lands outside the pipeline's own graph |
| `gold_retrieval_evaluation` | `99_evaluate_retrieval` | a measurement of the retrieval, run when it is worth measuring, not derived from the market data on a schedule |

Every table, every key and every join is in
[`images_readme/04-lakehouse-er.png`](images_readme/04-lakehouse-er.png). The
shape of the medallion is in
[`images_readme/01-medallion.png`](images_readme/01-medallion.png).

Three things those pictures make obvious that a list does not.

**Bronze to silver is one to one.** Every source gets its own silver table and
no business logic happens on that hop, so a parsing bug is fixed by replaying
bronze rather than by re-fetching from a rate limited API.

**The first three gold views are siblings, not a chain.** `gold_interval_premium`,
`gold_daily_profile` and `gold_split_episodes` each read the same three silver
tables and compute all three results internally, returning one. That is what
keeps the interval premium, the daily profile and the episode boundaries
arithmetically consistent with each other.

**The two validations sit in gold on purpose.** `gold_price_source_agreement`
and `gold_cost_validation` are checks against publishers outside this project,
and they are tables recomputed on every run rather than scripts somebody
remembers to invoke.

### Checking it agrees with the local build

This is the check worth running after any change to the analysis, because it is
the one that proves moving the code did not move the numbers.

```sql
SELECT market_day,
       count(*) AS episodes,
       round(sum(extra_cost_eur)) AS eur,
       round(max(max_abs_spread), 2) AS worst_spread
FROM bootcamp_students.doriel.gold_split_episodes
GROUP BY 1 ORDER BY 1;
```

```bash
python - <<'EOF'
import pandas as pd
e = pd.read_parquet("data/lakehouse/gold/gold_split_episodes.parquet")
print(e.groupby("market_day")
       .agg(episodes=("episode_id", "count"),
            eur=("extra_cost_eur", lambda s: round(s.sum())),
            worst_spread=("max_abs_spread", lambda s: round(s.max(), 2)))
       .to_string())
EOF
```

Compare only the days present on both sides. They should match exactly. A one
euro difference on a day is rounding, not divergence: SQL rounds a half up and
Python rounds it to even, so a sum ending in `.5` differs by one. Check the
unrounded sum before chasing it.

### The validations, as queries

```sql
-- Do the two publishers agree, interval by interval?
SELECT count(*) AS intervals,
       sum(CASE WHEN agrees THEN 1 ELSE 0 END) AS agreeing,
       round(max(pt_difference), 4) AS worst_pt_difference
FROM bootcamp_students.doriel.gold_price_source_agreement;

-- Does the congestion rent match REE's published figure?
-- Rent against rent. our_import_cost_eur is a different quantity: see below.
SELECT round(sum(our_rent_eur))        AS ours,
       round(sum(congestion_rent_eur)) AS ree,
       round(100 * (sum(our_rent_eur) - sum(congestion_rent_eur))
                 / sum(congestion_rent_eur), 4) AS difference_pct
FROM bootcamp_students.doriel.gold_cost_validation;

-- The interesting rows: days REE recorded rent and this project found none
SELECT * FROM bootcamp_students.doriel.gold_cost_validation
WHERE episodes = 0 AND congestion_rent_eur > 0
ORDER BY congestion_rent_eur DESC;
```

**`our_rent_eur` and `our_import_cost_eur` are two different numbers and mixing
them is the mistake that cost a day.** The rent counts energy crossing the
border in either direction, which is what REE publishes. The import cost counts
only the direction into the premium zone, which is what Portugal actually paid.
They agree only while the flow is one directional, which it was across one
summer window and is not across a year. Compare rent to rent.

### A unit against its own baseline

```sql
-- Units that look offline against a substantial baseline, worst first
SELECT unit_eic, unit_name, psr_label, hour_utc,
       output_mw, baseline_mw, deviation_pct
FROM   bootcamp_students.doriel.gold_unit_hourly_output
WHERE  looks_offline AND baseline_mw > 500
ORDER  BY hour_utc DESC
LIMIT  50;
```

Three things to know before reading a row of that as a finding.

**`looks_offline` is a signal, not a verdict.** It does not distinguish a forced
outage from a plant that was simply not dispatched. The largest slice is Spanish
CCGTs, which are idle for weeks at a time by design.

**The first 30 days of the history have no baseline**, because the baseline is
the median of the same local hour over the 30 prior days.

**A stopped plant publishes one zero, not a run of zeros.** That is the A03
encoding, and it is why gold expands the blocks while silver keeps published
points. `published_at_utc` and `source_block_minutes` carry the provenance per
row, so how much of an hour was actually published is answerable.

### When something fails

**"MANAGED table already exists with that name."** A table of that name was
created outside the pipeline, usually by an older version of the notebook. The
pipeline only manages tables it created. Drop it and re-run: everything from
bronze onwards is derived from the bytes in the Volume, so nothing is lost.

**The gold tables fail with a timezone error.** Spark returns timestamps without
a timezone and the market day is found by converting to CET. `as_utc` handles
this at the boundary; if a new table skips it, this is the symptom.

**A stream fails on a cast.** Something is in the landing zone that is not a
response document. The ESIOS catalogue and the request metadata files are both
filtered out by name for this reason.

**`Zones are on different settlement resolutions`.** The window straddles
1 October 2025. The guard is right and the fix is not to relax it: episode
grouping runs per resolution segment, and a run that ignored the change would
report every hourly episode as four quarter hourly ones.

**A publish or explain task cannot import `iberian`.** Read the `Repo root` line
it prints. The import block is a copy of the one in `01_build_medallion`, which
works, so a difference there is the thing to look at. Three runs were lost
inventing a second mechanism before copying the one that was already proven.

**The publish task says `unchanged`.** The data is the same as what is already
committed, ignoring the timestamp. Usually it means the export was run locally
and committed by hand. Set `force` to `yes` to override.

**The publish task fails with "did not come back base64 encoded".** The GitHub
contents API stops inlining a file over 1 MB and returns metadata with
`encoding: "none"`. `publish/github.py` falls back to the blobs API, which
serves it; if you see this, the fallback is not being reached.

**The commit is rejected with a conflict.** Something else wrote to the same
path between the read and the write. Re-run; the next attempt reads the new sha.

**`git push` is rejected with "fetch first".** The Job committed the data file
to the branch. `git pull --rebase` then push. Running
`git config pull.rebase true` once in this repository makes that the default,
which matters because the Job commits every afternoon.

---

## The Lakebase half

Lakebase is managed Postgres. It exists here because the application needs
single digit millisecond reads and a place to put what people do, and a
warehouse query per page load is seconds of latency plus a cluster that has to
be awake for a visitor to see anything.

The shape is in [`images_readme/02-loop.png`](images_readme/02-loop.png), and
the four tables are in
[`images_readme/05-lakebase-er.png`](images_readme/05-lakebase-er.png).

| Table | Written by | What it is |
|---|---|---|
| `iberian.episodes` | the daily Job, `04` | a small copy of what gold knows about each episode |
| `iberian.alerts` | the application | the manufacturer's write action |
| `iberian.episode_labels` | the application | the analyst's write action, and the evaluation ground truth |
| `iberian.agent_actions` | the application | one row per tool call, append only |

### Why `episodes` is owned and not synced

`episode_labels` has a foreign key to it, with `ON DELETE CASCADE`. A synced
table is managed by Databricks, and pointing a constraint at something another
system owns means the sync and the constraint can disagree about who is in
charge. Owning this one keeps referential integrity real, which is the point of
having it at all. Heavier read models with no constraint depending on them can
still arrive as synced tables.

### Setting it up

Three notebooks, in order, once each.

**`00b_apply_lakebase_schema`** reads `sql/001_application_tables.sql` and
applies it. Every statement in that file is idempotent, which is what lets the
file be the schema rather than a migration that happened once and drifted. Run
it again after changing the file.

**`00c_grant_service_principal`** creates the Postgres role for the service
principal the deployed application acts as, and applies
`sql/002_service_principal_grants.sql`. Being allowed to authenticate is not the
same as being allowed to read a table, and this notebook is both halves.

The service principal is not one created for this project: `service-principals
create` is admin only in this workspace. The one used came with a Databricks App
built earlier in the boot camp, and its OAuth secret can be minted at workspace
level with `databricks service-principal-secrets-proxy create <numeric id>`.

**`00d_enable_lakebase_cdf`** turns on the change data feed. Read the next
section before running it, because the configuration cannot be edited.

### The change data feed, and what it will not let you take back

`w.postgres.create_cdf_config` streams the Postgres write ahead log into Delta,
writing one `lb_<table>_history` table per source table, with `_pg_change_type`
(`insert`, `delete`, `update_preimage`, `update_postimage`), `_pg_lsn`, `_pg_xid`
and `_timestamp`. It flushes roughly every fifteen seconds. Public Preview at
the time of writing, so check the current documentation before trusting any
detail here.

Three things that cost time:

**It is schema level, not table level.** There is no per table toggle. You
configure a Postgres schema and you get every table in it.

**It is immutable once created.** Getting the destination catalog wrong means
creating a second configuration, not fixing the first. `00d` therefore checks
the destination catalog has an external storage location and reads the replica
identity of every table out of Postgres before it creates anything.

**It needs `REPLICA IDENTITY FULL` on every table.** Without it an update
carries only the primary key, the `update_preimage` rows come through empty, and
"what changed about this episode" is unanswerable from the history table. This
was missed on `iberian.episodes` in the first version of the schema, on the
reasoning that the application never writes there, which was wrong: the daily
Job upserts into it. `00d` found it by reading Postgres rather than by trusting
the SQL file, which is the reason it reads Postgres.

Empty tables come back as `CDF_STATE_SKIPPED`, which is not an error.

To see the state, run this **in the Lakebase SQL editor**, not in a Databricks
notebook:

```sql
SELECT * FROM wal2delta.tables;
```

The SDK is ahead of the deployed API here: `cdf-configs` responds, and
`cdf-statuses` returns `No API found for GET .../cdf-statuses`. `00d` wraps that
call and points at the query above.

### Bringing the changes back into Delta

`pipelines/01g_build_application_activity.py`, by hand.

It discovers every `lb_*_history` table by name, so a new application table
configured tomorrow appears without anybody editing the notebook. For each one
it reads the highest `sort_by` already in `silver_application_events` and merges
only rows above it, keyed on `(source_table, pg_lsn, sort_by, change_type)`.
Then it rebuilds `gold_application_activity`.

A watermark and a `MERGE`, not a stream. The first version used `foreachBatch`
and died on serverless with `INTERNAL_ERROR: Spark session is no longer usable`.
The watermark version is incremental without depending on a checkpoint being
intact, which for a notebook run by hand is the better property anyway.

Set the `reread` widget to `yes` to ignore the watermark and re-merge
everything. The merge is keyed, so that is safe and only costs time.

### Reaching Lakebase from outside Databricks

`src/iberian/app/lakebase.py`. Two things are worth knowing.

**Credentials last sixty minutes**, so every connection generates a fresh one
through the SDK rather than reading a stored password. A stored Lakebase
credential is a support ticket waiting to happen.

**Use the endpoint's own host, never the `-pooler` host.** The pooler fails with
`SASL authentication failed` against a generated credential.

Two more traps sit next to each other in the naming. The resource id is
`databricks-postgres` with a hyphen and the Postgres database name is
`databricks_postgres` with an underscore. A service principal's `role_id` is
likewise not its Postgres role name.

The endpoint's DNS is split horizon: inside the workspace the name resolves to a
private address over PrivateLink, and from the internet to a public load
balancer. The private address is what a notebook sees, and it looks alarming if
you meet it first.

### Checking it from a terminal

```bash
python scripts/check_lakebase_access.py
```

---

## The backfills

Two, and they are separate from the daily Job on purpose.

### Market history

`pipelines/01f_backfill_market_history.py`. Prices, cross-border schedules and
capacity, OMIE, weather and ESIOS, for as many days as you ask for.

```
catalog       bootcamp_students
schema        doriel
volume        raw
secret_scope  iberian
days          365
end_date      <blank means today>
chunk_days    30
sources       <blank means all>
refetch       no
```

Why this is not just `days = 365` on `01_build_medallion`: that notebook is what
the Job runs every afternoon with a three day trailing window for corrections.
Turning its window up to a year would make it re-fetch a year every day.

What it does that the daily notebook does not need to:

**Chunked and resumable.** Thirty days at a time by default, and it skips what
is already landed. A failed chunk costs that chunk.

**Filenames carry the chunk length**, `2025-10-01_30d.xml`, so a backfill file
can never overwrite a Job file. This matters more than it sounds: Auto Loader
tracks files by path, so an overwritten file is one it has already seen and will
never read again. An overwrite is the one way to land data that is silently
never ingested.

**It expands archives.** ENTSO-E returns a ZIP once a response holds more than
one document, and the pipeline filters the landing folders with `*.xml`, so a
zip landed there would be invisible.

It lands into the identical Volume layout, so the declarative pipeline picks it
up with Auto Loader and **nothing in `pipelines/transformations/` changes**.

Episode identity survives a backfill, because `episode_key` is the market day
plus the start time, computed from the episode itself rather than from the
sequential `episode_id`. Labels, published explanations and the evaluation set
keep pointing at the same episodes.

Agent spend stays bounded, because `02_explain_episodes` skips explained
episodes and caps a run at `max_new`.

**One known gap.** OMIE has no file for 2025-10-30 or 2025-11-27. Two retries
gave the same 404, so it is not intermittent. The unconfirmed hypothesis is that
the client always requests the `.1` version suffix and OMIE numbers
republications `.2`, `.3`, so a withdrawn `.1` fails permanently. ENTSO-E covers
both days, so the only loss is the independent cross-source price check on two
days out of the year.

**After a market history backfill, run `01b_load_notices` with a matching
`days`.** Otherwise the notice table only goes back a few days, the point in
time filter correctly finds nothing published before an old episode, and the
agent honestly reports that the published facts do not explain it. That is right
behaviour producing a misleading dashboard, and it moves the north star metric
for the wrong reason.

### Generation per unit

Three notebooks, in order, by hand.

```
01c_ingest_generation        days 365, one A73 document per zone per day
01d_build_generation_silver  -> silver_generation_per_unit
01e_build_generation_gold    -> gold_unit_hourly_output
```

`01c` is its own notebook because ENTSO-E limits 16.1.A to one day per request,
so a year is a loop of seven hundred and thirty requests. A loop of that length
inside a task that runs every afternoon would make seven hundred requests a day
for data that changed in one of them.

A year, rather than the curated market window, because the baseline needs one.
To say a unit was producing less than it usually does you need its own history,
and seventy days of summer tells you nothing about how the fleet behaves in
February.

`01e` takes `observations` (how many prior days the baseline uses, 30) and two
thresholds for `looks_offline`: `offline_output_mw` and `offline_baseline_mw`.

---

## The notice index, and the two retrieval paths

`gold_transmission_notices` holds the A78 transmission unavailability notices.
It is written by `01b_load_notices` rather than by the declarative pipeline,
because the notices come from an HTTP call and an API call does not belong
inside a unit of work the platform may retry. Nothing lands in the Volume on
this path.

`00_setup_notice_index` creates the table and its Databricks Vector Search
index, `gold_transmission_notices_index`, a Delta Sync index with managed
`databricks-gte-large-en` embeddings. The schema is fixed in that notebook
because the index is fixed to it: adding a column means recreating the index,
which is what `recreate_index` is for.

The agent can retrieve the same evidence two ways, and `notice_retrieval` on
`02_explain_episodes` chooses:

| Value | What it does |
|---|---|
| `direct` | calls ENTSO-E for that market day, parses the curves, filters in Python |
| `vector` | queries the index with `published_epoch <= start - 1s` in the filter |

**Production uses `direct`.** An index is only as fresh as its last sync, and
the north star's "within fifteen minutes of publication" does not survive a
retrieval path that can be a day behind. `vector` exists to be measured against
it.

`pipelines/99_evaluate_retrieval.py` does the measuring, and asks two questions.

**Agreement.** For every episode with an explanation, both paths are asked which
asset was tightest and at what capacity. A disagreement is a real finding,
because those two values are what the reader sees.

**Leakage.** The same retrieval is run with the publication filter and without
it, and both are checked for notices published after the episode began. With the
filter the count must be zero. Without it, the count is the size of the problem
the filter solves, and reporting it is what turns "we handled point in time
correctness" from a claim into a measurement. A zero that is zero because
nothing was tested is worthless, so both counts are printed side by side.

Every episode is written to `gold_retrieval_evaluation`, so the headline is a
row somebody else can re-run the query for:

```sql
SELECT count(*)                                   AS episodes,
       sum(CASE WHEN agree THEN 1 ELSE 0 END)     AS agreeing,
       sum(leaked_with_filter)                    AS leaked_with_filter,
       sum(leaked_without_filter)                 AS leaked_without_filter,
       sum(CASE WHEN leaked_without_filter > 0 THEN 1 ELSE 0 END) AS episodes_at_risk
FROM bootcamp_students.doriel.gold_retrieval_evaluation;

-- The two disagreements, which are the rows worth reading
SELECT * FROM bootcamp_students.doriel.gold_retrieval_evaluation
WHERE NOT agree;
```

The run of 27 September 2026, over 377 episodes with an explanation and no days
unavailable: agreement **375 of 377**, **0** future notices retrieved with the
filter, **1,151** without it across **277** episodes. Three quarters of the
episodes had something to exclude, which is what makes the zero mean something.

---

## The daily Job

`iberian-daily` runs five tasks at 16:00 Europe/Lisbon, which leaves margin
after the Iberian day-ahead results are published in the early afternoon.

```
ingest ──┬── load_notices
         └── transform ── explain ── publish
```

`load_notices` runs after `ingest` rather than beside it, so two tasks are not
hitting a rate limited public API at once, and it deliberately sits off the path
to `publish`. It lands the notices and syncs the index; a failure there marks
the run failed without stopping the explanations from being published.

`explain` is the one that is easy to miss and the one that makes the north star
metric a property of the system. It explains only episodes with no explanation
on file, capped at 25 per run, and writes to
`/Volumes/<catalog>/<schema>/<volume>/agent/explanations.jsonl`. It writes there
rather than to the repository because within a single run the Git checkout is
frozen at the commit the run started from, so a file this task committed would
be invisible to `publish` in the same run. The Volume is where the tasks of this
Job already hand things to each other.

`04_sync_episodes_to_lakebase` is **not** in the Job yet and is run by hand
after it. So are `01c`, `01d`, `01e`, `01f` and `01g`.

Retries differ per task and the reasons are in the YAML. `ingest` and
`load_notices` retry twice five minutes apart, because a public API refusing a
request once is ordinary weather. `explain` does not retry at all, because each
attempt costs model calls and a partial run has already written what it
produced, so tomorrow's run finishes the job for free. `publish` retries once,
because the write reads the file's current sha first.

The definition lives in `resources/iberian_job.yml` and is deployed with the
Asset Bundle. It was originally built by clicking, which is fine for finding out
what the settings are and wrong as the place to keep them, then bound to the
existing job id so the run history survived.

```bash
databricks bundle validate -t prod
databricks bundle deploy -t prod
databricks bundle run iberian_daily -t prod
```

Change the Job in the YAML, not in the interface. A bundle deployed Job is
marked as managed by the bundle and the workspace restricts editing it there;
either way an edit made by clicking is overwritten by the next deploy.

### What deploy does and does not do

**`bundle deploy` changes the Job definition only.** The schedule, the tasks,
the retries, the parameters.

**The code comes from the branch.** The Job is configured with `git_source`, so
each run takes its own snapshot into
`/Workspace/Repos/.internal/<id>_commits/<sha>`. A `git push` is therefore what
changes the code that runs in production.

**The workspace Git folder of the same repository is a different thing and no
task reads it.** Pulling it changes nothing about what the Job runs. This cost
three failed runs to establish, and the notebooks now print their own repo root
so the question can be settled in one line rather than by argument.

### Testing one task without the whole Job

`ingest` and `transform` take about four minutes together. When iterating on
`publish`, open the run in the UI and run that task alone rather than the Job.

### Trailing window, not one day

`ingest` and `load_notices` both ask for three market days ending today rather
than one. ENTSO-E republishes corrected documents, so a trailing window picks up
a correction. Landing a day twice is safe: `pipeline/dedupe.py` keeps the later
publication, and a republished notice is a new document with a new publication
time, so it lands as a new row rather than replacing the old one. That last part
is what keeps the point in time filter honest.

---

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on every pull
request. Everything in it is offline, with no credentials and no data on disk.

| Step | What it catches |
|---|---|
| `pytest -q` | the analysis, the parsers, the verifier, the publisher |
| `scripts/check_bundle_paths.py` | a `notebook_path` matching no file, or a `${var.x}` that is not declared |
| a clean install of `requirements-app.txt` | an import the app gained that was only ever installed as a side effect of the development requirements |

Commits to `app/public/data.json` do not trigger it. The Job writes that file
every afternoon, and running the suite because the market data changed says
nothing about the code.

**There is no deploy step, deliberately.** Personal access tokens are disabled
in this workspace, so CI cannot authenticate to Databricks at all. Given that
`git push` is already what changes the running code, what was actually missing
was anything checking the code first, and that is what this does.
`bundle deploy` stays manual, and the things it changes change rarely.

Run the checks locally the way CI does:

```bash
python -m pytest -q
python scripts/check_bundle_paths.py
```

---

## Tracing and experiments

Every evaluation run records itself. Nothing here is required to get the
numbers: without MLflow installed the run says so and carries on, which is why
`iberian/agent/tracing.py` exists at all.

### Recording locally needs the full MLflow, not the skinny one

`requirements.txt` pins `mlflow-skinny`, which is right for the Job: the
notebook environment wants the small package and the project only needs tracing
and logging there. Locally it is not enough. From MLflow 3.7 the default local
backend is SQLite rather than `./mlruns`, and the skinny package ships without
SQLAlchemy, so the database stores are never registered and a local run fails
with `unsupported URI 'sqlite:///.../mlflow.db'`. Install the full package in
the development environment and leave `requirements.txt` alone:

```bash
pip install mlflow                 # the full package, brings SQLAlchemy
```

This is a development dependency. Nothing in the Job needs it, and adding it to
`requirements.txt` would put a package into the Job environment for the sake of
a laptop.

```bash
# Local, recorded nowhere
python scripts/explain_episodes.py --limit 3 --labelled-only --no-mlflow

# Recorded in the workspace
python scripts/explain_episodes.py --limit 3 --labelled-only \
  --tracking-uri databricks
```

The run carries the endpoint, the attempt limit and the trace storage as
parameters, six metrics, `evaluation/explanations.jsonl` as an artifact, and the
failing episode keys as a tag.

| Metric | Why it is there |
|---|---|
| `grounded_rate` | the north star, as a share so runs over different episode counts compare |
| `first_attempt_rate` | a retry is not a failure, but it is worse, and the final verdict hides it |
| `claims_per_explanation` | what stops the first metric being vacuous: 100% grounded over prose containing no figures is a perfect and meaningless score |
| `numeric_claims` | the raw count behind it |

Three spans appear per explanation: `episode_facts` as RETRIEVER, `explain` as
AGENT and `verify` as PARSER. Read those before reading the code when an answer
looks wrong, because the trace shows what each step actually received rather
than what it was supposed to receive.

### Unity Catalog trace storage does not work here

MLflow recommends storing traces in Unity Catalog Delta tables rather than in
the experiment. The flags exist:

```bash
export MLFLOW_TRACING_SQL_WAREHOUSE_ID=<a warehouse from `databricks warehouses list`>
python scripts/explain_episodes.py --limit 3 --labelled-only \
  --tracking-uri databricks \
  --experiment /Users/<you>/iberian-energy-agent-uc \
  --trace-catalog bootcamp_students --trace-schema doriel
```

It binds, it provisions all four `otel` tables, and it exports nothing. The
spans table stays at zero rows across runs with the warehouse both cold and
warm. No cause has been established. ROADMAP.md records what was observed.

**Use a new experiment name if you try it.** A Unity Catalog trace location is
permanent: once an experiment is bound it cannot be pointed elsewhere, so
binding the one you already use costs you that experiment.

Reading it back needs the SQL, not the API, because `search_traces` returned
nothing even when asked correctly:

```sql
SELECT count(*) FROM <catalog>.<schema>.`<experiment id>_otel_spans`;
```

---

## The evaluation set

The north star metric needs human labels. They cannot be generated, and in
particular they cannot be generated by the same system that produced the
candidate cause, because then the metric measures the project agreeing with
itself.

There are two ways to produce them now, and they write to different places.

**In the workbench**, which is the one that closes the loop. A label written
there goes to `iberian.episode_labels` in Lakebase, flows back into Delta
through the change data feed, and lands in `silver_application_events` and
`gold_application_activity`. Two people may label the same episode and both rows
survive, because inter-rater disagreement is a measurement and not a conflict to
resolve at write time.

**In the terminal**, which is the original path and still works:

```bash
python scripts/build_evaluation_set.py     # the sheet, from gold
python scripts/label_episodes.py           # one at a time
python scripts/label_episodes.py --limit 10
python scripts/label_episodes.py --all     # revisit ones already labelled
```

By default the episodes are interleaved across strata rather than ordered by
spread, so a partially labelled sheet is still representative. `--by-spread`
gives the largest first.

`evaluation/cause_vocabulary.md` holds the allowed causes and what each one
means, and the same seven values are the `CHECK` constraint on
`iberian.episode_labels.true_cause`. A free text cause is rejected in the
database, because that constraint is what stops the ground truth becoming prose
nobody can aggregate. When the evidence does not settle it, `unclear` is a real
answer and a more useful one than a guess, because it is a class the agent must
also be able to produce.

48 episodes carry a label.

## Running the agent

The explanation agent needs a Databricks serving endpoint, so this is the one
local command that authenticates to the workspace.

```bash
# Read the fact sheets without spending a token
python scripts/explain_episodes.py --dry-run --limit 5

# Run it for real
python scripts/explain_episodes.py --limit 5

# Only episodes a human has labelled, which is what the metric needs
python scripts/explain_episodes.py --labelled-only

# One episode, by key. Re-runs it even though it is already on file, because
# that is the only reason to name one. This is how a rejection gets diagnosed.
python scripts/explain_episodes.py --episode 2026-08-01T1100
python scripts/explain_episodes.py --episode 2026-08-01T1100 --dry-run

# A larger model, to test whether grounding or model size does the work
python scripts/explain_episodes.py --labelled-only \
  --endpoint databricks-claude-opus-4-5 \
  --out evaluation/explanations_opus.jsonl
```

By default only episodes with no explanation on file are run, because an
episode's evidence is fixed once its market day settles and re-explaining the
rest would spend model calls reproducing answers that already exist. `--all`
re-runs everything. Records are merged by episode key, never overwritten
wholesale: an early version of this script overwrote the file and a `--limit 3`
test run silently destroyed 45 explanations.

Output lands in `evaluation/explanations.jsonl`, one record per episode, with
the rejected drafts and the final failing draft kept. The rejections are the
interesting rows: if the verifier never rejects anything, either the model is
flawless or the check is weak, and that needs settling rather than assuming.

`--dry-run` still performs retrieval. Only the model call is skipped. A preview
that showed different facts from the real run would be worse than no preview.

### Registering the agent in Unity Catalog

```bash
pip install boto3
python scripts/register_agent.py
```

`boto3` is needed once, locally. Unity Catalog stores model versions in the
workspace's cloud storage, S3 here, and the upload needs `boto3`, which plain
`mlflow` does not bring. The error message suggests `mlflow[databricks]`, but
that extra also pulls `databricks-agents`, whose dependency `whenever` has no
wheel for Python 3.14 and fails to build without a Rust compiler, taking the
whole install down with it. `boto3` alone is what is missing. The script checks
for it before doing anything, so a missing package costs nothing. It stays out
of `requirements.txt` because nothing in the Job needs it.

This logs `agents/mibel_agent.py` as a models-from-code `ResponsesAgent`, with
`src/iberian/` packaged beside it, and registers it as
`bootcamp_students.doriel.mibel_agent`. The input example is a real fact sheet
for the worst episode, and MLflow calls the model on it while logging, so
registering costs one model call.

It then loads the registered version in a separate process, from a directory
outside the repository, and prints where `iberian` was imported from. A path
ending in `code/iberian/` means the model carries its own code. Checking this in
the same process would prove nothing, because that process has already
imported the repository copy.

Registering is not deploying. Serving the model behind an endpoint is a
separate step and is not attempted. The workbench agent is a different agent and
is deployed, on Render, calling a serving endpoint directly.

### Reading the failures

```bash
python - <<'EOF'
import json, pathlib
for name in ["explanations.jsonl", "explanations_opus.jsonl"]:
    path = pathlib.Path("evaluation") / name
    if not path.exists():
        continue
    for line in path.open():
        record = json.loads(line)
        if record["grounded"]:
            continue
        print("=" * 70, f"\n{name}  {record['episode_key']}  {record['unsupported']}\n")
        print(record.get("final_draft") or record["rejected_drafts"][-1])
EOF
```

### If the SDK fails with `invalid_client`

The Databricks SDK reads `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET`
from the environment and tries machine to machine auth with them, ignoring the
CLI profile. The application's user facing OAuth credentials are named
`APP_OAUTH_CLIENT_ID` and `APP_OAUTH_CLIENT_SECRET` for exactly this reason. If
an older `.env` still exports the reserved names:

```bash
env -u DATABRICKS_CLIENT_ID -u DATABRICKS_CLIENT_SECRET \
  python scripts/explain_episodes.py --labelled-only
```

On Render the opposite is wanted: the application *should* act as the service
principal, so those two are set there and `DATABRICKS_CONFIG_PROFILE` is not.

---

## Things worth breaking on purpose

The guards in this codebase exist because each one protects a number that would
otherwise be wrong in a way nobody notices. Watching them fire is the fastest
way to understand what they are for.

**Make the two zones disagree on resolution.**

```python
import sys; sys.path.insert(0, "src")
import pandas as pd
from iberian.analysis.market_splitting import build_spread_series
from iberian.config import EIC_PORTUGAL, EIC_SPAIN

rows = pd.DataFrame([
    {"zone_eic": EIC_PORTUGAL, "ts_utc": pd.Timestamp("2026-09-03T18:00Z"),
     "price_eur_mwh": 50.0, "resolution": "PT15M"},
    {"zone_eic": EIC_SPAIN, "ts_utc": pd.Timestamp("2026-09-03T18:00Z"),
     "price_eur_mwh": 50.0, "resolution": "PT60M"},
])
build_spread_series(rows, EIC_PORTUGAL, EIC_SPAIN)   # raises
```

Without that guard the pivot lines an hourly price up with the first quarter of
the hour and silently drops the other three. Note that the guard is per
timestamp: a history spanning 1 October 2025 legitimately contains both
resolutions, and `resolution_segments` splits it before grouping.

**Feed it duplicate timestamps.** Duplicate the PT row above with a different
price and it refuses rather than picking one. That is the intraday contamination
hit early on, where one A44 document carried day-ahead and three intraday
auctions stacked on the same timestamps. It is also what fired the first time
the pipeline read the whole landing zone, which is what `pipeline/dedupe.py`
now resolves.

**Give `to_market_day` a naive timestamp.**

```python
import pandas as pd
from iberian.market_time import to_market_day
to_market_day(pd.Timestamp("2026-08-18 23:30:00"))   # raises
```

It has to. The market day begins at local midnight, so assuming a timezone here
would move every boundary by an hour or two and nothing would complain.

**Drop the flow direction from a generation row.** Take two rows for the same
unit and timestamp, one generation and one consumption, and set both
`flow_direction` values the same. The key collides. That is the shape of the
bug that produced 39,061 duplicates on the first build: a unit publishes its
output and its own station consumption as two separate TimeSeries, both
positive, both `businessType A01`, distinguishable only by whether the element
is `inBiddingZone_Domain.mRID` or `outBiddingZone_Domain.mRID`.

**Look for that element with ElementTree's path syntax.**

```python
series.find("{%s}inBiddingZone_Domain.mRID" % ns)   # always None
```

The dot in the tag name is a path operator to ElementTree, so this silently
matches nothing. The parser scans direct children by local name instead. A
silent `None` is how the duplicate bug survived a review.

**Change the settlement interval.** In `detect_episodes`, pass
`step=pd.Timedelta(hours=1)` against quarter hourly data and watch every
duration inflate by four while separate episodes merge into one. That was a real
bug, and the step is now a lookup from the stated resolution.

**Move the market day boundary.** Edit `MARKET_TIMEZONE` in `config.py` to
`"UTC"` and re-run `cross_check_prices.py`. The OMIE and ENTSO-E timestamps stop
lining up and the merge collapses, which is exactly how one would discover the
boundary is local midnight in CET and not UTC midnight.

**Turn off the point in time filter** with `--no-point-in-time` on
`explain_interval.py`. Notices published after the interval start appearing in
the explanation. That is the hindsight leak the evaluation numbers depend on not
having, and `99_evaluate_retrieval` measures its size.

**Invent a number in an explanation.** Take a record from
`evaluation/explanations.jsonl`, change one figure, and run it back through
`agent.verify.verify` against the sheet from `episode_facts`. The altered figure
comes back in `verdict.unsupported`. The check allows thousands separators, a
ratio written as a percentage, and rounding to the precision actually written.
It allows nothing else, which is what stops it being theatre.

**Swap the OMIE columns.** `orient_omie_columns` decides which column is
Portugal by fitting both assignments. Feed it a frame with the columns reversed
and it returns the other pair. On a fully coupled day the two are identical and
the question has no answer, which is why the orientation is only meaningful once
there is a split in the window.

**Ask the workbench agent to create the same alert twice.** The unique
constraint on `(created_by, zone, direction, threshold_eur_mwh)` turns the
second into an update rather than a duplicate row. A person who did not see the
first confirmation, or a retry, must not leave two rows behind.

**Give it a threshold of 30000.** The `CHECK` rejects it: the upper bound is the
market price cap, so a typo of an extra zero never reaches the table. The
rejection is recorded in `agent_actions` with status `rejected`, which is a
different state from `error`.

## Changing the analysis

**Severity bands** live in `config.py` as `SEVERITY_BANDS`. The current 5 and 20
EUR/MWh cuts are round numbers, not calibrated. With a year of data, look at the
distribution of `abs_premium_eur_mwh` and set them on percentiles instead.

**Saturation threshold** is `SATURATION_THRESHOLD` in
`analysis/interconnection.py`, currently 0.98. Raise it to 1.0 and see how many
episodes stop being explained; the published capacity and the schedule are
rounded independently, which is why it is not 1.0.

**The split threshold** is 0.01 EUR/MWh, and [ROADMAP.md](ROADMAP.md) quantifies
exactly what ignoring it costs against REE's published figures. Do not lower it
without reading that section.

**The baseline window** is the `observations` widget on `01e`, currently 30
days, and the `looks_offline` cut is the two `offline_*` widgets beside it.
Changing the window changes how many hours have a baseline at all: the first
`observations` days of the history have none.

**Weather locations** are in `ingestion/open_meteo.py`. They are chosen for what
drives the price rather than where people live, and the reasoning is in the
comment next to each one. Adding one changes the column set of
`gold_weather_context`, whose schema is built from `LOCATIONS`, so the pipeline
follows automatically.

**The explanation agent's rules** are `SYSTEM_PROMPT` in `agent/explain.py`,
ordered by importance. Changing them changes what the model writes but not what
it is allowed to write: that is `agent/verify.py`, and the prompt is not what
makes the guarantee.

**The workbench agent's rules** are the system prompt in `app/assistant.py`, and
the same split applies. What it is allowed to write is `app/actions.py` and the
`CHECK` constraints in `sql/001_application_tables.sql`, not the prompt.

**The budget** is `QUESTIONS_PER_SESSION` and `QUESTIONS_PER_HOUR` in
`app/limits.py`.

After changing any of these, run the tests. If nothing fails, the change was not
covered, and that is worth a new test rather than a shrug.

## The weather result, and why the obvious version was wrong

Correlating Spanish solar radiation against the Spanish price across all hours
gives about -0.79 in Andalusia, which looks like a strong finding and is mostly
an artefact. Radiation and price both move with time of day, so the correlation
is largely measuring the clock.

`within_hour_correlation` compares observations within the same local hour
instead, which removes it. The effect drops to about -0.14. The hour by hour
table is more informative than either single number: near zero around midday,
when the price is already at 22 to 25 EUR/MWh and cannot fall much further, and
about -0.55 at 20h local, when the price is near 190 and a cloudy evening costs
real money.

The honest reading is a price floor effect rather than a linear relationship,
and the Portuguese locations correlate closely enough with the Spanish ones that
correlation alone cannot single out Andalusia as the driver.
