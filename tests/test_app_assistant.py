"""The tool calling loop, driven by a scripted model.

The sequences worth testing are the awkward ones: a write the tools refuse, a
deletion the agent must not complete on its own, a model that never stops asking
for tools. All three are hard to produce against a live endpoint and trivial
against a script, which is why the model is a callable rather than a client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.app.actions import Actions  # noqa: E402
from iberian.app.assistant import TOOLS, Assistant  # noqa: E402


class FakeStore:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.statements: list[tuple[str, tuple]] = []

    def _answer(self, statement, args):
        self.statements.append((statement, tuple(args)))
        for marker, rows in self.rows.items():
            if marker in statement:
                return list(rows)
        return []

    def query(self, statement, args=()):
        return self._answer(statement, args)

    def execute(self, statement, args=()):
        rows = self._answer(statement, args)
        return rows[0] if rows else None


class ScriptedModel:
    """Replies in order, and remembers what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def __call__(self, messages, tools):
        self.seen.append(list(messages))
        return self.replies.pop(0) if self.replies else {"content": "done"}


def says(text):
    return {"content": text, "tool_calls": []}


def calls(name, arguments, call_id="call-1"):
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def build(model, rows=None):
    store = FakeStore(rows=rows)
    actions = Actions(store=store, session_id="s-1", created_by="doriel")
    return store, Assistant(actions=actions, chat=model)


EPISODE = {"episode_key": "2026-08-18T0745"}


# --- the shape the model is given --------------------------------------------


def test_every_tool_is_openai_shaped():
    for tool in TOOLS:
        assert tool["type"] == "function"
        function = tool["function"]
        assert function["name"] and function["description"]
        assert function["parameters"]["type"] == "object"


def test_the_write_tools_constrain_their_vocabularies_in_the_schema():
    """The enum is what stops a plausible invented value reaching validation.

    Validation would refuse it anyway, but a tool the model can only call
    correctly is better than one it calls wrongly and gets told off for.
    """
    by_name = {tool["function"]["name"]: tool["function"] for tool in TOOLS}
    label = by_name["submit_episode_label"]["parameters"]["properties"]
    assert "saturation_ordinary_capacity" in label["true_cause"]["enum"]
    assert set(label["confidence"]["enum"]) == {"high", "medium", "low"}


def test_the_system_prompt_is_sent_first():
    model = ScriptedModel(says("hello"))
    _, assistant = build(model)
    assistant.ask("hi")

    assert model.seen[0][0]["role"] == "system"
    assert "Never state a number" in model.seen[0][0]["content"]


# --- a plain answer -----------------------------------------------------------


def test_an_answer_without_tools_comes_straight_back():
    model = ScriptedModel(says("Portugal and Spain share a market."))
    _, assistant = build(model)
    turn = assistant.ask("what is market splitting")

    assert turn.reply == "Portugal and Spain share a market."
    assert turn.rounds == 1
    assert not turn.wrote_anything


# --- a write through the agent ------------------------------------------------


def test_the_agent_can_create_an_alert_and_report_it():
    model = ScriptedModel(
        calls("create_alert", {"zone": "PT", "direction": "above", "threshold_eur_mwh": 20}),
        says("Done, I will tell you when PT goes above 20."),
    )
    store, assistant = build(model, rows={"INSERT INTO iberian.alerts": [{"id": 5}]})
    turn = assistant.ask("warn me when PT goes above 20")

    assert turn.wrote_anything
    assert turn.results[0].ok
    assert "20" in turn.reply
    assert [s for s, _ in store.statements if "INSERT INTO iberian.alerts" in s]


def test_the_tool_result_is_handed_back_to_the_model():
    model = ScriptedModel(
        calls("create_alert", {"zone": "PT", "direction": "above", "threshold_eur_mwh": 20}),
        says("done"),
    )
    build(model, rows={"INSERT INTO iberian.alerts": [{"id": 5}]})[1].ask("x")

    tool_messages = [m for m in model.seen[1] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["status"] == "ok"


def test_a_refused_write_reaches_the_model_as_a_rejection_not_a_crash():
    """The model has to be able to tell the person why, in the tool's words."""
    model = ScriptedModel(
        calls("create_alert", {"zone": "PT", "direction": "above", "threshold_eur_mwh": 40000}),
        says("That is above the market cap, so I did not save it."),
    )
    _, assistant = build(model)
    turn = assistant.ask("warn me above 40000")

    assert turn.results[0].status == "rejected"
    assert not turn.wrote_anything

    tool_message = [m for m in model.seen[1] if m.get("role") == "tool"][0]
    assert "market cap" in json.loads(tool_message["content"])["message"]


# --- the deletion safeguard, which is the point -------------------------------


def test_the_agent_cannot_delete_in_one_turn():
    """Even a model that asks for confirmed on the first call is stopped.

    Not by the prompt. `delete_alert` without a person's answer in between is
    refused by the tool, and the refusal is what the model has to relay.
    """
    model = ScriptedModel(
        calls("delete_alert", {"alert_id": 4}),
        says("That would delete the PT alert above 20. Shall I?"),
    )
    store, assistant = build(
        model,
        rows={
            "SELECT * FROM iberian.alerts": [
                {"id": 4, "zone": "PT", "direction": "above", "threshold_eur_mwh": 20}
            ]
        },
    )
    turn = assistant.ask("delete alert 4")

    assert turn.results[0].status == "rejected"
    assert not [s for s, _ in store.statements if s.strip().startswith("DELETE")]
    assert "Shall I" in turn.reply


def test_a_deletion_goes_through_once_the_person_has_answered():
    model = ScriptedModel(
        calls("delete_alert", {"alert_id": 4, "confirmed": True}),
        says("Deleted."),
    )
    store, assistant = build(
        model,
        rows={
            "SELECT * FROM iberian.alerts": [
                {"id": 4, "zone": "PT", "direction": "above", "threshold_eur_mwh": 20}
            ],
            "DELETE FROM iberian.alerts": [{"id": 4}],
        },
    )
    turn = assistant.ask("yes, delete it", history=[{"role": "assistant", "content": "Shall I?"}])

    assert turn.results[0].ok
    assert [s for s, _ in store.statements if s.strip().startswith("DELETE")]


def test_the_deleted_row_is_not_fed_back_to_the_model():
    # Nothing good comes of the model quoting the primary key of something that
    # no longer exists.
    model = ScriptedModel(calls("delete_alert", {"alert_id": 4, "confirmed": True}), says("ok"))
    build(
        model,
        rows={
            "SELECT * FROM iberian.alerts": [
                {"id": 4, "zone": "PT", "direction": "above", "threshold_eur_mwh": 20}
            ],
            "DELETE FROM iberian.alerts": [{"id": 4}],
        },
    )[1].ask("yes")

    tool_message = [m for m in model.seen[1] if m.get("role") == "tool"][0]
    assert "saved" not in json.loads(tool_message["content"])


# --- labels -------------------------------------------------------------------


def test_a_label_submitted_through_the_agent_reaches_the_table():
    model = ScriptedModel(
        calls(
            "submit_episode_label",
            {
                "episode_key": "2026-08-18T0745",
                "true_cause": "saturation_ordinary_capacity",
                "confidence": "medium",
            },
        ),
        says("Recorded."),
    )
    store, assistant = build(
        model,
        rows={
            "FROM iberian.episodes WHERE": [EPISODE],
            "INSERT INTO iberian.episode_labels": [{"id": 9}],
        },
    )
    turn = assistant.ask("label that one as ordinary capacity, medium confidence")

    assert turn.results[0].ok
    assert [s for s, _ in store.statements if "INSERT INTO iberian.episode_labels" in s]


# --- when the model misbehaves ------------------------------------------------


def test_a_tool_that_does_not_exist_is_answered_rather_than_raised():
    model = ScriptedModel(calls("delete_everything", {}), says("I cannot do that."))
    _, assistant = build(model)
    turn = assistant.ask("delete everything")

    assert "no tool called" in json.loads(
        [m for m in model.seen[1] if m.get("role") == "tool"][0]["content"]
    )["error"]
    assert turn.reply == "I cannot do that."


def test_arguments_that_are_not_valid_json_are_reported_to_the_model():
    model = ScriptedModel(
        {
            "content": "",
            "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "create_alert", "arguments": "{zone: PT,"}}
            ],
        },
        says("I mangled that, sorry."),
    )
    _, assistant = build(model)
    turn = assistant.ask("alert me")

    assert turn.results[0]["error"].startswith("The arguments were not")


def test_a_model_that_never_stops_is_stopped():
    """A loop that keeps calling tools is paid for by the round. Five and out,
    and the turn says plainly that it did not finish."""
    model = ScriptedModel(*[calls("my_alerts", {}) for _ in range(10)])
    _, assistant = build(model)
    turn = assistant.ask("what are my alerts")

    assert turn.stopped_early
    assert turn.rounds == 5
    assert "did not manage to finish" in turn.reply


def test_what_was_written_before_giving_up_is_still_reported():
    # The write happened. Saying nothing about it would be the worse failure.
    model = ScriptedModel(
        calls("create_alert", {"zone": "PT", "direction": "above", "threshold_eur_mwh": 20}),
        *[calls("my_alerts", {}) for _ in range(10)],
    )
    _, assistant = build(model, rows={"INSERT INTO iberian.alerts": [{"id": 1}]})
    turn = assistant.ask("alert me")

    assert turn.stopped_early
    assert turn.wrote_anything


# --- history ------------------------------------------------------------------


def test_earlier_messages_are_replayed_so_a_confirmation_has_context():
    model = ScriptedModel(says("ok"))
    _, assistant = build(model)
    assistant.ask(
        "yes",
        history=[
            {"role": "user", "content": "delete alert 4"},
            {"role": "assistant", "content": "That would delete the PT alert. Shall I?"},
        ],
    )

    roles = [m["role"] for m in model.seen[0]]
    assert roles == ["system", "user", "assistant", "user"]