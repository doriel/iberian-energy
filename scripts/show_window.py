"""Print PT and ES prices side by side for a time window, from landed raw XML.

No API call: this reads what is already in data/raw, which is the point of
storing the raw payload in bronze. Use it to eyeball whether a detected split
is a real divergence or an artefact of how the two zones publish.

    python scripts/show_window.py --from 2026-09-03T16:30 --to 2026-09-03T19:00
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.parsing.entsoe_prices import (  # noqa: E402
    parse_day_ahead_prices,
    to_records,
)

ZONES = {"PT": EIC_PORTUGAL, "ES": EIC_SPAIN}


def load_landed(raw_dir: Path) -> pd.DataFrame:
    records: list[dict] = []
    for zone_label, eic in ZONES.items():
        pattern = f"entsoe/day_ahead_prices/zone={zone_label}/**/response.xml"
        paths = sorted(raw_dir.glob(pattern))
        if not paths:
            print(f"No landed files for {zone_label} under {raw_dir}")
            continue
        for path in paths:
            points = parse_day_ahead_prices(path.read_text(encoding="utf-8"), eic)
            for record in to_records(points):
                record["zone"] = zone_label
                record["source_file"] = str(path)
                records.append(record)
    return pd.DataFrame(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True, help="UTC, ISO format")
    parser.add_argument("--to", dest="end", required=True, help="UTC, ISO format")
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    prices = load_landed(Path(args.raw_dir))
    if prices.empty:
        print("Nothing landed. Run run_market_splitting.py first.")
        return 1

    window = prices[(prices["ts_utc"] >= start) & (prices["ts_utc"] < end)]
    if window.empty:
        print(f"No rows between {start} and {end}.")
        print(f"Landed range: {prices['ts_utc'].min()} to {prices['ts_utc'].max()}")
        return 1

    wide = window.pivot_table(
        index="ts_utc", columns="zone", values="price_eur_mwh", aggfunc="last"
    ).sort_index()
    wide["spread"] = wide.get("PT") - wide.get("ES")
    wide["split"] = wide["spread"].abs() > 0.01

    # Does the price change at this interval, per zone? A value that only ever
    # changes on the hour means the zone is publishing hourly blocks into a
    # quarter hourly grid, which is the thing to rule out.
    for zone in ("PT", "ES"):
        if zone in wide.columns:
            wide[f"{zone}_changed"] = wide[zone].diff().fillna(0).abs() > 0.001

    print(wide.to_string())

    print("\nWhere does each zone actually change price?")
    for zone in ("PT", "ES"):
        column = f"{zone}_changed"
        if column not in wide.columns:
            continue
        changed = wide.index[wide[column]]
        on_hour = sum(1 for ts in changed if ts.minute == 0)
        print(
            f"  {zone}: {len(changed)} changes in this window, "
            f"{on_hour} of them on the hour"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
