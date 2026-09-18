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
| ENTSO-E Transparency | XML API | **done** | A44 prices, A09 schedules, A61 capacity, A78 transmission outages, A80 generation outages. Bronze, silver and gold in the pipeline. |
| OMIE | Delimited files | **done** | A second, independent publication of the same day-ahead prices. Which column is Portugal is settled by fit rather than assumed. Feeds `gold_price_source_agreement`. |
| REE / ESIOS | JSON API | **done** | Congestion rent both directions, demand forecast (1775) and actual demand (1293). Feeds `gold_cost_validation`. The demand series are landed and parsed but the forecast error analysis is not written. |
| Open-Meteo | JSON API | **done** | Hourly radiation, wind and temperature at four locations chosen for their effect on price rather than for population. Feeds `gold_weather_context`. |
| ENTSO-E A78 notices | XML, semi-structured | **partial** | Retrieved directly by the agent with a point in time filter. Not yet a table and not yet a vector index. |
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
| Scheduled Job | **not started** | Ingestion then pipeline, as two tasks. Both run on demand today, and the notebook's date window is still a widget rather than derived from the run date. |
| Web service on Render | **partial** | Deployed, with Databricks authorization code sign in working end to end. It serves no data. |
| Lakebase, gold sync, CDF back to Delta | **blocked** | The workspace issues OAuth app integrations rather than service principal secrets. See the auth note below. |
| Databricks Vector Search | **not started** | For the notice text. Retrieval today is a direct A78 query with the point in time filter in Python. |
| Mosaic AI Agent Framework | **partial** | The agent exists and runs, as plain Python against a serving endpoint. Wrapping it in the framework, with tracing and a registered model, has not been done. |
| MLflow | **not started** | |
| Asset Bundles, CI/CD | **not started** | |

### A note on authentication

This workspace issues OAuth app integrations, not service principals with
machine to machine secrets. An app integration only supports the authorization
code flow, so the web service authenticates as whoever signs in and inherits
their permissions.

The consequence shapes the product rather than being a detail of it: anything
behind the sign in requires an account in this Databricks workspace, which none
of the three target users has. Public pages therefore have to be served from
published data rather than from a live query.

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

Three checks. Two are against publishers that share no code with this project,
the third is internal but adversarial by construction. All three are recomputed
by the pipeline rather than by a human running a script.

### Prices, against OMIE

ENTSO-E and OMIE publish the same settled day-ahead prices through entirely
separate channels. Across **6,240 intervals the two agree on every one, with a
largest difference of zero**. Not within the one cent tolerance the check
allows: identical.

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
border. Across 66 market days:

| | |
|---|---|
| This project | 11,518,200 EUR |
| REE congestion rent | 11,518,375 EUR |
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

| Model | Grounded | Notes |
|---|---|---|
| `databricks-claude-haiku-4-5` | 48 of 48 | all on the first attempt |
| `databricks-claude-opus-4-5` | 47 of 48 | one rejection |

Two things in that table are worth more than the percentages.

**The small model is as grounded as the large one.** That is the hypothesis the
design rests on: if the retrieval and the verification do the work, model size
should not matter much. Here it did not matter at all.

**The single rejection is the check doing its job.** Opus wrote "this 45-minute
episode" for an episode of 0.75 hours. The arithmetic is correct and the prompt
forbids it, because a model that computes cannot be distinguished from a model
that computes wrongly without redoing the computation. A reader would have
accepted that sentence without a second thought.

In 96 drafts across two models, neither invented a number.

#### What that result cost, and why it is worth stating

The first run of the full set reported 42 of 48 and 35 of 48. Every one of those
rejections was a defect in the verifier, not in the model:

1. Dates written in prose. "Published on 25 June 2026" was read as the numbers
   25 and 2026.
2. The settlement interval length. Every explanation wants to write "the single
   15 minute interval", and 15 was not in the fact sheet.
3. Digits inside a retrieved asset name. `AT 2 400/220 SRM` is a transformer,
   and 400 and 220 were read as invented measurements.
4. Negative prices written with the typographic minus sign, U+2212, which the
   extractor read as positive.

Each is now a test. The lesson is the one worth carrying: writing a verifier
that never rejects honest text is harder than writing the verifier, and a check
that cries wolf is worse than no check because it trains you to ignore it.

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
   the premium by interval: `gold_daily_profile`, `gold_interval_premium`. Over
   60 market days the worst hour is 10:00 UTC, decoupled in 37% of intervals at
   a mean premium of 12.93 EUR/MWh, which is midday local and coincides with the
   Spanish solar peak.
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
- The verifier has rejected exactly one live draft, and that draft was true. The
  check has never caught an actual invention, because in 96 drafts there was
  none to catch. Its strength against fabrication is demonstrated by its tests
  rather than by use, and that distinction should be made out loud rather than
  left for someone to notice.
- Episode grouping runs under a constant key so the whole series stays in one
  frame. The natural partition is `market_day`, which would split an episode
  running past local midnight in two. At a few thousand rows the constant key
  costs nothing, but the limitation should be understood before changing it.

## Next steps

In priority order. The platform is now the solid part; the remaining risk is in
the layers around it.

1. **A scheduled Job.** Ingestion then pipeline, as two tasks, with the
   notebook's window derived from the run date instead of a widget. Without it
   the platform is on demand rather than operating.
2. **Serve the gold tables from the web service**, from published data rather
   than a live query, given the authentication constraint above. This is the
   part the three personas actually touch, and it currently shows nothing.
3. **MLflow tracing and the Agent Framework wrapper**, so the evaluation runs
   are recorded as experiments rather than as files in a repository.
4. **Vector Search over the notice text**, replacing the direct A78 query. The
   point in time filter has to survive the move, or the evaluation numbers leak
   future information.
5. **Demand forecast error** from the ESIOS series already in silver. This is
   the missing half of persona 3.
6. **Resolve the `Pereiros-Rio Maior 1` question**, since it touches the labels.
7. **REN Datahub** for the Portuguese generation mix.