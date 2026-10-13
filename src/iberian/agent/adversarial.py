"""Corrupt a published explanation on purpose, and see whether the check fires.

The verifier has rejected numbers on live text many times and every one of
those rejections turned out to be a defect in the verifier rather than an
invention by the model. That is an honest thing to report and it is also a
problem: a check that has never caught anything has not been shown to work. Its
unit tests pin down the behaviour on sentences somebody wrote to exercise it,
which proves the function does what its author meant, not that it would survive
a real fabrication in a real explanation.

So this module takes the explanations that were actually published, together
with the fact sheets they were generated from, and edits a number in each one
into a number that was never retrieved. The verifier then runs on the result.
Anything it fails to catch is a hole, stated as a measurement rather than as a
worry.

**The mutations are the mistakes a language model makes**, not random noise.
Replacing 3,195 with 7 would be caught by anything and would prove nothing. The
interesting corruptions are the ones that still read like a plausible figure in
the same sentence:

- `transpose` swaps two adjacent digits: 3,195 becomes 3,915. The commonest
  copying error there is, and the result is the same order of magnitude.
- `magnitude` multiplies or divides by ten: 3,195 becomes 31,950. A decimal
  point in the wrong place, which is how a unit confusion usually surfaces.
- `drift` moves the last significant digit by one: 109.84 becomes 109.94. The
  hardest class, because it is the smallest possible lie.
- `foreign` substitutes a real number retrieved for a *different* episode. The
  nastiest and the most realistic: the figure exists, it is the right shape,
  it is sourced, and it belongs to another day.

One more mutation is generated and then deliberately scored apart:

- `rounded` replaces 3,195 with 3,200. The verifier accepts this **by design**,
  because "roughly 3,200 MW" is a faithful reading of 3,195 and rejecting it
  once cost a day of chasing false positives. Counting it as a miss would make
  the headline number look worse while describing nothing real, so it is
  reported in its own row and excluded from the detection rate.

What this cannot test is the class the verifier does not claim to cover: a true
figure attached to the wrong thing, "5,445 MW in both directions" for a one
direction capacity. The value is in the evidence, so a value check passes it.
That limitation is in the roadmap, and an LLM judge is the principled answer.
Mutating units here would measure a promise nobody made.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from iberian.agent.facts import FactSheet
from iberian.agent.verify import (
    _DATELIKE,
    _supports,
    allowed_numbers,
    extract_claims,
    verify,
)

#: Written forms that carry no claim, so corrupting them tests nothing. These
#: are the same values the verifier lets through, kept in step with it on
#: purpose: a mutation of a free number is not a fabrication the check was ever
#: meant to catch.
from iberian.agent.verify import FREE_NUMBERS

#: Mutations whose result the verifier is supposed to accept. Scored, reported,
#: and kept out of the detection rate.
BENIGN = frozenset({"rounded"})

_NUMBER_IN_TEXT = re.compile(r"(?<![\w.])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)")

# Dates and clock times are quoted text rather than measurements, and the
# verifier masks them before extracting claims. Corrupting one produces a
# mutant the numeric check is not responsible for and the date check is, so
# scoring it would charge one component for another's job.
#
# The verifier's own pattern is imported rather than restated. The first
# version of this file carried a copy, the copy was narrower on prose dates
# like "1 August 2026", and the evaluation duly reported seventeen holes in
# the numeric check that were nothing of the kind. A second definition of the
# same idea is a second thing to keep in step, and this one did not stay.
_SKIP = re.compile(
    rf"{_DATELIKE.pattern}|\d{{1,2}}:\d{{2}}|\b\d{{4}}\b", re.VERBOSE
)


@dataclass(frozen=True)
class Mutant:
    """One corrupted explanation, and what was done to it."""

    episode_key: str
    kind: str
    original: str
    replacement: str
    text: str

    @property
    def benign(self) -> bool:
        return self.kind in BENIGN


#: Why a mutant was not caught. The distinction is the whole value of the
#: exercise: a check that declares a tolerance and then honours it is working,
#: while a corruption it lets through for no stated reason is a hole. Folding
#: the two together produces one number that describes neither.
TOLERATED = "tolerance"   # inside the rounding the verifier documents
FREE = "free-number"      # on the small allowlist the verifier declares
MISSED = "missed"         # no reason. This is the number that matters.
CAUGHT = "caught"


@dataclass(frozen=True)
class Outcome:
    """A mutant, the verdict the check returned, and why it returned it."""

    mutant: Mutant
    caught: bool
    reason: str
    category: str = CAUGHT


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges holding a date or a clock time, which are not claims.

    Computed over the whole string rather than a window around each number.
    A window has to be wide enough to see the start of "2026-08-18" from its
    last digit, and any width that is enough there is too wide somewhere else.
    The first version of this used a window and corrupted the day of a date,
    which the tests caught before the evaluation was ever run.
    """
    return [match.span() for match in _SKIP.finditer(text)]


def _inside(span: tuple[int, int], protected: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end
               for other_start, other_end in protected)


def _parse(written: str) -> float:
    return float(written.replace(",", ""))


def _format_like(original: str, value: float) -> str:
    """Write the new value the way the old one was written.

    A mutant that changes 3,195 into 3915.0 would be caught partly because it
    looks wrong, which is not the property under test. Keeping the thousands
    separators and the decimal places means the only thing that changed is the
    quantity.
    """
    decimals = len(original.split(".")[1]) if "." in original else 0
    grouped = "," in original
    body = f"{value:,.{decimals}f}" if grouped else f"{value:.{decimals}f}"
    return body


def _transpose(value: float, written: str) -> float | None:
    digits = [c for c in written if c.isdigit()]
    for i in range(len(digits) - 1):
        if digits[i] != digits[i + 1]:
            swapped = digits[:]
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            rebuilt = "".join(swapped)
            if "." in written:
                decimals = len(written.split(".")[1])
                out = float(rebuilt[:-decimals] + "." + rebuilt[-decimals:])
            else:
                out = float(rebuilt)
            if out != value:
                return out
    return None


def _magnitude(value: float, _written: str) -> float | None:
    return value * 10.0 if value else None


def _drift(value: float, written: str) -> float | None:
    decimals = len(written.split(".")[1]) if "." in written else 0
    step = 10.0 ** (-decimals)
    return round(value + step, decimals + 2)


def _rounded(value: float, _written: str) -> float | None:
    """Round to a scale the verifier accepts as a faithful reading."""
    for scale in (10, 100, 1000):
        candidate = round(value / scale) * scale
        if candidate != value and candidate != 0:
            return float(candidate)
    return None


_MUTATORS = {
    "transpose": _transpose,
    "magnitude": _magnitude,
    "drift": _drift,
    "rounded": _rounded,
}


def mutate(
    text: str,
    sheet: FactSheet,
    episode_key: str = "",
    kinds: tuple[str, ...] = ("transpose", "magnitude", "drift", "rounded"),
) -> list[Mutant]:
    """Every single number corruption of one explanation, one per site per kind.

    One mutation per mutant, deliberately. Changing two numbers at once would
    inflate the detection rate, because catching either counts as catching the
    mutant and a check that finds one in two would score as perfect.
    """
    allowed = allowed_numbers(sheet)
    protected = _protected_spans(text)
    out: list[Mutant] = []

    for match in _NUMBER_IN_TEXT.finditer(text):
        written = match.group(1)
        start, end = match.span(1)
        if _inside((start, end), protected):
            continue
        try:
            value = _parse(written)
        except ValueError:
            continue
        if value in FREE_NUMBERS:
            continue

        for kind in kinds:
            new_value = _MUTATORS[kind](value, written)
            if new_value is None or new_value == value:
                continue
            # A corruption that lands on another retrieved value is not a
            # fabrication, it is a different true number, so there is nothing
            # for the check to catch and scoring it would be dishonest. The
            # benign rounding class is exempt: landing on an allowed value is
            # the whole point of it.
            if kind not in BENIGN and new_value in allowed:
                continue
            replacement = _format_like(written, new_value)
            out.append(
                Mutant(
                    episode_key=episode_key,
                    kind=kind,
                    original=written,
                    replacement=replacement,
                    text=text[:start] + replacement + text[end:],
                )
            )
    return out


def foreign_mutants(
    text: str,
    sheet: FactSheet,
    others: list[FactSheet],
    episode_key: str = "",
    seed: int = 0,
    per_episode: int = 3,
) -> list[Mutant]:
    """Replace a number with one retrieved for a different episode.

    The hardest case to catch and the easiest to produce accidentally: the
    model has a real, sourced, correctly shaped figure in front of it and
    attaches it to the wrong day. Values that happen to appear in this
    episode's own sheet are skipped, because then nothing was fabricated.
    """
    mine = allowed_numbers(sheet)
    pool = sorted(
        {
            value
            for other in others
            for value in allowed_numbers(other)
            if value not in mine and value not in FREE_NUMBERS and value > 0
        }
    )
    if not pool:
        return []

    rng = random.Random(f"{seed}:{episode_key}")
    protected = _protected_spans(text)
    sites = [
        match
        for match in _NUMBER_IN_TEXT.finditer(text)
        if not _inside(match.span(1), protected)
    ]
    out: list[Mutant] = []
    for match in sites[:per_episode]:
        written = match.group(1)
        start, end = match.span(1)
        try:
            value = _parse(written)
        except ValueError:
            continue
        if value in FREE_NUMBERS:
            continue
        # Prefer a stolen value of the same order of magnitude. A capacity in
        # place of a price is caught by the reader before the verifier, so it
        # would not be a fair test of the verifier.
        same_scale = [
            other
            for other in pool
            if other != value and 0.1 <= (other / value if value else 0) <= 10
        ]
        replacement_value = rng.choice(same_scale or pool)
        replacement = _format_like(written, replacement_value)
        out.append(
            Mutant(
                episode_key=episode_key,
                kind="foreign",
                original=written,
                replacement=replacement,
                text=text[:start] + replacement + text[end:],
            )
        )
    return out


def _why_not_caught(mutant: Mutant, sheet: FactSheet) -> str:
    """Attribute a miss to a declared rule, or refuse to attribute it at all.

    Only two excuses are accepted, and both are things the verifier says about
    itself in its own module: the rounding tolerance, and the short list of
    numbers that carry no claim. Anything else is `MISSED`, and `MISSED` is
    what this evaluation exists to count.
    """
    value = float(mutant.replacement.replace(",", ""))
    if value in FREE_NUMBERS:
        return FREE

    claims = [c for c in extract_claims(mutant.replacement) if c.value == value]
    if claims and any(
        _supports(allowed, claims[0]) for allowed in allowed_numbers(sheet)
    ):
        return TOLERATED

    return MISSED


def score(mutants: list[Mutant], sheet: FactSheet) -> list[Outcome]:
    """Run the verifier over each mutant and record whether, and why, it fired."""
    out = []
    for mutant in mutants:
        verdict = verify(mutant.text, sheet, require_sources=False)
        caught = bool(verdict.unsupported)
        out.append(
            Outcome(
                mutant=mutant,
                caught=caught,
                reason=verdict.describe() if not verdict.ok else "",
                category=CAUGHT if caught else _why_not_caught(mutant, sheet),
            )
        )
    return out


def summarise(outcomes: list[Outcome]) -> dict[str, dict[str, int | float]]:
    """Per mutation class: caught, and each reason it was not.

    The rate is over the mutants that were not excused by a declared rule,
    because a check cannot be marked down for honouring a tolerance it
    publishes. The excused counts stay in the row so nobody has to take that
    on trust.
    """
    table: dict[str, dict[str, int | float]] = {}
    for outcome in outcomes:
        row = table.setdefault(
            outcome.mutant.kind,
            {"mutants": 0, "caught": 0, TOLERATED: 0, FREE: 0, MISSED: 0, "rate": 0.0},
        )
        row["mutants"] = int(row["mutants"]) + 1
        if outcome.caught:
            row["caught"] = int(row["caught"]) + 1
        else:
            row[outcome.category] = int(row[outcome.category]) + 1

    for kind, row in table.items():
        judged = int(row["caught"]) + int(row[MISSED])
        row["rate"] = round(100.0 * int(row["caught"]) / judged, 1) if judged else 0.0
        if kind in BENIGN:
            row["accepted"] = int(row["mutants"]) - int(row["caught"])
    return table