"""Quantity document parsing: capacity, flows, scheduled exchanges.

Same sparse Point mechanics as prices, different value tag and a direction
instead of a single zone.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.parsing.entsoe_prices import parse_quantity_series  # noqa: E402

NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def quantity_document(points_xml: str, resolution: str = "PT60M") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="{NS}">
  <mRID>capacity-test</mRID>
  <TimeSeries>
    <mRID>1</mRID>
    <contract_MarketAgreement.type>A01</contract_MarketAgreement.type>
    <out_Domain.mRID>{EIC_SPAIN}</out_Domain.mRID>
    <in_Domain.mRID>{EIC_PORTUGAL}</in_Domain.mRID>
    <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
    <Period>
      <timeInterval>
        <start>2026-09-02T22:00Z</start>
        <end>2026-09-03T22:00Z</end>
      </timeInterval>
      <resolution>{resolution}</resolution>
      {points_xml}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


def qpoint(position: int, quantity: float) -> str:
    return (
        f"<Point><position>{position}</position>"
        f"<quantity>{quantity}</quantity></Point>"
    )


def test_parses_capacity_with_direction():
    points_xml = "".join(qpoint(i, 3000.0) for i in range(1, 25))
    parsed = parse_quantity_series(quantity_document(points_xml))

    assert len(parsed) == 24
    assert parsed[0].out_domain == EIC_SPAIN
    assert parsed[0].in_domain == EIC_PORTUGAL
    assert parsed[0].quantity_mw == 3000.0
    assert parsed[0].contract_type == "A01"
    assert parsed[0].ts_utc == datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)


def test_sparse_quantities_are_forward_filled():
    """The same omission rule as prices, so the same fix has to apply."""
    points_xml = qpoint(1, 2500.0) + qpoint(10, 1800.0)
    parsed = parse_quantity_series(quantity_document(points_xml))

    assert len(parsed) == 24
    assert [p.quantity_mw for p in parsed[:9]] == [2500.0] * 9
    assert [p.quantity_mw for p in parsed[9:]] == [1800.0] * 15


def test_quarter_hourly_quantities():
    points_xml = "".join(qpoint(i, 1000.0) for i in range(1, 97))
    parsed = parse_quantity_series(quantity_document(points_xml, "PT15M"))

    assert len(parsed) == 96
    assert (parsed[1].ts_utc - parsed[0].ts_utc).total_seconds() == 900


def test_acknowledgement_returns_empty():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:8:0">
  <Reason><code>999</code><text>No matching data found</text></Reason>
</Acknowledgement_MarketDocument>"""

    assert parse_quantity_series(xml) == []
