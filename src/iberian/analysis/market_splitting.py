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

#: How long one settlement interval lasts, by the resolution ENTSO-E states.
#: A lookup rather than an inference, because during a market-wide change of
#: settlement resolution the inferred step is the mode of a year and is wrong
#: on one side of the change.
RESOLUTION_STEPS = {
    "PT15M": pd.Timedelta(minutes=15),
    "PT30M": pd.Timedelta(minutes=30),
    "PT60M": pd.Timedelta(minutes=60),
    "PT1H": pd.Timedelta(minutes=60),
}

#: The columns `detect_episodes` returns, in order.
EPISODE_COLUMNS = [
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
    "settlement_resolution",
]


def step_for(resolution) -> pd.Timedelta | None:
    """The interval length a resolution code stands for.

    `None` in, `None` out, so a caller with no resolution column falls back to
    inferring the step. An unrecognised code raises rather than defaulting: a
    wrong step does not fail anywhere, it quietly reports every episode at the
    wrong duration.
    """
    if resolution is None or (isinstance(resolution, float) and resolution != resolution):
        return None
    key = str(resolution).strip()
    if not key:
        return None
    if key not in RESOLUTION_STEPS:
        raise ValueError(
            f"Unrecognised settlement resolution {resolution!r}. Known: "
            f"{sorted(RESOLUTION_STEPS)}. Add it to RESOLUTION_STEPS rather "
            "than letting the step be inferred, which would be wrong for every "
            "interval in that segment."
        )
    return RESOLUTION_STEPS[key]


def resolution_segments(frame: pd.DataFrame, column: str = "resolution"):
    """Contiguous runs of one settlement resolution, oldest first.

    Iberia moved from hourly to quarter hourly settlement partway through the
    history this platform now holds. A year that spans that change is not
    broken data and must not be resampled into one grid: downsampling the
    quarter hours throws away the detail that makes splitting visible, and
    upsampling the hours invents three readings nobody cleared.

    So the series is cut where the resolution changes, each piece is analysed
    on its own terms, and the pieces are put back together. One episode
    straddling the changeover instant comes out as two, which is the honest
    answer: the settlement basis under it changed.
    """
    if frame.empty or column not in frame.columns:
        yield None, frame
        return

    ordered = frame.sort_values("ts_utc")
    if ordered[column].nunique(dropna=False) <= 1:
        yield ordered[column].iloc[0], ordered
        return

    changed = ordered[column].ne(ordered[column].shift())
    for _, part in ordered.groupby(changed.cumsum(), sort=True):
        yield part[column].iloc[0], part


def mixed_resolutions(frame: pd.DataFrame, column: str = "resolution") -> bool:
    """Whether this frame spans more than one settlement resolution."""
    return (
        not frame.empty
        and column in frame.columns
        and frame[column].nunique(dropna=False) > 1
    )


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

    # Both zones must be on the same settlement interval AT EACH INSTANT.
    # Pivoting PT60M against PT15M lines one hourly price up with the first
    # quarter of the hour and leaves the other three blank, which then get
    # dropped, so three quarters of the day silently disappears.
    #
    # The check is per timestamp rather than over the whole frame, and that
    # distinction is the whole point. Iberia moved from hourly to quarter
    # hourly settlement partway through the history this platform holds, so a
    # year legitimately contains both. What can never happen is the two zones
    # clearing the same instant on different grids: they are one coupled
    # market. An earlier version of this guard tested the frame as a whole and
    # refused a perfectly good year the first time one was loaded.
    if "resolution" in prices.columns:
        per_instant = prices.groupby("ts_utc")["resolution"].nunique(dropna=False)
        disagreeing = per_instant[per_instant > 1]
        if not disagreeing.empty:
            sample = (
                prices[prices["ts_utc"].isin(disagreeing.index[:3])]
                .sort_values(["ts_utc", "zone_eic"])
                [["ts_utc", "zone_eic", "resolution"]]
            )
            raise ValueError(
                f"{len(disagreeing)} timestamp(s) where the two zones report "
                "different settlement resolutions. They clear one coupled "
                "market, so this is a parsing or deduplication fault upstream "
                f"rather than something to resample away. Sample:\n"
                f"{sample.to_string(index=False)}"
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

    # Carried rather than recomputed downstream. Everything that groups or
    # measures these intervals needs to know how long one lasts, and inferring
    # it from the gaps is exactly what goes wrong across a change of
    # resolution.
    if "resolution" in prices.columns:
        wide["resolution"] = wide["ts_utc"].map(
            prices.groupby("ts_utc")["resolution"].first()
        )

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

    3. A frame spanning a change of settlement resolution is cut at the
       change and each piece grouped with its own step. Without that, the step
       is the mode of the whole history and is wrong on one side of it: at a
       15 minute step every hourly decoupled interval becomes an episode of
       its own, so the count explodes and every duration is a quarter of what
       it was. Nothing fails; the table is just wrong. Passing `step`
       explicitly turns this off, because then the caller has said what it
       wants.

    Durations come out in real hours, so a two interval split at PT15M is 0.5
    hours, not 2.
    """
    empty_columns = list(EPISODE_COLUMNS)

    if step is None and mixed_resolutions(flagged):
        pieces = []
        for resolution, segment in resolution_segments(flagged):
            found = detect_episodes(segment, step=step_for(resolution))
            if not found.empty:
                pieces.append(found)
        if not pieces:
            return pd.DataFrame(columns=empty_columns)
        episodes = (
            pd.concat(pieces, ignore_index=True)
            .sort_values("start_utc")
            .reset_index(drop=True)
        )
        # Renumbered across the whole series so the id stays unique. Nothing
        # downstream keys on it, `episode_key` is the market day and the start
        # time, but a repeated id in a table is a trap for whoever reads it.
        episodes["episode_id"] = range(1, len(episodes) + 1)
        return episodes[empty_columns]

    decoupled = flagged.loc[flagged["is_decoupled"]].sort_values("ts_utc").copy()
    if decoupled.empty:
        return pd.DataFrame(columns=empty_columns)

    if step is None:
        step = infer_step(flagged)

    if "resolution" in flagged.columns and not flagged["resolution"].empty:
        settlement_resolution = flagged["resolution"].iloc[0]
    else:
        settlement_resolution = None

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

    # On the row, so a reader can see that a 2026 episode counted quarter hours
    # and a 2025 one counted hours without having to know when the market
    # changed.
    episodes["settlement_resolution"] = settlement_resolution

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