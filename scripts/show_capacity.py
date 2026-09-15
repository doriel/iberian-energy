"""Plot the day-ahead capacity curve next to the splits, as text.

The saturation table says the border was full. This says how big the border
was, which is the more interesting question: a border full at 4815 MW is a
busy day, a border full at 434 MW is an incident.

    python scripts/show_capacity.py --start 2026-09-03 --days 2
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.interconnection import build_border_series  # noqa: E402
from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    flag_decoupling,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.border import fetch_border  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402
from iberian.parsing.entsoe_prices import parse_prices_response, to_records  # noqa: E402

BAR_WIDTH = 40


def bar(value: float, peak: float) -> str:
    if peak <= 0 or pd.isna(value):
        return ""
    filled = int(round(BAR_WIDTH * value / peak))
    return "#" * filled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    start_day = date.fromisoformat(args.start)
    wanted = {start_day + timedelta(days=i) for i in range(args.days)}
    start_utc, end_utc = market_day_range(start_day, args.days)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    records: list[dict] = []
    for eic in (EIC_PORTUGAL, EIC_SPAIN):
        response = client.day_ahead_prices(eic, start_utc, end_utc)
        points = parse_prices_response(response, eic)
        records.extend(r for r in to_records(points) if r["market_day"] in wanted)

    schedules, capacity = fetch_border(
        client, start_utc, end_utc, Path(args.raw_dir), start_day, verbose=False
    )

    flagged = flag_decoupling(
        build_spread_series(pd.DataFrame(records), EIC_PORTUGAL, EIC_SPAIN)
    )
    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))
    joined = flagged.merge(border, on="ts_utc", how="left")

    # One row per hour keeps it readable: capacity is published hourly anyway.
    hourly = (
        joined.set_index("ts_utc")
        .resample("1h")
        .agg(
            capacity_mw=("capacity_mw", "max"),
            net_flow_mw=("net_flow_mw", "max"),
            utilisation=("utilisation", "max"),
            splits=("is_decoupled", "sum"),
            worst_spread=("spread_eur_mwh", lambda s: s.abs().max()),
        )
        .dropna(subset=["capacity_mw"])
    )

    peak = hourly["capacity_mw"].max()
    median = hourly["capacity_mw"].median()
    print(f"Peak capacity ES to PT: {peak:.0f} MW, median {median:.0f} MW\n")
    print(f"{'hour (UTC)':<18}{'capacity':>9}{'flow':>8}{'util':>7}{'split':>7}  curve")

    for ts, row in hourly.iterrows():
        marker = f"{int(row['splits'])}x" if row["splits"] else ""
        util = f"{row['utilisation']:.0%}" if pd.notna(row["utilisation"]) else ""
        print(
            f"{ts:%Y-%m-%d %H:%M}  {row['capacity_mw']:>8.0f}"
            f"{row['net_flow_mw']:>8.0f}{util:>7}{marker:>7}  "
            f"{bar(row['capacity_mw'], peak)}"
        )

    # A collapse relative to the day's own norm is the thing worth explaining.
    reduced = hourly[hourly["capacity_mw"] < 0.5 * median]
    if not reduced.empty:
        print(f"\nHours where capacity fell below half the median ({median / 2:.0f} MW):")
        for ts, row in reduced.iterrows():
            splits = int(row["splits"])
            note = f", {splits} decoupled interval(s)" if splits else ""
            print(f"  {ts:%Y-%m-%d %H:%M}  {row['capacity_mw']:.0f} MW{note}")
        print("\nThese are the hours to explain with outage notices.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
