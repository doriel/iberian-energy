"""Parser tests, including the sparse Point trap that silently shifts a day."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import EIC_PORTUGAL  # noqa: E402
from iberian.parsing.entsoe_prices import parse_day_ahead_prices  # noqa: E402

NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def timeseries(
    points_xml: str,
    start: str,
    end: str,
    resolution: str = "PT60M",
    contract_type: str | None = None,
    mrid: str = "1",
) -> str:
    contract = (
        f"<contract_MarketAgreement.type>{contract_type}"
        "</contract_MarketAgreement.type>"
        if contract_type
        else ""
    )
    return f"""<TimeSeries>
    <mRID>{mrid}</mRID>
    {contract}
    <currency_Unit.name>EUR</currency_Unit.name>
    <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <Period>
      <timeInterval>
        <start>{start}</start>
        <end>{end}</end>
      </timeInterval>
      <resolution>{resolution}</resolution>
      {points_xml}
    </Period>
  </TimeSeries>"""


def document(points_xml: str, start: str, end: str, resolution: str = "PT60M") -> str:
    return wrap(timeseries(points_xml, start, end, resolution))


def wrap(*series: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="{NS}">
  <mRID>test</mRID>
  {"".join(series)}
</Publication_MarketDocument>"""


def point(position: int, amount: float) -> str:
    return f"<Point><position>{position}</position><price.amount>{amount}</price.amount></Point>"


def test_parses_a_dense_day():
    points = "".join(point(i, 40.0 + i) for i in range(1, 25))
    xml = document(points, "2026-09-01T22:00Z", "2026-09-02T22:00Z")

    parsed = parse_day_ahead_prices(xml, EIC_PORTUGAL)

    assert len(parsed) == 24
    assert parsed[0].ts_utc == datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)
    assert parsed[0].price_eur_mwh == 41.0
    assert parsed[-1].price_eur_mwh == 64.0


def test_sparse_points_are_forward_filled():
    """ENTSO-E omits repeated values. Positions 1, 5 and 20 imply 24 hours."""
    points = point(1, 30.0) + point(5, 55.0) + point(20, 12.5)
    xml = document(points, "2026-09-01T22:00Z", "2026-09-02T22:00Z")

    parsed = parse_day_ahead_prices(xml, EIC_PORTUGAL)

    assert len(parsed) == 24
    assert [p.price_eur_mwh for p in parsed[:4]] == [30.0] * 4
    assert [p.price_eur_mwh for p in parsed[4:19]] == [55.0] * 15
    assert [p.price_eur_mwh for p in parsed[19:]] == [12.5] * 5


def test_quarter_hourly_resolution():
    points = "".join(point(i, 20.0) for i in range(1, 97))
    xml = document(points, "2026-09-01T22:00Z", "2026-09-02T22:00Z", "PT15M")

    parsed = parse_day_ahead_prices(xml, EIC_PORTUGAL)

    assert len(parsed) == 96
    assert (parsed[1].ts_utc - parsed[0].ts_utc).total_seconds() == 900


def test_intraday_series_are_excluded_by_default():
    """The real trap: one A44 document carries day-ahead plus several intraday
    sessions on the same timestamps. Keeping all of them gives you several
    prices per hour, which looks like duplicate rows but is several auctions."""
    day_ahead = "".join(point(i, 50.0) for i in range(1, 25))
    intraday = "".join(point(i, 61.0) for i in range(1, 25))
    session_two = "".join(point(i, 63.0) for i in range(1, 13))

    xml = wrap(
        timeseries(day_ahead, "2026-09-11T22:00Z", "2026-09-12T22:00Z",
                   contract_type="A01", mrid="1"),
        timeseries(intraday, "2026-09-11T22:00Z", "2026-09-12T22:00Z",
                   contract_type="A07", mrid="2"),
        timeseries(session_two, "2026-09-12T10:00Z", "2026-09-12T22:00Z",
                   contract_type="A07", mrid="3"),
    )

    parsed = parse_day_ahead_prices(xml, EIC_PORTUGAL)

    assert len(parsed) == 24
    assert {p.market for p in parsed} == {"day_ahead"}
    assert all(p.price_eur_mwh == 50.0 for p in parsed)
    assert len({p.ts_utc for p in parsed}) == 24  # no duplicate timestamps


def test_intraday_can_be_requested_explicitly():
    day_ahead = "".join(point(i, 50.0) for i in range(1, 25))
    intraday = "".join(point(i, 61.0) for i in range(1, 25))
    xml = wrap(
        timeseries(day_ahead, "2026-09-11T22:00Z", "2026-09-12T22:00Z",
                   contract_type="A01"),
        timeseries(intraday, "2026-09-11T22:00Z", "2026-09-12T22:00Z",
                   contract_type="A07", mrid="2"),
    )

    parsed = parse_day_ahead_prices(
        xml, EIC_PORTUGAL, markets={"day_ahead", "intraday"}
    )

    assert len(parsed) == 48
    assert {p.market for p in parsed} == {"day_ahead", "intraday"}


def test_series_without_contract_type_is_kept_as_unknown():
    """Do not silently drop a document shape we have not seen before."""
    points = "".join(point(i, 44.0) for i in range(1, 25))
    parsed = parse_day_ahead_prices(document(points, "2026-09-11T22:00Z",
                                             "2026-09-12T22:00Z"), EIC_PORTUGAL)

    assert len(parsed) == 24
    assert {p.market for p in parsed} == {"unknown"}


def test_two_market_days_in_one_response_do_not_collide():
    """A UTC day request returns two market days. They must stay distinct."""
    first = "".join(point(i, 50.0) for i in range(1, 25))
    second = "".join(point(i, 70.0) for i in range(1, 25))
    xml = wrap(
        timeseries(first, "2026-09-11T22:00Z", "2026-09-12T22:00Z",
                   contract_type="A01", mrid="1"),
        timeseries(second, "2026-09-12T22:00Z", "2026-09-13T22:00Z",
                   contract_type="A01", mrid="5"),
    )

    parsed = parse_day_ahead_prices(xml, EIC_PORTUGAL)

    assert len(parsed) == 48
    assert len({p.ts_utc for p in parsed}) == 48


def test_acknowledgement_document_returns_empty():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:8:0">
  <Reason><code>999</code><text>No matching data found</text></Reason>
</Acknowledgement_MarketDocument>"""

    assert parse_day_ahead_prices(xml, EIC_PORTUGAL) == []
