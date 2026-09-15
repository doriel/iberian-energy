"""Fetch and land the cross-border series for the PT/ES interconnector."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from iberian.config import EIC_PORTUGAL, EIC_SPAIN
from iberian.ingestion.entsoe import EntsoeClient
from iberian.parsing.entsoe_prices import parse_quantities_response, quantities_to_records

DIRECTIONS = [
    ("ES_to_PT", EIC_SPAIN, EIC_PORTUGAL),
    ("PT_to_ES", EIC_PORTUGAL, EIC_SPAIN),
]


def _land(raw_dir: Path, kind: str, direction: str, day: date, response) -> None:
    target = raw_dir / "entsoe" / "crossborder" / f"kind={kind}" / f"dir={direction}"
    target.mkdir(parents=True, exist_ok=True)
    name = f"{day:%Y-%m-%d}{response.suggested_extension}"
    (target / name).write_bytes(response.content)


def fetch_border(
    client: EntsoeClient,
    start_utc: datetime,
    end_utc: datetime,
    raw_dir: Path,
    day_label: date,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (schedules, capacity) as tidy frames, landing the raw XML.

    Both directions, because ENTSO-E publishes one per request and a net flow
    needs both sides.
    """
    schedule_records: list[dict] = []
    capacity_records: list[dict] = []

    for direction, out_domain, in_domain in DIRECTIONS:
        schedule = client.scheduled_exchanges(out_domain, in_domain, start_utc, end_utc)
        _land(raw_dir, "A09", direction, day_label, schedule)
        points = parse_quantities_response(schedule)
        schedule_records.extend(quantities_to_records(points, "scheduled_exchange"))
        if verbose:
            print(f"  schedules {direction}: {len(points)} points")

        capacity = client.forecasted_transfer_capacity(
            out_domain, in_domain, start_utc, end_utc
        )
        _land(raw_dir, "A61", direction, day_label, capacity)
        points = parse_quantities_response(capacity)
        capacity_records.extend(quantities_to_records(points, "forecasted_capacity"))
        if verbose:
            print(f"  capacity  {direction}: {len(points)} points")

    return pd.DataFrame(schedule_records), pd.DataFrame(capacity_records)
