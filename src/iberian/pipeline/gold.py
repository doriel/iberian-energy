"""Gold tables, one per persona.

The proposal commits to three users and states that every gold table must
serve one of them. That constraint is worth taking literally: a table nobody
named can be cut, and a persona with no table is a promise not kept.

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
    RESOLUTION_STEPS,
    detect_episodes,
    flag_decoupling,
    infer_step,
    mixed_resolutions,
    resolution_segments,
    step_for,
)
from iberian.market_time import to_market_day


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
        "resolution": "settlement_resolution",
    }
    present = {src: dst for src, dst in columns.items() if src in joined.columns}
    out = joined[list(present)].rename(columns=present).copy()

    # How long this row covers. Without it, anything that averages or counts
    # these rows weights a quarter hour the same as an hour, and this table now
    # holds both: Iberia changed settlement resolution partway through the
    # history. Null for a code nobody has taught this module about, which is
    # visible, rather than a guess, which is not.
    if "settlement_resolution" in out.columns:
        out["interval_hours"] = [
            RESOLUTION_STEPS[str(value).strip()] / pd.Timedelta(hours=1)
            if value is not None
            and not (isinstance(value, float) and value != value)
            and str(value).strip() in RESOLUTION_STEPS
            else None
            for value in out["settlement_resolution"]
        ]

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

    # Counting intervals was right while every interval was the same length.
    # It stopped being right the day the history spanned both hourly and
    # quarter hourly settlement: an hour of the quarter hourly period
    # contributes four rows and an hour of the hourly period one, so a plain
    # count weights the recent period four times as heavily and the answer a
    # manufacturer acts on drifts towards whichever period has more rows.
    #
    # Weighting by real time is the same number whenever both halves are on
    # one grid, and the defensible one when they are not.
    if "interval_hours" in intervals.columns:
        timed = intervals.copy()
        timed["_hours"] = pd.to_numeric(timed["interval_hours"], errors="coerce")
        timed["_decoupled_hours"] = timed["_hours"] * timed["is_decoupled"].astype(float)
        totals = (
            timed.groupby("hour_of_day_utc")[["_hours", "_decoupled_hours"]]
            .sum(min_count=1)
            .rename(columns={"_hours": "hours", "_decoupled_hours": "decoupled_hours"})
            .reset_index()
        )
        profile = profile.merge(totals, on="hour_of_day_utc", how="left")

    if "hours" in profile.columns and profile["hours"].fillna(0).gt(0).all():
        profile["split_probability"] = (
            profile["decoupled_hours"] / profile["hours"]
        )
    else:
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

    Two money columns, and they answer different questions:

    `extra_cost_eur` is what Portugal paid extra, importing direction only. An
    hour Portugal exported at a premium cost Portugal nothing, so it counts
    zero here, which is right for the question being asked.

    `congestion_rent_eur` is the price difference applied to the flow in
    whichever direction it went. That is what a system operator publishes, and
    it is the only one of the two that can be checked against REE.
    """
    if flagged.empty:
        return pd.DataFrame()

    # Same reason as in `detect_episodes`, and it matters more here: `step`
    # also scales the cost. An hourly interval costed at a quarter of an hour
    # understates the money by a factor of four, and the cost figure is the
    # one this project has validated to 0.0015% against REE's published
    # congestion rent. Getting it silently wrong would cost that claim.
    if step is None and mixed_resolutions(flagged):
        pieces = []
        for resolution, segment in resolution_segments(flagged):
            found = gold_split_episodes(
                segment, border=border, step=step_for(resolution)
            )
            if not found.empty:
                pieces.append(found)
        if not pieces:
            return pd.DataFrame()
        out = (
            pd.concat(pieces, ignore_index=True)
            .sort_values("start_utc")
            .reset_index(drop=True)
        )
        out["episode_id"] = range(1, len(out) + 1)
        return out

    step = step or infer_step(flagged)
    episodes = detect_episodes(flagged, step=step)

    # Every other gold table carries market_day, and the Delta writer uses it
    # for replaceWhere so that re-running one day replaces only that day. This
    # table had no such column, which silently dropped it to a full overwrite:
    # a scheduled daily run would have deleted every earlier episode. An
    # episode that crosses midnight is attributed to the day it started in,
    # which is also how anyone reading the table would describe it.
    if episodes.empty:
        episodes["market_day"] = pd.Series(dtype="object")
        return episodes
    episodes["market_day"] = episodes["start_utc"].apply(to_market_day)

    step_hours = step / pd.Timedelta(hours=1)

    if border is not None and not border.empty:
        merged = flagged.merge(border, on="ts_utc", how="left")
    else:
        merged = flagged.copy()

    costs = []
    rents = []
    saturated_share = []
    for _, episode in episodes.iterrows():
        window = merged[
            (merged["ts_utc"] >= episode["start_utc"])
            & (merged["ts_utc"] < episode["end_utc"])
            & merged["is_decoupled"]
        ]
        if "net_flow_mw" in window.columns and not window.empty:
            # What Portugal paid extra, which is the journalist's number. Only
            # the importing direction counts, because on an hour Portugal
            # exported it did not pay a premium, it collected one.
            imported_mwh = window["net_flow_mw"].clip(lower=0) * step_hours
            costs.append(float((window["spread_eur_mwh"].abs() * imported_mwh).sum()))

            # The congestion rent, which is a different quantity and is the one
            # REE publishes: the price difference applied to whatever crossed
            # the border, in whichever direction it crossed.
            #
            # These two were treated as the same thing for months and the
            # agreement with REE looked near perfect, because every day in the
            # window happened to have Portugal importing. A year of history
            # broke that: in February 2026 the average flow was 195 MW from
            # Portugal into Spain, 830 decoupled intervals ran that way, and
            # the import cost read 12 per cent of REE's rent. The deviation by
            # month tracked the count of exporting intervals almost exactly,
            # and the five months with none of them agreed to the cent.
            flowed_mwh = window["net_flow_mw"].abs() * step_hours
            rents.append(float((window["spread_eur_mwh"].abs() * flowed_mwh).sum()))
        else:
            costs.append(None)
            rents.append(None)

        if "is_saturated" in window.columns and not window.empty:
            saturated_share.append(float(window["is_saturated"].mean()))
        else:
            saturated_share.append(None)

    episodes = episodes.copy()
    episodes["extra_cost_eur"] = costs
    episodes["congestion_rent_eur"] = rents
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