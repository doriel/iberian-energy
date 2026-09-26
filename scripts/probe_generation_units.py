"""Measure before building: how much data is actually behind [16.1.A]?

The plan is to add actual generation per generation unit as the project's Volume
evidence. Whether that works depends on a number nobody has yet: how many units
report in Spain and Portugal, and at what resolution. A plan built on a guess
about that number is a plan that falls over on the day of the ingestion.

So this fetches ONE day for each zone, parses it, and prints what is there:
units, resolution, rows for that day, and what a window would come to. It writes
nothing and changes nothing. Two API requests.

    python scripts/probe_generation_units.py
    python scripts/probe_generation_units.py --day 2026-08-18 --window 90

The day matters more than it looks. A Sunday in August is not a Tuesday in
January, and a day where half the fleet was idle reports fewer units. Probe two
or three before trusting the extrapolation.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402

#: ENTSO-E namespaces the whole document and the version is in the URI, so it is
#: read from the root rather than hard coded. A hard coded namespace is a parser
#: that breaks silently the day the schema version moves.
def namespace(root: ET.Element) -> str:
    return root.tag.split("}")[0].strip("{") if "}" in root.tag else ""


#: How long each resolution is worth, for turning periods into a row count.
RESOLUTION_MINUTES = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "PT1H": 60}


def summarise(xml: str) -> dict:
    """Units, resolutions and points in one day's document.

    Counted from the document rather than assumed, because the shape of this
    response is the thing being measured.
    """
    root = ET.fromstring(xml)
    ns = namespace(root)
    tag = (lambda name: f"{{{ns}}}{name}") if ns else (lambda name: name)

    units: dict[str, str] = {}
    resolutions: Counter = Counter()
    psr_types: Counter = Counter()
    points = 0

    for series in root.iter(tag("TimeSeries")):
        resource = series.find(f".//{tag('MktPSRType')}/{tag('PowerSystemResources')}")
        if resource is not None:
            code = resource.findtext(tag("mRID")) or "unknown"
            units[code] = resource.findtext(tag("name")) or ""

        psr = series.findtext(f".//{tag('MktPSRType')}/{tag('psrType')}")
        if psr:
            psr_types[psr] += 1

        for period in series.iter(tag("Period")):
            resolution = period.findtext(tag("resolution")) or "unknown"
            found = len(list(period.iter(tag("Point"))))
            resolutions[resolution] += found
            points += found

    return {
        "units": units,
        "resolutions": resolutions,
        "psr_types": psr_types,
        "points": points,
        "series": len(list(root.iter(tag("TimeSeries")))),
    }


def report(label: str, xml: str, window_days: int) -> int:
    print(f"\n{'=' * 66}\n{label}\n{'=' * 66}")

    if not xml.strip():
        print("  empty response, nothing published for this day")
        return 0

    if "Acknowledgement" in xml[:2000]:
        reason = xml.split("<text>")[1].split("</text>")[0] if "<text>" in xml else "?"
        print(f"  ENTSO-E declined: {reason}")
        return 0

    found = summarise(xml)
    print(f"  time series      {found['series']:>8,}")
    print(f"  distinct units   {len(found['units']):>8,}")
    print(f"  points that day  {found['points']:>8,}")

    print("\n  resolutions:")
    for resolution, count in found["resolutions"].most_common():
        minutes = RESOLUTION_MINUTES.get(resolution)
        note = f"{1440 // minutes} per unit per day" if minutes else "unrecognised"
        print(f"    {resolution:<10} {count:>8,} points   ({note})")

    print("\n  production types:")
    for psr, count in found["psr_types"].most_common(8):
        print(f"    {psr:<6} {count:>5} series")

    print("\n  a few units, so you can see what these are:")
    for code, name in list(found["units"].items())[:5]:
        print(f"    {code}  {name}")

    projected = found["points"] * window_days
    print(f"\n  over {window_days} days: {projected:>12,} rows")
    return projected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=None, help="YYYY-MM-DD, default: three days ago")
    parser.add_argument("--window", type=int, default=90, help="days to extrapolate over")
    args = parser.parse_args()

    token = os.environ.get("ENTSOE_SECURITY_TOKEN")
    if not token:
        print("ENTSOE_SECURITY_TOKEN is not set. `set -a && source .env && set +a`")
        return 1

    if args.day:
        start = datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        # Not yesterday. Generation per unit is published with a lag and a
        # recent day can look thin for reasons that have nothing to do with the
        # fleet.
        start = (datetime.now(timezone.utc) - timedelta(days=3)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    end = start + timedelta(days=1)

    print(f"Probing {start:%Y-%m-%d}, one day, two requests.")

    client = EntsoeClient(security_token=token)
    total = 0
    for label, eic in (("Spain", EIC_SPAIN), ("Portugal", EIC_PORTUGAL)):
        try:
            response = client.actual_generation_per_unit(eic, start, end)
        except Exception as exc:
            print(f"\n{label}: request failed\n  {str(exc)[:400]}")
            continue
        total += report(f"{label}  ({eic})", response.body, args.window)

    print(f"\n{'=' * 66}")
    print(f"  Both zones over {args.window} days: {total:>12,} rows")
    if total >= 1_000_000:
        print("  Clears the million row threshold. Volume is achievable.")
    else:
        needed = -(-1_000_000 // max(total // args.window, 1))
        print(f"  Short of a million. That window would need about {needed} days.")
    print(f"{'=' * 66}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())