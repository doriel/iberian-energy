"""Parse ENTSO-E A44 price documents into tidy rows.

Two traps this handles explicitly, both of which silently corrupt price series
if you ignore them:

1. Namespaces. The document namespace carries a version suffix that changes
   between API revisions, so hardcoding it breaks without warning. We match on
   local tag names instead.

2. Sparse Points. ENTSO-E omits a Point when its value repeats the previous
   one. A naive parser that trusts len(Points) produces a short day and
   misaligns every timestamp after the first gap. We forward fill positions up
   to the count implied by the time interval and resolution.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from iberian.config import CONTRACT_TYPE_LABELS, DEFAULT_MARKETS

RESOLUTION_MINUTES = {
    "PT60M": 60,
    "PT30M": 30,
    "PT15M": 15,
}


@dataclass(frozen=True)
class PricePoint:
    zone_eic: str
    ts_utc: datetime
    price_eur_mwh: float
    resolution: str
    currency: str
    unit: str
    market: str = "unknown"
    contract_type: str | None = None
    series_mrid: str | None = None


def _local(tag: str) -> str:
    """Strip the namespace from an element tag."""
    return tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    for child in element.iter():
        if _local(child.tag) == name:
            return child
    return None


def _findall(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _local(child.tag) == name]


def _text(element: ET.Element | None) -> str | None:
    return element.text.strip() if element is not None and element.text else None


def _parse_instant(value: str) -> datetime:
    # ENTSO-E emits 2026-09-01T22:00Z, which fromisoformat rejects before 3.11.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _expand_period(period: ET.Element, value_tag: str) -> list[tuple[datetime, float, str]]:
    """Walk one Period into (timestamp, value, resolution) triples.

    This is where the sparse Point handling lives, and it is identical for
    price documents and quantity documents, so both share it rather than each
    growing its own subtly different copy of the forward fill.
    """
    interval = _find(period, "timeInterval")
    if interval is None:
        return []

    start_text = _text(_find(interval, "start"))
    end_text = _text(_find(interval, "end"))
    resolution = _text(_find(period, "resolution")) or "PT60M"
    if not start_text or not end_text:
        return []

    step_minutes = RESOLUTION_MINUTES.get(resolution)
    if step_minutes is None:
        raise ValueError(f"Unsupported resolution {resolution!r}")

    start = _parse_instant(start_text)
    end = _parse_instant(end_text)
    expected = int((end - start).total_seconds() // (step_minutes * 60))

    published: dict[int, float] = {}
    for point in _findall(period, "Point"):
        position_text = _text(_find(point, "position"))
        value_text = _text(_find(point, value_tag))
        if position_text is None or value_text is None:
            continue
        published[int(position_text)] = float(value_text)

    if not published:
        return []

    expanded: list[tuple[datetime, float, str]] = []
    last_value = published[min(published)]
    for position in range(1, expected + 1):
        if position in published:
            last_value = published[position]
        expanded.append(
            (
                start + timedelta(minutes=step_minutes * (position - 1)),
                last_value,
                resolution,
            )
        )
    return expanded


def parse_day_ahead_prices(
    xml_body: str,
    zone_eic: str,
    markets: Collection[str] | None = None,
) -> list[PricePoint]:
    """Turn one A44 payload into a flat list of price points.

    One document carries several markets. `contract_MarketAgreement.type` is
    what separates them: A01 is the daily auction, which is the day-ahead price
    everyone means, and A07 is intraday, of which MIBEL runs several sessions a
    day. Keeping all of them produces several prices on the same timestamp,
    which looks like a parser bug but is really several auctions.

    `markets` defaults to day-ahead only. Pass None-equivalent wider sets to
    keep intraday too, which is worth landing in bronze even if gold ignores it.

    A series with no contract type is labelled "unknown" and kept by default,
    because silently dropping a document shape we have not seen before is worse
    than letting the duplicate guard downstream complain about it.

    Returns an empty list for an Acknowledgement document, which is how the API
    reports "no data for that window" rather than an HTTP error.
    """
    wanted = set(markets) if markets is not None else set(DEFAULT_MARKETS)

    root = ET.fromstring(xml_body)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        return []

    points: list[PricePoint] = []

    for series in _findall(root, "TimeSeries"):
        currency = _text(_find(series, "currency_Unit.name")) or "EUR"
        unit = _text(_find(series, "price_Measure_Unit.name")) or "MWH"
        contract_type = _text(_find(series, "contract_MarketAgreement.type"))
        series_mrid = _text(_find(series, "mRID"))
        market = (
            CONTRACT_TYPE_LABELS.get(contract_type, f"contract_{contract_type}")
            if contract_type
            else "unknown"
        )

        if market not in wanted:
            continue

        for period in _findall(series, "Period"):
            for ts_utc, price, resolution in _expand_period(period, "price.amount"):
                points.append(
                    PricePoint(
                        zone_eic=zone_eic,
                        ts_utc=ts_utc,
                        price_eur_mwh=price,
                        resolution=resolution,
                        currency=currency,
                        unit=unit,
                        market=market,
                        contract_type=contract_type,
                        series_mrid=series_mrid,
                    )
                )

    return points


@dataclass(frozen=True)
class QuantityPoint:
    """A value from a quantity document: capacity, flow or scheduled exchange."""

    out_domain: str
    in_domain: str
    ts_utc: datetime
    quantity_mw: float
    resolution: str
    contract_type: str | None = None
    series_mrid: str | None = None


def parse_quantity_series(xml_body: str) -> list[QuantityPoint]:
    """Parse a quantity document: A61 capacity, A09 exchanges, A11 flows.

    Same Period and sparse Point mechanics as the price documents, but the
    value tag is `quantity` and the direction lives on the series rather than
    being a single zone. Direction matters: ENTSO-E returns one direction per
    request, so PT to ES and ES to PT are separate documents.
    """
    root = ET.fromstring(xml_body)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        return []

    points: list[QuantityPoint] = []

    for series in _findall(root, "TimeSeries"):
        out_domain = _text(_find(series, "out_Domain.mRID")) or ""
        in_domain = _text(_find(series, "in_Domain.mRID")) or ""
        contract_type = _text(_find(series, "contract_MarketAgreement.type"))
        series_mrid = _text(_find(series, "mRID"))

        for period in _findall(series, "Period"):
            for ts_utc, quantity, resolution in _expand_period(period, "quantity"):
                points.append(
                    QuantityPoint(
                        out_domain=out_domain,
                        in_domain=in_domain,
                        ts_utc=ts_utc,
                        quantity_mw=quantity,
                        resolution=resolution,
                        contract_type=contract_type,
                        series_mrid=series_mrid,
                    )
                )

    return points


def parse_prices_response(
    response, zone_eic: str, markets: Collection[str] | None = None
) -> list[PricePoint]:
    """Parse every document in a response, zipped or not.

    Always prefer this over calling the parser on `.body`: a long enough
    request comes back as a ZIP of many documents, and reading only the first
    one loses most of the data without raising anything.
    """
    points: list[PricePoint] = []
    for document in response.documents():
        points.extend(parse_day_ahead_prices(document, zone_eic, markets))
    return points


def parse_quantities_response(response) -> list[QuantityPoint]:
    """Parse every quantity document in a response, zipped or not."""
    points: list[QuantityPoint] = []
    for document in response.documents():
        points.extend(parse_quantity_series(document))
    return points


def quantities_to_records(points: list[QuantityPoint], label: str) -> list[dict]:
    from iberian.market_time import to_market_day

    return [
        {
            "series_kind": label,
            "out_domain": p.out_domain,
            "in_domain": p.in_domain,
            "ts_utc": p.ts_utc,
            "market_day": to_market_day(p.ts_utc),
            "quantity_mw": p.quantity_mw,
            "resolution": p.resolution,
            "contract_type": p.contract_type,
        }
        for p in points
    ]


def to_records(points: list[PricePoint]) -> list[dict]:
    """Flatten to dicts, ready for a DataFrame or a Delta write."""
    from iberian.market_time import to_market_day

    return [
        {
            "zone_eic": p.zone_eic,
            "ts_utc": p.ts_utc,
            "market_day": to_market_day(p.ts_utc),
            "price_eur_mwh": p.price_eur_mwh,
            "resolution": p.resolution,
            "currency": p.currency,
            "unit": p.unit,
            "market": p.market,
            "contract_type": p.contract_type,
            "series_mrid": p.series_mrid,
        }
        for p in points
    ]
