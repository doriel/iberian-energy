"""Does interconnection saturation explain the price splits?

Fetches prices, scheduled exchanges and day-ahead capacity for a range of
market days, joins them interval by interval, and reports whether the border
was actually full when the two zones priced apart.

    python scripts/analyse_saturation.py --start 2026-09-03 --days 2
    python scripts/analyse_saturation.py --start 2026-09-03 --days 1 --show-splits
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.interconnection import (  # noqa: E402
    SATURATION_THRESHOLD,
    attach_to_intervals,
    build_border_series,
    saturation_evidence,
)
from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    detect_episodes,
    flag_decoupling,
    infer_step,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.border import fetch_border  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402
from iberian.parsing.entsoe_prices import parse_prices_response, to_records  # noqa: E402


def fetch_prices(client: EntsoeClient, start_utc, end_utc, wanted_days) -> pd.DataFrame:
    records: list[dict] = []
    for label, eic in (("PT", EIC_PORTUGAL), ("ES", EIC_SPAIN)):
        response = client.day_ahead_prices(eic, start_utc, end_utc)
        points = parse_prices_response(response, eic)
        rows = [r for r in to_records(points) if r["market_day"] in wanted_days]
        records.extend(rows)
        print(f"  prices    {label}: {len(rows)} points")
    return pd.DataFrame(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="first market day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument(
        "--show-splits", action="store_true", help="print every decoupled interval"
    )
    args = parser.parse_args()

    start_day = date.fromisoformat(args.start)
    wanted_days = {start_day + timedelta(days=i) for i in range(args.days)}
    start_utc, end_utc = market_day_range(start_day, args.days)
    raw_dir = Path(args.raw_dir)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    print(f"Market days {start_day} to {start_day + timedelta(days=args.days - 1)}")
    print(f"Window {start_utc:%Y-%m-%d %H:%M}Z to {end_utc:%Y-%m-%d %H:%M}Z\n")

    prices = fetch_prices(client, start_utc, end_utc, wanted_days)
    schedules, capacity = fetch_border(
        client, start_utc, end_utc, raw_dir, start_day
    )

    if prices.empty or schedules.empty or capacity.empty:
        print("\nMissing one of prices, schedules or capacity. Cannot join.")
        return 1

    flagged = flag_decoupling(build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN))
    step = infer_step(flagged)

    # Spain to Portugal is the direction that matters for a Portuguese premium:
    # a full border there is what stops cheap Spanish power reaching Portugal.
    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))
    joined = attach_to_intervals(flagged, border)

    missing_capacity = int(joined["capacity_mw"].isna().sum())
    if missing_capacity:
        print(
            f"\nWARNING {missing_capacity} intervals have no capacity published "
            "and are excluded from the evidence table."
        )

    evidence = saturation_evidence(joined)
    print("\n" + "=" * 66)
    print("Does a full border explain the splits?")
    print("=" * 66)

    if not evidence.get("intervals"):
        print("No intervals with both a price spread and a capacity figure.")
        return 1

    print(f"Intervals with both:        {evidence['intervals']}")
    print(f"Decoupled:                  {evidence['decoupled']}")
    print(f"Border at or above {SATURATION_THRESHOLD:.0%}:     {evidence['saturated']}")
    print()
    print(f"Decoupled AND saturated:    {evidence['decoupled_and_saturated']}")
    print(f"Decoupled, NOT saturated:   {evidence['decoupled_not_saturated']}")
    print(f"Saturated, NOT decoupled:   {evidence['saturated_not_decoupled']}")
    print()
    print(
        f"Splits explained by saturation: "
        f"{evidence['share_of_splits_explained']:.0%}"
    )
    mean_split = evidence["mean_utilisation_when_split"]
    mean_coupled = evidence["mean_utilisation_when_coupled"]
    if mean_split is not None:
        print(f"Mean utilisation when split:    {mean_split:.1%}")
    if mean_coupled is not None:
        print(f"Mean utilisation when coupled:  {mean_coupled:.1%}")

    episodes = detect_episodes(flagged, step=step)
    if not episodes.empty:
        print("\nEpisodes:")
        print(episodes.to_string(index=False))

    if args.show_splits:
        columns = [
            "ts_utc",
            "price_pt",
            "price_es",
            "spread_eur_mwh",
            "net_flow_mw",
            "capacity_mw",
            "utilisation",
            "is_saturated",
        ]
        splits = joined.loc[joined["is_decoupled"], columns]
        print("\nEvery decoupled interval:")
        print(splits.to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
