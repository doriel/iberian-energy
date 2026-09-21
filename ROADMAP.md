# Roadmap

What is built, what is not, and what the numbers have been checked against.

Status keys: **done**, **partial**, **not started**.

See [README.md](README.md) for what the project is and [GUIDE.md](GUIDE.md) for
how to run it.

## Data sources

The platform combines structured market data with an unstructured evidence
layer, and the sources deliberately differ in shape (XML, JSON, delimited
files, free text), not just in hostname. All four structured sources now flow
through the declarative pipeline end to end.

| Source | Shape | Status | Notes |
|---|---|---|---|
| ENTSO-E Transparency | XML API | **done** | A44 prices, A09 schedules and A61 capacity, with bronze, silver and gold in the pipeline. A78 transmission notices are not landed: they are fetched at explanation time, see the next row. A80 generation outages are parsed but not used. An earlier version of this table said all five were in the pipeline, which was not true. |
| OMIE | Delimited files | **done** | A second, independent publication of the same day-ahead prices. Which column is Portugal is settled by fit rather than assumed. Feeds `gold_price_source_agreement`. |
| REE / ESIOS | JSON API | **done** | Congestion rent both directions, demand forecast (1775) and actual demand (1293). Feeds `gold_cost_validation`. The demand series are landed and parsed but the forecast error analysis is not written. |
| Open-Meteo | JSON API | **done** | Hourly radiation, wind and temperature at six locations chosen for their effect on price rather than for population. Feeds `gold_weather_context`. |
| ENTSO-E A78 notices | XML, semi-structured | **partial** | Retrieved directly by the agent with a point in time filter. `gold_transmission_notices` and its vector index exist, created by `pipelines/00_setup_notice_index.py`, and hold a placeholder until the loading task is written. |
| REN Datahub | API / files | **not started** | Portuguese generation mix. Open access. |
| REN / ERSE announcements | Unstructured text | **not started** | The narrative evidence layer. A78 notices partly cover this. |

## Platform and architecture

| Component | Status | Notes |
|---|---|---|
| Medallion bronze / silver / gold | **done** | 18 tables. Runs locally as parquet and on Databricks as Delta from the same modules. |
| Lakeflow declarative pipelines | **done** | `pipelines/transformations/`. Bronze and silver are streaming tables over the Volume with Auto Loader, gold is materialized views. The pipeline file contains no transformation of its own. |
| Raw landing zone | **done** | A Unity Catalog Volume, with the same directory layout as the local `data/raw/`, which is what let sixty market days be copied up and read without translation. |
| Delta and Unity Catalog | **done** | The pipeline owns every table from bronze onwards. The ingestion notebook writes none. |
| Databricks Git folder | **done** | Notebook and pipeline both import `src/iberian/`, so the logic stays covered by the test suite. |
| Secrets | **done** | Both API tokens in a scope, set into the environment at the notebook boundary, so the modules keep one authentication path across a laptop and the workspace. |
| Foundation Model serving | **done** | The agent queries a serving endpoint through the SDK, with the endpoint as an argument so models can be compared. |
| Incremental ingestion | **done** | Auto Loader reads only unseen files. Republished documents are resolved by publication time. |
| Scheduled Job | **done** | `iberian-daily`, four tasks, 16:00 Europe/Lisbon. Ingest lands a trailing three day window derived from the run date, transform runs the pipeline, explain generates the explanations for episodes that have none, publish commits the dashboard data. |
| Explanation generation in the Job | **done** | `pipelines/02_explain_episodes.py`. This is what makes the north star's "within fifteen minutes of publication" a property of the system rather than of somebody being at a laptop. Incremental: two or three episodes a day, capped at 25 so the first run after a gap cannot become a hundred model calls. |
| Agent output as a table | **done** | `gold_episode_explanations`, written by the explain task rather than by the declarative pipeline. A deliberate exception to "the pipeline owns every table": the pipeline runs before the explanations exist, so a pipeline-owned copy would always be a day behind. The JSONL in the Volume stays the record of work and the table is overwritten from it on every run, so the file is always the side that is right. |
| Publishing to the web | **done** | `pipelines/03_publish_dashboard.py` builds the JSON from the gold tables and commits it over the GitHub contents API. Render watches the branch, so the commit is the deploy. Nothing is committed when the data has not changed. |
| Web service on Render | **done** | The dashboard is served at `/`, from published data, with no sign in. The Databricks authorization code flow still works end to end at `/auth`. |
| Lakebase, gold sync, CDF back to Delta | **blocked** | The workspace issues OAuth app integrations rather than service principal secrets. See the auth note below. |
| Databricks Vector Search | **partial** | Index `gold_transmission_notices_index` on the shared endpoint, Delta Sync with managed `databricks-gte-large-en` embeddings, created early to hold a slot on an endpoint that had been full. A smoke test confirmed the point in time filter: with it, only notices published before the episode are returned; without it, a notice published after the episode was the top result. Loading the real notices and switching retrieval over are still to do. |
| Mosaic AI Agent Framework | **done, not deployed** | `agents/mibel_agent.py` is an MLflow `ResponsesAgent`, the interface Databricks currently recommends, registered in Unity Catalog as `bootcamp_students.doriel.mibel_agent` by `scripts/register_agent.py`. The library is packaged with it, and that is checked rather than assumed: the registered version is loaded in a separate process outside the repository, and `iberian` has to import from the model's own `code/` directory. Serving it behind an endpoint was left out on purpose; the daily Job calls the same code directly, and an endpoint would add a running cost for no user. |
| MLflow tracing | **done** | `agent/tracing.py` resolves `mlflow.trace` once, or a no-op where MLflow is absent, so the library keeps no platform imports and the suite still runs in two seconds. Retrieval, generation and verification appear as nested spans. |
| MLflow experiment tracking | **done** | `agent/experiment.py`. Each evaluation run records endpoint and attempts as parameters, the north star plus five supporting metrics, the explanations file as an artifact, and the failing episodes as a tag. |
| Unity Catalog trace storage | **attempted, not used** | Provisions and binds correctly; nothing exports. See below. |
| Asset Bundles | **done** | `databricks.yml` plus `resources/iberian_job.yml`, bound to the existing job id so the run history survived. The Job is managed by the bundle, so the definition is changed in YAML rather than by clicking: an edit made in the interface is overwritten by the next deploy. |
| CI | **partial** | Tests, an offline check that every `notebook_path` resolves and every bundle variable is declared, and an install of the app's own requirements in a clean environment. It does not deploy: see the auth note. |

### A note on authentication

This workspace issues OAuth app integrations, not service principals with
machine to machine secrets. An app integration only supports the authorization
code flow, so the web service authenticates as whoever signs in and inherits
their permissions.

The consequence shapes the product rather than being a detail of it: anything
behind the sign in requires an account in this Databricks workspace, which none
of the three target users has. Public pages therefore have to be served from
published data rather than from a live query.

The same policy blocks automated deployment. Personal access tokens are
disabled for this workspace and no service principal is available, so continuous
integration has no way to authenticate to Databricks. It costs less than it
sounds: the Job is configured with `git_source` and snapshots the branch on
every run, so a `git push` already changes the code that runs in production. CI
therefore checks and does not deploy, and `databricks bundle deploy -t prod`,
which only changes the Job definition, stays a deliberate manual step.

A second, unrelated trap sits next to this one. The Databricks SDK treats
`DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` as machine to machine
credentials and will use them in preference to a CLI profile, failing with
`invalid_client`. The app's own OAuth credentials are therefore named
`APP_OAUTH_CLIENT_ID` and `APP_OAUTH_CLIENT_SECRET`.

## The analytical core

| Capability | Status |
|---|---|
| Market splitting detection | **done**, with episode grouping and correct quarter hourly arithmetic |
| Interconnection saturation as the mechanism | **done**, verified interval by interval |
| Attribution to named transmission assets | **done**, with the unexplained remainder reported explicitly |
| Point in time correctness | **done**, enforced server side via `periodStartUpdate` and client side in `binding_assets` |
| Cross source price validation | **done**, as a gold table, not a script |
| Cost figure validated against the system operator | **done**, as a gold table, not a script |
| Weather effect, controlled for time of day | **done**, and the naive version was wrong |
| Demand forecast error | **partial**, the series are in silver, the analysis is not written |
| Agent receives retrieved facts only | **done**, `agent/facts.py` assembles named, sourced facts and the model never sees market data |
| Numeric hallucination checked programmatically | **done**, `agent/verify.py`, with an unverified answer never returned |
| Human labelled episodes | **done**, all 48 |
| North star metric measured | **done** for groundedness, over the full labelled set and two models |
| ~100 hand labelled outage notices | **not started** |

## Validated results

Four checks. Two are against publishers that share no code with this project,
the third is internal but adversarial by construction, and the fourth asks
whether a striking shape in the data is the market or a fault. The first three
are recomputed by the pipeline on every run rather than by a human remembering
to invoke a script.

### Prices, against OMIE

ENTSO-E and OMIE publish the same settled day-ahead prices through entirely
separate channels. Across the 60 market day window from 2026-07-16 to
2026-09-13, **5,760 intervals, the two agree on every one, with a largest
difference of zero**. Not within the one cent tolerance the check allows:
identical. The live dashboard carries the current figures, which move with each
daily run.

That is a stronger result than it first sounds. The Iberian market day runs from
local midnight in CET rather than UTC midnight, the day is 96 quarter hourly
intervals, and ENTSO-E omits a repeated value from its XML rather than
publishing it twice. An error in the market day boundary, the interval grid or
the sparse Point handling would misalign the two series and appear here at once.

The table keeps every compared interval with both prices and the difference, so
a future disagreement is visible as a row rather than as a failed assertion.
`gold_price_source_agreement` carries an expectation on `agrees` that records
rather than drops, because a disagreement is a finding to report and not a
reason to withhold data.

### Cost, against REE

`gold_split_episodes.extra_cost_eur` is the premium Portugal paid multiplied by
the energy actually imported while the zones priced apart, computed from
ENTSO-E prices and schedules. REE publishes the congestion rent on the same
border. Across the same 60 market days:

| | |
|---|---|
| This project | 11,302,847 EUR |
| REE congestion rent | 11,303,022 EUR |
| Difference | -0.0015% |

This is not an independent measurement, since both series descend from the same
market clearing: in implicit coupling the allocated capacity is the scheduled
exchange. It is a check on the implementation, and a demanding one. The market
day boundary in local CET, the 96 quarter hourly intervals, the forward fill of
sparse Points, the direction of flow across the border and the sign of the
spread all have to be correct for the figures to agree.

The residual is fully accounted for. On most days the agreement is exact. On the
rest, the price spread equals the 0.01 EUR/MWh threshold below which this
project does not count a split. On 22 August, three intervals at 0.01 EUR/MWh
with 5,400 MW crossing the border produce 40 EUR of rent that this project does
not count.

That threshold is deliberate and is not being changed. One cent per MWh is
market rounding, and counting it would inflate the episode count with events no
manufacturer or journalist would recognise as events. The cost of the choice is
now quantified at 0.0015% of the total.

It also shows why saturation and price separation need separate detectors. On
that day the border was full and the prices separated by the minimum tick: a
real constraint with no economic consequence. The saturation flag derives from
utilisation against capacity rather than from the spread, so it registers the
day regardless.

`gold_cost_validation` keeps a row per market day including days where one side
published and the other did not, because a day REE recorded rent for and this
project found no episode on is exactly the kind of gap an inner join would
delete.

### Explanations, against the retrieved evidence

Every figure in a generated explanation is checked against the set of values
that were actually retrieved, and an explanation that fails is not returned.
Over all 48 labelled episodes:

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

### The hourly concentration, checked rather than assumed

Decoupling is not spread across the day. It concentrates in the middle of it,
and a concentration that sharp is worth being suspicious of before presenting
it, because a data fault and a market pattern look identical in a bar chart.

`scripts/check_hourly_shape.py` is the check. Over 5,760 intervals and 61 market
days:

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

Two honest qualifications. September carries thirteen days and 57 episodes, so
that month's edges are individual events rather than a band. And the handful of
splits at 17:00, 18:00 and 20:00 UTC, where utilisation reaches 1.0 with the sun
already gone, are the evening demand peak rather than the solar flood. They are
the same measurement of a different mechanism, and lumping them together would
overstate how single-caused the pattern is.

## Unity Catalog trace storage, tried and set aside

MLflow's current guidance is that traces on Databricks belong in Unity Catalog
Delta tables rather than in the experiment's own store. That was configured, and
it does not work in this workspace. What is recorded here is what was observed,
because the next person to try it deserves the evidence rather than a shrug.

What worked:

- The experiment bound to `UnityCatalog(catalog_name='bootcamp_students',
  schema_name='doriel', table_prefix='2122925066106828')`, confirmed by reading
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
optional flags and the default run does not pass them. The tracing itself is
unaffected either way, since it is the same spans reaching a different
destination.

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

## Three personas, three gold tables

Every gold table must serve one of these users. A table nobody needs can be cut;
a user with no table is a gap in the product.

1. **Manufacturer deciding when to run equipment.** Needs the daily profile and
   the premium by interval: `gold_daily_profile`, `gold_interval_premium`. The
   worst hour is 10:00 UTC, midday local, decoupled in 36.7% of intervals, and
   mean utilisation peaks in the same hour at 0.91. The concentration is real
   and is the actionable part of the product: an hour that is reliably worse is
   something a manufacturer can schedule around, where a single expensive
   episode is not.
2. **Journalist or regulator watcher needing a defensible number with a cause.**
   Needs `gold_split_episodes` plus the attribution, the gap the notices do not
   explain, `gold_cost_validation`, and a written explanation where every figure
   names the document behind it.
3. **Grid analyst tracking forecast error and interconnection saturation.**
   Needs utilisation over time, the A78 curves and `gold_weather_context`.
   Forecast error is the part still missing, and the ESIOS series for it are in
   silver.

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
  hundred drafts there was none to catch. Every numeric rejection it has
  produced on live text was its own defect. Its strength against fabrication is
  demonstrated by its tests rather than by use, and that distinction should be
  made out loud rather than left for someone to notice. The date check is the
  exception: it caught three real errors on its first run.
- Episodes are counted from a spread above 0.01 EUR/MWh, so the labelled set
  includes events of no economic consequence. `2026-08-01T1100` has a premium of
  0.03 EUR/MWh on prices of 0.53 and 0.50. The threshold is right as a physical
  test of decoupling, but the north star speaks of *significant* anomalies, and
  reporting groundedness over the full set mixes in non-events. Reporting it
  over `moderate` and `severe` episodes, with the full set as a secondary
  figure, is the change to consider.
- **Resolved: the unnamed asset is the publisher's, not ours.** The A78
  notice published on 2026-07-17 is an A53 planned maintenance with no asset
  block at all: no registered resource, no name, no identifier. The parser is
  right. The fact sheet now says the operator did not name the asset, instead
  of passing the parser's placeholder to the model as though it were a name.
- **The verifier checks values, not what they are attached to.** Models wrote
  "nine notices were in force, published on 13 August", fastening one notice's
  publication date to the count of all of them. The date was in the sheet, so
  it passed. Fixed at the source: the date is now keyed and annotated as
  belonging to the most restrictive notice only. The general limitation stands
  and is worth saying: a check on values cannot catch a true value attached to
  the wrong subject.
- Unity Catalog trace storage provisions its tables and binds the experiment,
  and then exports nothing into them. Two runs, one cold warehouse and one warm,
  both produced zero spans. No cause has been established and none should be
  claimed until one is.
- Episode grouping runs under a constant key so the whole series stays in one
  frame. The natural partition is `market_day`, which would split an episode
  running past local midnight in two. At a few thousand rows the constant key
  costs nothing, but the limitation should be understood before changing it.

## The plan to 2 October

Twelve days. The platform and the delivery path are the solid parts and the
daily loop closes end to end, so the remaining work is evidence and honesty,
not infrastructure. Ordered by what the presentation cannot go without.

### Must do

1. ~~`gold_episode_explanations` as a Delta table.~~ **Done.** The explain task
   now writes the table alongside the JSONL, with a comment on every column.
2. ~~Re-run Opus over all 48 under the current verifier.~~ **Done.** 48 of 48,
   all on the first attempt.
3. ~~Register the `ResponsesAgent` in Unity Catalog.~~ **Done.** Version 3,
   with the packaged code verified in a clean process. Versions 1 and 2 are
   failed uploads from a missing `boto3`, see GUIDE.
4. **Rehearse the presentation with the warehouse already warm.** A cold start
   in front of an audience reads as the platform being slow.

### Should do, in this order

5. **Report groundedness over `moderate` and `severe` episodes** as the headline
   figure, with the full set secondary. See the open question above.
6. **The `constrained_asset` empty name.** One parser question, and it decides
   whether the journalist persona's evidence names an asset or says "unnamed".
7. **Resolve the `Pereiros-Rio Maior 1` notice**, since it touches the labels
   and therefore the metric.
8. **Demand forecast error** from the ESIOS series already in silver. The
   missing half of persona 3, and the last table any persona is short of.
9. **Vector Search over the notice text**, replacing the direct A78 query. The
   point in time filter has to survive the move or the evaluation numbers leak
   information from the future. This is the largest remaining item and the one
   most likely to be cut; it is listed last on purpose.

### Not doing, and why

- **Lakebase read models and CDF back to Delta.** Blocked on a service
  principal this workspace does not issue. The request is drafted; if it is
  granted in time it unblocks CI deployment too, but nothing is planned around
  it arriving.
- **Unity Catalog trace storage.** Tried, provisions and binds, exports
  nothing, no cause established. Documented above as a limitation.
- **~100 hand labelled outage notices.** Only pays for itself if Vector Search
  lands, and it is behind Vector Search in the queue.
- **REN Datahub** for the Portuguese generation mix.
- **A price forecasting model.** Out of scope by an early decision and the
  decision still holds: it is hard to beat naive baselines and it distracts from
  the explanation layer, which is the part of this project nobody else has.