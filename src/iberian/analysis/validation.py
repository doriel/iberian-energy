"""Checks against publishers that share no code with this project.

Both live here rather than inside a script because a validation that can only
be run by a human typing a command is not part of the platform. As tables they
are queryable, they are recomputed on every pipeline run, and a regression
shows up as a number moving rather than as nobody noticing.

Neither of these is a claim that the figures are right in some absolute sense.
They are checks on the implementation, and demanding ones: the market day
boundary in local CET, the quarter hourly grid, the forward fill of sparse
Points, the direction of flow and the sign of the spread all have to be correct
for the numbers to line up.
"""

from __future__ import annotations

import pandas as pd

#: One cent per MWh. Below this the two publishers are rounding differently,
#: not disagreeing.
TOLERANCE = 0.01


def orient_omie_columns(merged: pd.DataFrame) -> tuple[str, str]:
    """Decide which OMIE column carries Portugal, by fit rather than by faith.

    The file publishes two price columns without naming the zones. On a coupled
    day both are identical, so the question cannot be settled; on a decoupled
    day the assignment is obvious. Fitting both and taking the smaller error
    gets it right whenever the answer is knowable and is stable when it is not.
    """
    straight = (
        (merged["price_pt"] - merged["price_first_eur_mwh"]).abs().mean()
        + (merged["price_es"] - merged["price_second_eur_mwh"]).abs().mean()
    ) / 2
    swapped = (
        (merged["price_pt"] - merged["price_second_eur_mwh"]).abs().mean()
        + (merged["price_es"] - merged["price_first_eur_mwh"]).abs().mean()
    ) / 2

    if straight <= swapped:
        return "price_first_eur_mwh", "price_second_eur_mwh"
    return "price_second_eur_mwh", "price_first_eur_mwh"


AGREEMENT_COLUMNS = [
    "ts_utc",
    "market_day",
    "price_pt_entsoe",
    "price_pt_omie",
    "price_es_entsoe",
    "price_es_omie",
    "pt_difference",
    "es_difference",
    "agrees",
]


def price_source_agreement(
    spread: pd.DataFrame, omie: pd.DataFrame, tolerance: float = TOLERANCE
) -> pd.DataFrame:
    """Interval by interval, do ENTSO-E and OMIE publish the same price?

    `spread` is the output of `build_spread_series`, `omie` the tidy OMIE rows.
    Only intervals present in both are compared, because an interval one of
    them did not publish is a coverage gap rather than a disagreement.
    """
    if spread.empty or omie.empty:
        return pd.DataFrame(columns=AGREEMENT_COLUMNS)

    merged = spread.merge(
        omie[["ts_utc", "price_first_eur_mwh", "price_second_eur_mwh"]],
        on="ts_utc",
        how="inner",
    )
    if merged.empty:
        return pd.DataFrame(columns=AGREEMENT_COLUMNS)

    pt_column, es_column = orient_omie_columns(merged)

    from iberian.market_time import to_market_day

    out = pd.DataFrame(
        {
            "ts_utc": merged["ts_utc"],
            "market_day": merged["ts_utc"].apply(to_market_day),
            "price_pt_entsoe": merged["price_pt"],
            "price_pt_omie": merged[pt_column],
            "price_es_entsoe": merged["price_es"],
            "price_es_omie": merged[es_column],
        }
    )
    # Rounded before comparing, because 100.01 minus 100.00 in binary floating
    # point is 0.010000000000005, which is larger than a tolerance of 0.01. Both
    # publishers quote to the cent, so anything beyond six decimals is an
    # artefact of the subtraction rather than a difference either of them
    # published.
    out["pt_difference"] = (out["price_pt_entsoe"] - out["price_pt_omie"]).abs().round(6)
    out["es_difference"] = (out["price_es_entsoe"] - out["price_es_omie"]).abs().round(6)
    out["agrees"] = (out["pt_difference"] <= tolerance) & (
        out["es_difference"] <= tolerance
    )
    return out[AGREEMENT_COLUMNS].sort_values("ts_utc").reset_index(drop=True)


COST_COLUMNS = [
    "market_day",
    "episodes",
    "our_cost_eur",
    "congestion_rent_eur",
    "difference_eur",
    "difference_pct",
]


def cost_validation(
    episodes: pd.DataFrame, rent_by_day: pd.DataFrame
) -> pd.DataFrame:
    """This project's extra import cost against REE's published congestion rent.

    In implicit market coupling the allocated capacity is the scheduled
    exchange, so the two quantities are the same thing computed from opposite
    ends of the same clearing. That makes this a check on the implementation
    rather than an independent measurement, and it is worth saying so out loud
    rather than presenting the agreement as corroboration it is not.

    Days where either side published nothing are kept with a zero, because a
    day this project found no episode on and REE recorded rent for is exactly
    the kind of gap worth seeing.
    """
    if episodes.empty and rent_by_day.empty:
        return pd.DataFrame(columns=COST_COLUMNS)

    ours = (
        episodes.groupby("market_day")
        .agg(episodes=("start_utc", "count"), our_cost_eur=("extra_cost_eur", "sum"))
        .reset_index()
        if not episodes.empty
        else pd.DataFrame(columns=["market_day", "episodes", "our_cost_eur"])
    )
    theirs = (
        rent_by_day[["market_day", "congestion_rent_eur"]]
        if not rent_by_day.empty
        else pd.DataFrame(columns=["market_day", "congestion_rent_eur"])
    )

    merged = ours.merge(theirs, on="market_day", how="outer")
    merged["episodes"] = merged["episodes"].fillna(0).astype(int)
    merged["our_cost_eur"] = merged["our_cost_eur"].fillna(0.0)
    merged["congestion_rent_eur"] = merged["congestion_rent_eur"].fillna(0.0)

    merged["difference_eur"] = merged["our_cost_eur"] - merged["congestion_rent_eur"]
    # A percentage of nothing is not zero, it is undefined, and reporting it as
    # zero would hide a day where this project found a cost REE did not.
    merged["difference_pct"] = [
        None if rent == 0 else (difference / rent) * 100.0
        for difference, rent in zip(
            merged["difference_eur"], merged["congestion_rent_eur"]
        )
    ]

    return merged[COST_COLUMNS].sort_values("market_day").reset_index(drop=True)