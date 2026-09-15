"""Gold tables, one per persona.

The platform is designed around three users, and every gold table must serve
one of them. That constraint is worth taking literally: a table nobody named
can be cut, and a persona with no table is a gap in the product.

1. A manufacturer deciding when to run energy intensive equipment.
2. A journalist or regulator watcher needing a defensible number with a cause.
3. A grid analyst tracking interconnection saturation.

These are pandas functions on purpose. They take tidy frames and return tidy
frames, so the same code runs in a local test, in a Databricks notebook over a
pandas-on-Spark frame, or inside a Lakeflow pipeline. Nothing here imports
Databricks, which is what keeps the logic testable in milliseconds.
"""

from __future__ import annotations

import pandas as pd

from iberian.analysis.market_splitting import (
    detect_episodes,
    flag_decoupling,
    infer_step,
)


def gold_interval_premium(joined: pd.DataFrame) -> pd.DataFrame:
    """Persona 1 and 3: one row per settlement interval, fully described.

    `joined` is the flagged spread series merged with the border series.
    """
    columns = {
        "ts_utc": "ts_utc",
        "price_pt": "price_pt_eur_mwh",
        "price_es": "price_es_eur_mwh",
        "spread_eur_mwh": "premium_eur_mwh",
        "abs_spread": "abs_premium_eur_mwh",
        "is_decoupled": "is_decoupled",
        "premium_side": "premium_side",
        "severity": "severity",
        "net_flow_mw": "net_flow_mw",
        "capacity_mw": "capacity_mw",
        "utilisation": "utilisation",
        "is_saturated": "is_saturated",
    }
    present = {src: dst for src, dst in columns.items() if src in joined.columns}
    out = joined[list(present)].rename(columns=present).copy()

    if "ts_utc" in out.columns:
        from iberian.market_time import to_market_day

        out["market_day"] = out["ts_utc"].apply(to_market_day)
        out["hour_of_day_utc"] = out["ts_utc"].dt.hour
    return out.sort_values("ts_utc").reset_index(drop=True)


def gold_daily_profile(intervals: pd.DataFrame) -> pd.DataFrame:
    """Persona 1: when, on a typical day, does Portugal pay?

    The manufacturer does not care about one episode. They care whether 18:00
    is reliably worse than 04:00, because that is a decision they can act on
    every week. This is the table that answers that.
    """
    if intervals.empty:
        return pd.DataFrame()

    grouped = intervals.groupby("hour_of_day_utc")
    profile = grouped.agg(
        intervals=("ts_utc", "count"),
        decoupled_intervals=("is_decoupled", "sum"),
        mean_premium_eur_mwh=("premium_eur_mwh", "mean"),
        worst_premium_eur_mwh=("premium_eur_mwh", lambda s: s.abs().max()),
    ).reset_index()

    profile["split_probability"] = (
        profile["decoupled_intervals"] / profile["intervals"]
    )

    if "utilisation" in intervals.columns:
        profile = profile.merge(
            grouped["utilisation"].mean().rename("mean_utilisation").reset_index(),
            on="hour_of_day_utc",
            how="left",
        )
    if "capacity_mw" in intervals.columns:
        profile = profile.merge(
            grouped["capacity_mw"].mean().rename("mean_capacity_mw").reset_index(),
            on="hour_of_day_utc",
            how="left",
        )

    return profile.sort_values("hour_of_day_utc").reset_index(drop=True)


def gold_split_episodes(
    flagged: pd.DataFrame,
    border: pd.DataFrame | None = None,
    step: pd.Timedelta | None = None,
) -> pd.DataFrame:
    """Persona 2: the defensible number, with a cause and a cost.

    A journalist needs one line they can print: how long, how much, and why.
    The cost is the honest part that is easy to get wrong, so it is stated as
    the premium applied to the energy actually imported during the episode,
    not to Portuguese demand, which would overstate it wildly.
    """
    if flagged.empty:
        return pd.DataFrame()

    step = step or infer_step(flagged)
    episodes = detect_episodes(flagged, step=step)
    if episodes.empty:
        return episodes

    step_hours = step / pd.Timedelta(hours=1)

    if border is not None and not border.empty:
        merged = flagged.merge(border, on="ts_utc", how="left")
    else:
        merged = flagged.copy()

    costs = []
    saturated_share = []
    for _, episode in episodes.iterrows():
        window = merged[
            (merged["ts_utc"] >= episode["start_utc"])
            & (merged["ts_utc"] < episode["end_utc"])
            & merged["is_decoupled"]
        ]
        if "net_flow_mw" in window.columns and not window.empty:
            imported_mwh = window["net_flow_mw"].clip(lower=0) * step_hours
            costs.append(float((window["spread_eur_mwh"].abs() * imported_mwh).sum()))
        else:
            costs.append(None)

        if "is_saturated" in window.columns and not window.empty:
            saturated_share.append(float(window["is_saturated"].mean()))
        else:
            saturated_share.append(None)

    episodes = episodes.copy()
    episodes["extra_cost_eur"] = costs
    episodes["share_saturated"] = saturated_share
    episodes["explained_by_saturation"] = [
        None if share is None else share >= 0.5 for share in saturated_share
    ]
    return episodes.reset_index(drop=True)


def gold_weather_context(
    intervals: pd.DataFrame, weather: pd.DataFrame
) -> pd.DataFrame:
    """Persona 2 and 3: the upstream driver, from the second source.

    ENTSO-E says the border was full. Weather says why Spanish power was cheap
    enough to be worth importing. Hourly weather is broadcast onto the quarter
    hourly grid, which is legitimate because the observation is hourly, and
    marked as such rather than implying a precision it does not have.
    """
    if intervals.empty or weather.empty:
        return pd.DataFrame()

    wide = weather.pivot_table(
        index="ts_utc",
        columns="location",
        values=["shortwave_radiation_wm2", "wind_speed_100m_kmh", "temperature_c"],
        aggfunc="mean",
    )
    wide.columns = [f"{variable}__{location}" for variable, location in wide.columns]
    wide = wide.sort_index()

    target = pd.DatetimeIndex(intervals["ts_utc"]).sort_values().unique()
    aligned = wide.reindex(
        target, method="ffill", tolerance=pd.Timedelta(minutes=59)
    )
    aligned.index.name = "ts_utc"

    out = intervals.merge(aligned.reset_index(), on="ts_utc", how="left")
    out["weather_resolution"] = "PT60M"
    return out


def gold_tables(
    flagged: pd.DataFrame,
    border: pd.DataFrame | None = None,
    weather: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Build every gold table, keyed by the name it lands under."""
    joined = (
        flagged.merge(border, on="ts_utc", how="left")
        if border is not None and not border.empty
        else flagged
    )

    intervals = gold_interval_premium(joined)
    tables = {
        "gold_interval_premium": intervals,
        "gold_daily_profile": gold_daily_profile(intervals),
        "gold_split_episodes": gold_split_episodes(flagged, border),
    }
    if weather is not None and not weather.empty:
        tables["gold_weather_context"] = gold_weather_context(intervals, weather)
    return tables
