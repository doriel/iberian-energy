"""Unit tests for the market splitting core.

Synthetic data only, so these run anywhere with no credentials and no network.
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
    summarise,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402

START = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def make_prices(pt: list[float], es: list[float], skip_hours: set[int] | None = None):
    """Build tidy long price rows. skip_hours drops an hour from both zones."""
    skip_hours = skip_hours or set()
    rows = []
    for hour, (pt_price, es_price) in enumerate(zip(pt, es)):
        if hour in skip_hours:
            continue
        ts = START + timedelta(hours=hour)
        rows.append({"zone_eic": EIC_PORTUGAL, "ts_utc": ts, "price_eur_mwh": pt_price})
        rows.append({"zone_eic": EIC_SPAIN, "ts_utc": ts, "price_eur_mwh": es_price})
    return pd.DataFrame(rows)


def pipeline(prices: pd.DataFrame) -> pd.DataFrame:
    spread = build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN)
    return detect_episodes(flag_decoupling(spread))


def test_fully_coupled_market_has_no_episodes():
    prices = make_prices([50.0] * 24, [50.0] * 24)
    assert pipeline(prices).empty


def test_single_split_is_one_episode_with_correct_duration():
    pt = [50.0] * 24
    es = [50.0] * 24
    for hour in (18, 19, 20):
        pt[hour] = 75.0  # Portugal pays the premium for three evening hours

    episodes = pipeline(prices=make_prices(pt, es))

    assert len(episodes) == 1
    episode = episodes.iloc[0]
    assert episode["duration_hours"] == 3
    assert episode["start_utc"] == START + timedelta(hours=18)
    # end_utc is exclusive: the split covers 18:00, 19:00 and 20:00, and stops
    # at 21:00. Reporting the last interval's start instead would make a one
    # interval episode look like it lasted no time at all.
    assert episode["end_utc"] == START + timedelta(hours=21)
    assert episode["peak_spread"] == pytest.approx(25.0)
    assert episode["premium_side"] == "PT"
    assert episode["max_severity"] == "severe"


def test_spain_can_pay_the_premium_too():
    pt = [50.0] * 24
    es = [50.0] * 24
    es[10] = 58.0

    episode = pipeline(make_prices(pt, es)).iloc[0]
    assert episode["premium_side"] == "ES"
    assert episode["peak_spread"] == pytest.approx(-8.0)
    assert episode["max_severity"] == "moderate"


def test_missing_hour_splits_one_episode_into_two():
    """A data gap must break the episode, otherwise duration is overstated."""
    pt = [50.0] * 24
    es = [50.0] * 24
    for hour in range(8, 16):
        pt[hour] = 65.0

    with_gap = make_prices(pt, es, skip_hours={11, 12})
    episodes = pipeline(with_gap)

    assert len(episodes) == 2
    assert episodes["duration_hours"].tolist() == [3, 3]


def test_rounding_noise_is_not_a_split():
    pt = [50.0] * 24
    es = [50.0] * 24
    pt[5] = 50.005  # below the epsilon, this is a rounding artefact

    assert pipeline(make_prices(pt, es)).empty


def test_severity_bands():
    cases = [(3.0, "minor"), (12.0, "moderate"), (40.0, "severe")]
    for delta, expected in cases:
        pt = [50.0] * 24
        es = [50.0] * 24
        pt[2] = 50.0 + delta
        assert pipeline(make_prices(pt, es)).iloc[0]["max_severity"] == expected


def test_hour_missing_on_one_side_only_is_dropped():
    prices = make_prices([50.0] * 5, [50.0] * 5)
    prices = prices.drop(
        prices[
            (prices["zone_eic"] == EIC_SPAIN)
            & (prices["ts_utc"] == START + timedelta(hours=3))
        ].index
    )

    spread = build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN)
    assert len(spread) == 4
    assert (START + timedelta(hours=3)) not in spread["ts_utc"].tolist()


def test_summarise_reports_headline_numbers():
    pt = [50.0] * 24
    es = [50.0] * 24
    for hour in (1, 2):
        pt[hour] = 90.0
    pt[20] = 55.0

    summary = summarise(pipeline(make_prices(pt, es)))
    assert summary["episode_count"] == 2
    assert summary["decoupled_hours"] == 3
    assert summary["worst_spread"] == pytest.approx(40.0)
    assert summary["longest_episode_hours"] == 2
