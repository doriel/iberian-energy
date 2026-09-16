"""ESIOS parsing, against payloads shaped like the ones the API returned.

The fixtures here are trimmed copies of real responses, so the field names and
the timestamp format are what the probe actually observed rather than what the
documentation implies.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.ingestion.esios import (  # noqa: E402
    GEO_PENINSULA,
    INDICATORS,
    EsiosClient,
    IndicatorResponse,
    congestion_rent_by_day,
    forecast_error,
    infer_resolution,
    to_frame,
    to_records,
)


def build(indicator_id: int, values: list[dict], name: str = "test") -> IndicatorResponse:
    payload = {"indicator": {"id": indicator_id, "short_name": name, "values": values}}
    return IndicatorResponse(
        indicator_id=indicator_id, content=json.dumps(payload).encode("utf-8")
    )


def quarter_hours(count: int, start: str = "2026-09-01T04:00:00Z") -> list[str]:
    first = pd.Timestamp(start)
    return [(first + pd.Timedelta(minutes=15 * i)).isoformat() for i in range(count)]


def value_row(stamp: str, value: float, geo_id: int = GEO_PENINSULA) -> dict:
    # The real payload carries a local datetime as well. It is deliberately
    # present in the fixture and deliberately unused by the parser.
    local = pd.Timestamp(stamp).tz_convert("Europe/Madrid").isoformat()
    return {
        "value": value,
        "datetime": local,
        "datetime_utc": stamp,
        "tz_time": stamp,
        "geo_id": geo_id,
        "geo_name": "Península" if geo_id == GEO_PENINSULA else "Otro",
    }


def test_timestamps_come_from_the_utc_field_not_the_local_one():
    """The API already converted. Redoing it by hand is how bugs get in."""
    response = build(1119, [value_row("2026-09-01T04:00:00Z", 12.5)])
    rows = to_records(response)

    assert len(rows) == 1
    assert rows[0]["ts_utc"] == pd.Timestamp("2026-09-01T04:00:00Z")
    assert str(rows[0]["ts_utc"].tz) == "UTC"


def test_market_day_follows_the_iberian_boundary_not_utc_midnight():
    """22:00Z in summer is already the next market day."""
    response = build(1119, [value_row("2026-08-31T22:00:00Z", 3.0)])
    rows = to_records(response)

    assert rows[0]["market_day"] == date(2026, 9, 1)


def test_other_geographies_are_dropped_rather_than_summed():
    """Two geographies in one series is a silent doubling waiting to happen."""
    stamps = quarter_hours(2)
    response = build(
        1119,
        [
            value_row(stamps[0], 10.0),
            value_row(stamps[0], 99.0, geo_id=1234),
            value_row(stamps[1], 20.0),
        ],
    )
    rows = to_records(response)

    assert len(rows) == 2
    assert [row["value"] for row in rows] == [10.0, 20.0]
    assert {row["geo_id"] for row in rows} == {GEO_PENINSULA}


def test_geo_filter_can_be_disabled_for_exploration():
    stamps = quarter_hours(1)
    response = build(
        1119, [value_row(stamps[0], 10.0), value_row(stamps[0], 99.0, geo_id=1234)]
    )
    assert len(to_records(response, geo_id=None)) == 2


def test_resolution_is_recorded_because_indicators_disagree():
    """Congestion rent is quarter hourly, the demand forecast is hourly."""
    rent = to_records(
        build(1119, [value_row(s, 1.0) for s in quarter_hours(8)])
    )
    assert {row["resolution"] for row in rent} == {"PT15M"}

    hourly = [
        (pd.Timestamp("2026-09-01T04:00:00Z") + pd.Timedelta(hours=i)).isoformat()
        for i in range(6)
    ]
    forecast = to_records(build(1775, [value_row(s, 25000.0) for s in hourly]))
    assert {row["resolution"] for row in forecast} == {"PT60M"}


def test_a_single_point_does_not_claim_a_resolution():
    assert infer_resolution([pd.Timestamp("2026-09-01T00:00:00Z")]) == "unknown"
    assert infer_resolution([]) == "unknown"


def test_known_indicators_are_named_so_tables_are_readable():
    rows = to_records(build(1119, [value_row("2026-09-01T04:00:00Z", 1.0)]))
    assert rows[0]["indicator"] == "congestion_rent_pt_import"


def test_empty_values_do_not_explode():
    assert to_records(build(1119, [])) == []
    assert to_frame([]).empty


def test_null_values_survive_as_null_rather_than_zero():
    """A missing publication is not a rent of zero euros."""
    stamps = quarter_hours(2)
    response = build(
        1119,
        [
            value_row(stamps[0], 5.0),
            {**value_row(stamps[1], 0.0), "value": None},
        ],
    )
    rows = to_records(response)
    assert rows[0]["value"] == 5.0
    assert rows[1]["value"] is None


def test_congestion_rent_sums_both_directions_per_market_day():
    stamps = quarter_hours(4, "2026-09-01T04:00:00Z")
    frame = to_frame(
        [
            build(INDICATORS["congestion_rent_pt_import"],
                  [value_row(s, 100.0) for s in stamps]),
            build(INDICATORS["congestion_rent_pt_export"],
                  [value_row(s, 0.0) for s in stamps]),
        ]
    )
    daily = congestion_rent_by_day(frame)

    assert len(daily) == 1
    assert daily.iloc[0]["congestion_rent_eur"] == pytest.approx(400.0)
    assert daily.iloc[0]["intervals"] == 8


def test_congestion_rent_on_an_empty_frame_returns_the_right_shape():
    daily = congestion_rent_by_day(pd.DataFrame(columns=["indicator_id", "value"]))
    assert daily.empty
    assert list(daily.columns) == ["market_day", "congestion_rent_eur", "intervals"]


def test_forecast_error_is_actual_minus_forecast():
    hours = [
        (pd.Timestamp("2026-09-01T04:00:00Z") + pd.Timedelta(hours=i)).isoformat()
        for i in range(3)
    ]
    frame = to_frame(
        [
            build(INDICATORS["demand_forecast_d1"],
                  [value_row(s, 25000.0) for s in hours]),
            build(INDICATORS["demand_actual"],
                  [value_row(s, 26000.0) for s in hours]),
        ]
    )
    errors = forecast_error(frame)

    assert len(errors) == 3
    assert errors["error_mw"].iloc[0] == pytest.approx(1000.0)
    assert errors["error_pct"].iloc[0] == pytest.approx(4.0)


def test_forecast_error_without_both_series_returns_empty_not_nonsense():
    hours = [pd.Timestamp("2026-09-01T04:00:00Z").isoformat()]
    frame = to_frame(
        [build(INDICATORS["demand_forecast_d1"], [value_row(hours[0], 25000.0)])]
    )
    assert forecast_error(frame).empty


def test_client_refuses_to_start_without_a_token():
    with pytest.raises(ValueError, match="ESIOS token"):
        EsiosClient("")


def test_window_is_formatted_as_the_api_expects():
    from iberian.ingestion.esios import _iso

    assert (
        _iso(datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc))
        == "2026-09-01T04:00:00Z"
    )
    # A naive datetime is assumed UTC rather than silently taking the machine's
    # timezone, which would give a different window on a laptop in Lisbon.
    assert _iso(datetime(2026, 9, 1, 4, 0)) == "2026-09-01T04:00:00Z"
