"""Find out what an A80 unavailability document actually looks like.

The outage notices are the unstructured evidence layer: the thing the agent
retrieves to explain why capacity collapsed. Their XML is a different shape
from the price and quantity documents, and I would rather read the real one
than write a parser against a guess.

    python scripts/probe_outages.py --start 2026-09-03 --days 2

Saves the raw XML and prints the structure: which elements each TimeSeries
carries, so we can decide what becomes a row, what becomes retrievable text,
and what the point in time correctness filter keys on.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import BIDDING_ZONES, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient, EntsoeRequestError  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402
from iberian.parsing.entsoe_prices import _find, _findall, _local, _text  # noqa: E402

MAX_SERIES_SHOWN = 3


def describe(documents: list[str]) -> None:
    """Summarise the schema across every document, then show a couple in full.

    A ZIP of outage notices can hold hundreds of documents. Printing them all
    is useless, so aggregate the element names first, which is the schema
    discovered rather than assumed, and only then show examples.
    """
    roots: Counter[str] = Counter()
    doc_level: Counter[str] = Counter()
    series_level: Counter[str] = Counter()
    series_total = 0
    examples: list[ET.Element] = []
    created: list[str] = []

    for xml_body in documents:
        root = ET.fromstring(xml_body)
        roots[_local(root.tag)] += 1

        for child in root:
            tag = _local(child.tag)
            if tag == "TimeSeries":
                continue
            if (child.text or "").strip():
                doc_level[tag] += 1
            if tag == "createdDateTime":
                created.append((child.text or "").strip())

        series_list = _findall(root, "TimeSeries")
        series_total += len(series_list)
        for series in series_list:
            for element in series.iter():
                tag = _local(element.tag)
                if tag != "TimeSeries" and (element.text or "").strip():
                    series_level[tag] += 1
            if len(examples) < MAX_SERIES_SHOWN:
                examples.append(series)

    print(f"  Documents: {len(documents)}")
    for name, count in roots.most_common():
        print(f"    root {name}: {count}")
    print(f"  TimeSeries blocks across all documents: {series_total}")

    if created:
        print(f"  createdDateTime range: {min(created)} to {max(created)}")
        print("    ^ this is the publication timestamp, the key for the")
        print("      point in time filter when evaluating cause attribution")

    print("\n  Document level elements:")
    for tag, count in doc_level.most_common():
        print(f"    {tag:45} {count}")

    print("\n  TimeSeries level elements:")
    for tag, count in series_level.most_common():
        print(f"    {tag:45} {count}")

    print(f"\n  First {len(examples)} TimeSeries in full:")
    for index, series in enumerate(examples, start=1):
        print(f"\n  --- TimeSeries {index} ---")
        for element in series.iter():
            tag = _local(element.tag)
            value = (element.text or "").strip()
            if tag == "TimeSeries" or not value:
                continue
            print(f"    {tag:45} {value[:70]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="first market day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    start_day = date.fromisoformat(args.start)
    start_utc, end_utc = market_day_range(start_day, args.days)
    raw_dir = Path(args.raw_dir)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    print(
        f"A80 unavailability, {start_day} to "
        f"{start_day + timedelta(days=args.days - 1)}\n"
    )

    for label, eic in BIDDING_ZONES.items():
        print(f"{label} ({eic})")
        try:
            response = client.generation_unavailability(eic, start_utc, end_utc)
        except EntsoeRequestError as exc:
            print(f"  FAIL  {exc.reason or exc}\n")
            continue
        except RuntimeError as exc:
            print(f"  FAIL  {exc}\n")
            continue

        if response.is_empty:
            print("  EMPTY  no notices published for this window\n")
            continue

        target = raw_dir / "entsoe" / "outages" / f"zone={label}"
        target.mkdir(parents=True, exist_ok=True)
        out_path = (
            target
            / f"{start_day:%Y-%m-%d}_{args.days}d{response.suggested_extension}"
        )
        out_path.write_bytes(response.content)
        kind = "ZIP archive" if response.is_zip else "XML"
        print(f"  Saved {out_path} ({len(response.content)} bytes, {kind})")

        try:
            documents = response.documents()
        except ValueError as exc:
            print(f"  FAIL  {exc}\n")
            continue

        describe(documents)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
