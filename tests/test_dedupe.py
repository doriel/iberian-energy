"""Republished documents, and which version wins.

This only appears when reading a landing zone rather than a request: overlapping
backfills leave the same market day in two files. The rule being tested is that
the later publication supersedes the earlier one, which is how the transparency
platform itself treats a correction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.market_splitting import build_spread_series  # noqa: E402
from iberian.pipeline.dedupe import (  # noqa: E402
    PRICE_KEYS,
    QUANTITY_KEYS,
    conflicts,
    latest_per_key,
)

PT = "10YPT-REN------W"
ES = "10YES-REE------0"
T0 = pd.Timestamp("2026-08-18T10:00Z")


def prices(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "zone_eic": zone,
                "ts_utc": T0,
                "price_eur_mwh": price,
                "market": "day_ahead",
                "resolution": "PT15M",
                "landed_at": pd.Timestamp(landed),
            }
            for zone, price, landed in rows
        ]
    )


def test_the_later_publication_wins():
    frame = prices(
        (PT, 100.0, "2026-08-18T12:00Z"),
        (PT, 110.0, "2026-08-20T12:00Z"),  # the correction
    )
    kept = latest_per_key(frame, PRICE_KEYS)

    assert len(kept) == 1
    assert kept.iloc[0]["price_eur_mwh"] == 110.0


def test_a_row_with_a_publication_time_beats_one_without():
    frame = prices((PT, 100.0, "2026-08-18T12:00Z"))
    frame = pd.concat([frame, prices((PT, 90.0, "2026-08-19T12:00Z"))])
    frame.iloc[0, frame.columns.get_loc("landed_at")] = pd.NaT

    kept = latest_per_key(frame, PRICE_KEYS)
    assert kept.iloc[0]["price_eur_mwh"] == 90.0


def test_the_two_zones_are_not_collapsed_into_one():
    frame = prices((PT, 100.0, "2026-08-18T12:00Z"), (ES, 40.0, "2026-08-18T12:00Z"))
    assert len(latest_per_key(frame, PRICE_KEYS)) == 2


def test_intraday_is_not_collapsed_onto_day_ahead():
    """Different markets on one timestamp are different facts, not duplicates."""
    frame = prices((PT, 100.0, "2026-08-18T12:00Z"))
    intraday = prices((PT, 105.0, "2026-08-18T18:00Z"))
    intraday["market"] = "intraday"

    assert len(latest_per_key(pd.concat([frame, intraday]), PRICE_KEYS)) == 2


def test_deduplicating_lets_the_spread_be_computed_at_all():
    """The failure this exists for: the pipeline reads every landed file."""
    frame = pd.concat(
        [
            prices((PT, 100.0, "2026-08-18T12:00Z"), (ES, 40.0, "2026-08-18T12:00Z")),
            prices((PT, 110.0, "2026-08-20T12:00Z"), (ES, 45.0, "2026-08-20T12:00Z")),
        ]
    )

    try:
        build_spread_series(frame, PT, ES)
    except ValueError as error:
        assert "duplicated" in str(error)
    else:  # pragma: no cover - the guard is what makes the fix necessary
        raise AssertionError("expected the duplicate guard to fire")

    spread = build_spread_series(latest_per_key(frame, PRICE_KEYS), PT, ES)
    assert len(spread) == 1
    assert spread.iloc[0]["spread_eur_mwh"] == 65.0


def test_quantities_deduplicate_on_direction_as_well_as_time():
    rows = pd.DataFrame(
        [
            {"ts_utc": T0, "quantity_mw": 3000.0, "in_domain": PT, "out_domain": ES,
             "series_kind": "scheduled_exchange", "landed_at": pd.Timestamp("2026-08-18T12:00Z")},
            {"ts_utc": T0, "quantity_mw": 3200.0, "in_domain": PT, "out_domain": ES,
             "series_kind": "scheduled_exchange", "landed_at": pd.Timestamp("2026-08-20T12:00Z")},
            {"ts_utc": T0, "quantity_mw": 500.0, "in_domain": ES, "out_domain": PT,
             "series_kind": "scheduled_exchange", "landed_at": pd.Timestamp("2026-08-18T12:00Z")},
        ]
    )
    kept = latest_per_key(rows, QUANTITY_KEYS)

    assert len(kept) == 2
    pt_import = kept[kept["in_domain"] == PT].iloc[0]
    assert pt_import["quantity_mw"] == 3200.0


def test_a_republication_that_changed_the_number_is_reportable():
    frame = prices(
        (PT, 100.0, "2026-08-18T12:00Z"),
        (PT, 110.0, "2026-08-20T12:00Z"),
    )
    assert len(conflicts(frame, PRICE_KEYS, "price_eur_mwh")) == 1


def test_a_republication_that_changed_nothing_is_not_a_conflict():
    frame = prices(
        (PT, 100.0, "2026-08-18T12:00Z"),
        (PT, 100.0, "2026-08-20T12:00Z"),
    )
    assert conflicts(frame, PRICE_KEYS, "price_eur_mwh").empty


def test_an_empty_frame_survives_both_helpers():
    empty = prices().iloc[0:0]
    assert latest_per_key(empty, PRICE_KEYS).empty
    assert conflicts(empty, PRICE_KEYS, "price_eur_mwh").empty