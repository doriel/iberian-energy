# How to test and play with this

Everything runs locally. Nothing here needs Databricks, and only the ENTSO-E
calls need a credential.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
```

## The mental model

Four layers, and each one is worth poking at separately.

**Raw** (`data/raw/`) holds API payloads byte for byte, exactly as they
arrived. Nothing is interpreted here. If a parser turns out to be wrong, this
is what you reprocess instead of re-hitting a rate limited API.

**Silver** (`data/lakehouse/silver/`) is the raw payloads parsed into tidy
rows, one table per source, no business logic applied.

**Gold** (`data/lakehouse/gold/`) is the three persona tables plus weather
context. This is what an app or an agent reads.

**The analysis modules** (`src/iberian/analysis/`) are pure functions on
dataframes. No network, no credentials, no Databricks. That is what makes the
whole thing testable in under two seconds.

## Start here

```bash
python -m pytest tests/ -q          # 74 tests, no network, ~1 second
python scripts/run_market_splitting.py --demo   # whole pipeline, synthetic data
python scripts/explore.py tables    # what the last pipeline run produced
```

The demo plants two known splits in a synthetic week, so you can verify the
detection by eye before trusting it on real data.

## Exploring what the pipeline built

```bash
python scripts/explore.py profile              # persona 1: when does PT pay?
python scripts/explore.py episodes             # persona 2: duration, cause, cost
python scripts/explore.py day --date 2026-09-03    # just the splits that day
python scripts/explore.py day --date 2026-09-03 --all   # every interval
python scripts/explore.py weather              # does solar move the ES price?
python scripts/explore.py compare              # ENTSO-E against OMIE
```

## Investigating one number

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
```

## Rebuilding

```bash
# One market day, cheap
python scripts/build_medallion.py --start 2026-09-03 --days 1

# A month, for a daily profile that actually means something
python scripts/build_medallion.py --start 2026-08-01 --days 31

# Skip a source to see the pipeline degrade gracefully
python scripts/build_medallion.py --start 2026-09-03 --days 1 --skip-weather
```

Seven days is not enough to tell a manufacturer when to run equipment. Thirty
is a minimum, sixty is better. That run costs nothing but time.

## Things worth breaking on purpose

The guards in this codebase exist because each one protects a number that
would otherwise be wrong in a way nobody notices. Watching them fire is the
fastest way to understand what they are for.

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

Without that guard the pivot lines an hourly price up with the first quarter
of the hour and silently drops the other three.

**Feed it duplicate timestamps.** Duplicate the PT row above with a different
price and it refuses rather than picking one. That is the intraday contamination
we hit early on, where one A44 document carried day-ahead and three intraday
auctions stacked on the same timestamps.

**Change the settlement interval.** In `detect_episodes`, pass
`step=pd.Timedelta(hours=1)` against quarter hourly data and watch every
duration inflate by four while separate episodes merge into one. That was a
real bug, and the arithmetic is now inferred from the data instead.

**Move the market day boundary.** Edit `MARKET_TIMEZONE` in `config.py` to
`"UTC"` and re-run `cross_check_prices.py`. The OMIE and ENTSO-E timestamps
stop lining up and the merge collapses, which is exactly how you would discover
the boundary is local midnight in CET and not UTC midnight.

**Turn off the point in time filter** with `--no-point-in-time` on
`explain_interval.py`. Notices published after the interval start appearing in
the explanation. That is the hindsight leak your evaluation numbers depend on
not having.

## Poking at the tables directly

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
current story does not account for, and finding the pattern in them is where
the next real result lives.

## Changing the analysis

**Severity bands** live in `config.py` as `SEVERITY_BANDS`. The current 5 and
20 EUR/MWh cuts are round numbers, not calibrated. Once you have a
month of data, look at the distribution of `abs_premium_eur_mwh` and set them
on percentiles instead.

**Saturation threshold** is `SATURATION_THRESHOLD` in
`analysis/interconnection.py`, currently 0.98. Raise it to 1.0 and see how many
episodes stop being explained; the published capacity and the schedule are
rounded independently, which is why it is not 1.0.

**Weather locations** are in `ingestion/open_meteo.py`. They are chosen for
what drives the price rather than where people live, and the reasoning is in
the comment next to each one.

After changing any of these, run the tests. If nothing fails, the change was
not covered, and that is worth a new test rather than a shrug.
