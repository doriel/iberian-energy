"""Find out which cross-border document types actually return data for PT/ES.

The saturation test needs two numbers per interval: how much the interconnector
could carry, and how much was scheduled across it. A61 day-ahead capacity is
confirmed in the API user guide. The rest are candidates, and guessing wrong
costs a debugging session, so probe them all once and let the API answer.

    python scripts/probe_crossborder.py
    python scripts/probe_crossborder.py --date 2026-09-03

Anything that returns data is landed under data/raw/entsoe/crossborder/.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import (  # noqa: E402
    EntsoeClient,
    EntsoeRequestError,
    RawResponse,
)
from iberian.market_time import market_day_window  # noqa: E402
from iberian.parsing.entsoe_prices import parse_quantities_response  # noqa: E402

DIRECTIONS = [
    ("ES->PT", EIC_SPAIN, EIC_PORTUGAL),
    ("PT->ES", EIC_PORTUGAL, EIC_SPAIN),
]


def land(raw_dir: Path, label: str, direction: str, day: date, response) -> None:
    target = raw_dir / "entsoe" / "crossborder" / f"kind={label}" / f"dir={direction}"
    target.mkdir(parents=True, exist_ok=True)
    name = f"{day:%Y-%m-%d}{response.suggested_extension}"
    (target / name).write_bytes(response.content)


def report(label: str, direction: str, response: RawResponse) -> bool:
    if response.is_empty:
        print(f"  EMPTY  {label:24} {direction}")
        return False

    points = parse_quantities_response(response)
    if not points:
        print(f"  EMPTY  {label:24} {direction}  (parsed to zero points)")
        return False

    values = [p.quantity_mw for p in points]
    stamps = {p.ts_utc for p in points}
    contracts = sorted({p.contract_type or "none" for p in points})
    print(
        f"  OK     {label:24} {direction}  {len(points)} points over "
        f"{len(stamps)} timestamps, {min(values):.0f} to {max(values):.0f} MW, "
        f"{points[0].resolution}, contract={','.join(contracts)}"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="market day, YYYY-MM-DD")
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    day = (
        date.fromisoformat(args.date)
        if args.date
        else (datetime.now(timezone.utc) - timedelta(days=2)).date()
    )
    start, end = market_day_window(day)
    raw_dir = Path(args.raw_dir)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    print(f"Probing PT/ES border for market day {day}\n")

    probes = [
        ("A61 forecasted capacity", client.forecasted_transfer_capacity, {}),
        ("A09 scheduled exchanges", client.scheduled_exchanges, {}),
        (
            "A09 scheduled (total A05)",
            client.scheduled_exchanges,
            {"contract_type": "A05"},
        ),
        ("A11 physical flows", client.physical_flows, {}),
    ]

    working: list[str] = []

    for label, method, extra in probes:
        for direction, out_domain, in_domain in DIRECTIONS:
            try:
                response = method(out_domain, in_domain, start, end, **extra)
            except EntsoeRequestError as exc:
                print(f"  FAIL   {label:24} {direction}  {exc.reason or exc}")
                continue
            except RuntimeError as exc:
                print(f"  FAIL   {label:24} {direction}  {exc}")
                continue

            if report(label, direction, response):
                working.append(f"{label} {direction}")
                land(raw_dir, label.split()[0], direction.replace("->", "_to_"),
                     day, response)

    print()
    if working:
        print("Usable for the saturation test:")
        for entry in working:
            print(f"  {entry}")
    else:
        print("Nothing returned data. Send me the output and we will widen the probe.")

    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(main())
