"""Cross check ENTSO-E against OMIE, and settle the OMIE column order.

Two independent publications of the same day-ahead prices. That buys two
things:

1. A real data quality check. Agreement is evidence the pipeline is sound;
   disagreement is a finding worth reporting rather than a bug to hide.

2. The answer to which OMIE column is Portugal. The file does not say, and both
   columns are identical whenever the market is coupled, which is most of the
   time. Only a decoupled interval can settle it, so this fits both
   assignments and reports which one the data supports.

    python scripts/cross_check_prices.py --date 2026-09-03
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.market_splitting import build_spread_series  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.ingestion.omie import OmieClient, parse_marginalpdbc  # noqa: E402
from iberian.ingestion.omie import to_records as omie_records  # noqa: E402
from iberian.market_time import market_day_window  # noqa: E402
from iberian.parsing.entsoe_prices import parse_prices_response, to_records  # noqa: E402

TOLERANCE = 0.01


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="market day, YYYY-MM-DD")
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    day = date.fromisoformat(args.date)
    start_utc, end_utc = market_day_window(day)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    records: list[dict] = []
    for eic in (EIC_PORTUGAL, EIC_SPAIN):
        response = client.day_ahead_prices(eic, start_utc, end_utc)
        records.extend(
            r
            for r in to_records(parse_prices_response(response, eic))
            if r["market_day"] == day
        )

    entsoe = build_spread_series(pd.DataFrame(records), EIC_PORTUGAL, EIC_SPAIN)
    if entsoe.empty:
        print("No ENTSO-E prices for that market day.")
        return 1

    omie_response = OmieClient().day_ahead_prices(day)
    if omie_response.looks_empty:
        print(f"OMIE returned nothing usable for {day}.")
        return 1

    target = Path(args.raw_dir) / "omie" / f"file_set={omie_response.file_set}"
    target.mkdir(parents=True, exist_ok=True)
    (target / omie_response.filename).write_bytes(omie_response.content)

    omie = pd.DataFrame(omie_records(parse_marginalpdbc(omie_response.text)))
    if omie.empty:
        print("OMIE file parsed to nothing.")
        return 1

    merged = entsoe.merge(omie, on="ts_utc", how="inner")
    print(f"Market day {day}")
    print(f"  ENTSO-E intervals   {len(entsoe)}")
    print(f"  OMIE periods        {len(omie)}")
    print(f"  Matched on ts_utc   {len(merged)}")

    if merged.empty:
        print("\nNo overlap. The two sources disagree on the timestamp mapping,")
        print("which is a real problem to solve before either can be trusted.")
        print(f"  ENTSO-E spans {entsoe['ts_utc'].min()} to {entsoe['ts_utc'].max()}")
        print(f"  OMIE spans    {omie['ts_utc'].min()} to {omie['ts_utc'].max()}")
        return 1

    # Fit both assignments. Whichever has the smaller error is the real one.
    straight = (
        (merged["price_pt"] - merged["price_first_eur_mwh"]).abs().mean()
        + (merged["price_es"] - merged["price_second_eur_mwh"]).abs().mean()
    ) / 2
    swapped = (
        (merged["price_pt"] - merged["price_second_eur_mwh"]).abs().mean()
        + (merged["price_es"] - merged["price_first_eur_mwh"]).abs().mean()
    ) / 2

    print("\nWhich OMIE column is Portugal?")
    print(f"  column 5 = PT, column 6 = ES   mean abs error {straight:>8.4f} EUR/MWh")
    print(f"  column 5 = ES, column 6 = PT   mean abs error {swapped:>8.4f} EUR/MWh")

    decoupled = merged.loc[merged["spread_eur_mwh"].abs() > TOLERANCE]
    if decoupled.empty:
        print("\n  Every interval was coupled, so the columns are identical and")
        print("  this cannot be settled from this day. Re-run on a day with a split.")
        return 1

    print(f"\n  {len(decoupled)} decoupled interval(s) actually carry the signal.")
    if straight < swapped:
        pt_column, es_column = "price_first_eur_mwh", "price_second_eur_mwh"
        print("  Verdict: column 5 is Portugal, column 6 is Spain.")
    else:
        pt_column, es_column = "price_second_eur_mwh", "price_first_eur_mwh"
        print("  Verdict: column 5 is Spain, column 6 is Portugal.")

    sample = decoupled.head(4)
    print(f"\n  {'ts_utc':<17} {'ENTSO PT':>9} {'OMIE PT':>9} {'ENTSO ES':>9} {'OMIE ES':>9}")
    for _, row in sample.iterrows():
        print(
            f"  {row['ts_utc']:%Y-%m-%d %H:%M} {row['price_pt']:>9.2f} "
            f"{row[pt_column]:>9.2f} {row['price_es']:>9.2f} {row[es_column]:>9.2f}"
        )

    merged["pt_diff"] = (merged["price_pt"] - merged[pt_column]).abs()
    merged["es_diff"] = (merged["price_es"] - merged[es_column]).abs()
    disagreements = merged.loc[
        (merged["pt_diff"] > TOLERANCE) | (merged["es_diff"] > TOLERANCE)
    ]

    print("\nData quality: do the two sources agree?")
    agreement = 1 - len(disagreements) / len(merged)
    print(f"  Intervals in agreement within {TOLERANCE} EUR/MWh: {agreement:.1%}")
    print(f"  Worst PT difference  {merged['pt_diff'].max():.4f} EUR/MWh")
    print(f"  Worst ES difference  {merged['es_diff'].max():.4f} EUR/MWh")

    if not disagreements.empty:
        print(f"\n  {len(disagreements)} interval(s) disagree. First few:")
        columns = ["ts_utc", "price_pt", pt_column, "price_es", es_column]
        print(disagreements[columns].head(5).to_string(index=False))
        print("\n  Report this rather than hiding it. Two independent publishers")
        print("  differing on a settled market price is a finding.")
    else:
        print("\n  Full agreement. Two independent sources, identical numbers,")
        print("  which is the evidence that the parsing and the market day")
        print("  arithmetic are both right.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
