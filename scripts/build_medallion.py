"""Run the medallion end to end, locally, over a range of market days.

Bronze lands raw payloads byte for byte. Silver parses them into tidy tables.
Gold builds one table per persona. The local target is partitioned parquet,
which Databricks reads directly and converts to Delta with one statement, so
the port is mechanical rather than a rewrite.

    python scripts/build_medallion.py --start 2026-09-01 --days 7
    python scripts/build_medallion.py --from-silver

Gold is a pure function of silver, so `--from-silver` recomputes the gold
tables from what is already on disk without calling any API. That is the
operation to run after changing analysis logic: reprocessing sixty days of
stored rows takes seconds, while re-ingesting them takes an hour and asks four
providers for data they already sent.

It is a mode of this script rather than a separate one on purpose. Two entry
points that both build gold are two code paths that drift apart.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.interconnection import build_border_series  # noqa: E402
from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    flag_decoupling,
    infer_step,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.border import fetch_border  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.ingestion.omie import OmieClient, parse_marginalpdbc  # noqa: E402
from iberian.ingestion.omie import to_records as omie_to_records  # noqa: E402
from iberian.ingestion.open_meteo import LOCATIONS, OpenMeteoClient  # noqa: E402
from iberian.ingestion.open_meteo import to_records as weather_to_records  # noqa: E402
from iberian.market_time import market_day_range  # noqa: E402
from iberian.parsing.entsoe_prices import parse_prices_response, to_records  # noqa: E402
from iberian.pipeline.gold import gold_tables  # noqa: E402

SILVER_WEATHER_LOCATIONS = ("ES_andalusia", "ES_galicia", "PT_lisbon", "PT_alentejo")


def write_table(frame: pd.DataFrame, root: Path, name: str, layer: str) -> None:
    if frame.empty:
        print(f"  {layer}/{name}: empty, not written")
        return
    target = root / layer
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{name}.parquet"
    frame.to_parquet(path, index=False)
    print(f"  {layer}/{name}: {len(frame):>6} rows -> {path}")


def read_table(root: Path, layer: str, name: str, required: bool = True) -> pd.DataFrame:
    path = root / layer / f"{name}.parquet"
    if not path.exists():
        if required:
            raise SystemExit(f"{path} not found. Run an ingesting build first.")
        print(f"  {layer}/{name}: missing, continuing without it")
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    print(f"  {layer}/{name}: {len(frame):>6} rows")
    return frame


def build_gold(
    prices: pd.DataFrame,
    schedules: pd.DataFrame,
    capacity: pd.DataFrame,
    weather: pd.DataFrame,
    out_dir: Path,
) -> tuple[dict, pd.Timedelta]:
    """The one place gold is built, whichever mode got us here."""
    flagged = flag_decoupling(build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN))
    step = infer_step(flagged)
    border = build_border_series(schedules, capacity, (EIC_SPAIN, EIC_PORTUGAL))

    tables = gold_tables(flagged, border, weather)

    print("\nGOLD")
    for name, frame in tables.items():
        write_table(frame, out_dir, name, "gold")

    return tables, step


def report(tables: dict, step: pd.Timedelta, out_dir: Path) -> None:
    profile = tables.get("gold_daily_profile")
    if profile is not None and not profile.empty:
        worst = profile.loc[profile["split_probability"].idxmax()]
        print(
            f"\n  Worst hour for Portugal: {int(worst['hour_of_day_utc']):02d}:00 UTC, "
            f"split in {worst['split_probability']:.0%} of intervals, "
            f"mean premium {worst['mean_premium_eur_mwh']:+.2f} EUR/MWh"
        )

    episodes = tables.get("gold_split_episodes")
    if episodes is not None and not episodes.empty:
        total = episodes["extra_cost_eur"].dropna().sum()
        days = episodes["market_day"].nunique()
        print(
            f"  Episodes: {len(episodes)} across {days} market day(s), "
            f"extra import cost {total:,.0f} EUR"
        )

    print(f"\nSettlement interval {int((step / pd.Timedelta(hours=1)) * 60)} minutes")
    print(f"Lakehouse written under {out_dir}/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="first market day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--out-dir", default="data/lakehouse")
    parser.add_argument("--skip-weather", action="store_true")
    parser.add_argument("--skip-omie", action="store_true")
    parser.add_argument(
        "--from-silver",
        action="store_true",
        help="recompute gold from the silver tables on disk, without ingesting",
    )
    args = parser.parse_args()

    if not args.from_silver and not args.start:
        parser.error("--start is required unless --from-silver is given")

    out_dir = Path(args.out_dir)

    if args.from_silver:
        print("SILVER, from disk")
        tables, step = build_gold(
            read_table(out_dir, "silver", "silver_entsoe_prices"),
            read_table(out_dir, "silver", "silver_entsoe_schedules"),
            read_table(out_dir, "silver", "silver_entsoe_capacity"),
            read_table(out_dir, "silver", "silver_weather", required=False),
            out_dir,
        )
        report(tables, step, out_dir)
        return 0

    start_day = date.fromisoformat(args.start)
    days = [start_day + timedelta(days=offset) for offset in range(args.days)]
    wanted = set(days)
    start_utc, end_utc = market_day_range(start_day, args.days)

    raw_dir = Path(args.raw_dir)

    settings = Settings.from_env()
    client = EntsoeClient(settings.require_entsoe_token())

    print(f"Market days {days[0]} to {days[-1]}\n")
    print("BRONZE and SILVER")

    # --- prices, ENTSO-E ---
    price_records: list[dict] = []
    for label, eic in (("PT", EIC_PORTUGAL), ("ES", EIC_SPAIN)):
        response = client.day_ahead_prices(eic, start_utc, end_utc)
        target = raw_dir / "entsoe" / "day_ahead_prices" / f"zone={label}"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{start_day:%Y-%m-%d}_{args.days}d{response.suggested_extension}").write_bytes(
            response.content
        )
        rows = [
            row
            for row in to_records(parse_prices_response(response, eic))
            if row["market_day"] in wanted
        ]
        price_records.extend(rows)
        print(f"  entsoe prices {label}: {len(rows)} rows")

    prices = pd.DataFrame(price_records)
    if prices.empty:
        print("No prices. Stopping.")
        return 1

    # --- border, ENTSO-E ---
    schedules, capacity = fetch_border(
        client, start_utc, end_utc, raw_dir, start_day, verbose=False
    )
    print(f"  entsoe schedules: {len(schedules)} rows")
    print(f"  entsoe capacity:  {len(capacity)} rows")

    # --- OMIE, second publication of the same prices ---
    omie_rows: list[dict] = []
    if not args.skip_omie:
        omie_client = OmieClient()
        for day in days:
            try:
                response = omie_client.day_ahead_prices(day)
            except RuntimeError as exc:
                print(f"  omie {day}: FAILED {exc}")
                continue
            if response.looks_empty:
                print(f"  omie {day}: empty")
                continue
            target = raw_dir / "omie" / f"file_set={response.file_set}"
            target.mkdir(parents=True, exist_ok=True)
            (target / response.filename).write_bytes(response.content)
            omie_rows.extend(omie_to_records(parse_marginalpdbc(response.text)))
        print(f"  omie prices:      {len(omie_rows)} rows")

    # --- Open-Meteo, the causal upstream ---
    weather_rows: list[dict] = []
    if not args.skip_weather:
        weather_client = OpenMeteoClient()
        for location in SILVER_WEATHER_LOCATIONS:
            if location not in LOCATIONS:
                continue
            try:
                points, _ = weather_client.hourly(location, days[0], days[-1])
            except RuntimeError as exc:
                print(f"  weather {location}: FAILED {exc}")
                continue
            weather_rows.extend(weather_to_records(points))
        print(f"  open-meteo:       {len(weather_rows)} rows")

    silver = {
        "silver_entsoe_prices": prices,
        "silver_entsoe_schedules": schedules,
        "silver_entsoe_capacity": capacity,
        "silver_omie_prices": pd.DataFrame(omie_rows),
        "silver_weather": pd.DataFrame(weather_rows),
    }
    print()
    for name, frame in silver.items():
        write_table(frame, out_dir, name, "silver")

    # --- gold ---
    tables, step = build_gold(
        prices, schedules, capacity, pd.DataFrame(weather_rows), out_dir
    )
    report(tables, step, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())