"""Quarter hourly behaviour.

Iberian day-ahead prices arrive at PT15M since the 15 minute MTU. Treating
those intervals as hours overstates every duration by four and merges episodes
across coupled intervals, so these tests pin the arithmetic down.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    detect_episodes,
    flag_decoupling,
    infer_step,
    summarise,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402

START = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)


def quarter_hourly(pt: list[float], es: list[float]) -> pd.DataFrame:
    rows = []
    for index, (pt_price, es_price) in enumerate(zip(pt, es)):
        ts = START + QUARTER * index
        rows.append(
            {
                "zone_eic": EIC_PORTUGAL,
                "ts_utc": ts,
                "price_eur_mwh": pt_price,
                "resolution": "PT15M",
            }
        )
        rows.append(
            {
                "zone_eic": EIC_SPAIN,
                "ts_utc": ts,
                "price_eur_mwh": es_price,
                "resolution": "PT15M",
            }
        )
    return pd.DataFrame(rows)


def pipeline(prices: pd.DataFrame) -> pd.DataFrame:
    flagged = flag_decoupling(build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN))
    return detect_episodes(flagged)


def test_step_is_inferred_as_fifteen_minutes():
    prices = quarter_hourly([50.0] * 96, [50.0] * 96)
    spread = build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN)
    assert infer_step(spread) == pd.Timedelta(minutes=15)


def test_two_quarter_hours_is_half_an_hour_not_two_hours():
    """The bug this was written for: 20:30 to 20:45 reported as 2 hours."""
    pt = [50.0] * 96
    es = [50.0] * 96
    pt[82] = 57.91
    pt[83] = 52.77

    episodes = pipeline(quarter_hourly(pt, es))

    assert len(episodes) == 1
    episode = episodes.iloc[0]
    assert episode["intervals"] == 2
    assert episode["duration_hours"] == pytest.approx(0.5)


def test_episode_breaks_on_a_single_coupled_quarter_hour():
    """With an hourly step, coupled quarters in the middle were swallowed."""
    pt = [50.0] * 96
    es = [50.0] * 96
    pt[40] = 70.0
    # index 41 stays coupled
    pt[42] = 70.0

    episodes = pipeline(quarter_hourly(pt, es))

    assert len(episodes) == 2
    assert episodes["duration_hours"].tolist() == [0.25, 0.25]


def test_end_is_one_step_past_the_last_interval():
    pt = [50.0] * 96
    es = [50.0] * 96
    pt[8] = 66.0

    episode = pipeline(quarter_hourly(pt, es)).iloc[0]

    assert episode["start_utc"] == START + QUARTER * 8
    assert episode["end_utc"] == START + QUARTER * 9
    assert episode["duration_hours"] == pytest.approx(0.25)


def test_summary_totals_are_in_real_hours():
    pt = [50.0] * 96
    es = [50.0] * 96
    for index in range(20, 28):  # eight consecutive quarters = two hours
        pt[index] = 80.0

    summary = summarise(pipeline(quarter_hourly(pt, es)))

    assert summary["episode_count"] == 1
    assert summary["decoupled_hours"] == pytest.approx(2.0)
    assert summary["longest_episode_hours"] == pytest.approx(2.0)


def test_mixed_resolutions_across_zones_are_rejected():
    """One zone hourly and the other quarter hourly cannot be compared."""
    rows = []
    for index in range(4):
        rows.append(
            {
                "zone_eic": EIC_PORTUGAL,
                "ts_utc": START + QUARTER * index,
                "price_eur_mwh": 50.0,
                "resolution": "PT15M",
            }
        )
    rows.append(
        {
            "zone_eic": EIC_SPAIN,
            "ts_utc": START,
            "price_eur_mwh": 50.0,
            "resolution": "PT60M",
        }
    )

    with pytest.raises(ValueError, match="different settlement resolutions"):
        build_spread_series(pd.DataFrame(rows), EIC_PORTUGAL, EIC_SPAIN)


def test_hourly_data_still_works():
    """The hourly path must keep working for historical data."""
    rows = []
    for hour in range(24):
        ts = START + timedelta(hours=hour)
        pt_price = 90.0 if hour in (10, 11) else 50.0
        for eic, price in ((EIC_PORTUGAL, pt_price), (EIC_SPAIN, 50.0)):
            rows.append(
                {
                    "zone_eic": eic,
                    "ts_utc": ts,
                    "price_eur_mwh": price,
                    "resolution": "PT60M",
                }
            )

    episodes = pipeline(pd.DataFrame(rows))

    assert len(episodes) == 1
    assert episodes.iloc[0]["duration_hours"] == pytest.approx(2.0)
