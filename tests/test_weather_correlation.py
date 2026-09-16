"""The weather correlation, and the confound it has to survive.

The first run of this analysis reported a correlation around -0.75 between
solar radiation and the Spanish price, and reported almost exactly the same
figure for Lisbon and the Alentejo, where the mechanism cannot apply. That is
the signature of a time of day confound rather than a finding, and these tests
pin down the difference between the two so it cannot come back quietly.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.weather import (  # noqa: E402
    compare_locations,
    local_hours,
    weather_columns,
    within_hour_correlation,
)

START = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def solar(hour: int) -> float:
    """A clean daily radiation curve, zero at night, peaking near midday."""
    if hour < 6 or hour > 18:
        return 0.0
    return 800.0 * math.sin(math.pi * (hour - 6) / 12)


def build_frame(
    days: int,
    cloud: list[float],
    price_of: callable,
    column: str = "shortwave_radiation_wm2__ES_andalusia",
) -> pd.DataFrame:
    rows = []
    for day in range(days):
        for hour in range(24):
            ts = START + timedelta(days=day, hours=hour)
            radiation = solar(hour) * cloud[day]
            rows.append(
                {
                    "ts_utc": ts,
                    column: radiation,
                    "price_es_eur_mwh": price_of(day, hour, radiation),
                }
            )
    return pd.DataFrame(rows)


def test_time_of_day_alone_produces_a_strong_but_meaningless_correlation():
    """Radiation and price both follow the clock, and nothing else.

    Day to day, cloud cover and price move independently. A correlation that
    survives here would be measuring the solar cycle, which is exactly the
    number that must not be quoted.
    """
    rng = np.random.default_rng(20260915)
    days = 40
    cloud = list(rng.uniform(0.55, 1.0, days))
    noise = rng.normal(0.0, 12.0, days)

    frame = build_frame(
        days,
        cloud,
        lambda day, hour, radiation: 100.0 - 0.06 * solar(hour) + noise[day],
    )

    result = within_hour_correlation(
        frame, "shortwave_radiation_wm2__ES_andalusia"
    )

    assert result.naive < -0.5, "the raw correlation should look impressive"
    assert abs(result.within) < 0.15, "and should vanish once the hour is fixed"
    assert result.confounded_by_hour is True


def test_a_real_within_hour_effect_survives_the_control():
    """Cloudier days are dearer at the same hour, so the control keeps it."""
    rng = np.random.default_rng(7)
    days = 40
    cloud = list(rng.uniform(0.4, 1.0, days))

    frame = build_frame(
        days,
        cloud,
        lambda day, hour, radiation: 200.0 - 0.1 * radiation,
    )

    result = within_hour_correlation(
        frame, "shortwave_radiation_wm2__ES_andalusia"
    )

    assert result.within < -0.9
    assert result.confounded_by_hour is False


def test_night_hours_are_excluded_rather_than_diluting_the_estimate():
    """Radiation is identically zero at night, so there is nothing to correlate.

    Keeping those rows would add pairs with no weather variation and pull the
    pooled figure toward zero for a reason that has nothing to do with energy.
    """
    rng = np.random.default_rng(1)
    days = 20
    cloud = list(rng.uniform(0.5, 1.0, days))

    frame = build_frame(
        days,
        cloud,
        lambda day, hour, radiation: 200.0 - 0.1 * radiation,
    )

    result = within_hour_correlation(
        frame, "shortwave_radiation_wm2__ES_andalusia"
    )
    by_hour = result.by_hour.set_index("local_hour")

    # Madrid is UTC+2 in July, so 00:00Z is 02:00 local and deep in the night.
    # The hour is still reported, with no correlation attached to it, rather
    # than dropped, so the reader can see what was excluded and why.
    assert pd.isna(by_hour.loc[2, "correlation"])
    assert by_hour.loc[2, "observations"] == days

    # The curve is strictly positive from 07:00Z to 17:00Z, which is 11 hours.
    assert result.hours_used == 11
    assert result.hours_used < len(by_hour)


def test_hours_are_local_not_utc():
    """The solar cycle follows local time, and so must the fixed effect."""
    stamps = pd.Series(
        [
            pd.Timestamp("2026-07-15T22:00Z"),  # summer, UTC+2
            pd.Timestamp("2026-01-15T23:00Z"),  # winter, UTC+1
        ]
    )
    assert list(local_hours(stamps)) == [0, 0]


def test_naive_number_is_as_strong_in_portugal_as_in_spain():
    """The placebo that makes the confound undeniable.

    Portuguese cloud cover cannot move the Spanish price. If the naive figure
    is just as strong there, the naive figure is not about the weather.
    """
    rng = np.random.default_rng(99)
    days = 40
    spain_cloud = list(rng.uniform(0.55, 1.0, days))
    lisbon_cloud = list(rng.uniform(0.55, 1.0, days))
    noise = rng.normal(0.0, 12.0, days)

    rows = []
    for day in range(days):
        for hour in range(24):
            rows.append(
                {
                    "ts_utc": START + timedelta(days=day, hours=hour),
                    "shortwave_radiation_wm2__ES_andalusia": solar(hour)
                    * spain_cloud[day],
                    "shortwave_radiation_wm2__PT_lisbon": solar(hour)
                    * lisbon_cloud[day],
                    "price_es_eur_mwh": 100.0 - 0.06 * solar(hour) + noise[day],
                }
            )

    table = compare_locations(pd.DataFrame(rows)).set_index("location")

    assert set(table.index) == {"ES_andalusia", "PT_lisbon"}
    assert abs(table.loc["ES_andalusia", "naive"] - table.loc["PT_lisbon", "naive"]) < 0.1
    assert bool(table.loc["PT_lisbon", "confounded"]) is True


def test_weather_columns_finds_every_location():
    frame = pd.DataFrame(
        columns=[
            "ts_utc",
            "shortwave_radiation_wm2__ES_andalusia",
            "shortwave_radiation_wm2__PT_lisbon",
            "wind_speed_100m_kmh__ES_galicia",
            "price_es_eur_mwh",
        ]
    )
    assert weather_columns(frame, "shortwave_radiation_wm2") == [
        "shortwave_radiation_wm2__ES_andalusia",
        "shortwave_radiation_wm2__PT_lisbon",
    ]
    assert weather_columns(frame, "wind_speed_100m_kmh") == [
        "wind_speed_100m_kmh__ES_galicia"
    ]


def test_too_little_data_reports_nothing_rather_than_a_number():
    frame = pd.DataFrame(
        {
            "ts_utc": [START],
            "shortwave_radiation_wm2__ES_andalusia": [500.0],
            "price_es_eur_mwh": [40.0],
        }
    )
    result = within_hour_correlation(
        frame, "shortwave_radiation_wm2__ES_andalusia"
    )

    assert result.naive is None
    assert result.within is None
    assert result.confounded_by_hour is None
    assert result.by_hour.empty


def test_missing_column_fails_loudly():
    frame = pd.DataFrame({"ts_utc": [START], "price_es_eur_mwh": [40.0]})
    with pytest.raises(KeyError, match="shortwave"):
        within_hour_correlation(frame, "shortwave_radiation_wm2__ES_andalusia")
