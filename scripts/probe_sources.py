"""Probe the sources beyond ENTSO-E, and show what each actually returns.

Same discipline that has already saved this project four debugging sessions:
look at the real payload before writing a parser against a guess.

    python scripts/probe_sources.py --date 2026-09-03

Open-Meteo needs no credentials. OMIE needs none either. If both work, there
are three data shapes on disk: XML, JSON and delimited text.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.ingestion.omie import OmieClient, parse_marginalpdbc  # noqa: E402
from iberian.ingestion.open_meteo import (  # noqa: E402
    LOCATIONS,
    OpenMeteoClient,
    parse_hourly,
)


def probe_open_meteo(day: date, raw_dir: Path) -> bool:
    print("=" * 68)
    print("Open-Meteo (JSON, no credentials)")
    print("=" * 68)

    client = OpenMeteoClient()
    ok = False

    for location in ("ES_andalusia", "ES_galicia", "PT_lisbon"):
        _, _, why = LOCATIONS[location]
        try:
            points, payload = client.hourly(location, day, day)
        except RuntimeError as exc:
            print(f"  FAIL  {location}: {exc}")
            continue

        if not points:
            print(f"  EMPTY {location}: no hourly block in the response")
            continue

        target = raw_dir / "open_meteo" / f"location={location}"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{day:%Y-%m-%d}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        radiation = [p.shortwave_radiation_wm2 for p in points if p.shortwave_radiation_wm2 is not None]
        wind = [p.wind_speed_100m_kmh for p in points if p.wind_speed_100m_kmh is not None]
        temps = [p.temperature_c for p in points if p.temperature_c is not None]

        print(f"  OK    {location:<14} {len(points)} hours   ({why})")
        if temps:
            print(f"          temperature   {min(temps):.1f} to {max(temps):.1f} C")
        if wind:
            print(f"          wind 100m     {min(wind):.0f} to {max(wind):.0f} km/h")
        if radiation:
            print(f"          radiation     {min(radiation):.0f} to {max(radiation):.0f} W/m2")
        ok = True

    return ok


def probe_omie(day: date, raw_dir: Path) -> bool:
    print("\n" + "=" * 68)
    print("OMIE (delimited files, no credentials)")
    print("=" * 68)

    client = OmieClient()
    try:
        response = client.day_ahead_prices(day)
    except RuntimeError as exc:
        print(f"  FAIL  {exc}")
        return False

    if response.looks_empty:
        print(f"  EMPTY {response.filename} came back empty or as an HTML page.")
        print("        The download endpoint or the filename pattern may differ.")
        print(f"        First 200 bytes: {response.content[:200]!r}")
        return False

    target = raw_dir / "omie" / f"file_set={response.file_set}"
    target.mkdir(parents=True, exist_ok=True)
    (target / response.filename).write_bytes(response.content)
    print(f"  Saved {target / response.filename} ({len(response.content)} bytes)")

    print("\n  First 8 lines exactly as they arrived:")
    for line in response.text.splitlines()[:8]:
        print(f"    {line!r}")

    prices = parse_marginalpdbc(response.text)
    if not prices:
        print("\n  Parser produced nothing. The layout differs from the assumed")
        print("  year;month;day;period;price;price. Send the lines above.")
        return False

    print(f"\n  Parsed {len(prices)} periods for market day {prices[0].market_day}")
    print(f"    {'period':>7}  {'ts_utc':<17}  {'col5':>9}  {'col6':>9}")
    for price in prices[:4]:
        print(
            f"    {price.period:>7}  {price.ts_utc:%Y-%m-%d %H:%M}  "
            f"{price.price_pt_eur_mwh:>9.2f}  {price.price_es_eur_mwh:>9.2f}"
        )
    print("\n  Which column is Portugal and which is Spain has to be settled")
    print("  against the ENTSO-E prices for the same day, not assumed.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="market day, YYYY-MM-DD")
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    day = (
        date.fromisoformat(args.date)
        if args.date
        else (datetime.now(timezone.utc) - timedelta(days=2)).date()
    )
    raw_dir = Path(args.raw_dir)

    weather_ok = probe_open_meteo(day, raw_dir)
    omie_ok = probe_omie(day, raw_dir)

    print("\n" + "=" * 68)
    print("Source count")
    print("=" * 68)
    sources = ["ENTSO-E (XML API, working)"]
    sources.append(f"Open-Meteo (JSON API, {'working' if weather_ok else 'FAILED'})")
    sources.append(f"OMIE (delimited files, {'working' if omie_ok else 'FAILED'})")
    for entry in sources:
        print(f"  {entry}")
    print("\n  Still to add: REE/ESIOS and REN Datahub for the generation mix.")

    return 0 if (weather_ok and omie_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
