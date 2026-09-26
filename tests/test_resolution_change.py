"""A year that spans the change from hourly to quarter hourly settlement.

Iberia moved the day-ahead auction from PT60M to PT15M partway through the
history this platform holds. The first time a year of prices was loaded, the
pipeline stopped with "Zones are on different settlement resolutions", which
was the guard testing the wrong invariant: it asked whether the whole frame had
one resolution, when what has to be true is that the two zones agree at each
instant.

Relaxing the guard alone would have been worse than leaving it. The step used
to group episodes is inferred from the data, and the mode of a mixed year is
whichever resolution has more rows. At a 15 minute step every hourly decoupled
interval becomes an episode of one, so the count explodes and every duration is
a quarter of the truth. Nothing raises. The table is just wrong, and it is the
table a journalist quotes.

So these tests are about the seam, not the two halves.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.market_splitting import (  # noqa: E402
    build_spread_series,
    detect_episodes,
    flag_decoupling,
    mixed_resolutions,
    resolution_segments,
    step_for,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.pipeline.gold import (  # noqa: E402
    gold_daily_profile,
    gold_interval_premium,
    gold_split_episodes,
)

#: The changeover instant used throughout. The real date is whatever the data
#: says; what these tests pin down is the behaviour at the seam.
CHANGEOVER = pd.Timestamp("2026-01-01 00:00", tz="UTC")


def rows(start, count, step, resolution, pt, es):
    """Price rows for both zones over `count` intervals."""
    out = []
    for index in range(count):
        ts = start + index * step
        pt_price = pt[index] if isinstance(pt, (list, tuple)) else pt
        es_price = es[index] if isinstance(es, (list, tuple)) else es
        out.append({"zone_eic": EIC_PORTUGAL, "ts_utc": ts,
                    "price_eur_mwh": pt_price, "resolution": resolution})
        out.append({"zone_eic": EIC_SPAIN, "ts_utc": ts,
                    "price_eur_mwh": es_price, "resolution": resolution})
    return out


HOUR = pd.Timedelta(hours=1)
QUARTER = pd.Timedelta(minutes=15)


def mixed_frame() -> pd.DataFrame:
    """Four coupled hours, then four coupled quarters, with a split in each.

    The hourly half has three consecutive decoupled hours. The quarter hourly
    half has three consecutive decoupled quarters. Same shape, different
    duration, which is what the assertions turn on.
    """
    hourly = rows(
        CHANGEOVER - 4 * HOUR, 4, HOUR, "PT60M",
        pt=[100.0, 140.0, 145.0, 150.0], es=[100.0, 100.0, 100.0, 100.0],
    )
    quarter = rows(
        CHANGEOVER, 4, QUARTER, "PT15M",
        pt=[100.0, 140.0, 145.0, 150.0], es=[100.0, 100.0, 100.0, 100.0],
    )
    return pd.DataFrame(hourly + quarter)


# --- the guard -----------------------------------------------------------------


def test_a_year_spanning_the_change_is_accepted():
    """The failure that stopped the pipeline. Both resolutions in one frame is
    a real market history, not a fault."""
    spread = build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN)
    assert len(spread) == 8


def test_the_two_zones_disagreeing_at_one_instant_still_raises():
    """This is the thing that was always wrong and must stay refused. One
    coupled market cannot clear the same instant on two grids, so this is a
    parsing or deduplication fault rather than something to resample away."""
    frame = pd.DataFrame([
        {"zone_eic": EIC_PORTUGAL, "ts_utc": CHANGEOVER,
         "price_eur_mwh": 100.0, "resolution": "PT60M"},
        {"zone_eic": EIC_SPAIN, "ts_utc": CHANGEOVER,
         "price_eur_mwh": 100.0, "resolution": "PT15M"},
    ])

    with pytest.raises(ValueError) as caught:
        build_spread_series(frame, EIC_PORTUGAL, EIC_SPAIN)

    message = str(caught.value)
    assert "different settlement resolutions" in message
    assert "deduplication" in message, "should point at the real cause"


def test_the_error_names_the_timestamps_rather_than_the_whole_frame():
    frame = pd.DataFrame([
        {"zone_eic": EIC_PORTUGAL, "ts_utc": CHANGEOVER,
         "price_eur_mwh": 100.0, "resolution": "PT60M"},
        {"zone_eic": EIC_SPAIN, "ts_utc": CHANGEOVER,
         "price_eur_mwh": 100.0, "resolution": "PT15M"},
    ])
    with pytest.raises(ValueError) as caught:
        build_spread_series(frame, EIC_PORTUGAL, EIC_SPAIN)
    assert "2026-01-01" in str(caught.value)


def test_the_resolution_travels_with_the_spread():
    """Everything downstream needs to know how long a row covers, and
    inferring it from the gaps is what goes wrong across the change."""
    spread = build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN)
    assert list(spread["resolution"]) == ["PT60M"] * 4 + ["PT15M"] * 4


def test_a_frame_with_no_resolution_column_still_works():
    """Several scripts build a spread from rows that never carried one."""
    frame = mixed_frame().drop(columns=["resolution"])
    frame = frame[frame["ts_utc"] >= CHANGEOVER]
    spread = build_spread_series(frame, EIC_PORTUGAL, EIC_SPAIN)
    assert "resolution" not in spread.columns
    assert len(spread) == 4


# --- the step ------------------------------------------------------------------


def test_the_step_comes_from_the_code_not_the_gaps():
    assert step_for("PT60M") == HOUR
    assert step_for("PT15M") == QUARTER
    assert step_for("PT1H") == HOUR


def test_an_unrecognised_resolution_raises_rather_than_defaulting():
    """A wrong step does not fail anywhere. It reports every episode in that
    segment at the wrong duration, quietly."""
    with pytest.raises(ValueError) as caught:
        step_for("PT5M")
    assert "PT5M" in str(caught.value)


def test_a_missing_resolution_falls_back_rather_than_raising():
    assert step_for(None) is None
    assert step_for("") is None
    assert step_for(float("nan")) is None


# --- segmentation --------------------------------------------------------------


def test_segments_are_contiguous_runs_oldest_first():
    spread = build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN)
    found = [(resolution, len(part)) for resolution, part in resolution_segments(spread)]
    assert found == [("PT60M", 4), ("PT15M", 4)]


def test_a_single_resolution_is_one_segment():
    spread = build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN)
    hourly = spread[spread["resolution"] == "PT60M"]
    assert [resolution for resolution, _ in resolution_segments(hourly)] == ["PT60M"]
    assert mixed_resolutions(hourly) is False


# --- episodes, which is where the damage would have been -----------------------


def test_each_half_becomes_one_episode_rather_than_one_per_interval():
    """The bug a relaxed guard alone would have introduced.

    Three consecutive decoupled hours grouped at a 15 minute step are three
    episodes of one interval each, because every gap is four times the step.
    """
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    episodes = detect_episodes(flagged)

    assert len(episodes) == 2, "one episode per half, not one per interval"
    assert list(episodes["intervals"]) == [3, 3]


def test_durations_are_real_hours_on_both_sides_of_the_change():
    """Three hours is three hours and three quarters is forty five minutes.
    A single step across the whole series makes one of them wrong."""
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    episodes = detect_episodes(flagged).sort_values("start_utc")

    assert list(episodes["duration_hours"]) == [3.0, 0.75]


def test_each_episode_says_which_grid_it_was_measured_on():
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    episodes = detect_episodes(flagged).sort_values("start_utc")
    assert list(episodes["settlement_resolution"]) == ["PT60M", "PT15M"]


def test_episode_ids_stay_unique_across_the_seam():
    """Each segment numbers from one on its own, so they are renumbered."""
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    episodes = detect_episodes(flagged)
    assert episodes["episode_id"].is_unique


def test_the_end_of_an_hourly_episode_is_one_hour_past_its_last_interval():
    """Not one quarter, which is what a mixed step would give."""
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    hourly = detect_episodes(flagged).sort_values("start_utc").iloc[0]
    assert hourly["end_utc"] - hourly["start_utc"] == 3 * HOUR


def test_an_explicit_step_turns_the_segmenting_off():
    """A caller that passes a step has said what it wants, and this must not
    quietly do something else."""
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    episodes = detect_episodes(flagged, step=QUARTER)
    assert len(episodes) > 2, "at a forced quarter step the hourly half fragments"


def test_a_single_resolution_series_is_unchanged_by_any_of_this():
    """The regression guard. Most of the history is one resolution and its
    answers must not move."""
    quarter_only = mixed_frame()
    quarter_only = quarter_only[quarter_only["ts_utc"] >= CHANGEOVER]
    flagged = flag_decoupling(build_spread_series(quarter_only, EIC_PORTUGAL, EIC_SPAIN))

    episodes = detect_episodes(flagged)
    assert len(episodes) == 1
    assert episodes.iloc[0]["duration_hours"] == 0.75
    assert episodes.iloc[0]["intervals"] == 3


# --- the gold tables -----------------------------------------------------------


def test_the_interval_table_says_how_long_each_row_covers():
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    intervals = gold_interval_premium(flagged)

    assert list(intervals["settlement_resolution"]) == ["PT60M"] * 4 + ["PT15M"] * 4
    assert list(intervals["interval_hours"]) == [1.0] * 4 + [0.25] * 4


def test_the_daily_profile_weights_by_time_rather_than_by_row_count():
    """Otherwise an hour of the quarter hourly period counts four times as
    much as an hour of the hourly one, and the manufacturer acts on a number
    that drifts towards whichever period has more rows.

    Hour 20 here holds one decoupled hourly interval, a full hour. Hour 23
    holds three decoupled quarters and one coupled quarter, forty five minutes
    of a whole hour. By rows, hour 23 reads 75% and hour 20 reads 100%. By
    time they read the same, because they are the same.
    """
    hourly = rows(
        pd.Timestamp("2025-12-31 20:00", tz="UTC"), 1, HOUR, "PT60M",
        pt=[150.0], es=[100.0],
    )
    quarter = rows(
        pd.Timestamp("2025-12-31 23:00", tz="UTC"), 4, QUARTER, "PT15M",
        pt=[150.0, 150.0, 150.0, 100.0], es=[100.0, 100.0, 100.0, 100.0],
    )
    flagged = flag_decoupling(
        build_spread_series(pd.DataFrame(hourly + quarter), EIC_PORTUGAL, EIC_SPAIN)
    )
    profile = gold_daily_profile(gold_interval_premium(flagged)).set_index(
        "hour_of_day_utc"
    )

    assert profile.loc[20, "hours"] == 1.0
    assert profile.loc[23, "hours"] == 1.0
    assert profile.loc[20, "split_probability"] == 1.0
    assert profile.loc[23, "split_probability"] == 0.75

    # The row count would have said 1.0 and 0.75 as well here, but on one
    # interval against four. The point is that `hours` is now the denominator.
    assert profile.loc[20, "intervals"] == 1
    assert profile.loc[23, "intervals"] == 4


def test_the_profile_still_works_without_an_interval_length():
    """Scripts that build intervals from rows with no resolution."""
    quarter_only = mixed_frame()
    quarter_only = quarter_only[quarter_only["ts_utc"] >= CHANGEOVER]
    flagged = flag_decoupling(
        build_spread_series(quarter_only.drop(columns=["resolution"]),
                            EIC_PORTUGAL, EIC_SPAIN)
    )
    profile = gold_daily_profile(gold_interval_premium(flagged))
    assert not profile.empty
    assert profile["split_probability"].notna().all()


def test_the_episode_cost_uses_the_step_of_its_own_segment():
    """The cost scales with the interval length, and it is the figure
    validated to 0.0015% against REE's published congestion rent. An hourly
    interval costed as a quarter hour understates the money fourfold.

    Same premium and same flow on both sides of the change, so the hourly
    episode must cost exactly four times the quarter hourly one.
    """
    frame = mixed_frame()
    spread = build_spread_series(frame, EIC_PORTUGAL, EIC_SPAIN)
    flagged = flag_decoupling(spread)

    border = pd.DataFrame({
        "ts_utc": flagged["ts_utc"],
        "net_flow_mw": 1000.0,
        "capacity_mw": 1000.0,
        "utilisation": 1.0,
        "is_saturated": True,
    })

    episodes = gold_split_episodes(flagged, border).sort_values("start_utc")
    hourly, quarterly = episodes.iloc[0], episodes.iloc[1]

    assert hourly["extra_cost_eur"] == pytest.approx(4 * quarterly["extra_cost_eur"])
    # 3 intervals x 1000 MW x 1 h, at premiums of 40, 45 and 50 euro.
    assert hourly["extra_cost_eur"] == pytest.approx(135_000.0)


def test_episode_ids_in_gold_stay_unique_across_the_seam():
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    episodes = gold_split_episodes(flagged)
    assert episodes["episode_id"].is_unique
    assert len(episodes) == 2


def test_the_market_day_survives_the_segmenting():
    """Every gold writer uses it for replaceWhere, and an episode without one
    turns a partial rewrite into a full overwrite."""
    flagged = flag_decoupling(build_spread_series(mixed_frame(), EIC_PORTUGAL, EIC_SPAIN))
    episodes = gold_split_episodes(flagged)
    assert episodes["market_day"].notna().all()