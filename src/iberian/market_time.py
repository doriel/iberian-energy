"""Market day arithmetic.

The Iberian market day is local midnight to local midnight in CET, not UTC
midnight. Requesting a UTC calendar day returns two partially overlapping
market days, which is how you end up with twice the rows you expected and a
spread computed across a boundary that does not exist in the market.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from iberian.config import MARKET_TIMEZONE

_TZ = ZoneInfo(MARKET_TIMEZONE)


def market_day_window(day: date) -> tuple[datetime, datetime]:
    """UTC start and end of one Iberian market day.

    In summer this is 22:00Z to 22:00Z, in winter 23:00Z to 23:00Z, and on the
    two clock change days the window is 23 or 25 hours long. Deriving it from
    the timezone rather than hardcoding an offset is what makes those days work.
    """
    local_start = datetime.combine(day, time(0, 0), tzinfo=_TZ)
    local_end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=_TZ)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def market_day_range(start_day: date, days: int) -> tuple[datetime, datetime]:
    """UTC window spanning several consecutive market days."""
    if days < 1:
        raise ValueError("days must be at least 1")
    window_start, _ = market_day_window(start_day)
    _, window_end = market_day_window(start_day + timedelta(days=days - 1))
    return window_start, window_end


def to_market_day(ts_utc: datetime) -> date:
    """Which market day a UTC instant belongs to."""
    return ts_utc.astimezone(_TZ).date()


def as_utc(frame, *columns: str):
    """Make timestamp columns tz-aware in UTC, and say so when they are not.

    Spark hands a pandas function timestamps without a timezone. `to_market_day`
    converts to CET to find the market day, which a naive timestamp cannot do,
    so the whole gold build fails at the boundary between the two worlds.

    The tempting fix is to localize wherever the data happens to be read. That
    would be worse than the crash: the market day begins at local midnight, so
    localizing into the wrong zone shifts every boundary by an hour or two and
    nothing raises. Everything that crosses this boundary is UTC by
    construction, having come from an ENTSO-E instant, so it is localized as
    UTC and anything already carrying a zone is converted to it.
    """
    import pandas as pd

    out = frame.copy()
    for column in columns:
        if column not in out.columns:
            continue
        values = pd.to_datetime(out[column], utc=True, errors="coerce")
        out[column] = values
    return out