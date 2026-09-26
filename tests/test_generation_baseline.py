"""The baseline, which is the number the outage claim rests on.

Two and a half million rows go through these four functions and the gold table
is what the agent reads before it says a plant being out explains a price. A
baseline that is quietly wrong does not fail anywhere: it produces a confident
explanation of something that did not happen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.generation_baseline import (  # noqa: E402
    assess,
    deviation,
    deviation_percent,
    looks_offline,
    median,
)


# --- the median ----------------------------------------------------------------


def test_the_middle_of_an_odd_series():
    assert median([10.0, 30.0, 20.0]) == 20.0


def test_the_mean_of_the_middle_two_of_an_even_series():
    assert median([10.0, 20.0, 30.0, 40.0]) == 25.0


def test_the_order_it_arrives_in_does_not_matter():
    assert median([5, 1, 4, 2, 3]) == median([1, 2, 3, 4, 5])


def test_a_median_survives_the_outages_a_mean_would_not():
    """The reason this is a median.

    Thirty days of a 300 MW plant, five of them spent out. The mean reads 250
    and today's 0 looks like an ordinary bad day. The median still reads 300.
    """
    history = [0.0] * 5 + [300.0] * 25
    assert median(history) == 300.0
    assert sum(history) / len(history) == 250.0


def test_no_history_is_unknown_rather_than_zero():
    """Zero would read as "this unit normally produces nothing"."""
    assert median([]) is None
    assert median(None) is None


def test_nulls_inside_the_history_are_dropped_not_counted_as_zero():
    """Spark puts a null in an array as None, and counting it as zero would
    drag every baseline towards nothing."""
    assert median([100.0, None, 300.0]) == 200.0


def test_values_that_are_not_numbers_are_dropped():
    assert median([100.0, "no", 300.0]) == 200.0
    assert median([float("nan"), 100.0, 300.0]) == 200.0


def test_a_single_observation_is_its_own_median():
    assert median([42.0]) == 42.0


# --- the deviation -------------------------------------------------------------


def test_below_the_baseline_is_negative():
    assert deviation(20.0, 300.0) == -280.0


def test_above_the_baseline_is_positive():
    assert deviation(320.0, 300.0) == 20.0


def test_a_missing_baseline_gives_no_deviation():
    assert deviation(20.0, None) is None
    assert deviation(None, 300.0) is None


def test_the_percentage_is_relative_to_the_baseline():
    assert deviation_percent(150.0, 300.0) == -50.0


def test_a_near_zero_baseline_refuses_a_percentage():
    """A unit that normally makes 0.2 MW and makes 2 MW today is up 900 per
    cent and nothing happened. Somebody sorts by that column descending."""
    assert deviation_percent(2.0, 0.2) is None
    assert deviation_percent(2.0, 0.0) is None


def test_a_baseline_just_above_the_floor_still_answers():
    assert deviation_percent(2.0, 1.0) == 100.0


def test_the_percentage_floor_is_a_parameter():
    assert deviation_percent(2.0, 5.0, minimum_baseline_mw=10.0) is None
    assert deviation_percent(2.0, 5.0, minimum_baseline_mw=1.0) == -60.0


# --- the offline signal --------------------------------------------------------


def test_nothing_produced_where_something_usually_is():
    assert looks_offline(0.0, 300.0) is True


def test_station_service_still_reads_as_offline():
    """A stopped plant draws its own supply and reports a small positive
    number. A strict zero would miss most real stoppages."""
    assert looks_offline(0.4, 300.0) is True


def test_a_unit_that_is_merely_low_is_not_offline():
    assert looks_offline(50.0, 300.0) is False


def test_a_small_unit_sitting_idle_is_not_an_outage():
    """Without this, every peaker that did not run today reads as a failure and
    the signal drowns in units nobody expected to be running."""
    assert looks_offline(0.0, 4.0) is False


def test_a_missing_number_is_a_conservative_no_rather_than_a_null():
    """This column gets filtered on, and a null behaving like "maybe" in a
    WHERE clause is worse than a no."""
    assert looks_offline(None, 300.0) is False
    assert looks_offline(0.0, None) is False


def test_both_thresholds_are_parameters():
    assert looks_offline(5.0, 300.0, output_threshold_mw=10.0) is True
    assert looks_offline(0.0, 6.0, baseline_threshold_mw=5.0) is True


# --- the one call the UDF makes ------------------------------------------------


def test_assess_returns_every_column_the_gold_table_needs():
    found = assess(20.0, [300.0] * 30)
    assert set(found) == {
        "baseline_mw",
        "baseline_observations",
        "deviation_mw",
        "deviation_pct",
        "looks_offline",
    }


def test_assess_agrees_with_the_functions_it_is_made_of():
    """One call rather than four, because it crosses into Spark. It must not
    quietly do anything different from the pieces tested above."""
    history = [280.0, 300.0, 310.0]
    found = assess(150.0, history)

    assert found["baseline_mw"] == median(history)
    assert found["deviation_mw"] == deviation(150.0, median(history))
    assert found["deviation_pct"] == deviation_percent(150.0, median(history))
    assert found["looks_offline"] == looks_offline(150.0, median(history))


def test_assess_counts_the_usable_observations_not_the_array_length():
    """A thin baseline is worth knowing about, and a count that includes nulls
    would make it look thicker than it is."""
    found = assess(20.0, [300.0, None, 300.0, None])
    assert found["baseline_observations"] == 2


def test_the_first_hour_a_unit_ever_reports_has_no_baseline():
    """And says so, rather than reporting a deviation from nothing."""
    found = assess(300.0, [])

    assert found["baseline_mw"] is None
    assert found["baseline_observations"] == 0
    assert found["deviation_mw"] is None
    assert found["deviation_pct"] is None
    assert found["looks_offline"] is False


def test_an_hour_with_no_reading_still_reports_its_baseline():
    """The unit published nothing this hour. That is not the same as the unit
    having no usual behaviour, and the baseline is still the useful half."""
    found = assess(None, [300.0] * 30)

    assert found["baseline_mw"] == 300.0
    assert found["deviation_mw"] is None
    assert found["looks_offline"] is False


def test_the_worked_example_from_the_readme():
    """A 300 MW unit at 20 MW against thirty ordinary days."""
    found = assess(20.0, [295.0, 300.0, 305.0] * 10)

    assert found["baseline_mw"] == 300.0
    assert found["deviation_mw"] == -280.0
    assert round(found["deviation_pct"], 2) == -93.33
    assert found["looks_offline"] is False, "20 MW is low, not off"