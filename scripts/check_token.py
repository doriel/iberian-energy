"""Five second sanity check: is the token good and are the EIC codes right?

Run this before anything else. It makes one small request and tells you which
of the three likely problems you have, instead of making you infer it from a
seven day fetch that prints nothing.

    python scripts/check_token.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import BIDDING_ZONES, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient, EntsoeRequestError  # noqa: E402
from iberian.market_time import market_day_window  # noqa: E402
from iberian.parsing.entsoe_prices import parse_prices_response  # noqa: E402


def main() -> int:
    settings = Settings.from_env()

    try:
        token = settings.require_entsoe_token()
    except RuntimeError as exc:
        print(f"FAIL  {exc}")
        return 1

    print(f"Token loaded, ends with ...{token[-6:]}")

    # Two days back: day-ahead results for that window are definitely published.
    # The window is a market day (local midnight in CET), not a UTC calendar
    # day, otherwise the request straddles two market days and returns both.
    day = (datetime.now(timezone.utc) - timedelta(days=2)).date()
    start, end = market_day_window(day)
    print(f"Asking for market day {day} ({start:%H:%M}Z to {end:%H:%M}Z)\n")

    client = EntsoeClient(token)
    failures = 0

    for label, eic in BIDDING_ZONES.items():
        try:
            response = client.day_ahead_prices(eic, start, end)
        except EntsoeRequestError as exc:
            print(f"FAIL  {label} ({eic}): {exc}")
            failures += 1
            continue
        except RuntimeError as exc:
            print(f"FAIL  {label} ({eic}): network or timeout. {exc}")
            failures += 1
            continue

        if response.is_empty:
            print(
                f"EMPTY {label} ({eic}): the API answered, so the token works, "
                "but no data came back. Either the EIC code is wrong for this "
                "document type, or that window has no published prices."
            )
            failures += 1
            continue

        points = parse_prices_response(response, eic)
        if not points:
            print(f"EMPTY {label} ({eic}): response parsed to zero day-ahead points.")
            failures += 1
            continue

        prices = [p.price_eur_mwh for p in points]
        stamps = {p.ts_utc for p in points}
        duplicates = len(points) - len(stamps)
        warning = f"  WARNING {duplicates} duplicated timestamps" if duplicates else ""

        print(
            f"OK    {label} ({eic}): {len(points)} points over {len(stamps)} "
            f"timestamps, {min(prices):.2f} to {max(prices):.2f} EUR/MWh, "
            f"resolution {points[0].resolution}{warning}"
        )
        if duplicates:
            failures += 1

    print()
    if failures:
        print("Something is off. Send the output above and we will fix it.")
        return 1

    print("Both zones good. Run the full fetch:")
    print("  python scripts/run_market_splitting.py --start 2026-09-01 --days 7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
