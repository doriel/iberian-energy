"""Transmission outages on the PT/ES border, and the publication time filter.

Two questions in one script:

1. Does A78 explain the capacity collapse? The generation outages in A80 are
   months long planned maintenance of power stations, which cannot explain why
   the interconnector fell to 434 MW for one evening. A78 is keyed on the
   border rather than on a zone, so it should.

2. Does the publication time filter work? periodStartUpdate and periodEndUpdate
   select notices by when they were published rather than when the outage runs.
   That is the server side version of point in time correctness, and if it
   works the evaluation set cannot leak information from the future.

    python scripts/probe_transmission.py --start 2026-09-03 --days 2
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import (  # noqa: E402
    BUSINESS_TRANSMISSION_PLANNED,
    BUSINESS_TRANSMISSION_UNPLANNED,
    EIC_PORTUGAL,
    EIC_SPAIN,
    Settings,
)
from iberian.ingestion.entsoe import EntsoeClient, EntsoeRequestError  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402
from iberian.parsing.entsoe_prices import _find, _findall, _local, _text  # noqa: E402

DIRECTIONS = [
    ("ES->PT", EIC_SPAIN, EIC_PORTUGAL),
    ("PT->ES", EIC_PORTUGAL, EIC_SPAIN),
]

BUSINESS_LABELS = {
    BUSINESS_TRANSMISSION_UNPLANNED: "unplanned",
    BUSINESS_TRANSMISSION_PLANNED: "planned",
}

EXAMPLES_SHOWN = 2


def summarise(documents: list[str], label: str) -> int:
    """Discover the schema rather than assume it.

    A78 nests the asset and the reason instead of flattening them into dotted
    tag names the way A80 does, so hardcoding field names here would quietly
    print "unnamed asset" for every outage. Aggregate the tags that are
    actually present, then show whole series as examples.
    """
    created: list[str] = []
    business: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    examples: list[ET.Element] = []
    series_count = 0

    for xml_body in documents:
        root = ET.fromstring(xml_body)
        created_element = _find(root, "createdDateTime")
        if created_element is not None and created_element.text:
            created.append(created_element.text.strip())

        for series in _findall(root, "TimeSeries"):
            series_count += 1
            for element in series.iter():
                tag = _local(element.tag)
                if tag != "TimeSeries" and (element.text or "").strip():
                    tags[tag] += 1
            business_element = _find(series, "businessType")
            business[_text(business_element) or "?"] += 1
            if len(examples) < EXAMPLES_SHOWN:
                examples.append(series)

    print(f"  {label}: {len(documents)} documents, {series_count} outages")
    if created:
        print(f"    published between {min(created)} and {max(created)}")
    for code, count in business.most_common():
        print(f"    businessType {code} ({BUSINESS_LABELS.get(code, code)}): {count}")

    if tags:
        print("    elements present:")
        for tag, count in tags.most_common():
            print(f"      {tag:50} {count}")

    for index, series in enumerate(examples, start=1):
        print(f"\n    --- example outage {index} ---")
        for element in series.iter():
            tag = _local(element.tag)
            value = (element.text or "").strip()
            if tag == "TimeSeries" or not value:
                continue
            print(f"      {tag:50} {value[:60]}")

    return series_count


def run(client: EntsoeClient, label: str, **kwargs) -> int:
    try:
        response = client.transmission_unavailability(**kwargs)
    except EntsoeRequestError as exc:
        print(f"  {label}: FAIL {exc.reason or exc}")
        return 0
    except RuntimeError as exc:
        print(f"  {label}: FAIL {exc}")
        return 0

    if response.is_empty:
        print(f"  {label}: no notices")
        return 0

    try:
        documents = response.documents()
    except ValueError as exc:
        print(f"  {label}: FAIL {exc}")
        return 0

    return summarise(documents, label)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="first market day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=2)
    args = parser.parse_args()

    start_day = date.fromisoformat(args.start)
    start_utc, end_utc = market_day_range(start_day, args.days)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    print("=" * 70)
    print(f"A78 transmission unavailability, PT/ES border, {start_day} +{args.days}d")
    print("=" * 70)

    total = 0
    for direction, out_domain, in_domain in DIRECTIONS:
        total += run(
            client,
            f"A78 all types {direction}",
            out_domain=out_domain,
            in_domain=in_domain,
            period_start=start_utc,
            period_end=end_utc,
        )

    if not total:
        print("\nNothing on the border. Worth trying without a businessType filter")
        print("over a wider window, or checking whether REN and REE publish these")
        print("under a different border EIC.")

    print("\n" + "=" * 70)
    print("Publication time filter: what was NEW in the 48h before the market day")
    print("=" * 70)
    print("If this returns fewer notices than the query above, the server side")
    print("point in time filter works and the evaluation set cannot leak.\n")

    published_from = start_utc - timedelta(days=2)
    for direction, out_domain, in_domain in DIRECTIONS:
        run(
            client,
            f"A78 published in the 48h before {direction}",
            out_domain=out_domain,
            in_domain=in_domain,
            period_start=start_utc,
            period_end=end_utc,
            published_start=published_from,
            published_end=start_utc,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
