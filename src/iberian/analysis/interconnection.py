"""Interconnection saturation: the causal half of the story.

Measuring that PT and ES priced apart is descriptive. The claim the platform
actually makes is that they priced apart *because* the interconnector could not
carry any more cheap Spanish power. That needs two numbers per interval:

    utilisation = scheduled net flow / available capacity in that direction

When utilisation reaches 1 the border is full, the market splits, and the two
zones are free to clear at different prices. That is the fact the agent gets to
cite, rather than asserting a cause it inferred from a correlation.

One join detail decides whether the numbers mean anything: ENTSO-E publishes
day-ahead capacity hourly (PT60M) and scheduled exchanges quarter hourly
(PT15M). Dividing one by the other without aligning them either drops three
quarters of every hour or silently compares an interval against the wrong
hour's limit.
"""

from __future__ import annotations

import pandas as pd

# At or above this share of capacity, treat the border as full. Not 1.0,
# because the published capacity and the schedule are rounded independently and
# a genuinely full border often lands a megawatt or two short.
SATURATION_THRESHOLD = 0.98


def align_capacity_to_schedule(
    capacity: pd.DataFrame,
    target_index: pd.Series | pd.Index,
    ts_column: str = "ts_utc",
    value_column: str = "capacity_mw",
) -> pd.Series:
    """Broadcast hourly capacity onto the quarter hourly schedule grid.

    Forward fill is correct here: an hourly NTC applies to every interval
    inside that hour. The tolerance is what keeps it honest, so a missing hour
    of capacity leaves those intervals empty instead of quietly inheriting the
    previous hour's limit and inventing headroom that was never published.
    """
    series = (
        capacity.set_index(ts_column)[value_column]
        .sort_index()
        .groupby(level=0)
        .last()
    )
    target = pd.DatetimeIndex(target_index).sort_values().unique()
    return series.reindex(
        target, method="ffill", tolerance=pd.Timedelta(minutes=59)
    )


def build_border_series(
    schedules: pd.DataFrame,
    capacity: pd.DataFrame,
    a_to_b: tuple[str, str],
) -> pd.DataFrame:
    """One row per interval with net flow, capacity and utilisation.

    `schedules` and `capacity` are tidy frames with out_domain, in_domain,
    ts_utc and a value column. `a_to_b` names the direction treated as
    positive, as (out_domain, in_domain).

    Net flow matters because both directions get published: a raw 4700 MW in
    one direction means nothing until you subtract whatever was scheduled back
    the other way.
    """
    source, sink = a_to_b

    forward = schedules[
        (schedules["out_domain"] == source) & (schedules["in_domain"] == sink)
    ]
    reverse = schedules[
        (schedules["out_domain"] == sink) & (schedules["in_domain"] == source)
    ]

    if forward.empty:
        raise ValueError(
            f"No scheduled exchanges from {source} to {sink}. "
            "Check the direction, the platform publishes one per request."
        )

    frame = (
        forward.groupby("ts_utc")["quantity_mw"]
        .last()
        .rename("scheduled_forward_mw")
        .to_frame()
    )
    frame["scheduled_reverse_mw"] = (
        reverse.groupby("ts_utc")["quantity_mw"].last() if not reverse.empty else 0.0
    )
    frame["scheduled_reverse_mw"] = frame["scheduled_reverse_mw"].fillna(0.0)
    frame["net_flow_mw"] = (
        frame["scheduled_forward_mw"] - frame["scheduled_reverse_mw"]
    )

    forward_capacity = capacity[
        (capacity["out_domain"] == source) & (capacity["in_domain"] == sink)
    ]
    if forward_capacity.empty:
        raise ValueError(f"No capacity published from {source} to {sink}.")

    frame["capacity_mw"] = align_capacity_to_schedule(
        forward_capacity.rename(columns={"quantity_mw": "capacity_mw"}),
        frame.index,
    )

    # Utilisation only means anything for flow in the direction we have the
    # capacity for. Flow the other way is headroom, not saturation.
    frame["utilisation"] = (
        frame["net_flow_mw"].clip(lower=0) / frame["capacity_mw"]
    ).where(frame["capacity_mw"] > 0)

    frame["is_saturated"] = frame["utilisation"] >= SATURATION_THRESHOLD
    return frame.reset_index()


def attach_to_intervals(flagged: pd.DataFrame, border: pd.DataFrame) -> pd.DataFrame:
    """Join the price decoupling flags with the border state, interval by interval."""
    return flagged.merge(border, on="ts_utc", how="left")


def saturation_evidence(joined: pd.DataFrame) -> dict:
    """Does saturation actually explain the splits, or is it a coincidence?

    Returns the contingency between decoupling and a full border, which is the
    number to look at before claiming a causal story in a presentation.
    """
    usable = joined.dropna(subset=["utilisation"])
    if usable.empty:
        return {"intervals": 0}

    split = usable["is_decoupled"]
    full = usable["is_saturated"]

    both = int((split & full).sum())
    split_only = int((split & ~full).sum())
    full_only = int((~split & full).sum())
    neither = int((~split & ~full).sum())

    explained = both / split.sum() if split.sum() else 0.0
    precision = both / full.sum() if full.sum() else 0.0

    return {
        "intervals": int(len(usable)),
        "decoupled": int(split.sum()),
        "saturated": int(full.sum()),
        "decoupled_and_saturated": both,
        "decoupled_not_saturated": split_only,
        "saturated_not_decoupled": full_only,
        "neither": neither,
        "share_of_splits_explained": explained,
        "share_of_saturation_that_split": precision,
        "mean_utilisation_when_split": float(
            usable.loc[split, "utilisation"].mean()
        )
        if split.any()
        else None,
        "mean_utilisation_when_coupled": float(
            usable.loc[~split, "utilisation"].mean()
        )
        if (~split).any()
        else None,
    }
