# Iberian Energy Capstone

MIBEL electricity market intelligence: detect market splitting between Portugal
and Spain, and explain why prices moved using grounded evidence.

Portugal and Spain share the MIBEL wholesale market and clear at the same price
whenever the interconnection has room. When it saturates, the zones decouple and
Portugal usually pays the premium. This project detects those episodes, prices
them, attributes them to named transmission assets, and produces an explanation
in prose where every figure is traceable to a published document.

## Why the code is shaped this way

The ingestion, parsing, analysis and agent modules are plain Python with no
Databricks imports. That is deliberate. It means you can iterate on logic in
VS Code in seconds instead of waiting on a cluster, the logic is unit testable,
and the same functions get called from a Lakeflow pipeline without modification.

Bronze stores the raw API payload exactly as returned. Parsing happens on the
bronze to silver hop, so a parsing bug is fixable by replaying what you already
landed rather than re-hitting a rate limited API.

The agent never sees market data. It is handed a fact sheet assembled in Python,
where every value carries the document it came from, and what it writes is
checked against that sheet before anyone reads it. A number that was not
retrieved is rejected, not softened.

## Layout

```
src/iberian/
  config.py                      EIC codes, document types, thresholds, settings
  market_time.py                 the Iberian market day, local midnight in CET
  ingestion/entsoe.py            API client, returns raw XML for bronze
  ingestion/omie.py              delimited day-ahead files, independent channel
  ingestion/esios.py             REE indicators, congestion rent and demand
  ingestion/open_meteo.py        hourly weather at four price relevant locations
  parsing/entsoe_prices.py       A44 XML -> tidy rows (handles sparse Points)
  parsing/entsoe_outages.py      A78/A80 curves, with point in time filtering
  analysis/market_splitting.py   decoupling detection + episode grouping
  analysis/interconnection.py    utilisation and saturation
  analysis/weather.py            within hour of day correlation
  pipeline/                      bronze, silver and gold builders
  agent/facts.py                 retrieved evidence as named, sourced facts
  agent/verify.py                rejects any figure that was not retrieved
  agent/explain.py               generation loop with one corrected retry
scripts/
  build_medallion.py             end to end run over a range of market days
  explore.py                     read the tables without writing code
  explain_interval.py            the evidence behind one moment
  build_evaluation_set.py        the labelling sheet
  label_episodes.py              label episodes in the terminal
  explain_episodes.py            run the agent and report groundedness
tests/                           synthetic data, no network, no credentials
data/raw/                        local stand in for the bronze landing zone
evaluation/                      labelling sheet, vocabulary, agent output
```

## Run it now, without credentials

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
python scripts/run_market_splitting.py --demo
```

134 tests, about a second, no network. The demo plants two known splits in a
synthetic week, so the output is verifiable by eye before real data arrives.

## Run it with real data

1. Get an ENTSO-E security token (see below).
2. `cp .env.example .env` and fill in the token.
3. ```bash
   export $(grep -v '^#' .env | xargs)
   python scripts/build_medallion.py --start 2026-08-01 --days 31
   python scripts/explore.py episodes
   ```

`GUIDE.md` covers the rest: exploring the tables, investigating a single
interval, the labelling workflow, and running the agent.

### ENTSO-E token

1. Register at https://transparency.entsoe.eu and verify the email.
2. Email `transparency@entsoe.eu` with subject `RESTful API access` and your
   registered email address in the body. Access is granted within three
   working days.
3. Once granted, log in, go to **My Account**, and generate a token.

### ESIOS token

Request one from `consultasios@ree.es`. The token is personal to the account it
was issued to. REE's terms require that anything published reads from your own
server rather than from theirs, so the web service must never call ESIOS
directly.

## What the numbers have been checked against

**Prices, against OMIE.** ENTSO-E and OMIE publish the same settled day-ahead
prices through entirely separate channels. Agreement is exact to four decimal
places across 96 intervals.

**Cost, against REE.** Across 60 market days this project computes 11,302,847
EUR of extra import cost against REE's published congestion rent of 11,303,022
EUR, a difference of 0.0015%. Both descend from the same market clearing, so
this is a check on the implementation rather than an independent measurement.
It is still a demanding one, and `ROADMAP.md` explains why and accounts for the
residual in full.

**Explanations, against the retrieved evidence.** Every figure the agent writes
is matched against the set of values that were retrieved. On the first five
labelled episodes, 5 of 5 explanations passed without a retry. That is a small
sample and the verifier has not yet rejected a live draft, so it is reported as
a first result rather than a finding.

## What is not here yet

- Lakeflow declarative pipeline definitions
- Databricks Vector Search over the notice text, with point in time correctness
- Lakebase read models and the change feed back into Delta, blocked on
  authentication (see `ROADMAP.md`)
- The web service serving gold data, currently sign in only
- REN Datahub for the Portuguese generation mix

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

**Episodes break on data gaps.** A missing hour ends an episode. Without that,
a data outage stitches two unrelated splits into one and the duration figure
becomes wrong in a way nobody notices.

**Facts must come from one interval.** The fact sheet takes the prices, the
capacity and the flow from the single worst interval rather than a maximum here
and a minimum there. Mixing them produced evidence that could not be reconciled,
a spread that was not the difference of the two prices and a flow larger than
the capacity, and a model handed that writes something false through no fault of
its own.

**`DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` are reserved names.**
The Databricks SDK picks them up and attempts machine to machine auth, which
overrides your CLI profile and fails. The app's OAuth credentials are therefore
named `APP_OAUTH_CLIENT_ID` and `APP_OAUTH_CLIENT_SECRET`.