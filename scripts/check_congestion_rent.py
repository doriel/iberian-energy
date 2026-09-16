"""Check this project's cost figure against the one REE publishes.

`gold_split_episodes.extra_cost_eur` is the premium Portugal paid multiplied by
the energy actually imported while the zones priced apart. It is computed here,
from ENTSO-E prices and schedules. REE publishes the congestion rent on the
same border, which is the same economic quantity computed by the people who
run the interconnector.

Two independent calculations of one number is a much better position to defend
than one calculation asserted confidently, and this is the script that finds
out whether they agree.

    python scripts/check_congestion_rent.py
    python scripts/check_congestion_rent.py --start 2026-09-01 --days 7

They are not the same definition and a perfect match would be suspicious.
Congestion rent is generally the price difference applied to the capacity
allocated in the market coupling, while the cost here uses the net scheduled
flow. Where the two diverge, the size and direction of the divergence is the
result, not an error to hide.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.ingestion.esios import (  # noqa: E402
    INDICATORS,
    EsiosClient,
    EsiosError,
    congestion_rent_by_day,
    to_frame,
)
from iberian.market_time import market_day_range  # noqa: E402

RENT_INDICATORS = (
    INDICATORS["congestion_rent_pt_import"],
    INDICATORS["congestion_rent_pt_export"],
)


def load_episodes(root: Path) -> pd.DataFrame:
    path = root / "gold" / "gold_split_episodes.parquet"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run scripts/build_medallion.py first.")
    episodes = pd.read_parquet(path)
    if "market_day" not in episodes.columns:
        raise SystemExit(
            "This episodes table predates the market_day column. Rebuild it."
        )
    return episodes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="first market day, defaults to the earliest in gold")
    parser.add_argument("--days", type=int, help="defaults to the whole gold range")
    parser.add_argument("--root", default="data/lakehouse")
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    episodes = load_episodes(Path(args.root))
    if episodes.empty:
        print("No episodes in gold. Nothing to check.")
        return 0

    days_present = sorted(pd.to_datetime(episodes["market_day"]).dt.date.unique())
    start = date.fromisoformat(args.start) if args.start else days_present[0]
    count = args.days or ((days_present[-1] - start).days + 1)
    if count < 1:
        raise SystemExit("The requested window ends before it starts.")

    window = [start + timedelta(days=offset) for offset in range(count)]
    start_utc, end_utc = market_day_range(start, count)

    print(f"Market days {window[0]} to {window[-1]}\n")

    token = os.environ.get("ESIOS_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "ESIOS_TOKEN is not set.\n"
            "Add it to .env, then: export $(grep -v '^#' .env | xargs)"
        )

    client = EsiosClient(token)
    responses = []
    raw_dir = Path(args.raw_dir) / "esios"
    for indicator_id in RENT_INDICATORS:
        try:
            response = client.indicator(indicator_id, start_utc, end_utc)
        except EsiosError as exc:
            print(f"  indicator {indicator_id}: {exc}")
            continue
        target = raw_dir / f"indicator={indicator_id}"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{start:%Y-%m-%d}_{count}d.json").write_bytes(response.content)
        responses.append(response)

    if not responses:
        print("ESIOS returned nothing. Cannot compare.")
        return 1

    rent = congestion_rent_by_day(to_frame(responses))
    if rent.empty:
        print("No congestion rent published for this window.")
        return 0

    ours = (
        episodes.assign(market_day=pd.to_datetime(episodes["market_day"]).dt.date)
        .groupby("market_day")
        .agg(
            our_cost_eur=("extra_cost_eur", "sum"),
            episodes=("start_utc", "count"),
            decoupled_hours=("duration_hours", "sum"),
        )
        .reset_index()
    )

    merged = rent.merge(ours, on="market_day", how="outer").fillna(
        {"congestion_rent_eur": 0.0, "our_cost_eur": 0.0, "episodes": 0,
         "decoupled_hours": 0.0}
    )
    merged = merged[merged["market_day"].isin(window)].sort_values("market_day")

    print(f"{'market day':<12} {'episodes':>9} {'our cost':>14} "
          f"{'REE rent':>14} {'ratio':>8}")
    for _, row in merged.iterrows():
        ratio = (
            f"{row['our_cost_eur'] / row['congestion_rent_eur']:.2f}"
            if row["congestion_rent_eur"] > 0
            else "n/a"
        )
        print(
            f"{str(row['market_day']):<12} {int(row['episodes']):>9} "
            f"{row['our_cost_eur']:>14,.0f} {row['congestion_rent_eur']:>14,.0f} "
            f"{ratio:>8}"
        )

    total_ours = merged["our_cost_eur"].sum()
    total_ree = merged["congestion_rent_eur"].sum()
    print(f"\n  Our figure:  {total_ours:>14,.0f} EUR")
    print(f"  REE's rent:  {total_ree:>14,.0f} EUR")
    if total_ree > 0:
        print(f"  Ratio:       {total_ours / total_ree:>14.2f}")

    # The agreement worth reporting is not on the euros, which use different
    # definitions, but on which days had congestion at all. That is a claim
    # about the phenomenon rather than about arithmetic.
    ours_days = set(merged.loc[merged["our_cost_eur"] > 0, "market_day"])
    ree_days = set(merged.loc[merged["congestion_rent_eur"] > 0, "market_day"])
    both = ours_days & ree_days
    print(f"\n  Days we flagged as decoupled:      {len(ours_days)}")
    print(f"  Days REE earned congestion rent:   {len(ree_days)}")
    print(f"  Days both agree:                   {len(both)}")

    only_ours = sorted(ours_days - ree_days)
    only_ree = sorted(ree_days - ours_days)
    if only_ours:
        print(f"\n  We flagged, REE did not: {', '.join(str(d) for d in only_ours)}")
        print("  Worth investigating. Either the detector is firing on noise,")
        print("  or the rent was published late.")
    if only_ree:
        print(f"\n  REE earned rent, we saw no split: "
              f"{', '.join(str(d) for d in only_ree)}")
        print("  More interesting. Congestion the price series did not reveal,")
        print("  which is the kind of gap worth a paragraph in the write up.")
    if not only_ours and not only_ree:
        print("\n  Perfect agreement on which days had congestion. That is the")
        print("  claim worth making, independently of the euro figures.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
