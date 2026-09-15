"""The grounded explanation for one interval, with every number sourced.

This is the shape of what the agent will eventually produce, except assembled
in Python so the facts can be checked before a model is allowed near them. The
agent's job is to phrase this, never to compute it.

    python scripts/explain_interval.py --at 2026-09-03T18:00

Every figure printed here comes from a retrieved document. Nothing is inferred.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.interconnection import build_border_series  # noqa: E402
from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    flag_decoupling,
    infer_step,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.border import fetch_border  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import market_day_window, to_market_day  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_outages_response,
)
from iberian.parsing.entsoe_prices import parse_prices_response, to_records  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", required=True, help="UTC instant, e.g. 2026-09-03T18:00")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument(
        "--no-point-in-time",
        action="store_true",
        help="allow notices published after the interval (shows the leak)",
    )
    args = parser.parse_args()

    at = datetime.fromisoformat(args.at).replace(tzinfo=timezone.utc)
    day = to_market_day(at)
    start_utc, end_utc = market_day_window(day)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    records: list[dict] = []
    for eic in (EIC_PORTUGAL, EIC_SPAIN):
        response = client.day_ahead_prices(eic, start_utc, end_utc)
        records.extend(to_records(parse_prices_response(response, eic)))

    prices = pd.DataFrame(records)
    if prices.empty:
        print("No prices for that market day.")
        return 1

    flagged = flag_decoupling(build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN))
    step = infer_step(flagged)

    schedules, capacity = fetch_border(
        client, start_utc, end_utc, Path(args.raw_dir), day, verbose=False
    )
    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))
    joined = flagged.merge(border, on="ts_utc", how="left")

    row = joined.loc[joined["ts_utc"] == at]
    if row.empty:
        print(f"No interval at {at:%Y-%m-%d %H:%M}Z. Market day {day} covers")
        print(f"{joined['ts_utc'].min()} to {joined['ts_utc'].max()}.")
        return 1
    row = row.iloc[0]

    interval_end = at + step

    print("=" * 68)
    print(f"Interval {at:%Y-%m-%d %H:%M}Z to {interval_end:%H:%M}Z (market day {day})")
    print("=" * 68)

    print("\nPrices [ENTSO-E A44 day-ahead]")
    print(f"  Portugal            {row['price_pt']:>10.2f} EUR/MWh")
    print(f"  Spain               {row['price_es']:>10.2f} EUR/MWh")
    print(f"  Spread              {row['spread_eur_mwh']:>+10.2f} EUR/MWh")
    verdict = "DECOUPLED" if row["is_decoupled"] else "coupled"
    if row["is_decoupled"]:
        print(f"  Status              {verdict}, {row['premium_side']} pays the premium")
    else:
        print(f"  Status              {verdict}")

    print("\nInterconnection [A09 scheduled exchanges, A61 day-ahead capacity]")
    if pd.isna(row.get("capacity_mw")):
        print("  No capacity published for this interval.")
    else:
        print(f"  Net flow ES to PT   {row['net_flow_mw']:>10.0f} MW")
        print(f"  Capacity            {row['capacity_mw']:>10.0f} MW")
        print(f"  Utilisation         {row['utilisation']:>10.1%}")
        state = "FULL" if row["is_saturated"] else "headroom available"
        print(f"  Border              {state}")

    cutoff = None if args.no_point_in_time else at
    print("\nConstrained assets [A78 transmission unavailability]")
    if cutoff:
        print(f"  Only notices published before {cutoff:%Y-%m-%d %H:%M}Z are used.")
    else:
        print("  POINT IN TIME FILTER OFF: later notices included, for comparison.")

    # Only the direction that carries the premium. A constraint the other way
    # explains nothing about why Spanish power could not reach Portugal.
    direction = (EIC_SPAIN, EIC_PORTUGAL)
    response = client.transmission_unavailability(*direction, start_utc, end_utc)
    curves = [] if response.is_empty else parse_outages_response(response)

    assets = binding_assets(
        curves, at, interval_end, published_before=cutoff, direction=direction
    )
    if not assets:
        print("  No transmission notice covers this interval in this direction.")
    else:
        print(f"  {len(assets)} asset(s) constrained ES to PT, tightest first:\n")
        for entry in assets[:8]:
            published = (
                f"{entry['published_at']:%Y-%m-%d}"
                if entry["published_at"]
                else "unknown"
            )
            print(
                f"    {entry['available_mw']:>7.0f} MW  {entry['asset']:<28} "
                f"{entry['status']:<10} published {published}"
            )

    # The honest bit. A78 is asset level, A61 is the net border figure after
    # the operator's security assessment. They are related but not the same
    # number, and pretending otherwise is how an explanation gets taken apart.
    if assets and not pd.isna(row.get("capacity_mw")):
        tightest = assets[0]["available_mw"]
        gap = tightest - row["capacity_mw"]
        print("\nAttribution gap")
        print(f"  Tightest published notice   {tightest:>10.0f} MW")
        print(f"  Border capacity (A61)       {row['capacity_mw']:>10.0f} MW")
        print(f"  Unexplained by notices      {gap:>10.0f} MW")
        if gap > 0:
            print("  The notices account for part of the restriction. The rest is")
            print("  the operator's security margin and network limits, which are")
            print("  not published as unavailability notices. State this as a")
            print("  limitation rather than claiming the notices explain the number.")

    print("\nWhat can be claimed from the above")
    if row["is_decoupled"] and not pd.isna(row.get("capacity_mw")):
        if row["is_saturated"]:
            print("  The zones priced apart while the border was at its published")
            print("  limit, with named assets under notice in that direction.")
            print("  Every figure is retrieved, not inferred. What cannot be")
            print("  claimed is that those notices are the whole cause.")
        else:
            print("  The zones priced apart with headroom on the border. Saturation")
            print("  does NOT explain this one, and that is worth investigating.")
    elif not row["is_decoupled"]:
        print("  Prices were coupled. Nothing to explain.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
