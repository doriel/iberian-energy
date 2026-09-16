"""Poke at the lakehouse without writing code.

Everything the pipeline produced is parquet under data/lakehouse/. This reads
it back and prints the views worth looking at, so you can check the numbers by
eye before trusting them in a presentation.

    python scripts/explore.py tables
    python scripts/explore.py profile
    python scripts/explore.py episodes
    python scripts/explore.py day --date 2026-09-03
    python scripts/explore.py weather
    python scripts/explore.py compare
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)


def load(root: Path, layer: str, name: str) -> pd.DataFrame:
    path = root / layer / f"{name}.parquet"
    if not path.exists():
        raise SystemExit(
            f"{path} not found. Run scripts/build_medallion.py first."
        )
    return pd.read_parquet(path)


def cmd_tables(root: Path, args) -> None:
    print(f"Lakehouse at {root}\n")
    for layer in ("silver", "gold"):
        folder = root / layer
        if not folder.exists():
            continue
        print(f"{layer.upper()}")
        for path in sorted(folder.glob("*.parquet")):
            frame = pd.read_parquet(path)
            size_kb = path.stat().st_size / 1024
            print(f"  {path.stem:<28} {len(frame):>6} rows  {size_kb:>7.1f} KB")
            print(f"    columns: {', '.join(frame.columns)}")
        print()


def cmd_profile(root: Path, args) -> None:
    profile = load(root, "gold", "gold_daily_profile")
    intervals = load(root, "gold", "gold_interval_premium")

    days = intervals["market_day"].nunique()
    print(f"Daily profile over {days} market day(s)\n")
    if days < 30:
        print("  CAUTION: fewer than 30 days. This profile is not yet stable")
        print("  enough to tell a manufacturer when to run equipment.\n")

    print(f"{'hour':>5} {'intervals':>10} {'splits':>7} {'P(split)':>9} "
          f"{'mean prem':>10} {'worst':>8} {'mean util':>10}  profile")
    peak = profile["split_probability"].max() or 1.0
    for _, row in profile.iterrows():
        bar = "#" * int(round(28 * row["split_probability"] / peak))
        util = (
            f"{row['mean_utilisation']:.0%}"
            if "mean_utilisation" in profile.columns and pd.notna(row.get("mean_utilisation"))
            else ""
        )
        print(
            f"{int(row['hour_of_day_utc']):>4}h {int(row['intervals']):>10} "
            f"{int(row['decoupled_intervals']):>7} {row['split_probability']:>9.0%} "
            f"{row['mean_premium_eur_mwh']:>+10.2f} {row['worst_premium_eur_mwh']:>8.2f} "
            f"{util:>10}  {bar}"
        )

    # The mean premium above averages over coupled intervals too, which is the
    # right number for "what do I expect to pay", but it hides how bad a split
    # is when one happens. Both are worth seeing.
    split_only = intervals[intervals["is_decoupled"]]
    if not split_only.empty:
        print(
            f"\n  Across all hours: mean premium when actually decoupled is "
            f"{split_only['abs_premium_eur_mwh'].mean():+.2f} EUR/MWh, "
            f"versus {intervals['abs_premium_eur_mwh'].mean():+.2f} averaged "
            "over every interval."
        )


def cmd_episodes(root: Path, args) -> None:
    episodes = load(root, "gold", "gold_split_episodes")
    if episodes.empty:
        print("No episodes.")
        return

    columns = [
        "start_utc", "end_utc", "duration_hours", "intervals",
        "peak_spread", "premium_side", "max_severity",
        "share_saturated", "extra_cost_eur",
    ]
    present = [c for c in columns if c in episodes.columns]
    print(episodes[present].to_string(index=False))

    total = episodes["extra_cost_eur"].dropna().sum() if "extra_cost_eur" in episodes else 0
    hours = episodes["duration_hours"].sum()
    print(f"\n  {len(episodes)} episodes, {hours:.2f} decoupled hours, "
          f"{total:,.0f} EUR of extra import cost")
    if "share_saturated" in episodes.columns:
        explained = episodes["share_saturated"].fillna(0).ge(0.5).sum()
        print(f"  {explained} of {len(episodes)} explained by a full border")


def cmd_day(root: Path, args) -> None:
    intervals = load(root, "gold", "gold_interval_premium")
    frame = intervals[intervals["market_day"].astype(str) == args.date]
    if frame.empty:
        available = sorted(intervals["market_day"].astype(str).unique())
        print(f"No rows for {args.date}. Available: {', '.join(available)}")
        return

    columns = [
        "ts_utc", "price_pt_eur_mwh", "price_es_eur_mwh", "premium_eur_mwh",
        "is_decoupled", "net_flow_mw", "capacity_mw", "utilisation",
    ]
    present = [c for c in columns if c in frame.columns]
    view = frame[present]
    if not args.all:
        view = view[frame["is_decoupled"]]
        print(f"Decoupled intervals on {args.date} (use --all for every interval)\n")
    print(view.to_string(index=False))


def cmd_weather(root: Path, args) -> None:
    from iberian.analysis.weather import (
        compare_locations,
        weather_columns,
        within_hour_correlation,
    )

    context = load(root, "gold", "gold_weather_context")
    radiation = weather_columns(context, "shortwave_radiation_wm2")
    if not radiation:
        print("No weather columns. Did build_medallion run with weather enabled?")
        return

    print("Does Spanish solar move the Spanish price?\n")
    print("  The naive column correlates radiation against price across every")
    print("  interval. Both follow the solar clock, so it mostly measures the")
    print("  time of day. The within hour column compares days against each")
    print("  other at the same hour, which is the question actually worth")
    print("  asking, and it is the only one of the two worth quoting.\n")

    table = compare_locations(context, "shortwave_radiation_wm2")

    print(f"  {'location':<16} {'intervals':>10} {'hours':>6} "
          f"{'naive':>8} {'within hour':>12}")
    for _, row in table.iterrows():
        naive = "n/a" if pd.isna(row["naive"]) else f"{row['naive']:+.3f}"
        within = "n/a" if pd.isna(row["within_hour"]) else f"{row['within_hour']:+.3f}"
        print(
            f"  {row['location']:<16} {int(row['observations']):>10} "
            f"{int(row['hours_used']):>6} {naive:>8} {within:>12}"
        )

    portuguese = table[table["location"].str.startswith("PT")]
    spanish = table[table["location"].str.startswith("ES")]
    if not portuguese.empty and not spanish.empty:
        pt_naive = portuguese["naive"].abs().max()
        es_naive = spanish["naive"].abs().max()
        if pd.notna(pt_naive) and pd.notna(es_naive) and abs(pt_naive - es_naive) < 0.15:
            print("\n  Note the Portuguese rows. Portuguese cloud cover cannot move")
            print("  the Spanish price, so a naive figure that is just as strong")
            print("  there is the proof that the naive figure is about the clock.")

    days = context["market_day"].nunique() if "market_day" in context.columns else 0
    if days < 30:
        print(f"\n  CAUTION: only {days} market day(s). The within hour estimate")
        print("  compares days against each other, so it needs many of them.")

    detail = spanish if not spanish.empty else table
    column = f"shortwave_radiation_wm2__{detail.iloc[0]['location']}"
    result = within_hour_correlation(context, column)
    by_hour = result.by_hour
    if not by_hour.empty:
        print(f"\n  Hour by hour, {detail.iloc[0]['location']} (local time):\n")
        print(f"  {'hour':>5} {'obs':>5} {'mean W/m2':>10} {'mean price':>11} "
              f"{'corr':>8}")
        for _, row in by_hour.iterrows():
            correlation = (
                "flat" if pd.isna(row["correlation"]) else f"{row['correlation']:+.3f}"
            )
            print(
                f"  {int(row['local_hour']):>4}h {int(row['observations']):>5} "
                f"{row['mean_weather']:>10.1f} {row['mean_price']:>11.2f} "
                f"{correlation:>8}"
            )
        print("\n  'flat' means the radiation did not vary within that hour,")
        print("  which at night is every reading being zero.")

def cmd_compare(root: Path, args) -> None:
    """ENTSO-E against OMIE, from the stored silver tables."""
    entsoe = load(root, "silver", "silver_entsoe_prices")
    omie = load(root, "silver", "silver_omie_prices")

    from iberian.config import EIC_PORTUGAL

    pt = entsoe[entsoe["zone_eic"] == EIC_PORTUGAL][["ts_utc", "price_eur_mwh"]]
    pt = pt.rename(columns={"price_eur_mwh": "entsoe_pt"})
    merged = pt.merge(
        omie[["ts_utc", "price_first_eur_mwh"]], on="ts_utc", how="inner"
    )
    if merged.empty:
        print("No overlap between the two sources.")
        return

    merged["diff"] = (merged["entsoe_pt"] - merged["price_first_eur_mwh"]).abs()
    print(f"Matched {len(merged)} intervals across two independent publishers")
    print(f"  Maximum absolute difference: {merged['diff'].max():.4f} EUR/MWh")
    print(f"  Intervals differing by more than 0.01: {(merged['diff'] > 0.01).sum()}")
    if merged["diff"].max() <= 0.01:
        print("\n  Identical. That is the evidence that the parsing and the")
        print("  market day arithmetic are both right, from a source that")
        print("  shares no code with the first.")


COMMANDS = {
    "tables": cmd_tables,
    "profile": cmd_profile,
    "episodes": cmd_episodes,
    "day": cmd_day,
    "weather": cmd_weather,
    "compare": cmd_compare,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--root", default="data/lakehouse")
    parser.add_argument("--date", help="market day for the day command")
    parser.add_argument("--all", action="store_true", help="every interval, not just splits")
    args = parser.parse_args()

    if args.command == "day" and not args.date:
        parser.error("day needs --date")

    COMMANDS[args.command](Path(args.root), args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
