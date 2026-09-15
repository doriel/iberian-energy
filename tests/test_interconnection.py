"""Saturation joins, with the hourly to quarter hourly alignment pinned down."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.interconnection import (  # noqa: E402
    align_capacity_to_schedule,
    build_border_series,
    saturation_evidence,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402

START = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)


def schedule_rows(forward: list[float], reverse: list[float] | None = None):
    rows = []
    for index, value in enumerate(forward):
        rows.append(
            {
                "out_domain": EIC_SPAIN,
                "in_domain": EIC_PORTUGAL,
                "ts_utc": START + QUARTER * index,
                "quantity_mw": value,
            }
        )
    for index, value in enumerate(reverse or []):
        rows.append(
            {
                "out_domain": EIC_PORTUGAL,
                "in_domain": EIC_SPAIN,
                "ts_utc": START + QUARTER * index,
                "quantity_mw": value,
            }
        )
    return pd.DataFrame(rows)


def capacity_rows(hourly: list[float], out_=EIC_SPAIN, in_=EIC_PORTUGAL):
    return pd.DataFrame(
        [
            {
                "out_domain": out_,
                "in_domain": in_,
                "ts_utc": START + timedelta(hours=index),
                "quantity_mw": value,
            }
            for index, value in enumerate(hourly)
        ]
    )


def test_hourly_capacity_is_broadcast_across_the_hour():
    capacity = capacity_rows([3000.0, 2000.0]).rename(
        columns={"quantity_mw": "capacity_mw"}
    )
    target = [START + QUARTER * i for i in range(8)]

    aligned = align_capacity_to_schedule(capacity, pd.DatetimeIndex(target))

    assert aligned.tolist() == [3000.0] * 4 + [2000.0] * 4


def test_missing_capacity_hour_leaves_a_gap_rather_than_inventing_headroom():
    """Filling forward past the hour would invent a limit nobody published."""
    capacity = capacity_rows([3000.0]).rename(columns={"quantity_mw": "capacity_mw"})
    target = [START + QUARTER * i for i in range(12)]  # three hours of schedule

    aligned = align_capacity_to_schedule(capacity, pd.DatetimeIndex(target))

    assert aligned.iloc[:4].tolist() == [3000.0] * 4
    assert aligned.iloc[4:].isna().all()


def test_utilisation_uses_net_flow_not_gross():
    schedules = schedule_rows([1000.0, 1000.0, 1000.0, 1000.0], [400.0] * 4)
    capacity = capacity_rows([3000.0])

    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))

    assert border["net_flow_mw"].tolist() == [600.0] * 4
    assert border["utilisation"].round(3).tolist() == [0.2] * 4


def test_full_border_is_flagged_saturated():
    schedules = schedule_rows([2950.0, 3000.0, 1500.0, 0.0])
    capacity = capacity_rows([3000.0])

    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))

    assert border["is_saturated"].tolist() == [True, True, False, False]


def test_reverse_flow_is_headroom_not_saturation():
    """Flow the other way must not read as a full border in this direction."""
    schedules = schedule_rows([0.0] * 4, [2000.0] * 4)
    capacity = capacity_rows([3000.0])

    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))

    assert border["net_flow_mw"].tolist() == [-2000.0] * 4
    assert border["utilisation"].tolist() == [0.0] * 4
    assert not border["is_saturated"].any()


def test_evidence_counts_the_contingency():
    joined = pd.DataFrame(
        {
            "ts_utc": [START + QUARTER * i for i in range(4)],
            "is_decoupled": [True, True, False, False],
            "is_saturated": [True, False, True, False],
            "utilisation": [1.0, 0.5, 0.99, 0.2],
        }
    )

    evidence = saturation_evidence(joined)

    assert evidence["decoupled_and_saturated"] == 1
    assert evidence["decoupled_not_saturated"] == 1
    assert evidence["saturated_not_decoupled"] == 1
    assert evidence["share_of_splits_explained"] == pytest.approx(0.5)


def test_missing_direction_fails_loudly():
    schedules = schedule_rows([100.0] * 4)
    capacity = capacity_rows([3000.0], out_=EIC_PORTUGAL, in_=EIC_SPAIN)

    with pytest.raises(ValueError, match="No capacity published"):
        build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))
