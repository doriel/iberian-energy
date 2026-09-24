"""The capacity percentile, which is what a labeller actually decides on.

Worth its own tests because it is the number that separates "an outage reduced
this border" from "the border was simply full", and those are different causes
with different consequences for a reader.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.capacity import (  # noqa: E402
    ORDINARY_CAPACITY_PERCENTILE,
    is_ordinary,
    percentile_function,
    percentile_of,
)

SERIES = [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]


def test_the_lowest_value_sits_at_zero():
    # Strictly below, so the lowest capacity ever seen reads as 0 rather than a
    # small positive number that invites "almost the lowest".
    assert percentile_of(SERIES, 1000.0) == 0.0


def test_the_highest_value_sits_below_one_hundred():
    assert percentile_of(SERIES, 5000.0) == 80.0


def test_a_value_above_everything_seen_is_one_hundred():
    assert percentile_of(SERIES, 9999.0) == 100.0


def test_the_order_of_the_series_does_not_matter():
    assert percentile_of(list(reversed(SERIES)), 3000.0) == percentile_of(SERIES, 3000.0)


def test_a_missing_value_is_unknown_rather_than_zero():
    """Zero would read as "the lowest capacity ever recorded", which is the
    opposite of "we do not know"."""
    assert percentile_of(SERIES, None) is None
    assert percentile_of(SERIES, "not a number") is None


def test_an_empty_series_is_unknown():
    assert percentile_of([], 3000.0) is None


def test_the_bound_function_matches_the_direct_one():
    percentile = percentile_function(SERIES)
    assert percentile(3000.0) == percentile_of(SERIES, 3000.0)


def test_a_read_only_series_is_not_a_problem():
    """pandas hands back a read-only view of its own buffer, and an earlier
    version of this sorted it in place and crashed on exactly that."""
    import array

    frozen = array.array("d", SERIES)
    assert percentile_of(frozen, 3000.0) == 40.0


# --- the boundary that decides a label ---------------------------------------


def test_the_quartile_is_the_line_between_reduced_and_ordinary():
    assert is_ordinary(ORDINARY_CAPACITY_PERCENTILE) is True
    assert is_ordinary(ORDINARY_CAPACITY_PERCENTILE - 0.1) is False


def test_the_line_is_the_quartile_and_not_the_median():
    """Half of everything is below the median by construction, so a median cut
    calls an utterly ordinary capacity a reduction."""
    assert ORDINARY_CAPACITY_PERCENTILE < 50


def test_an_unknown_percentile_is_not_quietly_treated_as_ordinary():
    assert is_ordinary(None) is None