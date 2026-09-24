"""Where a border capacity sits among the others, which is the only reference
this data supports.

There is no published normal capacity for the Spain to Portugal border. The
operator recomputes the net transfer capacity every day from the whole system
state, and over one summer the values run from 210 to 7020 MW with no mode. So
"was this reduced" cannot be answered by comparing against a nominal rating,
because there is not one to compare against.

What can be answered is whether a figure is unusual for this border, and that is
a percentile of the window's own distribution. It is a weaker claim than a
reduction and it is the honest one: it says the border was carrying unusually
little, not why.

Module level because two callers need the same answer. The labelling sheet uses
it to put a number in front of a person deciding a cause, and the task that
copies episodes into Lakebase uses it to put the same number in front of a
person deciding the same thing in the application. Two implementations would
drift, and the drift would show up as the application and the sheet disagreeing
about an episode for reasons nobody could see.
"""

from __future__ import annotations

from typing import Callable, Sequence

#: Below this percentile the border was carrying unusually little and something
#: reduced it. At or above it the level is ordinary and there is no reduction to
#: explain, whatever notices happen to be in force.
#:
#: The quartile rather than the median, because half of all quarter hours are
#: below the median by construction and calling that half "unusual" empties the
#: word. On the first sixty two episodes the choice moved twelve of them between
#: `saturation_ordinary_capacity` and the outage labels.
ORDINARY_CAPACITY_PERCENTILE = 25.0


def percentile_of(series: Sequence[float], value: float | None) -> float | None:
    """The share of the series strictly below `value`, as a percentage.

    Strictly below, so the lowest capacity ever seen sits at 0 rather than at
    some small positive number that invites reading it as "almost the lowest".

    The series is not sorted and does not need to be: this is the mean of a
    boolean mask. An earlier version sorted it in place and crashed, because
    pandas hands back a read-only view of its own buffer.
    """
    if value is None or not len(series):
        return None
    try:
        target = float(value)
    except (TypeError, ValueError):
        return None
    below = sum(1 for item in series if float(item) < target)
    return round(below / len(series) * 100, 1)


def percentile_function(series: Sequence[float]) -> Callable[[float | None], float | None]:
    """The same thing, bound to one series, for a loop over many episodes."""
    values = [float(item) for item in series]

    def percentile(value: float | None) -> float | None:
        return percentile_of(values, value)

    return percentile


def is_ordinary(percentile: float | None) -> bool | None:
    """Whether the border was at a level it reaches routinely.

    None rather than False when the percentile is unknown, because "we could not
    tell" and "the capacity was ordinary" lead to different labels and
    collapsing them would quietly turn one into the other.
    """
    if percentile is None:
        return None
    return percentile >= ORDINARY_CAPACITY_PERCENTILE