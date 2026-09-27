# Roadmap

What is built, what is not, and what the numbers have been checked against.

Status keys: **done**, **partial**, **not started**.

See [README.md](README.md) for what the project is and [GUIDE.md](GUIDE.md) for
how to run it.

## Data sources

The platform combines structured market data with an unstructured evidence
layer, and the sources deliberately differ in shape (XML, JSON, delimited
files, free text), not just in hostname.

| Source | Shape | Status | Notes |
|---|---|---|---|
| ENTSO-E Transparency | XML API | **done** | A44 prices, A09 schedules, A61 capacity and A73 generation per unit, with bronze, silver and gold. A78 transmission notices are their own row below. A80 generation outages are parsed but not used. |
| OMIE | Delimited files | **done** | A second, independent publication of the same day-ahead prices. Which column is Portugal is settled by fit rather than assumed. Feeds `gold_price_source_agreement`. Two days of the year are missing, see below. |
| REE / ESIOS | JSON API | **done** | Congestion rent both directions, demand forecast (1775) and actual demand (1293). Feeds `gold_cost_validation`. The demand series are landed and parsed but the forecast error analysis is not written. |
| Open-Meteo | JSON API | **done** | Hourly radiation, wind and temperature at six locations chosen for their effect on price rather than for population. Feeds `gold_weather_context`. |
| ENTSO-E A78 notices | XML, semi-structured | **done** | Loaded into `gold_transmission_notices` by the `load_notices` task and synced to a vector index. Retrieved with a point in time filter on both paths. |
| ENTSO-E A73 generation per unit | XML API | **done** | A year, backfilled by hand. 2,537,447 silver rows, 192 Spanish units and 73 Portuguese over 364 days, and 2,248,681 rows in `gold_unit_hourly_output`. This is what satisfies Volume. |
| REN Datahub | API / files | **not started** | Portuguese generation mix. Open access. |
| REN / ERSE announcements | Unstructured text | **not started** | A second narrative evidence layer. A78 notices cover the transmission half. |

## Platform and architecture

| Component | Status | Notes |
|---|---|---|
| Medallion bronze / silver / gold | **done** | 18 tables in the declarative pipeline plus five written by notebooks, each exception with a stated reason. Runs locally as parquet and on Databricks as Delta from the same modules. |
| Lakeflow declarative pipelines | **done** | `pipelines/transformations/`. Bronze and silver are streaming tables over the Volume with Auto Loader, gold is materialized views. The pipeline file contains no transformation of its own. |
| Raw landing zone | **done** | A Unity Catalog Volume with the same directory layout as the local `data/raw/`, which is what let a local backfill be copied up and read without translation. |
| Delta and Unity Catalog | **done** | The pipeline owns every table it created. The ingestion notebook writes none. |
| Databricks Git folder | **done** | Notebooks and pipeline both import `src/iberian/`, so the logic stays covered by the test suite. |
| Secrets | **done** | Every token in a scope, set into the environment at the notebook boundary, so the modules keep one authentication path across a laptop and the workspace. |
| Foundation Model serving | **done** | Both agents query a serving endpoint through the SDK, with the endpoint as an argument so models can be compared. |
| Incremental ingestion | **done** | Auto Loader reads only unseen files. Republished documents are resolved by publication time. |
| Scheduled Job | **done** | `iberian-daily`, five tasks, 16:00 Europe/Lisbon: `ingest`, `load_notices`, `transform`, `explain`, `publish`. Retries differ per task and the reasons are in the YAML. |
| Historical backfill | **done** | `01f_backfill_market_history`, chunked, resumable, separate from the daily Job on purpose. 365 days landed: 499 files, 65 MB, about 12 minutes. |
| Explanation generation in the Job | **done** | `02_explain_episodes`. What makes the north star's "within fifteen minutes of publication" a property of the system rather than of somebody being at a laptop. Incremental, capped at 25 per run. |
| Agent output as a table | **done** | `gold_episode_explanations`, written by the explain task. The pipeline runs before the explanations exist, so a pipeline-owned copy would always be a day behind. The JSONL in the Volume stays the record of work and the table is overwritten from it on every run. |
| Publishing to the web | **done** | `03_publish_dashboard` builds the JSON from the gold tables and commits it over the GitHub contents API. Render watches the branch, so the commit is the deploy. Nothing is committed when the data has not changed. |
| Web service on Render | **done** | The dashboard at `/`, the workbench at `/workbench`, and the Databricks authorization code flow at `/auth`. |
| **Lakebase** | **done** | Four application tables applied from `sql/001_application_tables.sql`, a service principal with its own Postgres role and grants, and `04_sync_episodes_to_lakebase` copying gold into `iberian.episodes`. Credentials are generated per connection because they last sixty minutes. |
| **Lakebase change data feed** | **done** | `00d_enable_lakebase_cdf`. Schema level, immutable once created, off the write ahead log, flushed about every fifteen seconds into `lb_*_history` tables in Delta. Public Preview at the time of writing. |
| **Application activity back into Delta** | **done** | `01g_build_application_activity` reads every `lb_*_history` table by name, merges on a watermark, and builds `silver_application_events` and `gold_application_activity`. |
| **An agent that acts** | **done** | `src/iberian/app/assistant.py`, seven tools, three of which write. Every call is recorded in `iberian.agent_actions` with `ok`, `rejected` and `error` kept apart. Validation is in the tool, not the prompt. |
| Databricks Vector Search | **done** | `gold_transmission_notices_index`, Delta Sync with managed `databricks-gte-large-en` embeddings. `notice_retrieval` chooses `direct` or `vector`; production uses `direct` and `99_evaluate_retrieval` measures the other against it. |
| Mosaic AI Agent Framework | **done, not deployed** | `agents/mibel_agent.py` is an MLflow `ResponsesAgent`, registered in Unity Catalog as `bootcamp_students.doriel.mibel_agent`. The library is packaged with it, and that is checked rather than assumed: the registered version is loaded in a separate process outside the repository, and `iberian` has to import from the model's own `code/` directory. Serving it behind an endpoint was left out on purpose; the daily Job calls the same code directly. |
| MLflow tracing | **done** | `agent/tracing.py` resolves `mlflow.trace` once, or a no-op where MLflow is absent, so the library keeps no platform imports. Retrieval, generation and verification appear as nested spans. |
| MLflow experiment tracking | **done** | `agent/experiment.py`. Each evaluation run records endpoint and attempts as parameters, the north star plus five supporting metrics, the explanations file as an artifact, and the failing episodes as a tag. |
| Unity Catalog trace storage | **attempted, not used** | Provisions and binds correctly; nothing exports. See below. |
| Asset Bundles | **done** | `databricks.yml` plus `resources/iberian_job.yml`, bound to the existing job id so the run history survived. The Job is managed by the bundle, so an edit made in the interface is overwritten by the next deploy. |
| CI | **partial** | Tests, an offline check that every `notebook_path` resolves and every bundle variable is declared, and an install of the app's own requirements in a clean environment. It does not deploy: see the auth note. |

### A note on authentication

This workspace does not let a boot camp account create service principals:
`service-principals create` is refused as admin only. What made the Lakebase
half possible was an existing service principal that came with a Databricks App
built earlier in the course, whose OAuth secret can be minted at workspace level
with `service-principal-secrets-proxy`. That is what the deployed application
authenticates as, and it is why `00c_grant_service_principal` exists: being
allowed to authenticate is not the same as being allowed to read a table.

The user facing half is different and unchanged. The workspace issues OAuth app
integrations, which only support the authorization code flow, so anything behind
that sign in requires an account in this Databricks workspace, which none of the
three target users has. Public pages are therefore served from published data
rather than from a live query, and that shapes the product rather than being a
detail of it.

The same policy blocks automated deployment. Personal access tokens are disabled
for this workspace, so continuous integration has no way to authenticate to
Databricks. It costs less than it sounds: the Job is configured with
`git_source` and snapshots the branch on every run, so a `git push` already
changes the code that runs in production. CI therefore checks and does not
deploy, and `databricks bundle deploy -t prod`, which only changes the Job
definition, stays a deliberate manual step.

A second, unrelated trap sits next to this one. The Databricks SDK treats
`DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` as machine to machine
credentials and will use them in preference to a CLI profile, failing with
`invalid_client`. On Render that is exactly what is wanted, so those two carry
the service principal there; the user facing OAuth credentials are named
`APP_OAUTH_CLIENT_ID` and `APP_OAUTH_CLIENT_SECRET`, and the two pairs must
never share values.

## The analytical core

| Capability | Status |
|---|---|
| Market splitting detection | **done**, with episode grouping and correct arithmetic across the PT60M to PT15M change |
| Interconnection saturation as the mechanism | **done**, verified interval by interval |
| Attribution to named transmission assets | **done**, with the unexplained remainder reported explicitly |
| Point in time correctness | **done and measured**. 0 future notices retrieved with the filter, 1,151 without it across 277 of 377 episodes. See below |
| Cross source price validation | **done**, as a gold table, not a script |
| Cost figure validated against the system operator | **done**, as a gold table, rent against rent, over 366 days |
| A generation unit against its own baseline | **done**, `gold_unit_hourly_output`, 30 day median of the same local hour |
| Weather effect, controlled for time of day | **done**, and the naive version was wrong |
| Demand forecast error | **partial**, the series are in silver, the analysis is not written |
| Agent receives retrieved facts only | **done**, `agent/facts.py` assembles named, sourced facts and the model never sees market data |
| Numeric hallucination checked programmatically | **done**, `agent/verify.py`, with an unverified answer never returned |
| Human labelled episodes | **done**, 48 |
| North star metric measured | **done** for groundedness, over the full labelled set and two models |
| ~100 hand labelled outage notices | **not started** |

## Validated results

### Prices, against OMIE

ENTSO-E and OMIE publish the same settled day-ahead prices through entirely
separate channels. The two agree on every interval compared, with a largest
difference of zero. Not within the one cent tolerance the check allows:
identical. `gold_price_source_agreement` carries the current figures and the
live dashboard shows them.

That is a stronger result than it first sounds. The Iberian market day runs from
local midnight in CET rather than UTC midnight, the settlement resolution
changed from PT60M to PT15M on 1 October 2025, and ENTSO-E omits a repeated
value from its XML rather than publishing it twice. An error in the market day
boundary, the interval grid or the sparse Point handling would misalign the two
series and appear here at once.

The table keeps every compared interval with both prices and the difference, so
a future disagreement is visible as a row rather than as a failed assertion. The
expectation on `agrees` records rather than drops, because a disagreement is a
finding to report and not a reason to withhold data.

**Two days are missing from the OMIE side**, 2025-10-30 and 2025-11-27. Two
retries returned the same 404, so it is not intermittent. The unconfirmed
hypothesis is that the client always requests the `.1` version suffix and OMIE
numbers republications `.2`, `.3`, so a withdrawn `.1` fails permanently.
ENTSO-E covers both days, so the loss is the independent cross-source check on
two days out of the year, not the prices themselves.

### Cost, against REE, and the correction that got there

**Over 366 market days the two figures agree to -0.0042%.**

REE publishes the congestion rent on the Spain to Portugal border as ESIOS
indicator 599. `gold_split_episodes.congestion_rent_eur` is this project's
equivalent: the absolute spread multiplied by the energy that crossed the
border, in either direction, while the zones priced apart.

An earlier version of this comparison was wrong, and the way it was wrong is
worth keeping.

It compared REE's rent against `extra_cost_eur`, which multiplies the spread by
the imported energy only, clipping the flow at zero. That is the right number
for "what did Portugal pay", and the wrong number for "does this match REE".
Over a 70 day summer window the two agreed to 0.0015%, which looked like a
strong validation. It was an artifact: across that window the flow was one
directional, so clipping removed nothing. A year of data included days flowing
the other way, and the same comparison came out at **-21.2%**.

The fix was not to change the cost figure but to carry both quantities.
`congestion_rent_eur` counts both directions and is what the validation
compares. `extra_cost_eur` counts the import direction and is what the
journalist persona is shown. They answer different questions and the tables now
say which is which.

The lesson, recorded because it is the useful part: **a validation that agrees
on a narrow window has not been validated.** The 0.0015% number was not a
measurement of correctness, it was a measurement of the window.

This was never an independent measurement in the first place, since both series
descend from the same market clearing: in implicit coupling the allocated
capacity is the scheduled exchange. It is a check on the implementation, and a
demanding one. The market day boundary in local CET, the interval grid on both
sides of the resolution change, the forward fill of sparse Points, the direction
of flow across the border and the sign of the spread all have to be correct for
the figures to agree.

The residual is accounted for. On most days the agreement is exact. On the rest,
the price spread equals the 0.01 EUR/MWh threshold below which this project does
not count a split. That threshold is deliberate and is not being changed: one
cent per MWh is market rounding, and counting it would inflate the episode count
with events no manufacturer or journalist would recognise as events.

It also shows why saturation and price separation need separate detectors. On
those days the border was full and the prices separated by the minimum tick: a
real constraint with no economic consequence. The saturation flag derives from
utilisation against capacity rather than from the spread, so it registers the
day regardless.

`gold_cost_validation` keeps a row per market day including days where one side
published and the other did not, because a day REE recorded rent for and this
project found no episode on is exactly the kind of gap an inner join would
delete. A zero is written only where the episode count is genuinely zero; where
REE's figure is missing for some of a day's episodes the row is null rather than
silently short.

### Explanations, against the retrieved evidence

Every figure in a generated explanation is checked against the set of values
that were actually retrieved, every date against the evidence, and an
explanation that fails is not returned. Over all 48 labelled episodes:

| Model | Grounded | Passed first attempt | Verifier |
|---|---|---|---|
| `databricks-claude-haiku-4-5` | 48 of 48 | 45 | current, including the date check |
| `databricks-claude-opus-4-5` | 48 of 48 | 48 | current, including the date check |

**Same rules, same result, different route.** Both models reach 48 of 48. The
difference is the three retries: Haiku wrote the wrong day three times, the
retry caught all three, and Opus never made the slip. What reaches a reader is
the same. That is the hypothesis the design rests on, that retrieval and
verification do the work and model size matters little, now shown on two models
under identical rules rather than argued.

**No model has invented a number.** Across roughly a hundred drafts and two
models, the numeric check has never rejected a figure that turned out to be
fabricated. Every numeric rejection it has ever produced on live text was a
defect in the check, five of them, listed below.

**The three retries were all the same error, and it was a real one.** Three
explanations stated a day that was not the episode's. A reader checking one
claim would check that one, because a date is the only figure in the sentence
they can verify without the data. The retry, told which date was wrong and which
were permitted, corrected all three.

#### The verifier's own defect record

This is kept in full because the pattern is the finding. The first run of the
full set reported 42 of 48 and 35 of 48, and every one of those rejections was
the check being wrong, not the model:

1. Dates written in prose. "Published on 25 June 2026" was read as the numbers
   25 and 2026.
2. The settlement interval length. Every explanation wants to write "the single
   15 minute interval", and 15 was not in the fact sheet.
3. Digits inside a retrieved asset name. `AT 2 400/220 SRM` is a transformer,
   and 400 and 220 were read as invented measurements.
4. Negative prices written with the typographic minus sign, U+2212, which the
   extractor read as positive.
5. Unit conversions. A duration retrieved as 0.5 hours, written as "the 30
   minute window", was rejected. The figure came out of a document and the
   arithmetic is fixed by the unit, so the check can and now does redo it.

Defect 5 is worth singling out, because an earlier version of this document
presented Opus's "this 45-minute episode" for a 0.75 hour episode as the check
working correctly, and argued that a model which computes cannot be
distinguished from one which computes wrongly without redoing the computation.
That argument was wrong: the verifier can redo a unit conversion exactly and
cheaply, and it now does. The showcase catch was a sixth false positive.

Fixing defect 1 caused defect 6 by omission. Masking prose dates so they would
not be read as numbers left the dates themselves entirely unchecked, which is
how three wrong days reached the output. The date check is the fix, and it is
the only check here that has ever caught a real error.

Each defect is now a test. Two lessons, and the second is the uncomfortable one.
Writing a verifier that never rejects honest text is harder than writing the
verifier, and a check that cries wolf trains you to ignore it. And a check
narrowed to stop false positives can silently stop checking: the fix to defect 1
removed a whole class of error from view, and nothing failed to announce it.

### Retrieval, against itself with the filter turned off

Run on 27 September 2026 over the 377 episodes that carry an explanation, with
no days unavailable. Written to `gold_retrieval_evaluation`, one row per
episode.

| | |
|---|---|
| Agreement on the tightest asset, direct against vector | **375 of 377** |
| Future notices retrieved **with** the publication filter | **0** |
| Future notices retrieved **without** it | **1,151** |
| Episodes that had at least one to exclude | **277 of 377** |

**The second number alone would be worthless.** A filter that excludes nothing
also reports zero future notices, and a project that printed only that figure
would be claiming point in time correctness on the strength of a measurement
that cannot fail. The unfiltered count is printed beside it for that reason, and
it is large: 277 of 377 episodes, nearly three quarters, had at least one notice
published after they began that would have been retrieved as evidence for what
caused them. Every one of those would have been hindsight presented as
explanation, and the accuracy numbers built on them would have been inflated by
information from the future.

**The two disagreements are not yet explained.** Two episodes out of 377 return
a different tightest asset from the two paths. That is 0.5% and it does not
change the headline, but it is unresolved rather than understood, and the rows
are in the table for whoever looks next.

**What this does not measure.** Whether the retrieved asset is the right cause.
That is the labelled evaluation set, a different question with a human in it.
This asks only whether two mechanisms agree and whether one of them can see the
future.

### A generation unit against its own history

`gold_unit_hourly_output` compares every unit hour against the median of the
same local hour over the 30 prior days.

**Vandellos II**, a Spanish nuclear station, sits at 0 MW against a 1,042.4 MW
baseline for several consecutive days in March 2026. That is a real refuelling
outage, found without being told what to look for, and it is the clearest single
piece of evidence that the baseline works.

Four qualifications, all of them in the table's own column comments:

**`looks_offline` is a signal, not a finding.** It does not distinguish a forced
outage from a plant that simply was not dispatched. The largest slice is Spanish
CCGTs, 68 units, roughly 39 idle days each per year. 8.80% of hours with a
baseline carry the flag. The agent must corroborate against an A80 notice before
attributing a price move to an outage.

**The first 30 days have no baseline**, so the usable analytical window is 334
days rather than 364.

**Provenance is per row.** Only 3% of offline hours have a zero published in
that hour, 5,358 of 179,869. That is the A03 encoding rather than weak evidence:
a stopped plant publishes one zero and it holds until the next position.
`published_at_utc` and `source_block_minutes` carry this on every row.

**705 Spanish rows are hourly** among 2.19 million quarter hourly ones. A join
that assumes Spain is always PT15M is wrong in silence, and
`resolution_minutes` is the guard.

### The hourly concentration, checked rather than assumed

Decoupling is not spread across the day. It concentrates in the middle of it,
and a concentration that sharp is worth being suspicious of before presenting
it, because a data fault and a market pattern look identical in a bar chart.

`scripts/check_hourly_shape.py` is the check. Over the original 61 market day
summer window:

| UTC hour | Decoupled | Mean utilisation |
|---|---|---|
| 05 | 0.0% | 0.15 |
| 08 | 25.8% | 0.84 |
| 10 | **36.7%** | **0.91** |
| 13 | 11.2% | 0.83 |
| 16 | 1.2% | 0.57 |
| 19 | 0.0% | 0.09 |

Three things had to hold, and did.

**The distribution has tails.** It rises from 0.4% at 06:00 to a peak at 10:00
and decays through the afternoon, rather than starting and stopping. A hard
edged band with exact zeros either side would have been the signature of
something upstream, not of a market.

**The edges move with the calendar.** In local time the band runs 09-17 in July,
08-20 in August and 10-22 in September. Solar noon moves through the season, so
a pattern driven by Spanish solar has to move with it. A pattern pinned to the
same UTC hours all summer could not be the sun, since nothing physical is
anchored to UTC.

**Saturation happens where the prices separate and nowhere else.** Mean
utilisation is 0.15 to 0.27 overnight and peaks at 0.91 in the same hour the
splits peak. Outside the band the highest utilisation reached at all is 0.70:
the border is not full, so there is nothing to decouple the zones. There is no
ceiling short of saturation, which is the shape a generated series has and a
market does not.

Two honest qualifications. That window was one summer, so the figures above
describe summer and the year of history now available has not been re-analysed
against them. And the handful of splits at 17:00, 18:00 and 20:00 UTC, where
utilisation reaches 1.0 with the sun already gone, are the evening demand peak
rather than the solar flood. They are the same measurement of a different
mechanism, and lumping them together would overstate how single-caused the
pattern is.

## Unity Catalog trace storage, tried and set aside

MLflow's current guidance is that traces on Databricks belong in Unity Catalog
Delta tables rather than in the experiment's own store. That was configured, and
it does not work in this workspace. What is recorded here is what was observed,
because the next person to try it deserves the evidence rather than a shrug.

What worked:

- The experiment bound to `UnityCatalog(catalog_name='bootcamp_students',
  schema_name='doriel', table_prefix='<experiment id>')`, confirmed by reading
  `experiment.trace_location` back.
- MLflow provisioned all four tables, `..._otel_spans`, `_otel_logs`,
  `_otel_metrics` and `_otel_annotations`, so the schema ownership, the
  `CREATE TABLE` right and the SQL warehouse were all sufficient.
- The evaluation ran, three explanations, all grounded, and the run itself was
  recorded with its parameters, metrics and artifact.

What did not:

- `SELECT count(*)` on the spans table returns 0, after a run made with the
  warehouse already `RUNNING`.
- `mlflow.search_traces` returns nothing for that experiment.

**The cause is not established.** The first failure came after a run that had to
start a stopped warehouse, which made the serverless starter tier's auto-stop the
obvious suspect. A second run with the warehouse warm exported nothing either, so
that explanation does not hold and no other has been tested. Naming a cause here
would be inventing one.

The project therefore uses the experiment's own trace store, which works. Nothing
depends on the Unity Catalog path: `--trace-catalog` and `--trace-schema` are
optional flags and the default run does not pass them.

This is a limitation rather than a gap. Runs, parameters, metrics and artifacts,
which is what the north star metric needs, are recorded and queryable. Spans are
observability, and losing them costs debugging convenience rather than evidence.

## The weather result, corrected

The naive correlation between Spanish solar radiation and the Spanish price is
about -0.79 in Andalusia. That number is mostly the clock: radiation and price
both follow time of day, so a correlation across all hours measures the shared
trend rather than the effect.

Computed within each local hour, it falls to about -0.14. The hour by hour
breakdown is the real finding: near zero around midday, when the price is
already at 22 to 25 EUR/MWh and has little room to fall, and about -0.55 at 20h
local, when the price is near 190. This reads as a price floor effect rather
than a linear relationship.

One honest limitation. The Portuguese locations track the Spanish ones closely
enough that correlation alone cannot identify Andalusia specifically as the
driver. Saying otherwise would be overclaiming.

## The evaluation set, and what it deliberately is not

48 episodes, all carrying a human label, stratified so that a partially labelled
sheet was still representative while the work was in progress.

The labels are not generated. That constraint is not fussiness: the rule based
classifier already produces a candidate cause for every episode, so a set
labelled by the same system would make the north star metric measure the project
agreeing with itself, and the number would be worthless in exactly the way that
is hardest to detect from the outside.

That has a consequence worth stating plainly rather than hiding. The labeller
saw the candidate cause while labelling, so the labels are anchored to some
degree. The classifier and the human agree on every episode. The correct reading
of that is not "the agent is 100% accurate on cause", it is that cause
attribution on this evidence is close to mechanical, and the part that is not
mechanical is whether the prose stays inside the evidence.

So `explain_episodes.py` reports groundedness, not cause accuracy, and says so
in its own output. Cause accuracy would be measuring the rule based classifier,
which needs no model at all.

**The labelling has now moved into the product.** A label written in the
workbench goes to `iberian.episode_labels`, flows back into Delta through the
change data feed, and lands in `gold_application_activity`. The unique
constraint is per episode per author, so two reviewers may disagree and both
rows survive: inter-rater disagreement is a measurement, not a conflict to
resolve at write time. That is the loop closing, and it is the part of the
architecture that is not a dashboard.

## Three personas, and the tables that serve them

Every gold table must serve one of these users. A table nobody needs can be cut;
a user with no table is a gap in the product.

1. **Manufacturer deciding when to run equipment.** `gold_daily_profile` and
   `gold_interval_premium`. The worst hour in the summer window was 10:00 UTC,
   midday local, decoupled in 36.7% of intervals, with mean utilisation peaking
   in the same hour at 0.91. The concentration is the actionable part of the
   product: an hour that is reliably worse is something a manufacturer can
   schedule around, where a single expensive episode is not. The write action is
   a price alert, created through the agent.
2. **Journalist or regulator watcher needing a defensible number with a cause.**
   `gold_split_episodes` plus the attribution, the gap the notices do not
   explain, `gold_cost_validation`, and `gold_episode_explanations` where every
   figure names the document behind it.
3. **Grid analyst tracking forecast error and interconnection saturation.**
   `gold_interval_premium` for utilisation over time, `gold_unit_hourly_output`
   for a unit against its own baseline, `gold_transmission_notices` for the A78
   curves, and `gold_weather_context`. Forecast error is the part still missing,
   and the ESIOS series for it are in silver. The write action is a label, which
   becomes the evaluation ground truth.

`gold_application_activity` serves none of the three and is not meant to. It is
the platform watching itself: tool success rate, labels per episode, alerts
created.

## Open questions

Things that are known to be unresolved, kept here rather than left implicit.

- The `Pereiros-Rio Maior 1` notice, published 25 June, still appears as binding
  in September. Either the notice has no end date, or the parser is holding it
  open. Several labels carry `medium` confidence because of it, so this affects
  the evaluation set and not only the display.
- A78 notices are asset level while A61 is the net border figure after the
  operator's security assessment. They are related but not the same quantity.
  The fact sheet carries this as a caveat rather than pretending the gap is an
  error to be explained away.
- The numeric check has never caught an actual invention, because in roughly a
  hundred drafts there was none to catch. Its strength against fabrication is
  demonstrated by its tests rather than by use, and that distinction should be
  made out loud rather than left for someone to notice. The date check is the
  exception: it caught three real errors on its first run.
- Two of 377 episodes get a different tightest asset from the direct and the
  vector retrieval paths. 0.5%, unresolved rather than understood. The rows are
  in `gold_retrieval_evaluation`.
- Small rejected values, 0.23 and 0.44 among them, have not been checked for
  whether they are rounding false negatives in the verifier. They may be a
  seventh defect and they have not been looked at.
- Episodes are counted from a spread above 0.01 EUR/MWh, so the labelled set
  includes events of no economic consequence. `2026-08-01T1100` has a premium of
  0.03 EUR/MWh on prices of 0.53 and 0.50. The threshold is right as a physical
  test of decoupling, but the north star speaks of *significant* anomalies, and
  reporting groundedness over the full set mixes in non-events. Reporting it
  over `moderate` and `severe` episodes, with the full set as a secondary
  figure, is the change to consider.
- **The verifier checks values, not what they are attached to.** Models wrote
  "nine notices were in force, published on 13 August", fastening one notice's
  publication date to the count of all of them. The date was in the sheet, so
  it passed. Fixed at the source: the date is now keyed and annotated as
  belonging to the most restrictive notice only. The general limitation stands
  and is worth saying: a check on values cannot catch a true value attached to
  the wrong subject.
- The hourly concentration figures describe one summer. A year of history is now
  available and has not been re-analysed against them.
- Unity Catalog trace storage provisions its tables and binds the experiment,
  and then exports nothing into them. Two runs, one cold warehouse and one warm,
  both produced zero spans. No cause has been established and none should be
  claimed until one is.
- Episode grouping runs under a constant key within each resolution segment, so
  the whole series stays in one frame. The natural partition is `market_day`,
  which would split an episode running past local midnight in two. At this size
  the constant key costs nothing, but the limitation should be understood before
  changing it.
- `04_sync_episodes_to_lakebase` and `01g_build_application_activity` are run by
  hand rather than being tasks of the daily Job. Nothing prevents adding them;
  it was not done.

## What is left

The platform, the delivery path and the loop back from the application all close
end to end. What remains is evidence and presentation, not infrastructure.

### Before submitting

1. **Label 20 to 25 episodes in the workbench**, so the change data feed carries
   real rows rather than test ones and `gold_application_activity` has something
   to aggregate.
2. ~~Run `99_evaluate_retrieval`.~~ **Done**, 27 September. 375 of 377
   agreement, 0 leaked with the filter, 1,151 without it.
3. **The presentation.**

### Not doing, and why

- **Demand forecast error.** The series are in silver and the analysis is not
  written. It is the last table any persona is short of, and it is behind the
  evidence work in the queue.
- **~100 hand labelled outage notices.** Would measure retrieval quality
  directly rather than through the explanations. Real work, not started.
- **Unity Catalog trace storage.** Tried, provisions and binds, exports nothing,
  no cause established. Documented above as a limitation.
- **Serving the explanation agent behind an endpoint.** Registered, packaged and
  verified in a clean process. Deploying it would add a running cost for no
  user, since the daily Job calls the same code directly.
- **REN Datahub** for the Portuguese generation mix, and REN or ERSE
  announcements as a second narrative source.
- **A price forecasting model.** Out of scope by an early decision and the
  decision still holds: it is hard to beat naive baselines and it distracts from
  the explanation layer, which is the part of this project nobody else has.

### Loose ends, none of them blocking

- `agents/mibel_agent` versions 1 and 2 are failed uploads from a missing
  `boto3` and should be deleted. Version 3 is the real one.
- `app/public/data.json` is committed daily and is about 1.8 MB, which is large
  enough that the publish task needs the GitHub blobs API to read it back.
  Trimming what the dashboard actually needs would shrink it.
- An `origin` column on `agent_actions` separating a write the agent made from a
  write the interface made directly.
- `requirements-app.txt` has no trailing newline.
- Lowering the Lakebase `suspend_timeout_duration` after submission, since
  nothing will be reading it.
- Moving the declarative pipeline into the Asset Bundle. It lives in the
  workspace and is referenced by id, which `databricks.yml` already notes as the
  obvious next step.

## Future improvements

Things that are out of scope for the capstone and would be the right next work.

**A lockfile.** `requirements.txt` pins versions but nothing pins the transitive
set. A `pip-compile` output would make a Job environment reproducible rather
than merely specified.

**The declarative pipeline in the bundle.** See above. It is the last piece of
infrastructure that exists because somebody clicked.

**A shared session store.** The workbench keeps its session state and its budget
counters in process memory, which is correct for one instance and wrong for two.
Lakebase is already there and is where they would go.

**Point in time correctness as a test rather than a measurement.**
`99_evaluate_retrieval` measures the leak on real data. A synthetic fixture with
a notice deliberately published after an episode would turn that into something
CI can assert.

**Re-analysing the hourly concentration over the full year.** The summer figures
are the ones quoted everywhere in this document, and a year of data is now
landed.
