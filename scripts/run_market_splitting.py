"""End to end run: fetch -> land raw -> parse -> detect splits -> report.

Two modes:

    python scripts/run_market_splitting.py --demo
        Synthetic day with a known split. No token, no network. Use it to check
        the pipeline shape works before your ENTSO-E token arrives.

    python scripts/run_market_splitting.py --start 2026-09-01 --days 7
        Real data. Needs ENTSOE_SECURITY_TOKEN in the environment.

Raw payloads land under data/raw/entsoe/ exactly as returned. That directory is
the local stand in for the S3 bronze landing zone, so the bronze -> silver hop
you write for Databricks reads the same bytes you are reading here.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    detect_episodes,
    flag_decoupling,
    infer_step,
    summarise,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.parsing.entsoe_prices import parse_prices_response, to_records  # noqa: E402


def land_raw(raw_dir: Path, zone: str, start: datetime, response, meta: dict) -> Path:
    """Write the raw payload plus its request context, partitioned by date.

    Bytes, not text: a long enough request comes back as a ZIP and decoding it
    to a string corrupts the archive.
    """
    target = raw_dir / "entsoe" / "day_ahead_prices" / f"zone={zone}" / f"date={start:%Y-%m-%d}"
    target.mkdir(parents=True, exist_ok=True)

    payload_path = target / f"response{response.suggested_extension}"
    payload_path.write_bytes(response.content)
    (target / "_request.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return payload_path


def fetch_real(start_day: date, days: int, raw_dir: Path) -> pd.DataFrame:
    """One request per zone covering the whole market day range.

    The window comes from market_day_range, not from UTC midnight. Asking for a
    UTC calendar day straddles two Iberian market days and ENTSO-E returns both,
    which is where the duplicate looking rows came from.
    """
    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    window_start, window_end = market_day_range(start_day, days)
    wanted_days = {start_day + timedelta(days=offset) for offset in range(days)}
    print(f"Market day window: {window_start:%Y-%m-%d %H:%M}Z to {window_end:%Y-%m-%d %H:%M}Z")

    records: list[dict] = []
    for zone_label, eic in (("PT", EIC_PORTUGAL), ("ES", EIC_SPAIN)):
        response = client.day_ahead_prices(eic, window_start, window_end)
        land_raw(
            raw_dir,
            zone_label,
            window_start,
            response,
            {
                "params": response.params,
                "fetched_at_utc": response.fetched_at_utc,
                "status_code": response.status_code,
            },
        )

        if response.is_empty:
            print(f"  {zone_label}: no data returned")
            continue

        points = parse_prices_response(response, eic)
        rows = [r for r in to_records(points) if r["market_day"] in wanted_days]
        records.extend(rows)

        dropped = len(points) - len(rows)
        extra = f", {dropped} outside the requested days" if dropped else ""
        print(f"  {zone_label}: {len(rows)} day-ahead points{extra}")

    return pd.DataFrame(records)


def fetch_demo() -> pd.DataFrame:
    """A synthetic week with two planted splits, so output is verifiable by eye."""
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for hour in range(24 * 7):
        ts = start + timedelta(hours=hour)
        base = 60.0 + 18.0 * (1 if 17 <= ts.hour <= 21 else 0)

        pt, es = base, base
        # Day 2 evening: Portugal pays a severe premium for four hours.
        if ts.day == 2 and 18 <= ts.hour <= 21:
            pt = base + 34.0
        # Day 5 midday: Spain pays a moderate premium for two hours.
        if ts.day == 5 and 12 <= ts.hour <= 13:
            es = base + 9.0

        rows.append({"zone_eic": EIC_PORTUGAL, "ts_utc": ts, "price_eur_mwh": pt})
        rows.append({"zone_eic": EIC_SPAIN, "ts_utc": ts, "price_eur_mwh": es})

    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="synthetic data, no token")
    parser.add_argument("--start", help="first market day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)

    if args.demo:
        print("Demo mode: synthetic prices, no API call.")
        prices = fetch_demo()
    else:
        if not args.start:
            parser.error("--start is required unless you pass --demo")
        start_day = date.fromisoformat(args.start)
        print(f"Fetching {args.days} market day(s) from {start_day} for PT and ES")
        prices = fetch_real(start_day, args.days, raw_dir)

    if prices.empty:
        print("No price rows. Nothing to analyse.")
        return 1

    spread = build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN)
    flagged = flag_decoupling(spread)
    step = infer_step(flagged)
    episodes = detect_episodes(flagged, step=step)
    summary = summarise(episodes)

    step_hours = step / pd.Timedelta(hours=1)
    intervals = len(flagged)
    split_intervals = int(flagged["is_decoupled"].sum())
    share = (split_intervals / intervals * 100) if intervals else 0.0

    print()
    print(f"Settlement interval: {int(step_hours * 60)} minutes")
    print(f"Intervals analysed:  {intervals} ({intervals * step_hours:.0f} hours)")
    print(
        f"Decoupled:           {split_intervals} intervals "
        f"({split_intervals * step_hours:.2f} hours, {share:.1f}%)"
    )
    print(f"Episodes:            {summary['episode_count']}")
    if summary["episode_count"]:
        print(f"Worst spread:        {summary['worst_spread']:+.2f} EUR/MWh")
        print(f"Longest episode:     {summary['longest_episode_hours']:.2f} hours")
        print()
        print(episodes.to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
