"""Open-Meteo and OMIE parsing, against synthetic payloads.

The sources deliberately differ in shape: ENTSO-E is XML over an API,
Open-Meteo is columnar JSON, OMIE is delimited files. Each shape has its own
way of going quietly wrong, so each gets tests.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.ingestion.omie import parse_marginalpdbc  # noqa: E402
from iberian.ingestion.open_meteo import parse_hourly  # noqa: E402


def weather_payload(hours: int = 24, drop: str | None = None, short: str | None = None):
    times = [f"2026-09-03T{hour:02d}:00" for hour in range(hours)]
    payload = {
        "latitude": 37.39,
        "longitude": -5.98,
        "hourly": {
            "time": times,
            "temperature_2m": [20.0 + hour * 0.5 for hour in range(hours)],
            "wind_speed_100m": [12.0] * hours,
            "shortwave_radiation": [0.0 if hour < 7 else 600.0 for hour in range(hours)],
            "cloud_cover": [10.0] * hours,
        },
    }
    if drop:
        del payload["hourly"][drop]
    if short:
        payload["hourly"][short] = payload["hourly"][short][:-3]
    return payload


def test_columnar_json_becomes_rows():
    points = parse_hourly(weather_payload(), "ES_andalusia", 37.39, -5.98)

    assert len(points) == 24
    assert points[0].ts_utc == datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
    assert points[0].temperature_c == 20.0
    assert points[12].shortwave_radiation_wm2 == 600.0
    assert points[0].location == "ES_andalusia"


def test_missing_variable_becomes_nulls_not_a_crash():
    """A variable can be absent for a location. That must not lose the hours."""
    points = parse_hourly(
        weather_payload(drop="wind_speed_100m"), "PT_lisbon", 38.72, -9.14
    )

    assert len(points) == 24
    assert all(point.wind_speed_100m_kmh is None for point in points)
    assert points[0].temperature_c == 20.0


def test_mismatched_column_length_is_refused():
    """Zipping parallel arrays of different lengths truncates in silence."""
    with pytest.raises(ValueError, match="Refusing to zip"):
        parse_hourly(weather_payload(short="temperature_2m"), "PT_porto", 41.15, -8.61)


def test_empty_payload_is_empty_not_an_error():
    assert parse_hourly({}, "PT_porto", 41.15, -8.61) == []
    assert parse_hourly({"hourly": {}}, "PT_porto", 41.15, -8.61) == []


OMIE_FILE = """MARGINALPDBC;
2026;09;03;1;45.20;44.80;
2026;09;03;2;42.10;42.10;
2026;09;03;3;40.00;39.50;
*
"""


def test_omie_file_parses_with_header_and_footer_skipped():
    prices = parse_marginalpdbc(OMIE_FILE)

    assert len(prices) == 3
    assert prices[0].market_day == date(2026, 9, 3)
    assert prices[0].period == 1
    assert prices[0].price_first_eur_mwh == 45.20
    assert prices[0].price_second_eur_mwh == 44.80


def test_omie_periods_map_onto_the_market_day_not_clock_hours():
    """Period 1 is the start of the market day, which in summer is 22:00Z."""
    prices = parse_marginalpdbc(OMIE_FILE, periods_per_day=24)

    assert prices[0].ts_utc == datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)
    assert prices[1].ts_utc == datetime(2026, 9, 2, 23, 0, tzinfo=timezone.utc)


def test_omie_handles_a_twenty_five_period_day():
    """The October clock change day has 25 periods. Assuming 24 drops one."""
    lines = ["MARGINALPDBC;"]
    for period in range(1, 26):
        lines.append(f"2026;10;25;{period};50.00;50.00;")
    lines.append("*")

    prices = parse_marginalpdbc("\n".join(lines))

    assert len(prices) == 25
    assert prices[0].ts_utc == datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
    # The 25 hour day must still land its last period inside the window.
    assert prices[-1].ts_utc < datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc)


def test_omie_comma_decimals_are_handled():
    prices = parse_marginalpdbc("MARGINALPDBC;\n2026;09;03;1;45,20;44,80;\n*")

    assert prices[0].price_first_eur_mwh == 45.20


def test_omie_html_error_page_parses_to_nothing():
    """A wrong filename returns an HTML page with a 200, not a 404."""
    assert parse_marginalpdbc("<!DOCTYPE html><html><body>Not found</body></html>") == []
