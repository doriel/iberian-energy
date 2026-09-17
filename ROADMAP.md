# Roadmap

What is built, what is in progress, and what comes next.

Status keys: **done**, **partial**, **not started**.

## Data sources

The platform combines structured market data with an unstructured evidence
layer, and the sources deliberately differ in shape (XML, JSON, delimited
files, free text), not just in hostname.

| Source | Shape | Status | Notes |
|---|---|---|---|
| ENTSO-E Transparency | XML API | **done** | A44 prices, A09 schedules, A61 capacity, A78 transmission outages, A80 generation outages. |
| OMIE | Delimited files | **done** | A second, independent publication of the same day-ahead prices. Column order (which series is Portugal) was settled against ENTSO-E on a decoupled day rather than assumed. Agreement is exact across 96 intervals. |
| Open-Meteo | JSON API | **done** | Hourly radiation, wind and temperature at four locations chosen for their effect on price rather than for population. 60 market days ingested. |
| REE / ESIOS | JSON API | **partial** | Client written and used to validate the cost figure against published congestion rent. Demand forecast (1775) and actual demand (1293) are ingested but not yet in the medallion run and not yet analysed. |
| REN Datahub | API / files | **not started** | Portuguese generation mix. Open access. |
| REN / ERSE announcements | Unstructured text | **not started** | The narrative evidence layer. A78 notices partly cover this. |

## Platform and architecture

| Component | Status | Notes |
|---|---|---|
| Medallion bronze / silver / gold | **done** | Runs locally as parquet and on Databricks as Delta, from the same modules. Raw payloads land byte for byte, so a parser fix reprocesses stored bytes rather than re-calling a rate limited API. One code path builds gold, whether it is reached from a full run or from `--from-silver`. |
| Raw landing zone | **done** | A Unity Catalog Volume on Databricks, a local directory with the same partitioning for development. |
| Delta and Unity Catalog | **done** | Writes are idempotent per market day via `replaceWhere`, which makes a backfill safe to repeat. |
| Databricks Git folder | **done** | The notebook imports `src/iberian/` rather than reimplementing it, so pipeline logic stays covered by the test suite. |
| Foundation Model serving | **done** | The agent queries a Databricks serving endpoint through the SDK, with the endpoint as a command line argument so models can be compared. |
| Web service on Render | **partial** | Deployed, with Databricks authorization code sign in working end to end. It serves no data yet. |
| Lakebase, gold sync, CDF back to Delta | **blocked** | The workspace issues OAuth app integrations rather than service principal secrets, so the app authenticates as the signed in user and cannot act on its own. See the auth note below. |
| Lakeflow declarative pipelines | **not started** | The ingestion and analysis modules import no Databricks, so they run inside a pipeline unchanged. |
| Databricks Vector Search | **not started** | For the unstructured notices. Retrieval today is a direct A78 query with the point in time filter applied in Python. |
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
| Cross source price validation | **done**, ENTSO-E against OMIE |
| Cost figure validated against the system operator | **done**, see below |
| Weather effect, controlled for time of day | **done**, and the naive version was wrong |
| Demand forecast error | **partial**, ingestion written, analysis not yet run |
| Agent receives retrieved facts only | **done**, `agent/facts.py` assembles named, sourced facts and the model never sees the market data |
| Numeric hallucination checked programmatically | **done**, `agent/verify.py`, with an unverified answer never returned |
| Historical price spikes with known causes | **partial**, 40 of 48 episodes labelled |
| ~100 hand labelled outage notices | **not started** |
| North star metric measured | **partial**, groundedness measured on a first sample, see below |

## Validated results

Three checks. Two are against publishers that share no code with this project,
the third is internal but adversarial by construction.

**Prices, against OMIE.** ENTSO-E and OMIE publish the same settled day-ahead
prices through entirely separate channels. Agreement is exact to four decimal
places across 96 intervals, which is evidence that the parsing and the market
day arithmetic are both right. The Iberian market day runs from local midnight
in CET rather than from UTC midnight, and an error there would misalign every
timestamp.

**Cost, against REE.** `gold_split_episodes.extra_cost_eur` is the premium
Portugal paid multiplied by the energy actually imported while the zones priced
apart, computed here from ENTSO-E prices and schedules. REE publishes the
congestion rent on the same border. Across 60 market days:

| | |
|---|---|
| This project | 11,302,847 EUR |
| REE congestion rent | 11,303,022 EUR |
| Difference | 0.0015% |

This is not an independent measurement, since both series descend from the same
market clearing. It is a check on the implementation, and a demanding one: the
market day boundary in local CET, the 96 quarter hourly intervals, the forward
fill of ENTSO-E's sparse Points, the direction of flow across the border and the
sign of the spread would all have to be correct for the figures to agree.

The residual difference is fully accounted for. On 31 of the days the agreement
is exact. On the rest, the price spread equals the 0.01 EUR/MWh threshold below
which this project does not count a split. On 22 August, three intervals at
0.01 EUR/MWh with 5,400 MW crossing the border produce 40 EUR of rent that this
project does not count.

That threshold is deliberate and is not being changed. One cent per MWh is
market rounding, and counting it would inflate the episode count with events no
manufacturer or journalist would recognise as events. The cost of the choice is
now quantified at 0.0015% of the total.

It also shows why saturation and price separation need separate detectors. On
that day the border was full and the prices separated by the minimum tick: a
real constraint with no economic consequence. The saturation flag derives from
utilisation against capacity rather than from the spread, so it registers the
day regardless.

**Explanations, against the retrieved evidence.** Every figure in a generated
explanation is checked against the set of values that were actually retrieved,
and an explanation that fails is not returned. On the first five labelled
episodes, with `databricks-claude-haiku-4-5`, 5 of 5 passed and none needed the
retry, at 7 to 12 numeric claims each.

That result is reported with two caveats attached, because it is too clean to
take at face value. The sample is five. And the verifier has not yet rejected a
live draft, which means the check is either doing nothing or protecting against
something that has not happened yet, and only running the full set against a
second model will tell which. The check does reject in the tests, including the
adversarial cases (a plausible number that was never retrieved, a figure right
to the last decimal with no source named, a rounding that goes past the
precision actually written), so it is not inert.

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

48 episodes, of which 40 carry a human label, stratified so a partially labelled
sheet is still representative of the whole.

The labels are not generated. That constraint is not fussiness: the rule based
classifier already produces a candidate cause for every episode, so a set
labelled by the same system would make the north star metric measure the project
agreeing with itself, and the number would be worthless in exactly the way that
is hardest to detect from the outside.

That has a consequence worth stating plainly rather than hiding. The labeller
saw the candidate cause while labelling, so the labels are anchored to some
degree. On the 40 labelled so far, the classifier and the human agree on every
one. The correct reading of that is not "the agent is 100% accurate on cause",
it is that cause attribution on this evidence is close to mechanical, and the
part that is not mechanical is whether the prose stays inside the evidence.

So `explain_episodes.py` reports groundedness, not cause accuracy, and says so
in its own output. Cause accuracy would be measuring the rule based classifier,
which needs no model at all.

## Three personas, three gold tables

Every gold table must serve one of these users. A table nobody needs can be
cut; a user with no table is a gap in the product.

1. **Manufacturer deciding when to run equipment.** Needs the daily profile and
   the premium by interval. Over 60 market days the worst hour is 10:00 UTC,
   decoupled in 37% of intervals at a mean premium of 12.93 EUR/MWh, which is
   midday local time and coincides with the Spanish solar peak.
2. **Journalist or regulator watcher needing a defensible number with a cause.**
   Needs the episode table plus the attribution, including the gap the notices
   do not explain, the validation against REE above, and now a written
   explanation where every figure names the document behind it.
3. **Grid analyst tracking forecast error and interconnection saturation.**
   Needs utilisation over time and the A78 curves. Forecast error is the part
   still missing, and the ESIOS ingestion for it exists.

## Open questions

Things that are known to be unresolved, kept here rather than left implicit.

- The `Pereiros-Rio Maior 1` notice, published 25 June, still appears as binding
  in September. Either the notice has no end date, or the parser is holding it
  open. Several labels carry `medium` confidence because of it, so this affects
  the evaluation set and not only the display.
- The verifier has never rejected a live draft. Until it does, or until the
  full set runs against a larger model without a rejection either, the strength
  of the check is asserted by its tests rather than demonstrated in use.
- A78 notices are asset level while A61 is the net border figure after the
  operator's security assessment. They are related but not the same quantity.
  The fact sheet carries this as a caveat rather than pretending the gap is an
  error to be explained away.

## Next steps

In priority order. The remaining risk is concentrated in the explanation layer,
not in the platform.

1. Finish the remaining 8 labels, then run the agent over all 48 and compare
   `databricks-claude-haiku-4-5` against `databricks-claude-opus-4-5`. If
   grounding is doing the work, the small model should not be materially worse,
   and that is a result worth reporting either way.
2. Resolve the `Pereiros-Rio Maior 1` notice question, since it touches the
   labels.
3. Serve the gold tables from the web service, from published data rather than
   a live query, given the authentication constraint above.
4. Demand forecast error from the ESIOS series already ingested, and fold the
   ESIOS ingestion into the medallion run rather than leaving it in a validation
   script.
5. Wrap the agent in the Mosaic AI Agent Framework with MLflow tracing, and move
   retrieval to Vector Search over the notice text.
6. REN Datahub for the Portuguese generation mix.