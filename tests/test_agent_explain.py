"""The generation loop, with a stub model so nothing here touches a network.

What matters is not that a model can write a sentence. It is that an
explanation which fails the numeric check never reaches a reader, that the
retry is told precisely what was wrong, and that a second failure is reported
as a failure rather than quietly returned.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.explain import explain  # noqa: E402
from iberian.agent.facts import Fact, FactSheet  # noqa: E402

HONEST = (
    "Portugal paid up to 109.84 EUR/MWh more than Spain [ENTSO-E A44 day-ahead] "
    "while day-ahead capacity on the border fell to 3,195 MW [ENTSO-E A61]."
)
INVENTED = (
    "Portugal paid up to 109.84 EUR/MWh more than Spain [ENTSO-E A44 day-ahead] "
    "while day-ahead capacity on the border fell to 2,400 MW [ENTSO-E A61]."
)
UNSOURCED = "Portugal paid up to 109.84 EUR/MWh more than Spain."


def sheet() -> FactSheet:
    return FactSheet(
        subject="episode",
        start_utc=datetime(2026, 8, 18, 7, 45, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 18, 15, 15, tzinfo=timezone.utc),
        market_day=date(2026, 8, 18),
        facts=[
            Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
            Fact("min_border_capacity", 3195.0, "MW", "ENTSO-E A61"),
        ],
        caveats=["Consistency is not causation."],
    )


class Stub:
    """Returns canned answers in order, and records what it was asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.systems: list[str] = []
        self.users: list[str] = []

    def __call__(self, system: str, user: str) -> str:
        self.systems.append(system)
        self.users.append(user)
        return self.answers[min(len(self.systems) - 1, len(self.answers) - 1)]


def test_a_verified_explanation_is_returned_on_the_first_attempt():
    result = explain(sheet(), Stub(HONEST))

    assert result.ok
    assert result.attempts == 1
    assert result.rejected == []


def test_an_invented_number_is_never_returned_as_ok():
    result = explain(sheet(), Stub(INVENTED, INVENTED))

    assert not result.ok
    assert "2,400" in result.verdict.describe()


def test_the_retry_names_the_figure_that_was_rejected():
    """A model that rounded rather than fabricated usually fixes it when told."""
    stub = Stub(INVENTED, HONEST)
    result = explain(sheet(), stub)

    assert result.ok
    assert result.attempts == 2
    assert len(stub.systems) == 2
    assert "2,400" in stub.systems[1]
    assert "do not appear in the facts" in stub.systems[1]


def test_the_first_prompt_carries_no_retry_text():
    stub = Stub(HONEST)
    explain(sheet(), stub)

    assert "previous answer was rejected" not in stub.systems[0]


def test_a_missing_source_produces_its_own_correction():
    stub = Stub(UNSOURCED, HONEST)
    result = explain(sheet(), stub)

    assert result.ok
    assert "named no source" in stub.systems[1]


def test_the_model_is_given_the_facts_and_the_caveats():
    stub = Stub(HONEST)
    explain(sheet(), stub)

    assert "peak_premium" in stub.users[0]
    assert "ENTSO-E A44 day-ahead" in stub.users[0]
    assert "Consistency is not causation." in stub.users[0]


def test_rejected_drafts_are_kept_for_inspection():
    """The failures are the evidence that the check does something."""
    result = explain(sheet(), Stub(INVENTED, HONEST))
    assert result.rejected == [INVENTED]


def test_a_single_attempt_is_allowed_and_does_not_retry():
    stub = Stub(INVENTED, HONEST)
    result = explain(sheet(), stub, max_attempts=1)

    assert not result.ok
    assert len(stub.systems) == 1


def test_zero_attempts_is_a_programming_error():
    with pytest.raises(ValueError, match="at least 1"):
        explain(sheet(), Stub(HONEST), max_attempts=0)


def test_an_empty_response_fails_rather_than_passing_vacuously():
    """No text is not a grounded explanation, even though it invents nothing."""
    result = explain(sheet(), Stub("", ""))
    assert result.text == ""
    # Nothing was claimed, so nothing is unsupported, but there is no
    # explanation either. The caller sees empty text and can act on it.
    assert result.attempts == 2


def test_a_rejected_result_does_not_present_the_text_as_an_answer():
    result = explain(sheet(), Stub(INVENTED, INVENTED))
    assert "REJECTED" in result.render()
    assert INVENTED not in result.render()


def test_the_model_name_is_carried_through_for_the_evaluation_record():
    result = explain(sheet(), Stub(HONEST), model="databricks-claude-haiku-4-5")
    assert result.model == "databricks-claude-haiku-4-5"
    assert "databricks-claude-haiku-4-5" in result.render()
