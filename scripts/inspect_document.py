"""Print the structure of one A44 document, so we can see why point counts
do not match the expected 96 per day at PT15M.

    python scripts/inspect_document.py
    python scripts/inspect_document.py --zone ES --date 2026-09-12

Saves the raw XML too, so we can replay it without another API call.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import BIDDING_ZONES, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.parsing.entsoe_prices import _find, _findall, _local, _text  # noqa: E402


def describe(xml_body: str) -> None:
    root = ET.fromstring(xml_body)
    print(f"Root element: {_local(root.tag)}")

    # Document level fields, which is where a revision number would show up.
    for child in root:
        name = _local(child.tag)
        if name != "TimeSeries":
            print(f"  {name}: {(child.text or '').strip()[:60]}")

    series_list = _findall(root, "TimeSeries")
    print(f"\nTimeSeries blocks: {len(series_list)}\n")

    resolutions: Counter[str] = Counter()
    intervals: Counter[str] = Counter()

    for index, series in enumerate(series_list, start=1):
        attrs = {}
        for field in (
            "mRID",
            "businessType",
            "curveType",
            "auction.type",
            "contract_MarketAgreement.type",
            "in_Domain.mRID",
            "out_Domain.mRID",
        ):
            element = _find(series, field)
            if element is not None and element.text:
                attrs[field] = element.text.strip()

        periods = _findall(series, "Period")
        print(f"TimeSeries {index}: {attrs}")

        for period in periods:
            interval = _find(period, "timeInterval")
            start = _text(_find(interval, "start")) if interval is not None else "?"
            end = _text(_find(interval, "end")) if interval is not None else "?"
            resolution = _text(_find(period, "resolution")) or "?"
            points = _findall(period, "Point")
            positions = [
                int(_text(_find(p, "position")) or 0) for p in points
            ]

            resolutions[resolution] += 1
            intervals[f"{start} .. {end} @{resolution}"] += 1

            print(
                f"   Period {start} .. {end}  resolution={resolution}  "
                f"points={len(points)}  positions={min(positions, default=0)}"
                f"..{max(positions, default=0)}"
            )
        print()

    print("Resolution counts across all periods:")
    for resolution, count in resolutions.most_common():
        print(f"  {resolution}: {count} period(s)")

    print("\nDistinct interval+resolution combinations:")
    for label, count in intervals.most_common():
        marker = "  <-- repeated" if count > 1 else ""
        print(f"  {count}x  {label}{marker}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", default="PT", choices=sorted(BIDDING_ZONES))
    parser.add_argument("--date", help="UTC day to fetch, YYYY-MM-DD")
    args = parser.parse_args()

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    if args.date:
        start = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=2)
    end = start + timedelta(days=1)

    eic = BIDDING_ZONES[args.zone]
    print(f"Fetching {args.zone} ({eic}) for {start:%Y-%m-%d}\n")

    response = client.day_ahead_prices(eic, start, end)

    out_dir = Path("data/raw/inspect")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.zone}_{start:%Y%m%d}{response.suggested_extension}"
    out_path.write_bytes(response.content)
    print(f"Raw payload saved to {out_path} ({len(response.content)} bytes)\n")

    documents = response.documents()
    if len(documents) > 1:
        print(f"Response holds {len(documents)} documents\n")
    for index, document in enumerate(documents, start=1):
        if len(documents) > 1:
            print(f"--- document {index} ---")
        describe(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
