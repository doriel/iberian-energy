"""Open-Meteo: weather, the second source and the one that explains why.

ENTSO-E tells you the border was full and that Portugal paid. It cannot tell
you why Spanish power was cheap enough to be worth importing in the first
place. Weather can: solar radiation drives the Spanish midday price collapse,
wind drives the overnight one, and temperature drives the evening demand peak
that coincides with the capacity trough.

No authentication at all, and JSON rather than XML, which is also the point:
the sources are meant to differ in shape, not just in hostname.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Representative points, chosen for what drives the price rather than for where
# people live. Galicia and Castilla sit near the wind fleet that feeds the
# northern interconnection; Andalusia carries the solar that collapses Spanish
# midday prices; Lisbon and Porto carry Portuguese demand.
LOCATIONS = {
    "ES_galicia": (42.88, -8.54, "wind, near the northern interconnection"),
    "ES_castilla": (41.65, -4.72, "wind, central plateau"),
    "ES_andalusia": (37.39, -5.98, "solar, drives the midday price collapse"),
    "PT_porto": (41.15, -8.61, "demand, northern Portugal"),
    "PT_lisbon": (38.72, -9.14, "demand, southern Portugal"),
    "PT_alentejo": (38.57, -7.91, "solar and wind, Portuguese generation"),
}

HOURLY_VARIABLES = [
    "temperature_2m",
    "wind_speed_100m",
    "shortwave_radiation",
    "cloud_cover",
]


@dataclass(frozen=True)
class WeatherPoint:
    location: str
    latitude: float
    longitude: float
    ts_utc: datetime
    temperature_c: float | None
    wind_speed_100m_kmh: float | None
    shortwave_radiation_wm2: float | None
    cloud_cover_pct: float | None


class OpenMeteoClient:
    """Open-Meteo has no token. Rate limits are generous but not infinite."""

    def __init__(self, timeout_seconds: int = 30, max_retries: int = 3) -> None:
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._session = requests.Session()

    def _get(self, url: str, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._session.get(url, params=params, timeout=self._timeout)
                if response.status_code == 429:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"Open-Meteo request failed: {last_error}")

    def hourly(
        self,
        location: str,
        start_day: date,
        end_day: date,
        use_archive: bool | None = None,
    ) -> tuple[list[WeatherPoint], dict]:
        """Hourly weather for one location, plus the raw payload for bronze.

        Open-Meteo splits recent data from the reanalysis archive. The archive
        lags by about five days, so asking it for yesterday returns nothing
        while the forecast endpoint still has it. Choose by age rather than
        making the caller remember which is which.
        """
        if location not in LOCATIONS:
            raise ValueError(f"Unknown location {location!r}")
        latitude, longitude, _ = LOCATIONS[location]

        if use_archive is None:
            age_days = (datetime.now(timezone.utc).date() - end_day).days
            use_archive = age_days > 7

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "hourly": ",".join(HOURLY_VARIABLES),
            "timezone": "UTC",
        }
        payload = self._get(ARCHIVE_URL if use_archive else FORECAST_URL, params)
        return parse_hourly(payload, location, latitude, longitude), payload


def parse_hourly(
    payload: dict, location: str, latitude: float, longitude: float
) -> list[WeatherPoint]:
    """Turn the columnar JSON into rows.

    Open-Meteo returns parallel arrays rather than records, and a variable that
    was unavailable comes back as a column of nulls rather than being absent.
    Zipping without checking lengths would silently truncate to the shortest.
    """
    hourly = payload.get("hourly")
    if not hourly or "time" not in hourly:
        return []

    stamps = hourly["time"]
    columns = {name: hourly.get(name) or [None] * len(stamps) for name in HOURLY_VARIABLES}

    for name, values in columns.items():
        if len(values) != len(stamps):
            raise ValueError(
                f"Open-Meteo returned {len(values)} values for {name} but "
                f"{len(stamps)} timestamps. Refusing to zip mismatched columns."
            )

    points: list[WeatherPoint] = []
    for index, stamp in enumerate(stamps):
        ts = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        points.append(
            WeatherPoint(
                location=location,
                latitude=latitude,
                longitude=longitude,
                ts_utc=ts,
                temperature_c=columns["temperature_2m"][index],
                wind_speed_100m_kmh=columns["wind_speed_100m"][index],
                shortwave_radiation_wm2=columns["shortwave_radiation"][index],
                cloud_cover_pct=columns["cloud_cover"][index],
            )
        )
    return points


def to_records(points: list[WeatherPoint]) -> list[dict]:
    from iberian.market_time import to_market_day

    return [
        {
            "source": "open_meteo",
            "location": p.location,
            "zone": p.location.split("_")[0],
            "latitude": p.latitude,
            "longitude": p.longitude,
            "ts_utc": p.ts_utc,
            "market_day": to_market_day(p.ts_utc),
            "temperature_c": p.temperature_c,
            "wind_speed_100m_kmh": p.wind_speed_100m_kmh,
            "shortwave_radiation_wm2": p.shortwave_radiation_wm2,
            "cloud_cover_pct": p.cloud_cover_pct,
        }
        for p in points
    ]
