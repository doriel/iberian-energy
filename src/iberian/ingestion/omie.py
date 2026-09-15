"""OMIE: the Iberian market operator's own published files.

Third source, and deliberately a different shape. ENTSO-E is an XML API,
Open-Meteo is a JSON API, OMIE is flat files published daily. Together they
span XML, JSON and delimited files rather than three variations on one API.

It also buys something ENTSO-E cannot: an independent publication of the same
day-ahead prices. Two sources for one number is the basis of a real data
quality check, and disagreement between them is itself a finding.

The file format is not guessed here. Use scripts/probe_sources.py to print the
raw bytes first, then the parser below is fitted to what actually arrives.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

# OMIE serves its public files through a download endpoint keyed on a file set
# and a filename. marginalpdbc carries day-ahead marginal prices for both
# Iberian zones, which is the series that overlaps ENTSO-E A44.
DOWNLOAD_URL = "https://www.omie.es/es/file-download"
FILE_SET_DAY_AHEAD = "marginalpdbc"


@dataclass(frozen=True)
class OmieResponse:
    file_set: str
    filename: str
    content: bytes
    fetched_at_utc: str
    status_code: int

    @property
    def text(self) -> str:
        # OMIE files are Latin-1 in practice; decoding as UTF-8 throws on the
        # accented station names that appear in some file sets.
        return self.content.decode("latin-1")

    @property
    def looks_empty(self) -> bool:
        stripped = self.text.strip()
        return not stripped or stripped.lower().startswith("<!doctype")


class OmieClient:
    def __init__(self, timeout_seconds: int = 30, max_retries: int = 3) -> None:
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._session = requests.Session()

    def day_ahead_prices(self, day: date, file_set: str = FILE_SET_DAY_AHEAD) -> OmieResponse:
        filename = f"{file_set}_{day:%Y%m%d}.1"
        params = {"parents[0]": file_set, "filename": filename}

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._session.get(
                    DOWNLOAD_URL, params=params, timeout=self._timeout
                )
                if response.status_code == 429:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return OmieResponse(
                    file_set=file_set,
                    filename=filename,
                    content=response.content,
                    fetched_at_utc=datetime.now(timezone.utc).isoformat(),
                    status_code=response.status_code,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2**attempt)

        raise RuntimeError(f"OMIE request failed for {filename}: {last_error}")


@dataclass(frozen=True)
class OmiePrice:
    """Two price columns, deliberately not named PT and ES.

    Which column is which zone is not documented in the file and both are
    identical whenever the market is coupled, which is most of the time. Naming
    them here would bake in a guess that only shows up as wrong on exactly the
    intervals the project cares about. scripts/cross_check_prices.py settles it
    against ENTSO-E on a decoupled interval.
    """

    market_day: date
    period: int
    ts_utc: datetime
    price_first_eur_mwh: float
    price_second_eur_mwh: float


def parse_marginalpdbc(text: str, periods_per_day: int | None = None) -> list[OmiePrice]:
    """Parse a marginalpdbc file into rows.

    Layout, confirmed by probing rather than assumed:
        MARGINALPDBC;
        YYYY;MM;DD;period;price_pt;price_es;
        ...
        *

    The period is an index into the market day, not a clock hour, so the
    timestamp is derived from the market day window rather than by treating
    the number as an hour. On the clock change days that difference is the
    whole ballgame: a 25 period day exists and assuming 24 silently drops one.
    """
    from iberian.market_time import market_day_window

    rows: list[OmiePrice] = []
    day: date | None = None
    parsed: list[tuple[int, float, float]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*") or line.upper().startswith("MARGINAL"):
            continue

        fields = [field.strip() for field in line.split(";") if field.strip() != ""]
        if len(fields) < 6:
            continue

        try:
            year, month, day_of_month, period = (int(fields[i]) for i in range(4))
            price_first = float(fields[4].replace(",", "."))
            price_second = float(fields[5].replace(",", "."))
        except ValueError:
            continue

        day = date(year, month, day_of_month)
        parsed.append((period, price_first, price_second))

    if day is None or not parsed:
        return []

    window_start, window_end = market_day_window(day)
    total = periods_per_day or len(parsed)
    if total <= 0:
        return []
    step = (window_end - window_start) / total

    for period, price_first, price_second in parsed:
        rows.append(
            OmiePrice(
                market_day=day,
                period=period,
                ts_utc=window_start + step * (period - 1),
                price_first_eur_mwh=price_first,
                price_second_eur_mwh=price_second,
            )
        )

    return rows


def to_records(prices: list[OmiePrice]) -> list[dict]:
    return [
        {
            "source": "omie",
            "market_day": p.market_day,
            "period": p.period,
            "ts_utc": p.ts_utc,
            "price_first_eur_mwh": p.price_first_eur_mwh,
            "price_second_eur_mwh": p.price_second_eur_mwh,
        }
        for p in prices
    ]
