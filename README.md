# MIBEL Market Intelligence

MIBEL electricity market intelligence: detect market splitting between Portugal
and Spain, show that the interconnector was full when it happened, attribute
the lost capacity to named transmission assets, and explain why prices moved
using grounded evidence.

Two companion documents go deeper than this one:

- [GUIDE.md](GUIDE.md) is the hands on tour: what to run, what to look at, and
  which guards to break on purpose to see what they protect.
- [ROADMAP.md](ROADMAP.md) shows what is built, what is in progress, and what
  comes next.

## Validation

The cost figure this project reports is checked against the system operator's
own published number.

`gold_split_episodes.extra_cost_eur` is the premium Portugal paid multiplied by
the energy actually imported while the zones priced apart, computed from
ENTSO-E prices and schedules. REE publishes the day-ahead congestion rent on
the same border. Across 60 market days:

| | |
|---|---|
| This project | 11,302,847 EUR |
| REE congestion rent | 11,303,022 EUR |
| Difference | 0.0015% |

Both series descend from the same market clearing, so this is a check on the
implementation rather than an independent measurement. It is a demanding one:
the Iberian market day runs from local midnight in CET rather than UTC, prices
are quarter hourly, ENTSO-E omits repeated values from its series, and the
spread has a direction. Any of those handled wrongly and the figures do not
agree.

The residual 0.0015% is accounted for rather than waved away. See
[ROADMAP.md](ROADMAP.md#validated-results).

Day-ahead prices are separately checked against OMIE, an independent publisher
of the same settled figures, and agree exactly across all intervals.

## Why the code is shaped this way

The ingestion, parsing, analysis and gold modules are plain Python and pandas
with no Databricks imports. That is deliberate. It means you can iterate on
logic in VS Code in seconds instead of waiting on a cluster, the logic is unit
testable, and the same functions get called from a Lakeflow pipeline without
modification.

Bronze stores the raw API payload exactly as returned. Parsing happens on the
bronze to silver hop, so a parsing bug is fixable by replaying what you already
landed rather than re-hitting a rate limited API.

## What it does

1. **Detects splits.** PT and ES clear at the same price under coupling. Any
   interval where they differ by more than a rounding epsilon is decoupled,
   banded by severity, and contiguous intervals are grouped into episodes.
2. **Explains the mechanism.** Scheduled cross-border flow divided by day-ahead
   capacity gives utilisation per interval. A split with a full border is
   explained by saturation; one without is flagged as unexplained.
3. **Attributes the cause.** A78 transmission outage notices are parsed into
   capacity curves per asset, so a capacity collapse can be traced to the line
   that caused it, filtered to notices published before the interval.
4. **Cross checks the price.** OMIE publishes the same day-ahead prices
   independently of ENTSO-E. Agreement is a data quality check; disagreement is
   a finding.
5. **Adds the upstream driver.** Open-Meteo solar, wind and temperature explain
   why Spanish power was cheap enough to import in the first place.
6. **Validates the cost figure.** REE publishes the congestion rent on the same
   border, which is the same economic quantity computed by the operator of the
   interconnector. Agreement is 0.0015% across 60 market days, and the residual
   is accounted for rather than waved away.

## Data sources

| Source | Shape | What it provides |
|---|---|---|
| ENTSO-E Transparency | XML API (sometimes ZIP) | A44 prices, A09 scheduled exchanges, A61 day-ahead capacity, A78 transmission outages, A80 generation outages |
| OMIE | Delimited files | `marginalpdbc` day-ahead marginal prices for both zones |
| Open-Meteo | JSON API | Hourly solar radiation, wind and temperature at price relevant locations |
| REE / ESIOS | JSON API | Day-ahead congestion rent on the PT/ES border, demand forecast and actual demand |

ENTSO-E and ESIOS need a credential. OMIE and Open-Meteo are open.

## Layout

```
src/iberian/
  config.py                     EIC codes, document types, thresholds, settings
  market_time.py                market day windows (CET midnight, not UTC)
  ingestion/
    entsoe.py                   API client, returns raw bytes (XML or ZIP) for bronze
    border.py                   fetch and land PT/ES cross-border series
    omie.py                     OMIE file client and marginalpdbc parser
    open_meteo.py               weather client, locations and hourly parser
    esios.py                    REE indicators: congestion rent, demand forecast
  parsing/
    entsoe_prices.py            A44 prices and quantity series -> tidy rows
    entsoe_outages.py           A78 notices -> capacity curves, point in time filter
  analysis/
    market_splitting.py         decoupling detection + episode grouping
    interconnection.py          capacity/schedule alignment, saturation evidence
    weather.py                  weather against price, controlled for hour of day
  pipeline/
    gold.py                     one gold table per persona
app/                            web service, Databricks sign in
scripts/                        see "Scripts" below
tests/                          synthetic data, no network, no credentials
pipelines/                      Databricks notebook, imported as a Git folder
evaluation/                     hand labelled ground truth for cause attribution
data/raw/                       bronze: raw payloads, mirrors the Unity Catalog Volume
data/lakehouse/silver/          silver: parsed parquet, one table per source
data/lakehouse/gold/            gold: persona tables, what an app or agent reads
```

`data/` is gitignored. A clean checkout rebuilds it with `build_medallion.py`.
`evaluation/` is not: those labels are human judgement and cannot be
regenerated.

## Run it now, without credentials

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                        # 99 tests, about a second
python scripts/run_market_splitting.py --demo
```

The demo plants two known splits in a synthetic week, so the output is
verifiable by eye before real data arrives.

## Run it with real data

1. Get an ENTSO-E security token (see below).
2. `cp .env.example .env` and fill in the token.
3. Load the environment and confirm the token works:
   ```bash
   export $(grep -v '^#' .env | xargs)   # or use your IDE's env file support
   python scripts/check_token.py
   ```
4. Build the medallion and look at what it produced:
   ```bash
   python scripts/build_medallion.py --start 2026-09-01 --days 7
   python scripts/explore.py tables
   ```

`build_medallion.py` fetches whatever is missing from `data/raw/`, parses it to
silver and writes the gold tables. `--skip-weather` and `--skip-omie` leave a
source out. Seven days proves the pipeline; thirty or more is needed before the
daily profile means anything.

`--from-silver` recomputes only the gold tables from what is already on disk,
without calling any API. That is the operation to run after changing analysis
logic: gold is a pure function of silver, so reprocessing sixty days takes
seconds where re-ingesting them takes an hour.

### ENTSO-E token

1. Register at https://transparency.entsoe.eu and verify the email.
2. Email `transparency@entsoe.eu` with subject `RESTful API access` and your
   registered email address in the body. Access is granted within three
   working days.
3. Once granted, log in, go to **My Account**, and generate a token.

### ESIOS token

Email `consultasios@ree.es` with subject `Personal token request`, stating who
you are and what you intend to query. The token is personal, and REE's terms
require that anything published reads from your own server rather than theirs,
which is what the lakehouse here is for.

## Gold tables

Every gold table serves one of three target users.

| Table | Persona | One row per |
|---|---|---|
| `gold_interval_premium` | Manufacturer, grid analyst | settlement interval: prices, premium, severity, flow, capacity, utilisation |
| `gold_daily_profile` | Manufacturer | hour of day: split probability, mean and worst premium, mean capacity |
| `gold_split_episodes` | Journalist or regulator watcher | episode: duration, extra import cost, share of intervals saturated |
| `gold_weather_context` | Journalist, grid analyst | interval, with hourly weather broadcast onto it |

Extra import cost is the premium applied to energy actually imported during the
episode, not to Portuguese demand, which would overstate it wildly.

## Scripts

| Script | Purpose |
|---|---|
| `check_token.py` | One small request to confirm the token and EIC codes |
| `run_market_splitting.py` | Detection only, `--demo` or `--start/--days` |
| `build_medallion.py` | Bronze, silver and gold end to end, or `--from-silver` for gold alone |
| `explore.py` | Read the lakehouse back: `tables`, `profile`, `episodes`, `day`, `weather`, `compare` |
| `explain_interval.py` | The grounded, fully sourced explanation for one instant (`--at`, `--no-point-in-time`) |
| `build_evaluation_set.py` | Assemble the labelling sheet for cause attribution, point in time filtered |
| `check_congestion_rent.py` | The cost figure against REE's published congestion rent |
| `analyse_saturation.py` | Does a full border explain the splits across a range? |
| `show_capacity.py` | Capacity curve hour by hour with splits marked |
| `show_window.py` | PT and ES prices side by side from landed XML, no API call |
| `cross_check_prices.py` | ENTSO-E against OMIE, and which OMIE column is Portugal |
| `check_databricks_auth.py` | Whether a Databricks OAuth credential can get a token, and for which scopes |
| `probe_crossborder.py` | Which cross-border document types return data |
| `probe_transmission.py` | A78 notices on the border and the publication time filter |
| `probe_outages.py` | Structure of an A80 generation outage document |
| `probe_sources.py` | Raw OMIE and Open-Meteo payloads |
| `probe_esios.py` | Search the ESIOS indicator catalogue by name before using an id |
| `inspect_document.py` | Structure of one A44 document |

The `probe_` and `inspect_` scripts exist to look at a real payload before
writing a parser against a guess. Example invocations for all of them are in
[GUIDE.md](GUIDE.md).

## What is not here yet

See [ROADMAP.md](ROADMAP.md) for full status. In short:

- REN Datahub, and REN/ERSE published announcements
- Lakeflow pipeline definitions, Lakebase sync, Vector Search over outage
  notices, MLflow, Asset Bundles
- The agent, which will phrase what `explain_interval.py` already assembles
- The labelled evaluation set and programmatic numeric hallucination checks
- The Render app serving the gold tables, currently deployed with Databricks
  sign in working but no data behind it

## Known gotchas already handled

**Sparse Points.** ENTSO-E omits a `Point` when its value repeats the previous
one. Trusting `len(Points)` gives you a short day and misaligns every timestamp
after the first gap. The parser forward fills to the count implied by the time
interval and resolution.

**Namespace versions.** The document namespace carries a version suffix that
changes between API revisions. The parser matches on local tag names instead of
hardcoding the namespace.

**Empty is not an error.** "No data for that window" comes back as an
`Acknowledgement_MarketDocument` with HTTP 200, not a 404.

**Not every response is XML.** Large responses, outage documents in particular,
arrive as a ZIP archive of many XML files. The client keeps raw bytes and
detects ZIP and gzip, because decoding an archive to text fails as a confusing
parse error at line 1.

**Several markets in one document.** A single A44 document can stack day-ahead
and intraday auctions on the same timestamps. Series are filtered on contract
type, and duplicate timestamps raise rather than silently picking one.

**The market day is CET midnight.** Requesting a UTC calendar day returns two
overlapping market days. Windows are derived from `Europe/Madrid`, which also
gets the 23 and 25 hour clock change days right.

**Quarter hourly prices, hourly capacity.** Prices and schedules are PT15M,
capacity is PT60M. Capacity is aligned onto the quarter hourly grid before
dividing, mixed resolutions between zones raise, and the episode step is
inferred from the data so durations are not inflated by four.

**Episodes break on data gaps.** A missing interval ends an episode. Without
that, a data outage stitches two unrelated splits into one and the duration
figure becomes wrong in a way nobody notices.

**Outage curves are stored as breakpoints.** An A78 notice is a PT1M step
function over the whole outage window. A year long notice would be half a
million rows if expanded, so silver keeps the breakpoints and evaluates them
with a binary search.

**No hindsight in explanations.** Notices published after an interval are
excluded, server side via `periodStartUpdate` and client side in
`binding_assets`. Constraints are also kept per direction, so a PT to ES limit
is never cited as the reason power could not flow ES to PT.

**A one cent spread is not an event.** Intervals where PT and ES differ by the
0.01 EUR/MWh rounding epsilon are treated as coupled. REE's congestion rent
accounting has no such floor, which is the entire residual between the two cost
figures: 175 EUR out of 11.3 million. The threshold is kept, because counting
market rounding as a split would fill the episode table with non events. It is
also why saturation is detected from utilisation rather than from the spread:
on 22 August the border carried 5,400 MW while prices separated by one cent, a
real constraint with no economic consequence.

**Weather correlates with the clock.** Solar radiation and price both follow
the solar cycle, so a raw correlation between them mostly measures the time of
day. The first version of this analysis reported around -0.75 and reported
nearly the same figure for Lisbon as for Andalusia, where the mechanism cannot
apply. `analysis/weather.py` estimates within hour of day instead, in local
time, and reports the naive figure alongside so the size of the confound stays
visible.