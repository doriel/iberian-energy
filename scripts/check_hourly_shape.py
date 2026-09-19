"""Is the hourly concentration of decoupling the sun, or an artefact?

Decoupling in the gold tables falls inside a narrow band of hours and is exactly
zero outside it. That is either the Iberian summer working exactly as it should,
or something wrong upstream, and the two look identical in a bar chart.

The test that separates them is drift. Spanish solar output peaks at solar noon,
which moves through the calendar: across a window from mid July to mid September
the useful solar hours shrink at both ends by roughly an hour. If the band is
the sun, it tracks local clock time and narrows as the season turns. If it is
pinned to fixed UTC hours for sixty days regardless of the date, it is not the
sun, because nothing physical is anchored to UTC.

Three further checks sit alongside it:

  * whether the spread outside the band is exactly zero or merely small, since
    exact zero across thousands of intervals is what coupling actually looks
    like and anything else would be a threshold effect,
  * whether utilisation reaches saturation only inside the band, which is the
    mechanism the whole story rests on,
  * whether utilisation has a ceiling below 1.0 outside the band, which no
    market produces and a generator does.

    python scripts/check_hourly_shape.py
    python scripts/check_hourly_shape.py --root data/lakehouse

Nothing here writes anything or calls anything. It reads the tables that are
already on disk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import MARKET_TIMEZONE  # noqa: E402

BAR = "#"


def load(root: Path) -> pd.DataFrame:
    path = root / "gold" / "gold_interval_premium.parquet"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run scripts/build_medallion.py first.")
    frame = pd.read_parquet(path)
    frame["ts_utc"] = pd.to_datetime(frame["ts_utc"], utc=True)
    frame["hour_utc"] = frame["ts_utc"].dt.hour
    local = frame["ts_utc"].dt.tz_convert(MARKET_TIMEZONE)
    frame["hour_local"] = local.dt.hour
    # Dropping the offset before the period, because pandas warns that it is
    # about to drop it anyway and a warning in the middle of a diagnostic is
    # noise somebody has to decide is harmless.
    frame["month"] = local.dt.tz_localize(None).dt.to_period("M").astype(str)
    return frame


def histogram(frame: pd.DataFrame, column: str, title: str) -> None:
    print(f"\n{title}")
    grouped = frame.groupby(column).agg(
        intervals=("ts_utc", "count"),
        decoupled=("is_decoupled", "sum"),
    )
    grouped["share"] = grouped["decoupled"] / grouped["intervals"]
    widest = max(grouped["share"].max(), 0.0001)
    for hour, row in grouped.iterrows():
        bar = BAR * int(round(row["share"] / widest * 40))
        print(
            f"  {int(hour):02d}  {int(row['decoupled']):5d}/{int(row['intervals']):5d}"
            f"  {row['share']:6.1%}  {bar}"
        )


def band(frame: pd.DataFrame, column: str) -> tuple[int, int] | None:
    """First and last hour in which anything decoupled at all."""
    hours = sorted(frame.loc[frame["is_decoupled"], column].unique())
    return (int(hours[0]), int(hours[-1])) if hours else None


def drift(frame: pd.DataFrame) -> None:
    print("\nDoes the band move with the calendar?")
    print("  A band that is the sun narrows and shifts as the season turns.")
    print("  A band pinned to the same UTC hours for sixty days is not the sun.\n")
    print(f"  {'month':<10} {'UTC band':<12} {'local band':<12} {'decoupled':>10}")
    for month, chunk in frame.groupby("month"):
        utc = band(chunk, "hour_utc")
        local = band(chunk, "hour_local")
        print(
            f"  {month:<10} "
            f"{(f'{utc[0]:02d}-{utc[1]:02d}' if utc else 'none'):<12} "
            f"{(f'{local[0]:02d}-{local[1]:02d}' if local else 'none'):<12} "
            f"{int(chunk['is_decoupled'].sum()):>10}"
        )


def outside_the_band(frame: pd.DataFrame) -> None:
    edges = band(frame, "hour_utc")
    if edges is None:
        print("\nNothing is decoupled anywhere. That is its own problem.")
        return
    low, high = edges
    outside = frame[(frame["hour_utc"] < low) | (frame["hour_utc"] > high)]
    if outside.empty:
        print("\nEvery hour of the day carries at least one split. No band.")
        return

    gap = outside["abs_premium_eur_mwh"]
    print(f"\nOutside the {low:02d}-{high:02d} UTC band, {len(outside)} intervals:")
    print(f"  exactly zero       {int((gap == 0).sum())}")
    print(f"  above zero         {int((gap > 0).sum())}")
    print(f"  largest gap        {gap.max():.4f} EUR/MWh")
    print(
        "\n  Exact zero across every one of them is what coupling looks like: the"
        "\n  two zones clear at one price and both publishers report it to the"
        "\n  cent. A scatter of small non zero gaps instead would mean the band is"
        "\n  a threshold effect rather than a market state."
    )


def utilisation(frame: pd.DataFrame) -> None:
    if "utilisation" not in frame.columns or frame["utilisation"].isna().all():
        print("\nNo utilisation column, so the mechanism cannot be checked here.")
        return

    print("\nUtilisation by UTC hour, which is the mechanism:")
    grouped = frame.groupby("hour_utc").agg(
        mean=("utilisation", "mean"),
        max=("utilisation", "max"),
        saturated=("utilisation", lambda s: int((s >= 0.99).sum())),
        decoupled=("is_decoupled", "sum"),
    )
    print(f"  {'hour':<6}{'mean':>8}{'max':>8}{'>=0.99':>8}{'decoupled':>11}")
    for hour, row in grouped.iterrows():
        print(
            f"  {int(hour):02d}{row['mean']:>10.3f}{row['max']:>8.3f}"
            f"{int(row['saturated']):>8}{int(row['decoupled']):>11}"
        )

    edges = band(frame, "hour_utc")
    if edges:
        low, high = edges
        outside = frame[(frame["hour_utc"] < low) | (frame["hour_utc"] > high)]
        ceiling = outside["utilisation"].max()
        print(f"\n  Highest utilisation outside the band: {ceiling:.6f}")
        if 0.90 < ceiling < 0.999:
            print(
                "  A hard ceiling just below saturation, never reached and never"
                "\n  crossed, is not something a market produces. Treat the tables"
                "\n  as suspect until this is explained."
            )
        else:
            print(
                "  No artificial ceiling. Saturation is reached where the prices"
                "\n  separate and not elsewhere, which is the mechanism holding."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/lakehouse")
    args = parser.parse_args()

    frame = load(Path(args.root))
    days = frame["ts_utc"].dt.date.nunique()
    print(
        f"{len(frame)} intervals over {days} days, "
        f"{int(frame['is_decoupled'].sum())} decoupled "
        f"({frame['is_decoupled'].mean():.1%})"
    )

    histogram(frame, "hour_utc", "Share of intervals decoupled, by UTC hour:")
    histogram(
        frame,
        "hour_local",
        f"Same thing in local time ({MARKET_TIMEZONE}), which is where the sun is:",
    )
    drift(frame)
    outside_the_band(frame)
    utilisation(frame)

    print(
        "\nRead it in this order: if the band drifts month to month and"
        "\nutilisation saturates only inside it, the concentration is the market"
        "\nand can be presented as a finding. If the band sits on the same UTC"
        "\nhours all summer, or utilisation stops dead below 1.0 outside it, it"
        "\nis not, and nothing about that tab should be shown."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())