"""Find out what the ESIOS API actually offers before writing a client for it.

REE publishes thousands of indicators behind numeric ids. Guessing an id from
a blog post is how you end up parsing the wrong series for a week, so this
reads the catalogue, lets you search it by name, and then shows the real shape
of one indicator's values.

    python scripts/probe_esios.py --search demanda
    python scripts/probe_esios.py --search "precio mercado"
    python scripts/probe_esios.py --search interconexion
    python scripts/probe_esios.py --indicator 600 --start 2026-09-01 --days 1

Reads ESIOS_TOKEN from the environment.

Note on terms of use: the token REE issues is personal, and their conditions
say that anything published must read from your own server rather than from
theirs. That is what this project does anyway, since everything lands in the
lakehouse first, but it rules out the web app calling this API directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

BASE_URL = "https://api.esios.ree.es"
CATALOGUE_CACHE = Path("data/raw/esios/indicators.json")


def headers(token: str) -> dict[str, str]:
    return {
        "x-api-key": token,
        "Accept": "application/json; application/vnd.esios-api-v1+json",
        "Content-Type": "application/json",
    }


def token() -> str:
    value = os.environ.get("ESIOS_TOKEN", "").strip()
    if not value:
        raise SystemExit(
            "ESIOS_TOKEN is not set.\n"
            "Add it to .env, then: export $(grep -v '^#' .env | xargs)"
        )
    return value


def fold(text: str) -> str:
    """Lowercase and strip accents, so 'prevision' matches 'previsión'."""
    normalised = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in normalised if not unicodedata.combining(ch))


def catalogue(refresh: bool = False) -> list[dict]:
    """The full indicator list, cached because it is large and rarely changes.

    Their terms ask explicitly for no redundant requests, and re-downloading a
    catalogue that changes a few times a year on every run is exactly that.
    """
    if CATALOGUE_CACHE.exists() and not refresh:
        return json.loads(CATALOGUE_CACHE.read_text())["indicators"]

    response = requests.get(
        f"{BASE_URL}/indicators", headers=headers(token()), timeout=60
    )
    if response.status_code == 401:
        raise SystemExit("401 from ESIOS. The token was rejected.")
    response.raise_for_status()

    CATALOGUE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE_CACHE.write_text(response.text)
    print(f"Catalogue cached at {CATALOGUE_CACHE}\n")
    return response.json()["indicators"]


def cmd_search(args) -> None:
    needles = [fold(term) for term in args.search.split()]
    hits = [
        item
        for item in catalogue(refresh=args.refresh)
        if all(
            needle in fold(f"{item.get('name', '')} {item.get('description', '') or ''}")
            for needle in needles
        )
    ]

    if not hits:
        print(f"Nothing matched {args.search!r}. Try a single broader word.")
        return

    print(f"{len(hits)} indicator(s) matching {args.search!r}\n")
    for item in hits[: args.limit]:
        print(f"  {item['id']:>6}  {item.get('name', '')}")
        short = (item.get("short_name") or "").strip()
        if short and short != item.get("name"):
            print(f"          short: {short}")
    if len(hits) > args.limit:
        print(f"\n  ...and {len(hits) - args.limit} more. Narrow the search.")


def cmd_indicator(args) -> None:
    start = date.fromisoformat(args.start)
    end = start + timedelta(days=args.days)
    # Naive UTC window on purpose. The point here is to see the shape of the
    # response, not to be right about the Iberian market day, which is handled
    # by market_time once this becomes a real ingestion module.
    params = {
        "start_date": datetime.combine(
            start, datetime.min.time(), tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": datetime.combine(
            end, datetime.min.time(), tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    response = requests.get(
        f"{BASE_URL}/indicators/{args.indicator}",
        headers=headers(token()),
        params=params,
        timeout=60,
    )
    if response.status_code == 401:
        raise SystemExit("401 from ESIOS. The token was rejected.")
    if response.status_code == 404:
        raise SystemExit(f"No indicator {args.indicator}. Search the catalogue first.")
    response.raise_for_status()

    payload = response.json()["indicator"]
    values = payload.get("values", [])

    target = Path("data/raw/esios") / f"indicator={args.indicator}"
    target.mkdir(parents=True, exist_ok=True)
    landed = target / f"{start:%Y-%m-%d}_{args.days}d.json"
    landed.write_text(response.text)

    print(f"Indicator {payload['id']}: {payload.get('name', '')}")
    print(f"  units:  {payload.get('magnitud') or payload.get('step_type') or 'not stated'}")
    print(f"  values: {len(values)}")
    print(f"  landed: {landed}\n")

    if not values:
        print("  No values in this window. The indicator may publish on a")
        print("  different schedule, or need a geo_ids filter.")
        return

    print("  Fields on a value:")
    for key, value in values[0].items():
        print(f"    {key:<16} {value!r}")

    geos = sorted({v.get("geo_name") for v in values if v.get("geo_name")})
    if geos:
        print(f"\n  Geographies present: {', '.join(str(g) for g in geos)}")
        print("  More than one means you need a geo filter, or you will sum")
        print("  unrelated regions into one series without noticing.")

    stamps = [v["datetime"] for v in values[:5]]
    print("\n  First timestamps:")
    for stamp in stamps:
        print(f"    {stamp}")

    if len(values) > 1:
        first = datetime.fromisoformat(values[0]["datetime"])
        second = datetime.fromisoformat(values[1]["datetime"])
        minutes = (second - first).total_seconds() / 60
        print(f"\n  Apparent resolution: {minutes:.0f} minutes")
        print(f"  Offset from UTC in the timestamps: {first.utcoffset()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search", help="find indicators whose name contains these words")
    parser.add_argument("--indicator", type=int, help="fetch one indicator's values")
    parser.add_argument("--start", default="2026-09-01")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--refresh", action="store_true", help="re-download the catalogue")
    args = parser.parse_args()

    if args.indicator:
        cmd_indicator(args)
    elif args.search:
        cmd_search(args)
    else:
        parser.error("give --search or --indicator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
