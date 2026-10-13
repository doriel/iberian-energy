"""The mutations have to be wrong in the way they claim to be wrong.

An adversarial evaluation is only worth its number if the corruptions it
generates are real corruptions. A mutator that quietly produces the original
value, or a value that was retrieved after all, inflates the detection rate
without anybody noticing, which is exactly the failure this whole exercise
exists to rule out. So the mutators are tested before they are trusted.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.adversarial import (  # noqa: E402
    foreign_mutants,
    mutate,
    score,
    summarise,
)
from iberian.agent.facts import Fact, FactSheet  # noqa: E402
from iberian.agent.verify import allowed_numbers, verify  # noqa: E402

START = datetime(2026, 8, 18, 7, 45, tzinfo=timezone.utc)
END = datetime(2026, 8, 18, 15, 15, tzinfo=timezone.utc)


def sheet(*facts: Fact, day: date = date(2026, 8, 18)) -> FactSheet:
    return FactSheet(
        subject="test episode",
        start_utc=START,
        end_utc=END,
        market_day=day,
        facts=list(facts),
        caveats=[],
    )


def standard() -> FactSheet:
    return sheet(
        Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
        Fact("min_border_capacity", 3195.0, "MW", "ENTSO-E A61 day-ahead capacity"),
        Fact("constrained_asset", "Alcochete-Palmela", "", "ENTSO-E A78"),
    )


TEXT = (
    "Portugal paid 109.84 EUR/MWh more than Spain [ENTSO-E A44] while the "
    "border was limited to 3,195 MW [ENTSO-E A61]."
)


def test_the_original_text_passes():
    """The baseline. Without this, a detection rate means nothing."""
    assert verify(TEXT, standard(), require_sources=False).ok


def test_every_mutant_actually_changes_the_text():
    for mutant in mutate(TEXT, standard()):
        assert mutant.text != TEXT
        assert mutant.replacement != mutant.original


def test_harmful_mutants_never_land_on_a_retrieved_value():
    """A corruption that produces a real number is not a fabrication.

    Scoring one would be dishonest in whichever direction it fell, so the
    generator drops them and this pins that down.
    """
    allowed = allowed_numbers(standard())
    for mutant in mutate(TEXT, standard()):
        if mutant.benign:
            continue
        assert float(mutant.replacement.replace(",", "")) not in allowed


def test_transposition_keeps_the_order_of_magnitude():
    """3,195 to 3,915, not 3,195 to 7. A mutation anything would catch
    proves nothing about this check."""
    mutants = [m for m in mutate(TEXT, standard()) if m.kind == "transpose"]
    assert mutants
    for mutant in mutants:
        before = float(mutant.original.replace(",", ""))
        after = float(mutant.replacement.replace(",", ""))
        assert 0.1 <= after / before <= 10


def test_the_written_form_is_preserved():
    """Thousands separators and decimal places survive the edit.

    A mutant that looks wrong on the page could be caught for the wrong
    reason, and the question here is whether the quantity is checked.
    """
    for mutant in mutate(TEXT, standard()):
        if "," in mutant.original:
            assert "," in mutant.replacement
        if "." in mutant.original:
            decimals = len(mutant.original.split(".")[1])
            assert len(mutant.replacement.split(".")[1]) == decimals


def test_the_verifier_catches_the_harmful_mutations():
    """The claim the project makes, measured rather than asserted."""
    harmful = [m for m in mutate(TEXT, standard()) if not m.benign]
    assert harmful
    outcomes = score(harmful, standard())
    assert all(outcome.caught for outcome in outcomes), [
        (o.mutant.kind, o.mutant.original, o.mutant.replacement)
        for o in outcomes
        if not o.caught
    ]


def test_a_faithful_rounding_is_let_through():
    """Not a miss. Rejecting "roughly 3,200 MW" for 3,195 was our bug once,
    and the fix is the reason this class is scored on its own."""
    benign = [m for m in mutate(TEXT, standard()) if m.benign]
    assert benign
    accepted = [o for o in score(benign, standard()) if not o.caught]
    assert accepted, "the verifier should still accept a faithful rounding"


def test_dates_are_not_mutated():
    """The numeric check does not own dates; the date check does. Corrupting
    one here would measure the wrong thing."""
    text = "On 2026-08-18 at 07:45 the premium reached 109.84 EUR/MWh [A44]."
    for mutant in mutate(text, standard()):
        assert "2026-08-18" in mutant.text
        assert "07:45" in mutant.text


def test_prose_dates_are_not_mutated():
    """The regression that the evaluation itself found.

    The generator used to carry its own copy of the date pattern, narrower
    than the verifier's on dates written out in words. It corrupted the day of
    "1 August 2026", the verifier masked the result as a date and said nothing,
    and the run reported seventeen holes in the numeric check that were the
    date check's business entirely.
    """
    text = "The market split on 1 August 2026 and the premium reached 109.84 [A44]."
    for mutant in mutate(text, standard()):
        assert "1 August 2026" in mutant.text, mutant.text


def test_a_foreign_number_is_a_real_number_from_elsewhere():
    other = sheet(
        Fact("peak_premium", 42.70, "EUR/MWh", "ENTSO-E A44 day-ahead"),
        Fact("min_border_capacity", 2600.0, "MW", "ENTSO-E A61 day-ahead capacity"),
        day=date(2026, 9, 3),
    )
    mutants = foreign_mutants(TEXT, standard(), [other], episode_key="e1")
    assert mutants

    mine = allowed_numbers(standard())
    theirs = allowed_numbers(other)
    for mutant in mutants:
        value = float(mutant.replacement.replace(",", ""))
        assert value in theirs, "a foreign value has to come from another sheet"
        assert value not in mine, "otherwise nothing was fabricated"


def test_foreign_numbers_are_caught():
    other = sheet(
        Fact("peak_premium", 42.70, "EUR/MWh", "ENTSO-E A44 day-ahead"),
        Fact("min_border_capacity", 2600.0, "MW", "ENTSO-E A61 day-ahead capacity"),
        day=date(2026, 9, 3),
    )
    outcomes = score(
        foreign_mutants(TEXT, standard(), [other], episode_key="e1"), standard()
    )
    assert outcomes
    assert all(outcome.caught for outcome in outcomes)


def test_mutants_are_deterministic():
    """A detection rate that moves between runs is not a measurement."""
    other = sheet(Fact("peak_premium", 42.70, "EUR/MWh", "A44"), day=date(2026, 9, 3))
    first = foreign_mutants(TEXT, standard(), [other], episode_key="e1", seed=7)
    second = foreign_mutants(TEXT, standard(), [other], episode_key="e1", seed=7)
    assert [m.text for m in first] == [m.text for m in second]


def test_summary_separates_the_benign_class():
    outcomes = score(mutate(TEXT, standard()), standard())
    table = summarise(outcomes)
    assert "rounded" in table
    assert "accepted" in table["rounded"]
    for kind in ("transpose", "magnitude", "drift"):
        assert table[kind]["rate"] == 100.0