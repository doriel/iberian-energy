"""Parse A78 transmission unavailability into queryable capacity curves.

An A78 notice is not a flag saying a line is out. It carries the available
capacity of one asset as a step function over the whole outage window, at
PT1M resolution, sparse encoded: a position appears only when the value
changes.

That encoding matters enormously for how this is stored. One notice running
from January to December is 485,000 minutes. Expanding it the way the price
parser expands a day would produce half a million rows per asset, tens of
millions across the border, nearly all of them repeating the previous value.

So this keeps the breakpoints and evaluates on demand with a binary search.
Bronze still lands the raw XML; silver stores breakpoints, not minutes.
"""

from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from iberian.parsing.entsoe_prices import (
    RESOLUTION_MINUTES,
    _find,
    _findall,
    _local,
    _parse_instant,
    _text,
)

BUSINESS_TYPE_LABELS = {
    "A53": "planned",
    "A54": "unplanned",
    # The API guide names B12 and B13 as the filter values, but the documents
    # come back carrying A53 and A54. Classify on what is returned.
    "B12": "unplanned",
    "B13": "planned",
}


@dataclass(frozen=True)
class OutageCurve:
    """One asset's available capacity as a step function."""

    document_mrid: str | None
    created_at: datetime | None
    business_type: str | None
    asset_mrid: str | None
    asset_name: str | None
    asset_location: str | None
    psr_type: str | None
    in_domain: str | None
    out_domain: str | None
    start_utc: datetime
    end_utc: datetime
    resolution_minutes: int
    # Sorted breakpoints: the capacity holds until the next one.
    breakpoints: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return BUSINESS_TYPE_LABELS.get(self.business_type or "", "unknown")

    @property
    def label(self) -> str:
        return self.asset_name or self.asset_mrid or "unnamed asset"

    def covers(self, ts: datetime) -> bool:
        return self.start_utc <= ts < self.end_utc

    def capacity_at(self, ts: datetime) -> float | None:
        """Available capacity at an instant, or None outside the window."""
        if not self.covers(ts) or not self.breakpoints:
            return None
        stamps = [point[0] for point in self.breakpoints]
        index = bisect.bisect_right(stamps, ts) - 1
        if index < 0:
            return None
        return self.breakpoints[index][1]

    def minimum_between(self, start: datetime, end: datetime) -> float | None:
        """Tightest capacity across a window, which is what binds an interval."""
        values = [
            value
            for stamp, value in self.breakpoints
            if start <= stamp < end
        ]
        at_start = self.capacity_at(start)
        if at_start is not None:
            values.append(at_start)
        return min(values) if values else None


def parse_transmission_outages(xml_body: str) -> list[OutageCurve]:
    root = ET.fromstring(xml_body)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        return []

    document_mrid = _text(_find(root, "mRID"))
    created_text = _text(_find(root, "createdDateTime"))
    created_at = _parse_instant(created_text) if created_text else None

    curves: list[OutageCurve] = []

    for series in _findall(root, "TimeSeries"):
        business_type = _text(_find(series, "businessType"))
        in_domain = _text(_find(series, "in_Domain.mRID"))
        out_domain = _text(_find(series, "out_Domain.mRID"))

        # The asset is nested rather than flattened into dotted tag names, and
        # its mRID collides with the series mRID, so reach for the container.
        asset_mrid = asset_name = asset_location = psr_type = None
        for child in series:
            if _local(child.tag).endswith("RegisteredResource"):
                asset_mrid = _text(_find(child, "mRID"))
                asset_name = _text(_find(child, "name"))
                location = _find(child, "location")
                if location is not None:
                    asset_location = _text(_find(location, "name"))
                psr = _find(child, "asset_PSRType")
                if psr is not None:
                    psr_type = _text(_find(psr, "psrType"))

        for period in _findall(series, "Available_Period") or _findall(
            series, "Period"
        ):
            interval = _find(period, "timeInterval")
            if interval is None:
                continue
            start_text = _text(_find(interval, "start"))
            end_text = _text(_find(interval, "end"))
            resolution = _text(_find(period, "resolution")) or "PT60M"
            if not start_text or not end_text:
                continue

            step = RESOLUTION_MINUTES.get(resolution)
            if step is None:
                # A78 uses PT1M, which the price resolutions map does not carry.
                step = 1 if resolution == "PT1M" else None
            if step is None:
                raise ValueError(f"Unsupported resolution {resolution!r}")

            start = _parse_instant(start_text)
            end = _parse_instant(end_text)

            breakpoints: list[tuple[datetime, float]] = []
            for point in _findall(period, "Point"):
                position_text = _text(_find(point, "position"))
                quantity_text = _text(_find(point, "quantity"))
                if position_text is None or quantity_text is None:
                    continue
                offset = timedelta(minutes=step * (int(position_text) - 1))
                breakpoints.append((start + offset, float(quantity_text)))

            breakpoints.sort(key=lambda item: item[0])

            curves.append(
                OutageCurve(
                    document_mrid=document_mrid,
                    created_at=created_at,
                    business_type=business_type,
                    asset_mrid=asset_mrid,
                    asset_name=asset_name,
                    asset_location=asset_location,
                    psr_type=psr_type,
                    in_domain=in_domain,
                    out_domain=out_domain,
                    start_utc=start,
                    end_utc=end,
                    resolution_minutes=step,
                    breakpoints=breakpoints,
                )
            )

    return curves


def parse_outages_response(response) -> list[OutageCurve]:
    """Every A78 document in a response, zipped or not."""
    curves: list[OutageCurve] = []
    for document in response.documents():
        curves.extend(parse_transmission_outages(document))
    return curves


def binding_assets(
    curves: list[OutageCurve],
    start: datetime,
    end: datetime,
    published_before: datetime | None = None,
    direction: tuple[str, str] | None = None,
) -> list[dict]:
    """Which assets were constrained across an interval, tightest first.

    published_before enforces point in time correctness: when evaluating a
    cause attribution, a notice published after the anomaly must not be
    retrievable for it, or the accuracy numbers quietly include hindsight.

    direction, as (out_domain, in_domain), keeps the two sides of the border
    apart. A constraint on the Portugal to Spain direction says nothing about
    why Spanish power could not reach Portugal, and mixing them produces an
    explanation that cites the wrong asset.
    """
    rows: list[dict] = []
    for curve in curves:
        if published_before and curve.created_at and curve.created_at >= published_before:
            continue
        if direction and (curve.out_domain, curve.in_domain) != direction:
            continue
        if curve.end_utc <= start or curve.start_utc >= end:
            continue
        capacity = curve.minimum_between(start, end)
        if capacity is None:
            continue
        rows.append(
            {
                "asset": curve.label,
                "location": curve.asset_location,
                "status": curve.status,
                "business_type": curve.business_type,
                "available_mw": capacity,
                "published_at": curve.created_at,
                "outage_start": curve.start_utc,
                "outage_end": curve.end_utc,
                "direction": f"{curve.out_domain} -> {curve.in_domain}",
            }
        )

    rows.sort(key=lambda row: row["available_mw"])
    return rows


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
