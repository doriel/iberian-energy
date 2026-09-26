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
    "our_rent_eur",
    "congestion_rent_eur",
    "difference_eur",
    "difference_pct",
    "our_import_cost_eur",
]


def cost_validation(
    episodes: pd.DataFrame, rent_by_day: pd.DataFrame
) -> pd.DataFrame:
    """This project's congestion rent against REE's published congestion rent.

    In implicit market coupling the allocated capacity is the scheduled
    exchange, so the two quantities are the same thing computed from opposite
    ends of the same clearing. That makes this a check on the implementation
    rather than an independent measurement, and it is worth saying so out loud
    rather than presenting the agreement as corroboration it is not.

    ## What this used to compare, and why it was wrong

    It compared `extra_cost_eur`, the premium Portugal paid on the energy it
    imported, against REE's congestion rent. Those are different quantities:
    the first counts one direction and the second counts both. They agree only
    while every decoupled interval runs the same way.

    For seventy days of summer that held, and the agreement read 0.0015 per
    cent, which looked like a strong validation and was not one. Adding a year
    of history broke it: over twelve months the figure read -21 per cent, and
    the deviation by month tracked the number of decoupled intervals in which
    Portugal exported. February 2026 averaged 195 MW flowing into Spain and
    came out at -87 per cent; the five months with no exporting intervals at
    all agreed to the cent.

    So the comparison is now rent against rent, which is like for like and
    holds in both directions. `our_import_cost_eur` is carried alongside,
    because it is still the number the journalist wants, but it is not what
    this check is checking.

    Days where either side published nothing are kept with a zero, because a
    day this project found no episode on and REE recorded rent for is exactly
    the kind of gap worth seeing. A day that HAS episodes whose rent could not
    be computed is left null instead, because a missing figure and a genuine
    zero are different and filling both with zero hides the first.
    """
    if episodes.empty and rent_by_day.empty:
        return pd.DataFrame(columns=COST_COLUMNS)

    ours = (
        episodes.groupby("market_day")
        .agg(
            episodes=("start_utc", "count"),
            our_rent_eur=("congestion_rent_eur", "sum"),
            our_import_cost_eur=("extra_cost_eur", "sum"),
            rent_known=("congestion_rent_eur", "count"),
        )
        .reset_index()
        if not episodes.empty
        else pd.DataFrame(
            columns=[
                "market_day", "episodes", "our_rent_eur",
                "our_import_cost_eur", "rent_known",
            ]
        )
    )
    theirs = (
        rent_by_day[["market_day", "congestion_rent_eur"]]
        if not rent_by_day.empty
        else pd.DataFrame(columns=["market_day", "congestion_rent_eur"])
    )

    merged = ours.merge(theirs, on="market_day", how="outer")
    merged["episodes"] = merged["episodes"].fillna(0).astype(int)
    merged["rent_known"] = merged["rent_known"].fillna(0).astype(int)
    merged["congestion_rent_eur"] = merged["congestion_rent_eur"].fillna(0.0)

    # Zero only where zero is the truth. A day with no episodes really did cost
    # nothing. A day whose episodes exist but whose rent could not be computed,
    # because the border series had a hole, stays null: a missing figure and a
    # genuine zero are different things, and an earlier version filled both
    # with zero, which made a gap in the data look like agreement.
    incomplete = merged["episodes"] > merged["rent_known"]
    for column in ("our_rent_eur", "our_import_cost_eur"):
        merged[column] = [
            None if missing else (0.0 if value != value else float(value))
            for missing, value in zip(incomplete, merged[column])
        ]

    merged["difference_eur"] = [
        None if ours is None else ours - rent
        for ours, rent in zip(merged["our_rent_eur"], merged["congestion_rent_eur"])
    ]
    # A percentage of nothing is not zero, it is undefined, and reporting it as
    # zero would hide a day where this project found a rent REE did not.
    merged["difference_pct"] = [
        None if (ours is None or rent == 0) else ((ours - rent) / rent) * 100.0
        for ours, rent in zip(merged["our_rent_eur"], merged["congestion_rent_eur"])
    ]

    return merged[COST_COLUMNS].sort_values("market_day").reset_index(drop=True)