"""The two checks against outside publishers, as functions rather than scripts.

What is worth testing here is not that a merge works. It is that a real
disagreement is reported as one, that the OMIE column orientation is decided by
evidence rather than assumed, and that a day one side missed is visible instead
of quietly dropped.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.validation import (  # noqa: E402
    cost_validation,
    orient_omie_columns,
    price_source_agreement,
)

TS = pd.date_range("2026-08-18T10:00Z", periods=4, freq="15min")


def spread(pt: list[float], es: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": TS,
            "price_pt": pt,
            "price_es": es,
            "spread_eur_mwh": [a - b for a, b in zip(pt, es)],
        }
    )


def omie(first: list[float], second: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": TS[: len(first)],
            "price_first_eur_mwh": first,
            "price_second_eur_mwh": second,
        }
    )


# --- prices, against OMIE ---------------------------------------------------


def test_two_publishers_that_agree_are_reported_as_agreeing():
    result = price_source_agreement(
        spread([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]),
        omie([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]),
    )

    assert len(result) == 4
    assert result["agrees"].all()
    assert result["pt_difference"].max() == 0.0


def test_a_real_disagreement_is_not_smoothed_over():
    result = price_source_agreement(
        spread([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]),
        omie([100.0, 101.0, 95.0, 103.0], [40.0, 41.0, 42.0, 43.0]),
    )

    assert not result["agrees"].all()
    assert int((~result["agrees"]).sum()) == 1
    assert result.loc[~result["agrees"], "pt_difference"].iloc[0] == 7.0


def test_rounding_at_the_last_cent_is_not_a_disagreement():
    result = price_source_agreement(
        spread([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]),
        omie([100.01, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]),
    )
    assert result["agrees"].all()


def test_the_omie_columns_are_oriented_by_evidence():
    """The file names neither column, and on a coupled day it cannot be known."""
    merged = spread([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]).merge(
        omie([40.0, 41.0, 42.0, 43.0], [100.0, 101.0, 102.0, 103.0]), on="ts_utc"
    )
    assert orient_omie_columns(merged) == (
        "price_second_eur_mwh",
        "price_first_eur_mwh",
    )


def test_a_swapped_file_still_compares_correctly():
    result = price_source_agreement(
        spread([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]),
        omie([40.0, 41.0, 42.0, 43.0], [100.0, 101.0, 102.0, 103.0]),
    )
    assert result["agrees"].all()


def test_intervals_only_one_source_published_are_not_compared():
    """A coverage gap is not a disagreement, and counting it as one would lie."""
    short = omie([100.0, 101.0], [40.0, 41.0])
    result = price_source_agreement(
        spread([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]), short
    )
    assert len(result) == 2


def test_an_empty_side_produces_an_empty_comparison_not_an_error():
    empty = omie([], [])
    assert price_source_agreement(
        spread([100.0, 101.0, 102.0, 103.0], [40.0, 41.0, 42.0, 43.0]), empty
    ).empty


# --- cost, against REE ------------------------------------------------------


def episodes(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market_day": day,
                "start_utc": pd.Timestamp("2026-08-18T10:00Z"),
                "extra_cost_eur": cost,
            }
            for day, cost in rows
        ]
    )


def rent(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{"market_day": day, "congestion_rent_eur": value} for day, value in rows]
    )


def test_agreement_shows_as_a_difference_of_nothing():
    result = cost_validation(
        episodes((date(2026, 8, 18), 1_000_000.0)),
        rent((date(2026, 8, 18), 1_000_000.0)),
    )

    assert len(result) == 1
    assert result.iloc[0]["difference_eur"] == 0.0
    assert result.iloc[0]["difference_pct"] == 0.0


def test_several_episodes_in_one_day_are_summed_before_comparing():
    result = cost_validation(
        episodes((date(2026, 8, 18), 400_000.0), (date(2026, 8, 18), 600_000.0)),
        rent((date(2026, 8, 18), 1_000_000.0)),
    )

    assert result.iloc[0]["episodes"] == 2
    assert result.iloc[0]["our_cost_eur"] == 1_000_000.0
    assert result.iloc[0]["difference_eur"] == 0.0


def test_a_day_ree_recorded_and_this_project_missed_is_visible():
    """The interesting row, and the one a silent inner join would delete."""
    result = cost_validation(
        episodes((date(2026, 8, 18), 1_000_000.0)),
        rent((date(2026, 8, 18), 1_000_000.0), (date(2026, 8, 22), 40.0)),
    )

    missed = result[result["market_day"] == date(2026, 8, 22)].iloc[0]
    assert missed["episodes"] == 0
    assert missed["our_cost_eur"] == 0.0
    assert missed["difference_eur"] == -40.0


def test_a_percentage_of_nothing_is_undefined_rather_than_zero():
    result = cost_validation(
        episodes((date(2026, 8, 18), 500.0)), rent((date(2026, 8, 22), 40.0))
    )

    ours_only = result[result["market_day"] == date(2026, 8, 18)].iloc[0]
    assert ours_only["congestion_rent_eur"] == 0.0
    assert pd.isna(ours_only["difference_pct"])


def test_the_residual_this_project_actually_has_is_reported_not_hidden():
    """0.0015% across sixty days, which is the threshold and not an error."""
    result = cost_validation(
        episodes((date(2026, 8, 18), 11_302_847.0)),
        rent((date(2026, 8, 18), 11_303_022.0)),
    )
    row = result.iloc[0]

    assert round(row["difference_eur"], 0) == -175.0
    assert abs(row["difference_pct"]) < 0.01


def test_both_sides_empty_gives_an_empty_frame_with_its_columns():
    result = cost_validation(pd.DataFrame(), pd.DataFrame())
    assert result.empty
    assert "congestion_rent_eur" in result.columns