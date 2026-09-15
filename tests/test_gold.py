"""Gold tables, one per persona.

Every gold table must serve a named user. These tests check
the numbers those users would act on, especially the cost figure, which is the
easiest thing in the whole project to overstate by an order of magnitude.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.market_splitting import build_spread_series, flag_decoupling  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.pipeline.gold import (  # noqa: E402
    gold_daily_profile,
    gold_interval_premium,
    gold_split_episodes,
    gold_tables,
    gold_weather_context,
)

START = datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)


def build_flagged(pt: list[float], es: list[float]) -> pd.DataFrame:
    rows = []
    for index, (pt_price, es_price) in enumerate(zip(pt, es)):
        ts = START + QUARTER * index
        rows.append({"zone_eic": EIC_PORTUGAL, "ts_utc": ts, "price_eur_mwh": pt_price})
        rows.append({"zone_eic": EIC_SPAIN, "ts_utc": ts, "price_eur_mwh": es_price})
    return flag_decoupling(
        build_spread_series(pd.DataFrame(rows), EIC_PORTUGAL, EIC_SPAIN)
    )


def build_border(count: int, flow: float = 1000.0, capacity: float = 2000.0):
    return pd.DataFrame(
        {
            "ts_utc": [START + QUARTER * i for i in range(count)],
            "net_flow_mw": [flow] * count,
            "capacity_mw": [capacity] * count,
            "utilisation": [flow / capacity] * count,
            "is_saturated": [flow / capacity >= 0.98] * count,
        }
    )


def test_interval_table_carries_the_market_day_and_hour():
    flagged = build_flagged([50.0] * 8, [50.0] * 8)
    intervals = gold_interval_premium(flagged)

    assert len(intervals) == 8
    assert "premium_eur_mwh" in intervals.columns
    # 22:00Z on 2 September belongs to the market day of the 3rd.
    assert intervals["market_day"].iloc[0].isoformat() == "2026-09-03"
    assert intervals["hour_of_day_utc"].iloc[0] == 22


def test_daily_profile_answers_when_to_run_equipment():
    """Persona 1: is this hour reliably worse than that one?"""
    pt = [50.0] * 8
    es = [50.0] * 8
    pt[4] = 80.0  # one interval in the second hour carries a premium
    pt[5] = 80.0

    profile = gold_daily_profile(gold_interval_premium(build_flagged(pt, es)))

    first_hour = profile.loc[profile["hour_of_day_utc"] == 22].iloc[0]
    second_hour = profile.loc[profile["hour_of_day_utc"] == 23].iloc[0]

    assert first_hour["split_probability"] == 0.0
    assert second_hour["split_probability"] == 0.5
    assert second_hour["worst_premium_eur_mwh"] == pytest.approx(30.0)


def test_episode_cost_uses_imported_energy_not_national_demand():
    """The cost figure is premium times energy actually imported.

    Two quarter hours at a 30 EUR/MWh premium with 1000 MW flowing is
    30 * (1000 * 0.25) * 2 = 15,000 EUR. Multiplying by Portuguese demand
    instead would inflate this by two orders of magnitude, and it is exactly
    the number a journalist would quote.
    """
    pt = [50.0] * 8
    es = [50.0] * 8
    pt[4] = 80.0
    pt[5] = 80.0

    flagged = build_flagged(pt, es)
    episodes = gold_split_episodes(flagged, build_border(8))

    assert len(episodes) == 1
    assert episodes.iloc[0]["extra_cost_eur"] == pytest.approx(15000.0)
    assert episodes.iloc[0]["duration_hours"] == pytest.approx(0.5)


def test_episode_marks_whether_saturation_explains_it():
    pt = [50.0] * 8
    es = [50.0] * 8
    pt[2] = 70.0

    flagged = build_flagged(pt, es)

    full = gold_split_episodes(flagged, build_border(8, flow=2000.0, capacity=2000.0))
    assert full.iloc[0]["share_saturated"] == 1.0
    assert bool(full.iloc[0]["explained_by_saturation"]) is True

    loose = gold_split_episodes(flagged, build_border(8, flow=500.0, capacity=2000.0))
    assert loose.iloc[0]["share_saturated"] == 0.0
    assert bool(loose.iloc[0]["explained_by_saturation"]) is False


def test_reverse_flow_does_not_create_negative_cost():
    """Flow the other way is not a refund."""
    pt = [50.0] * 8
    es = [50.0] * 8
    pt[2] = 70.0

    episodes = gold_split_episodes(build_flagged(pt, es), build_border(8, flow=-800.0))

    assert episodes.iloc[0]["extra_cost_eur"] == 0.0


def test_hourly_weather_is_broadcast_but_labelled():
    flagged = build_flagged([50.0] * 8, [50.0] * 8)
    intervals = gold_interval_premium(flagged)
    weather = pd.DataFrame(
        [
            {
                "ts_utc": START,
                "location": "ES_andalusia",
                "shortwave_radiation_wm2": 0.0,
                "wind_speed_100m_kmh": 10.0,
                "temperature_c": 22.0,
            },
            {
                "ts_utc": START + timedelta(hours=1),
                "location": "ES_andalusia",
                "shortwave_radiation_wm2": 0.0,
                "wind_speed_100m_kmh": 14.0,
                "temperature_c": 21.0,
            },
        ]
    )

    out = gold_weather_context(intervals, weather)

    assert len(out) == 8
    column = "wind_speed_100m_kmh__ES_andalusia"
    assert out[column].iloc[0] == 10.0
    assert out[column].iloc[3] == 10.0  # still inside the first hour
    assert out[column].iloc[4] == 14.0  # second hour
    assert set(out["weather_resolution"]) == {"PT60M"}


def test_gold_tables_builds_every_persona_table():
    flagged = build_flagged([50.0] * 8, [50.0] * 8)
    tables = gold_tables(flagged, build_border(8))

    assert "gold_interval_premium" in tables
    assert "gold_daily_profile" in tables
    assert "gold_split_episodes" in tables


def test_empty_input_does_not_explode():
    empty = pd.DataFrame(
        columns=["ts_utc", "price_pt", "price_es", "spread_eur_mwh", "is_decoupled"]
    )
    assert gold_split_episodes(empty).empty
    assert gold_daily_profile(pd.DataFrame()).empty
