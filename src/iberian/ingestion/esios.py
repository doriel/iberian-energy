"""ESIOS, the Spanish system operator's data platform.

The fourth source, and the one that can check the project's headline number
rather than merely add to it.

`gold_split_episodes` reports an extra import cost: the premium Portugal paid
multiplied by the energy actually imported while the zones priced apart. That
figure is computed here, from prices and flows. REE publishes the congestion
rent on the same border, which is the same economic quantity computed by the
people who operate the interconnector. Two independent calculations of one
number is a far better position than one calculation asserted confidently.

They are not identical and should not be presented as if they were. Congestion
rent is generally the price difference applied to the capacity allocated in the
coupling, while the cost here uses the net scheduled flow. Where they diverge,
the divergence is the finding.

The second thing this source brings is forecast error. REE publishes its own
day-ahead demand forecast and the demand that actually materialised, which is
the metric the grid analyst persona was promised and that ENTSO-E alone did
not supply.

Terms of use, from the token issuer: the token is personal, and anything
published must read from your own server rather than from theirs. Everything
here lands in the lakehouse first, so the web app never touches this API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import requests

from iberian.market_time import to_market_day

BASE_URL = "https://api.esios.ree.es"

#: The mainland Spanish system. Indicators can carry several geographies and
#: summing them silently is how you produce a number nobody can reproduce.
GEO_PENINSULA = 8741

#: Only the indicators this project actually uses. The catalogue has thousands
#: and scripts/probe_esios.py is how new ones get found and checked.
INDICATORS: dict[str, int] = {
    # Day-ahead congestion rent on the Portuguese border, both directions.
    # This is the independent check on gold_split_episodes.extra_cost_eur.
    "congestion_rent_pt_import": 1119,
    "congestion_rent_pt_export": 1120,
    # Demand forecast published the day before, against what actually
    # happened. Together these give forecast error for persona 3.
    "demand_forecast_d1": 1775,
    "demand_actual": 1293,
}

INDICATOR_NAMES = {value: key for key, value in INDICATORS.items()}


class EsiosError(RuntimeError):
    """The API refused the request. Carries the status so 401 reads clearly."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"ESIOS returned {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class IndicatorResponse:
    """The raw payload plus enough context to land and parse it.

    Bytes rather than a parsed dict, because bronze stores what arrived. A
    parser fix should reprocess stored payloads instead of asking REE for the
    same data twice, which their terms specifically ask you not to do.
    """

    indicator_id: int
    content: bytes

    @property
    def payload(self) -> dict:
        import json

        return json.loads(self.content.decode("utf-8"))

    @property
    def filename(self) -> str:
        return f"indicator={self.indicator_id}.json"


class EsiosClient:
    """Minimal client. One endpoint, because one endpoint is all this needs."""

    def __init__(self, token: str, base_url: str = BASE_URL) -> None:
        if not token:
            raise ValueError("An ESIOS token is required. Set ESIOS_TOKEN.")
        self._token = token
        self._base_url = base_url.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._token,
            "Accept": "application/json; application/vnd.esios-api-v1+json",
            "Content-Type": "application/json",
        }

    def indicator(
        self, indicator_id: int, start_utc: datetime, end_utc: datetime
    ) -> IndicatorResponse:
        """Fetch one indicator over a UTC window.

        Callers pass a market day window from `market_time`, not a calendar
        day, so the series lines up with everything else in the project.
        """
        response = requests.get(
            f"{self._base_url}/indicators/{indicator_id}",
            headers=self._headers,
            params={
                "start_date": _iso(start_utc),
                "end_date": _iso(end_utc),
            },
            timeout=60,
        )
        if not response.ok:
            raise EsiosError(response.status_code, response.text[:300])
        return IndicatorResponse(indicator_id=indicator_id, content=response.content)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def infer_resolution(timestamps: list[pd.Timestamp]) -> str:
    """Label the interval, so PT15M and PT60M series are never silently mixed.

    Indicators differ: the congestion rent is quarter hourly, the demand
    forecast is hourly. Recording it is cheaper than discovering it later in a
    join that quietly dropped three quarters of the rows.
    """
    if len(timestamps) < 2:
        return "unknown"
    deltas = pd.Series(timestamps).sort_values().diff().dropna()
    if deltas.empty:
        return "unknown"
    minutes = int(deltas.mode().iloc[0].total_seconds() // 60)
    return f"PT{minutes}M"


def to_records(
    response: IndicatorResponse, geo_id: int | None = GEO_PENINSULA
) -> list[dict]:
    """Tidy rows from one indicator payload.

    `datetime_utc` is used rather than the local `datetime` field, because the
    API already did the conversion and redoing it by hand is how timezone bugs
    get introduced.
    """
    indicator = response.payload["indicator"]
    values = indicator.get("values") or []

    if geo_id is not None:
        values = [row for row in values if row.get("geo_id") == geo_id]
    if not values:
        return []

    stamps = [pd.Timestamp(row["datetime_utc"]).tz_convert("UTC") for row in values]
    resolution = infer_resolution(stamps)
    indicator_id = int(indicator["id"])
    name = INDICATOR_NAMES.get(indicator_id, indicator.get("short_name") or "")

    return [
        {
            "indicator_id": indicator_id,
            "indicator": name,
            "ts_utc": stamp,
            "market_day": to_market_day(stamp.to_pydatetime()),
            "value": float(row["value"]) if row.get("value") is not None else None,
            "geo_id": row.get("geo_id"),
            "geo_name": row.get("geo_name"),
            "resolution": resolution,
        }
        for row, stamp in zip(values, stamps)
    ]


def to_frame(responses: list[IndicatorResponse], geo_id: int | None = GEO_PENINSULA):
    """Several indicators stacked into one long frame."""
    rows: list[dict] = []
    for response in responses:
        rows.extend(to_records(response, geo_id=geo_id))
    return pd.DataFrame(
        rows,
        columns=[
            "indicator_id",
            "indicator",
            "ts_utc",
            "market_day",
            "value",
            "geo_id",
            "geo_name",
            "resolution",
        ],
    )


def congestion_rent_by_day(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily congestion rent on the Portuguese border, both directions summed.

    The two directions are published separately and only one of them is
    non-zero in any given interval, so adding them gives the total rent the
    border earned that day.
    """
    wanted = {
        INDICATORS["congestion_rent_pt_import"],
        INDICATORS["congestion_rent_pt_export"],
    }
    rent = frame[frame["indicator_id"].isin(wanted)]
    if rent.empty:
        return pd.DataFrame(columns=["market_day", "congestion_rent_eur", "intervals"])

    return (
        rent.groupby("market_day")
        .agg(
            congestion_rent_eur=("value", "sum"),
            intervals=("ts_utc", "count"),
        )
        .reset_index()
        .sort_values("market_day")
    )


def forecast_error(frame: pd.DataFrame) -> pd.DataFrame:
    """Day-ahead demand forecast against what actually happened.

    The two series are published at different resolutions, so the actual is
    averaged onto the forecast's hourly grid rather than the forecast being
    stretched to look finer than it is.
    """
    forecast = frame[frame["indicator_id"] == INDICATORS["demand_forecast_d1"]]
    actual = frame[frame["indicator_id"] == INDICATORS["demand_actual"]]
    if forecast.empty or actual.empty:
        return pd.DataFrame(
            columns=["ts_utc", "market_day", "forecast_mw", "actual_mw", "error_mw"]
        )

    hourly_actual = (
        actual.set_index("ts_utc")["value"]
        .resample("1h")
        .mean()
        .rename("actual_mw")
        .reset_index()
    )
    merged = (
        forecast[["ts_utc", "market_day", "value"]]
        .rename(columns={"value": "forecast_mw"})
        .merge(hourly_actual, on="ts_utc", how="inner")
    )
    merged["error_mw"] = merged["actual_mw"] - merged["forecast_mw"]
    merged["abs_error_mw"] = merged["error_mw"].abs()
    # Percentage error is what a grid analyst quotes, but it is meaningless
    # when the denominator is near zero, so it is left null there rather than
    # producing an impressive looking infinity.
    merged["error_pct"] = (
        merged["error_mw"] / merged["forecast_mw"].where(merged["forecast_mw"].abs() > 1)
    ) * 100
    return merged
