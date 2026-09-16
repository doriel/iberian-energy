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
| REE / ESIOS | JSON API | **done** | Day-ahead congestion rent on the Portuguese border, which is what validates this project's cost figure. Demand forecast and actual demand are ingested for forecast error but not yet analysed. |
| REN Datahub | API / files | **not started** | Portuguese generation mix. Open access. |
| REN / ERSE announcements | Unstructured text | **not started** | The narrative evidence layer. A78 notices partly cover this. |

## Platform and architecture

| Component | Status | Notes |
|---|---|---|
| Medallion bronze / silver / gold | **done** | Runs locally as parquet and on Databricks as Delta, from the same modules. Raw payloads land byte for byte, so a parser fix reprocesses stored bytes rather than re-calling a rate limited API. |
| Raw landing zone | **done** | A Unity Catalog Volume on Databricks, a local directory with the same partitioning for development. S3 is no longer needed as a separate step. |
| Delta and Unity Catalog | **done** | Writes are idempotent per market day via `replaceWhere`, which makes a backfill safe to repeat. |
| Databricks Git folder | **done** | The notebook imports `src/iberian/` rather than reimplementing it, so pipeline logic stays covered by the test suite. |
| Web service on Render | **partial** | Deployed, with Databricks authorization code sign in working end to end. It serves no data yet. |
| Lakebase, gold sync, CDF back to Delta | **blocked** | The workspace issues OAuth app integrations rather than service principal secrets, so the app authenticates as the signed in user and cannot act on its own. See the auth note below. |
| Lakeflow declarative pipelines | **not started** | The ingestion and analysis modules import no Databricks, so they run inside a pipeline unchanged. |
| Databricks Vector Search | **not started** | For the unstructured notices. |
| Mosaic AI Agent Framework | **not started** | `scripts/explain_interval.py` is the fact assembly the agent will phrase. |
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

## The analytical core

| Capability | Status |
|---|---|
| Market splitting detection | **done**, with episode grouping and correct quarter hourly arithmetic |
| Interconnection saturation as the mechanism | **done**, verified interval by interval |
| Attribution to named transmission assets | **done**, with the unexplained remainder reported explicitly |
| Point in time correctness | **done**, enforced server side via `periodStartUpdate` and client side in `binding_assets` |
| Cross source price validation | **done**, ENTSO-E against OMIE |
| Cost figure validated against the system operator | **done**, see below |
| Weather effect, controlled for time of day | **done** |
| Demand forecast error | **partial**, ingestion written, analysis not yet run |
| Agent receives retrieved facts only | **by design**, the assembly is in Python and produces no numbers of its own |
| Numeric hallucination checked programmatically | **not started** |
| North star metric measured | **not started**, needs the labelled evaluation set |
| ~100 hand labelled outage notices | **not started** |
| Historical price spikes with known causes | **not started** |

## Validated results

Two checks, both against publishers that share no code with this project.

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

## Three personas, three gold tables

Every gold table must serve one of these users. A table nobody needs can be
cut; a user with no table is a gap in the product.

1. **Manufacturer deciding when to run equipment.** Needs the daily profile and
   the premium by interval. Over 60 market days the worst hour is 10:00 UTC,
   decoupled in 37% of intervals at a mean premium of 12.93 EUR/MWh, which is
   midday local time and coincides with the Spanish solar peak.
2. **Journalist or regulator watcher needing a defensible number with a cause.**
   Needs the episode table plus the attribution, including the gap the notices
   do not explain, and now the validation against REE above.
3. **Grid analyst tracking forecast error and interconnection saturation.**
   Needs utilisation over time and the A78 curves. Forecast error is the part
   still missing, and the ESIOS ingestion for it exists.

## Next steps

In priority order. The remaining risk is concentrated in the explanation layer,
not in the platform.

1. The labelled evaluation set. Roughly 100 hand labelled outage notices plus
   historical price spikes with known causes. This cannot be automated or
   compressed, and the north star metric cannot be reported without it.
2. The agent, fed by the fact assembly in `scripts/explain_interval.py`.
3. The programmatic check for numeric hallucination.
4. Serving the gold tables from the web service, from published data rather
   than a live query, given the authentication constraint above.
5. Demand forecast error from the ESIOS series already ingested.
6. REN Datahub for the Portuguese generation mix.