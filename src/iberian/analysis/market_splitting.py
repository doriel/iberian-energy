"""Market splitting detection: the analytical core of the platform.

Under normal MIBEL coupling, Portugal and Spain clear the day-ahead auction at
exactly the same price. When the physical interconnection saturates, the market
splits and the two zones clear at different prices. Portugal usually, though
not always, ends up paying the premium.

The detection is deliberately simple and explainable, because every number here
has to survive a journalist or a regulator asking where it came from:

    decoupled(hour)  <=>  abs(price_PT - price_ES) > DECOUPLING_EPSILON
    premium(hour)     =   price_PT - price_ES

Contiguous decoupled hours are grouped into episodes with gaps and islands, so
the platform can talk about "a 7 hour split on the evening of the 12th" rather
than 7 unrelated rows.

No Spark, no Databricks, no credentials. Runs in a plain Python session, which
means it is unit testable and you can iterate on the logic in seconds instead
of waiting for a cluster.
"""

from __future__ import annotations

import pandas as pd

from iberian.config import DECOUPLING_EPSILON, SEVERITY_BANDS


def build_spread_series(
    prices: pd.DataFrame,
    pt_eic: str,
    es_eic: str,
    price_column: str = "price_eur_mwh",
) -> pd.DataFrame:
    """Pivot long price rows into one row per timestamp with both zones.

    Expects the tidy output of the A44 parser: zone_eic, ts_utc, price.
    Hours where either zone is missing are dropped, since a spread needs both
    sides and a half populated hour would silently read as a zero spread.
    """
    required = {"zone_eic", "ts_utc", price_column}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices is missing columns: {sorted(missing)}")

    # Fail loudly on duplicate timestamps per zone. An A44 document can carry
    # several TimeSeries covering the same window, and a pivot would quietly
    # keep one of them. Quietly picking a price is how you end up publishing a
    # spread you cannot defend, so make the caller deal with it upstream.
    duplicates = prices.duplicated(subset=["zone_eic", "ts_utc"], keep=False)
    if duplicates.any():
        offending = (
            prices.loc[duplicates]
            .groupby(["zone_eic", "ts_utc"])[price_column]
            .nunique()
        )
        conflicting = offending[offending > 1]
        sample = prices.loc[duplicates].head(6)
        raise ValueError(
            f"{int(duplicates.sum())} duplicated (zone, timestamp) rows, of which "
            f"{len(conflicting)} disagree on price. Deduplicate before computing "
            f"a spread. Sample:\n{sample.to_string(index=False)}"
        )

    # Both zones must be on the same settlement interval. Pivoting PT60M
    # against PT15M lines up one hourly price with the first quarter of the
    # hour and leaves the other three blank, which then get dropped, so three
    # quarters of the day silently disappears.
    if "resolution" in prices.columns:
        per_zone = prices.groupby("zone_eic")["resolution"].unique()
        distinct = {tuple(sorted(values)) for values in per_zone}
        if len(distinct) > 1 or any(len(values) > 1 for values in per_zone):
            raise ValueError(
                "Zones are on different settlement resolutions, which cannot be "
                f"compared interval by interval: {per_zone.to_dict()}. Resample "
                "to a common resolution first."
            )

    wide = (
        prices.pivot_table(
            index="ts_utc", columns="zone_eic", values=price_column, aggfunc="last"
        )
        .rename(columns={pt_eic: "price_pt", es_eic: "price_es"})
        .reset_index()
    )

    for column in ("price_pt", "price_es"):
        if column not in wide.columns:
            wide[column] = pd.NA

    wide = wide.dropna(subset=["price_pt", "price_es"]).sort_values("ts_utc")
    wide["spread_eur_mwh"] = wide["price_pt"] - wide["price_es"]
    return wide.reset_index(drop=True)


def _severity(abs_spread: float) -> str:
    for low, high, label in SEVERITY_BANDS:
        if low <= abs_spread < high:
            return label
    return "none"


def flag_decoupling(
    spread: pd.DataFrame, epsilon: float = DECOUPLING_EPSILON
) -> pd.DataFrame:
    """Add the decoupling flag, the premium direction, and a severity band."""
    out = spread.copy()
    out["abs_spread"] = out["spread_eur_mwh"].abs()
    out["is_decoupled"] = out["abs_spread"] > epsilon
    out["severity"] = out["abs_spread"].apply(
        lambda value: _severity(value) if value > epsilon else "none"
    )
    out["premium_side"] = out["spread_eur_mwh"].apply(
        lambda value: "PT" if value > epsilon else ("ES" if value < -epsilon else "none")
    )
    return out


def infer_step(frame: pd.DataFrame, column: str = "ts_utc") -> pd.Timedelta:
    """Work out the settlement interval from the data itself.

    Since the 15 minute MTU came in, Iberian day-ahead prices arrive as PT15M,
    not PT60M. Assuming hours against quarter hourly data overstates every
    duration by a factor of four and merges episodes across coupled intervals,
    so the step has to come from the data rather than from a default.
    """
    stamps = frame[column].drop_duplicates().sort_values()
    if len(stamps) < 2:
        raise ValueError(
            "Need at least two distinct timestamps to infer the settlement "
            "interval. Pass step explicitly."
        )

    deltas = stamps.diff().dropna()
    return deltas.mode().iloc[0]


def detect_episodes(
    flagged: pd.DataFrame, step: pd.Timedelta | None = None
) -> pd.DataFrame:
    """Group contiguous decoupled intervals into episodes.

    Gaps and islands, with two wrinkles that matter in practice:

    1. The step is inferred from the data unless given. At PT15M an episode
       must break on a 15 minute gap, not an hourly one, otherwise coupled
       intervals in the middle get swallowed and the episode reads longer and
       more continuous than it was.

    2. An episode also breaks on a missing timestamp, not just on a coupled
       interval. Without that, a data outage stitches two unrelated splits into
       one and the duration figure becomes a lie.

    Durations come out in real hours, so a two interval split at PT15M is 0.5
    hours, not 2.
    """
    empty_columns = [
        "episode_id",
        "start_utc",
        "end_utc",
        "intervals",
        "duration_hours",
        "mean_spread",
        "max_abs_spread",
        "peak_spread",
        "premium_side",
        "max_severity",
    ]

    decoupled = flagged.loc[flagged["is_decoupled"]].sort_values("ts_utc").copy()
    if decoupled.empty:
        return pd.DataFrame(columns=empty_columns)

    if step is None:
        step = infer_step(flagged)

    previous_ts = decoupled["ts_utc"].shift(1)
    starts_new = previous_ts.isna() | ((decoupled["ts_utc"] - previous_ts) > step)
    decoupled["episode_id"] = starts_new.cumsum()

    severity_rank = {"none": 0, "minor": 1, "moderate": 2, "severe": 3}
    decoupled["severity_rank"] = decoupled["severity"].map(severity_rank)

    grouped = decoupled.groupby("episode_id")
    episodes = grouped.agg(
        start_utc=("ts_utc", "min"),
        last_start_utc=("ts_utc", "max"),
        intervals=("ts_utc", "count"),
        mean_spread=("spread_eur_mwh", "mean"),
        max_abs_spread=("abs_spread", "max"),
        severity_rank=("severity_rank", "max"),
    ).reset_index()

    # Peak spread keeps its sign, so the direction of the worst interval survives.
    peak_idx = grouped["abs_spread"].idxmax()
    episodes["peak_spread"] = decoupled.loc[peak_idx, "spread_eur_mwh"].to_numpy()
    episodes["premium_side"] = decoupled.loc[peak_idx, "premium_side"].to_numpy()

    # end_utc is when the episode stops, which is one step past the last
    # interval that started inside it. Reporting the last start as the end
    # makes a single interval episode look instantaneous.
    episodes["end_utc"] = episodes["last_start_utc"] + step
    episodes["duration_hours"] = episodes["intervals"] * (
        step / pd.Timedelta(hours=1)
    )

    rank_to_severity = {rank: name for name, rank in severity_rank.items()}
    episodes["max_severity"] = episodes["severity_rank"].map(rank_to_severity)

    return episodes[empty_columns]


def summarise(episodes: pd.DataFrame) -> dict:
    """Headline numbers for the manufacturer and journalist personas.

    Every duration is in real hours, so it means the same thing whether the
    underlying data is PT60M or PT15M.
    """
    if episodes.empty:
        return {
            "episode_count": 0,
            "decoupled_hours": 0.0,
            "worst_spread": 0.0,
            "longest_episode_hours": 0.0,
        }

    worst_row = episodes.loc[episodes["max_abs_spread"].idxmax()]
    return {
        "episode_count": int(len(episodes)),
        "decoupled_hours": float(episodes["duration_hours"].sum()),
        "worst_spread": float(worst_row["peak_spread"]),
        "worst_spread_at": worst_row["start_utc"],
        "longest_episode_hours": float(episodes["duration_hours"].max()),
    }
