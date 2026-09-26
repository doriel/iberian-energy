"""Was this unit producing less than it usually does?

The question the grid analyst asks during an anomaly, and the one the agent
needs a defensible answer to before it says an outage explains a price. Pure
functions over plain numbers, so the judgements live here where tests can reach
them rather than inside a notebook where nothing ever runs them again.

## Why a median and not a mean

The thing being detected is a unit behaving unusually. A mean is dragged down by
exactly the events we are looking for: a plant that was out for five of the last
thirty days has a mean low enough that today's outage looks ordinary. A median
over thirty observations survives up to fifteen of them being outages.

## Why "looks offline" and not "is offline"

Nothing in this module knows why a unit is at zero. Planned maintenance, a
forced outage, no water, or a price below its marginal cost all produce the same
reading. This is a signal to be corroborated against an A80 notice, not a
statement about the plant, and the column is named so that somebody reading a
query result cannot mistake one for the other.

## Why the baseline can be thin

The window is the previous thirty *observations* at this hour, not the previous
thirty days. For a unit that reported every day those are the same thing. For a
unit commissioned in March, or one that only runs in winter, the window reaches
further back, and a baseline built from last February is a baseline worth
knowing about. So the count and the span come back alongside the number rather
than being hidden inside it.
"""

from __future__ import annotations

from typing import Iterable, Sequence

#: Below this, a unit is producing nothing that matters. Not zero, because a
#: plant drawing its own station service reports small positive values and a
#: strict zero would miss most real stoppages.
OFFLINE_OUTPUT_MW = 1.0

#: And the baseline has to be big enough for the absence to mean something.
#: Without this, every small unit that happens to be idle reads as an outage,
#: and the signal drowns in units nobody would have expected to be running.
OFFLINE_BASELINE_MW = 10.0

#: A percentage against a baseline near zero is arithmetic rather than
#: information: a unit that normally makes 0.2 MW and makes 2 MW today is up
#: 900 per cent and nothing happened.
MINIMUM_BASELINE_FOR_PERCENTAGE_MW = 1.0


def _numbers(values: Iterable | None) -> list[float]:
    """Whatever came in, as floats, with anything unusable dropped.

    Spark hands a null array through as None and a null element through as
    None, and both arrive here rather than being guarded at every call site.
    """
    if not values:
        return []
    kept: list[float] = []
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number:  # NaN, which compares unequal to itself
            continue
        kept.append(number)
    return kept


def median(values: Iterable | None) -> float | None:
    """The middle value, or the mean of the middle two.

    `None` for an empty series rather than zero. Zero would read as "this unit
    normally produces nothing", which is a claim, and the opposite of "we have
    never seen this unit before".
    """
    numbers = sorted(_numbers(values))
    if not numbers:
        return None
    middle = len(numbers) // 2
    if len(numbers) % 2:
        return numbers[middle]
    return (numbers[middle - 1] + numbers[middle]) / 2


def deviation(output_mw: float | None, baseline_mw: float | None) -> float | None:
    """How far off the usual, in megawatts. Negative means below."""
    if output_mw is None or baseline_mw is None:
        return None
    return float(output_mw) - float(baseline_mw)


def deviation_percent(
    output_mw: float | None,
    baseline_mw: float | None,
    minimum_baseline_mw: float = MINIMUM_BASELINE_FOR_PERCENTAGE_MW,
) -> float | None:
    """The same as a percentage, or `None` when a percentage would mislead.

    Refusing to answer is the point. A near zero baseline produces percentages
    in the hundreds for changes of a megawatt or two, and those numbers then get
    sorted descending by somebody looking for the biggest movers.
    """
    if output_mw is None or baseline_mw is None:
        return None
    if abs(float(baseline_mw)) < minimum_baseline_mw:
        return None
    return 100.0 * (float(output_mw) - float(baseline_mw)) / float(baseline_mw)


def looks_offline(
    output_mw: float | None,
    baseline_mw: float | None,
    output_threshold_mw: float = OFFLINE_OUTPUT_MW,
    baseline_threshold_mw: float = OFFLINE_BASELINE_MW,
) -> bool:
    """Producing nothing, when it normally produces something worth noticing.

    False rather than None when either number is missing: this is a flag that
    gets filtered on, and a null that behaves like "maybe" in a WHERE clause is
    worse than a conservative no.
    """
    if output_mw is None or baseline_mw is None:
        return False
    return (
        float(output_mw) <= output_threshold_mw
        and float(baseline_mw) >= baseline_threshold_mw
    )


def assess(
    output_mw: float | None,
    history: Sequence | None,
    output_threshold_mw: float = OFFLINE_OUTPUT_MW,
    baseline_threshold_mw: float = OFFLINE_BASELINE_MW,
) -> dict:
    """Everything derived from one hour and its history, in one call.

    One function rather than four because it crosses into Spark as a UDF, and
    four UDFs over two and a half million rows is four passes for work that is
    one pass. The pieces are still separately testable above.
    """
    usable = _numbers(history)
    baseline = median(usable)
    return {
        "baseline_mw": baseline,
        "baseline_observations": len(usable),
        "deviation_mw": deviation(output_mw, baseline),
        "deviation_pct": deviation_percent(output_mw, baseline),
        "looks_offline": looks_offline(
            output_mw, baseline, output_threshold_mw, baseline_threshold_mw
        ),
    }