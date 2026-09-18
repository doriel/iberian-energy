"""Market day windows, including the clock change days that are 23 and 25 hours.

Hardcoding a 22:00Z boundary works for eight months of the year and quietly
produces a wrong day for the other four, plus two broken days in between.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.market_time import (  # noqa: E402
    market_day_range,
    market_day_window,
    to_market_day,
)


def test_summer_market_day_starts_at_22z():
    start, end = market_day_window(date(2026, 9, 12))
    assert start == datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
    assert (end - start).total_seconds() / 3600 == 24


def test_winter_market_day_starts_at_23z():
    start, end = market_day_window(date(2026, 1, 15))
    assert start == datetime(2026, 1, 14, 23, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)


def test_spring_forward_day_is_23_hours():
    """Last Sunday of March: the market day loses an hour."""
    start, end = market_day_window(date(2026, 3, 29))
    assert (end - start).total_seconds() / 3600 == 23


def test_autumn_back_day_is_25_hours():
    """Last Sunday of October: the market day gains an hour."""
    start, end = market_day_window(date(2026, 10, 25))
    assert (end - start).total_seconds() / 3600 == 25


def test_range_spans_consecutive_days():
    start, end = market_day_range(date(2026, 9, 1), 7)
    assert start == datetime(2026, 8, 31, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 7, 22, 0, tzinfo=timezone.utc)
    assert (end - start).total_seconds() / 3600 == 24 * 7


def test_instant_maps_back_to_its_market_day():
    # 22:00Z on the 11th is already the market day of the 12th in summer.
    assert to_market_day(datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)) == date(
        2026, 9, 12
    )
    assert to_market_day(datetime(2026, 9, 11, 21, 59, tzinfo=timezone.utc)) == date(
        2026, 9, 11
    )


# --- the Spark boundary -----------------------------------------------------


def test_a_naive_timestamp_cannot_be_given_a_market_day():
    """The failure that broke the gold tables in the pipeline.

    Spark hands pandas timestamps with no timezone. Pinned here so nobody
    "fixes" `to_market_day` by assuming a zone, which would move the market day
    boundary silently instead of failing loudly.
    """
    import pandas as pd
    import pytest as _pytest

    from iberian.market_time import as_utc, to_market_day

    with _pytest.raises(TypeError):
        to_market_day(pd.Timestamp("2026-08-18 23:30:00"))

    frame = as_utc(pd.DataFrame({"ts_utc": [pd.Timestamp("2026-08-18 23:30:00")]}), "ts_utc")
    assert to_market_day(frame.iloc[0]["ts_utc"]) == date(2026, 8, 19)


def test_as_utc_leaves_an_already_aware_column_on_the_same_instant():
    import pandas as pd

    from iberian.market_time import as_utc, to_market_day

    aware = pd.DataFrame({"ts_utc": [pd.Timestamp("2026-08-18T23:30:00Z")]})
    assert as_utc(aware, "ts_utc").iloc[0]["ts_utc"] == aware.iloc[0]["ts_utc"]
    assert to_market_day(as_utc(aware, "ts_utc").iloc[0]["ts_utc"]) == date(2026, 8, 19)


def test_as_utc_ignores_a_column_that_is_not_there():
    import pandas as pd

    from iberian.market_time import as_utc

    frame = pd.DataFrame({"ts_utc": [pd.Timestamp("2026-08-18T10:00:00Z")]})
    assert list(as_utc(frame, "landed_at").columns) == ["ts_utc"]