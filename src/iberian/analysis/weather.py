"""Weather against price, with the time of day taken out.

A raw correlation between solar radiation and the Spanish price is one of the
easiest wrong numbers to produce in this project, because both variables are
driven by the same clock. Radiation peaks at midday. Price troughs at midday.
Correlate them across a whole week and you get something around -0.75, which
looks like a finding and is really just a restatement of "the sun is up during
the day".

The tell is that it does not matter where you measure the weather. Putting the
sensor in Lisbon gives you nearly the same number as putting it in Andalusia,
even though Lisbon sunshine has no bearing on Spanish solar output. Two series
that each follow the solar clock will correlate with each other regardless of
any causal link.

The question worth answering is different: holding the hour fixed, is a cloudy
day more expensive than a clear one? That is a within-hour comparison, and it
is what the functions here compute. In panel terms it is the within estimator
with hour of day as a fixed effect. Deviations are taken from the mean of the
same hour across days, then correlated, so the daily cycle common to both
series cancels out and only the day to day variation survives.

Two details that matter for the result to mean anything:

* The hour is local, not UTC. The solar cycle follows local time, and across a
  clock change a UTC hour maps to two different points in the solar day.
* Hours where the weather variable does not vary are dropped. At night every
  radiation reading is zero, so there is nothing to correlate, and including
  those rows drags the pooled estimate toward zero for a purely mechanical
  reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd

from iberian.config import MARKET_TIMEZONE

_TZ = ZoneInfo(MARKET_TIMEZONE)

#: Below this many observations an hourly correlation is noise, not signal.
MIN_OBSERVATIONS_PER_HOUR = 3

#: A weather variable flatter than this within an hour carries no information.
MIN_WEATHER_STD = 1e-9


@dataclass(frozen=True)
class CorrelationResult:
    """Both estimates side by side, because the contrast is the point."""

    weather_column: str
    price_column: str
    naive: float | None
    within: float | None
    observations: int
    hours_used: int
    by_hour: pd.DataFrame = field(repr=False)

    @property
    def confounded_by_hour(self) -> bool | None:
        """Did controlling for the hour change the answer materially?

        True means the raw correlation was mostly the solar clock. It is the
        expected outcome for a location whose weather does not actually drive
        the price, and the thing to check before quoting any figure.
        """
        if self.naive is None or self.within is None:
            return None
        return abs(self.naive) - abs(self.within) > 0.2

    def describe(self) -> str:
        if self.naive is None:
            return f"{self.weather_column}: not enough data"
        within = "undefined" if self.within is None else f"{self.within:+.3f}"
        return (
            f"{self.weather_column}: naive {self.naive:+.3f}, "
            f"within hour {within} "
            f"({self.observations} intervals, {self.hours_used} hours)"
        )


def local_hours(timestamps: pd.Series) -> pd.Series:
    """Hour of day in market local time.

    Naive timestamps are assumed to be UTC, which is what every table in this
    project stores.
    """
    values = pd.to_datetime(timestamps)
    if values.dt.tz is None:
        values = values.dt.tz_localize("UTC")
    return values.dt.tz_convert(_TZ).dt.hour


def within_hour_correlation(
    frame: pd.DataFrame,
    weather_column: str,
    price_column: str = "price_es_eur_mwh",
    min_observations_per_hour: int = MIN_OBSERVATIONS_PER_HOUR,
) -> CorrelationResult:
    """Correlate a weather variable with a price, holding hour of day fixed.

    Returns the naive correlation too, so the size of the confound is visible
    rather than silently corrected away.
    """
    required = {"ts_utc", weather_column, price_column}
    missing = required - set(frame.columns)
    if missing:
        raise KeyError(
            f"missing columns for the correlation: {', '.join(sorted(missing))}"
        )

    pair = frame[["ts_utc", weather_column, price_column]].dropna().copy()
    empty_by_hour = pd.DataFrame(
        columns=["local_hour", "observations", "correlation", "mean_weather", "mean_price"]
    )
    if len(pair) < min_observations_per_hour:
        return CorrelationResult(
            weather_column=weather_column,
            price_column=price_column,
            naive=None,
            within=None,
            observations=len(pair),
            hours_used=0,
            by_hour=empty_by_hour,
        )

    pair["local_hour"] = local_hours(pair["ts_utc"])

    naive = pair[weather_column].corr(pair[price_column])
    naive = None if pd.isna(naive) else float(naive)

    rows: list[dict] = []
    residuals: list[pd.DataFrame] = []

    for hour, group in pair.groupby("local_hour", sort=True):
        usable = (
            len(group) >= min_observations_per_hour
            and group[weather_column].std() > MIN_WEATHER_STD
            and group[price_column].std() > 0
        )
        correlation = (
            group[weather_column].corr(group[price_column]) if usable else float("nan")
        )
        rows.append(
            {
                "local_hour": int(hour),
                "observations": len(group),
                "correlation": None if pd.isna(correlation) else float(correlation),
                "mean_weather": float(group[weather_column].mean()),
                "mean_price": float(group[price_column].mean()),
            }
        )
        if usable:
            residuals.append(
                pd.DataFrame(
                    {
                        "weather": group[weather_column] - group[weather_column].mean(),
                        "price": group[price_column] - group[price_column].mean(),
                    }
                )
            )

    if residuals:
        pooled = pd.concat(residuals, ignore_index=True)
        within = pooled["weather"].corr(pooled["price"])
        within = None if pd.isna(within) else float(within)
    else:
        within = None

    return CorrelationResult(
        weather_column=weather_column,
        price_column=price_column,
        naive=naive,
        within=within,
        observations=len(pair),
        hours_used=len(residuals),
        by_hour=pd.DataFrame(rows, columns=empty_by_hour.columns),
    )


def weather_columns(frame: pd.DataFrame, variable: str) -> list[str]:
    """Every per location column for one weather variable, in a wide frame.

    `gold_weather_context` names its columns `<variable>__<location>`.
    """
    prefix = f"{variable}__"
    return sorted(column for column in frame.columns if column.startswith(prefix))


def compare_locations(
    frame: pd.DataFrame,
    variable: str = "shortwave_radiation_wm2",
    price_column: str = "price_es_eur_mwh",
) -> pd.DataFrame:
    """Run the comparison for every location, so the placebo is visible.

    Portuguese locations are the control here. Portuguese cloud cover has no
    mechanical effect on the Spanish price, so a naive correlation that is just
    as strong there as in Andalusia is the proof that the naive number measures
    the clock rather than the weather.
    """
    rows = []
    for column in weather_columns(frame, variable):
        result = within_hour_correlation(frame, column, price_column)
        rows.append(
            {
                "location": column.split("__", 1)[-1],
                "observations": result.observations,
                "hours_used": result.hours_used,
                "naive": result.naive,
                "within_hour": result.within,
                "confounded": result.confounded_by_hour,
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "location",
            "observations",
            "hours_used",
            "naive",
            "within_hour",
            "confounded",
        ],
    )
