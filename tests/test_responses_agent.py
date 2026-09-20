"""The served agent, driven end to end without a serving endpoint.

The point worth testing is not that MLflow's types work, it is that the
guarantee survives the move into them: a draft that fails verification must not
reach the caller as prose, and the caller must be able to tell the two apart
without reading English.

Skipped where MLflow is absent, because this module is the platform integration
and there is nothing to assert without the platform's types.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("mlflow", reason="the served agent is the MLflow integration")

from mlflow.types.responses import ResponsesAgentRequest  # noqa: E402

from iberian.agent.facts import Fact, FactSheet  # noqa: E402


def load_agent_module():
    """Import the model code the way MLflow does, by path rather than package."""
    spec = importlib.util.spec_from_file_location(
        "mibel_agent", ROOT / "agents" / "mibel_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGENT = load_agent_module()


def sheet() -> FactSheet:
    return FactSheet(
        subject="Market splitting episode starting 2026-08-18 07:45Z",
        start_utc=datetime(2026, 8, 18, 7, 45, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 18, 15, 15, tzinfo=timezone.utc),
        market_day=date(2026, 8, 18),
        facts=[
            Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
            Fact("min_border_capacity", 3195.0, "MW", "ENTSO-E A61"),
        ],
        caveats=["Consistency is not causation."],
    )


def request(answer: str, **custom) -> ResponsesAgentRequest:
    payload = {"fact_sheet": sheet().to_dict(), **custom}
    return ResponsesAgentRequest(
        input=[{"role": "user", "content": "explain this episode"}],
        custom_inputs=payload,
    )


def agent_returning(*answers: str):
    """An agent whose model says exactly these things, in order."""
    remaining = list(answers)

    def factory(endpoint: str):
        def complete(system: str, user: str) -> str:
            return remaining.pop(0) if remaining else ""

        return complete

    return AGENT.MibelExplanationAgent(completer_factory=factory)


GROUNDED = (
    "Portugal paid up to 109.84 EUR/MWh more than Spain, with the border at "
    "3195 MW (ENTSO-E A44 day-ahead, ENTSO-E A61)."
)
INVENTED = (
    "Portugal paid up to 109.84 EUR/MWh more than Spain, driven by 412 MW of "
    "lost wind (ENTSO-E A44 day-ahead)."
)


def text_of(response) -> str:
    return response.output[0].content[0]["text"]


# --- the happy path ---------------------------------------------------------


def test_a_grounded_explanation_is_returned_with_its_verdict():
    response = agent_returning(GROUNDED).predict(request(GROUNDED))
    assert "109.84" in text_of(response)
    assert response.custom_outputs["grounded"] is True
    assert response.custom_outputs["attempts"] == 1
    assert response.custom_outputs["unsupported"] == []


def test_the_sources_come_back_so_a_caller_can_show_them():
    response = agent_returning(GROUNDED).predict(request(GROUNDED))
    assert response.custom_outputs["sources"] == [
        "ENTSO-E A44 day-ahead",
        "ENTSO-E A61",
    ]
    assert "2026-08-18" in response.custom_outputs["subject"]


# --- the guarantee ----------------------------------------------------------


def test_an_invented_figure_never_reaches_the_caller_as_prose():
    # Both attempts invent, so the retry cannot save it.
    response = agent_returning(INVENTED, INVENTED).predict(request(INVENTED))
    body = text_of(response)
    assert "412" not in body
    assert body == AGENT.REFUSAL
    assert response.custom_outputs["grounded"] is False


def test_a_refusal_says_which_figure_was_the_problem():
    response = agent_returning(INVENTED, INVENTED).predict(request(INVENTED))
    unsupported = response.custom_outputs["unsupported"]
    assert unsupported, "a refusal with no offending claim is not actionable"
    assert any("412" in claim for claim in unsupported)


def test_a_corrected_retry_is_returned_and_counted():
    response = agent_returning(INVENTED, GROUNDED).predict(request(GROUNDED))
    assert response.custom_outputs["grounded"] is True
    assert response.custom_outputs["attempts"] == 2


def test_an_empty_answer_is_a_refusal_rather_than_a_vacuous_pass():
    # Silence contains no unsupported figure, so a naive numeric check passes
    # it. The task is to explain, and nothing is not an explanation.
    response = agent_returning("", "").predict(request(""))
    assert text_of(response) == AGENT.REFUSAL
    assert response.custom_outputs["grounded"] is False


# --- the seam ---------------------------------------------------------------


def test_a_request_without_a_fact_sheet_says_what_to_send():
    request_without = ResponsesAgentRequest(
        input=[{"role": "user", "content": "explain"}], custom_inputs={}
    )
    with pytest.raises(ValueError, match="fact_sheet is required"):
        agent_returning(GROUNDED).predict(request_without)


def test_a_fact_sheet_of_the_wrong_shape_is_refused():
    bad = ResponsesAgentRequest(
        input=[{"role": "user", "content": "explain"}],
        custom_inputs={"fact_sheet": "2026-08-18T0745"},
    )
    with pytest.raises(ValueError, match="must be an object"):
        agent_returning(GROUNDED).predict(bad)


def test_the_sheet_that_arrives_is_the_sheet_that_was_sent():
    restored = AGENT.resolve_fact_sheet({"fact_sheet": sheet().to_dict()})
    assert restored == sheet()
    assert restored.numbers() == {109.84, 3195.0}


# --- request time choices ---------------------------------------------------


def test_the_endpoint_can_be_chosen_per_request():
    seen: list[str] = []

    def factory(endpoint: str):
        seen.append(endpoint)
        return lambda system, user: GROUNDED

    agent = AGENT.MibelExplanationAgent(completer_factory=factory)
    response = agent.predict(request(GROUNDED, endpoint="databricks-claude-opus-4-5"))
    assert seen == ["databricks-claude-opus-4-5"]
    assert response.custom_outputs["model"] == "databricks-claude-opus-4-5"


def test_the_default_endpoint_is_used_when_none_is_given(monkeypatch):
    monkeypatch.delenv("IBERIAN_ENDPOINT", raising=False)
    seen: list[str] = []

    def factory(endpoint: str):
        seen.append(endpoint)
        return lambda system, user: GROUNDED

    AGENT.MibelExplanationAgent(completer_factory=factory).predict(request(GROUNDED))
    assert seen == [AGENT.DEFAULT_ENDPOINT]


def test_a_completer_is_built_once_per_endpoint():
    built: list[str] = []

    def factory(endpoint: str):
        built.append(endpoint)
        return lambda system, user: GROUNDED

    agent = AGENT.MibelExplanationAgent(completer_factory=factory)
    agent.predict(request(GROUNDED))
    agent.predict(request(GROUNDED))
    assert built == [AGENT.DEFAULT_ENDPOINT], "a served model must not reconnect per call"