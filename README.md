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

## Data sources

| Source | Shape | What it provides |
|---|---|---|
| ENTSO-E Transparency | XML API (sometimes ZIP) | A44 prices, A09 scheduled exchanges, A61 day-ahead capacity, A78 transmission outages, A80 generation outages |
| OMIE | Delimited files | `marginalpdbc` day-ahead marginal prices for both zones |
| Open-Meteo | JSON API | Hourly solar radiation, wind and temperature at price relevant locations |

Only ENTSO-E needs a credential.

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
  parsing/
    entsoe_prices.py            A44 prices and quantity series -> tidy rows
    entsoe_outages.py           A78 notices -> capacity curves, point in time filter
  analysis/
    market_splitting.py         decoupling detection + episode grouping
    interconnection.py          capacity/schedule alignment, saturation evidence
  pipeline/
    gold.py                     one gold table per persona
scripts/                        see "Scripts" below
tests/                          synthetic data, no network, no credentials
pipelines/                      reserved for Lakeflow definitions (empty)
data/raw/                       bronze: local stand in for the S3 landing zone
data/lakehouse/silver/          silver: parsed parquet, one table per source
data/lakehouse/gold/            gold: persona tables, what an app or agent reads
```

`data/` is gitignored. A clean checkout rebuilds it with `build_medallion.py`.

## Run it now, without credentials

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                        # 74 tests, about a second
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

### ENTSO-E token

1. Register at https://transparency.entsoe.eu and verify the email.
2. Email `transparency@entsoe.eu` with subject `RESTful API access` and your
   registered email address in the body. Access is granted within three
   working days.
3. Once granted, log in, go to **My Account**, and generate a token.

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
| `build_medallion.py` | Bronze, silver and gold end to end over a date range |
| `explore.py` | Read the lakehouse back: `tables`, `profile`, `episodes`, `day`, `weather`, `compare` |
| `explain_interval.py` | The grounded, fully sourced explanation for one instant (`--at`, `--no-point-in-time`) |
| `analyse_saturation.py` | Does a full border explain the splits across a range? |
| `show_capacity.py` | Capacity curve hour by hour with splits marked |
| `show_window.py` | PT and ES prices side by side from landed XML, no API call |
| `cross_check_prices.py` | ENTSO-E against OMIE, and which OMIE column is Portugal |
| `probe_crossborder.py` | Which cross-border document types return data |
| `probe_transmission.py` | A78 notices on the border and the publication time filter |
| `probe_outages.py` | Structure of an A80 generation outage document |
| `probe_sources.py` | Raw OMIE and Open-Meteo payloads |
| `inspect_document.py` | Structure of one A44 document |

The `probe_` and `inspect_` scripts exist to look at a real payload before
writing a parser against a guess. Example invocations for all of them are in
[GUIDE.md](GUIDE.md).

## What is not here yet

See [ROADMAP.md](ROADMAP.md) for full status. In short:

- REE/ESIOS and REN Datahub clients, and REN/ERSE published announcements
- Delta tables, Unity Catalog, S3 landing zone, and the Lakeflow pipeline
  definitions in `pipelines/`
- Lakebase sync, Vector Search over outage notices, MLflow, Asset Bundles
- The agent, which will phrase what `explain_interval.py` already assembles
- The labelled evaluation set and programmatic numeric hallucination checks
- The Render app that serves it

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
