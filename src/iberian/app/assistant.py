"""The agent that reads, and that acts.

The explanation agent elsewhere in this package does one thing: it is handed a
fact sheet and it writes prose about it, never choosing what to look at. This
one chooses. It is given tools, it decides which to call, and some of those
tools change the application's data.

That difference is the whole reason this file exists, and it is where the risk
moves. An agent that only writes prose can be wrong. An agent that can write to
a database can be wrong in a way somebody has to undo.

Three rules hold it in place, and none of them is a sentence in a prompt:

**The tools validate, not the prompt.** Every argument is checked in
`actions.py` against the same rules the schema enforces. A model that invents a
threshold of forty thousand gets a rejection it can read out, not a stored row.

**Deleting needs a second turn.** The agent cannot pass `confirmed=True` on the
same call that proposes a deletion, because the first call returns a refusal
describing what would go, and only a person answering can produce the turn that
sets it. The model cannot talk itself into it.

**Numbers come from tools.** The system prompt says so, but the guarantee is
that the interface renders figures from the tool results rather than from the
model's sentence. Prose that disagrees with the rows beside it is visibly wrong.

The model is reached through a `chat` callable rather than an SDK client, so the
loop can be driven in a test by a scripted sequence of replies. The sequences
that matter, a rejected write, a refused deletion, a model that loops, are all
awkward to produce against a live endpoint and trivial against a script.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from iberian.app.actions import CAUSES, CONFIDENCES, DIRECTIONS, ZONES, Actions

SYSTEM_PROMPT = """You help people understand the Iberian electricity market, \
where Portugal and Spain share a market that splits apart when the \
interconnection between them fills up and Portugal pays more.

You have tools. Use them.

Never state a number that did not come from a tool result. If you do not have a \
figure, say so and offer to look it up. You have no knowledge of current prices \
or episodes beyond what the tools return.

When a tool refuses something, say plainly what it refused and why, in the words \
the tool used. Do not retry it with different values hoping it passes, and do not \
apologise at length.

Deleting is permanent. When a deletion is refused pending confirmation, relay \
exactly what would be deleted and wait. Only call it again as confirmed when the \
person has said yes in a later message.

When you judge the cause of an episode, you may only use these causes: {causes}. \
Explain which the evidence supports rather than choosing for the person: the \
label is their judgement, not yours.

Be brief. A person reading this has a dashboard in front of them."""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


#: What the model is allowed to do. The descriptions are written for the model
#: rather than for a developer: they say when to reach for the tool, because
#: that is the decision it is actually making.
TOOLS: list[dict] = [
    _tool(
        "list_episodes",
        "Market splitting episodes, worst first. Use this before anything that "
        "needs an episode key. Set unlabelled_only when the person wants "
        "something to judge that nobody has judged yet.",
        {
            "limit": {"type": "integer", "description": "How many, at most 100."},
            "unlabelled_only": {
                "type": "boolean",
                "description": "Only episodes this person has not labelled.",
            },
        },
        [],
    ),
    _tool(
        "my_alerts",
        "The price alerts this person has created.",
        {},
        [],
    ),
    _tool(
        "my_labels",
        "The episode causes this person has judged.",
        {},
        [],
    ),
    _tool(
        "create_alert",
        "Save a price alert, so the person is told when a zone's price crosses a "
        "threshold. Use it when somebody asks to be notified or warned about a price.",
        {
            "zone": {"type": "string", "enum": list(ZONES)},
            "direction": {"type": "string", "enum": list(DIRECTIONS)},
            "threshold_eur_mwh": {
                "type": "number",
                "description": "EUR per MWh, above 0 and at most 3000.",
            },
        },
        ["zone", "direction", "threshold_eur_mwh"],
    ),
    _tool(
        "update_alert",
        "Change an existing alert's threshold, or switch it on or off.",
        {
            "alert_id": {"type": "integer"},
            "threshold_eur_mwh": {"type": "number"},
            "active": {"type": "boolean"},
        },
        ["alert_id"],
    ),
    _tool(
        "delete_alert",
        "Delete an alert permanently. Call it first without confirmed to see what "
        "would go, tell the person, and only call it again with confirmed true "
        "after they have said yes.",
        {
            "alert_id": {"type": "integer"},
            "confirmed": {
                "type": "boolean",
                "description": "True only when the person has just agreed.",
            },
        },
        ["alert_id"],
    ),
    _tool(
        "submit_episode_label",
        "Record the person's judgement of what caused an episode. This is "
        "evaluation ground truth, so record what they decided, never your own view.",
        {
            "episode_key": {"type": "string"},
            "true_cause": {"type": "string", "enum": list(CAUSES)},
            "confidence": {"type": "string", "enum": list(CONFIDENCES)},
            "notes": {"type": "string"},
        },
        ["episode_key", "true_cause", "confidence"],
    ),
]

#: Enough rounds for look up, act, and report. A model that has not finished by
#: then is looping, and the honest thing is to stop and say so rather than to
#: keep paying for it.
MAX_ROUNDS = 5


@dataclass
class Turn:
    """One exchange, and everything the interface needs to render it."""

    reply: str
    results: list[Any] = field(default_factory=list)
    rounds: int = 0
    stopped_early: bool = False

    @property
    def wrote_anything(self) -> bool:
        return any(getattr(result, "ok", False) for result in self.results)


class Assistant:
    """The tool calling loop, with the model behind a plain callable."""

    #: Reads are listed here so the loop knows which results are rows to show
    #: rather than actions to report.
    READS = {"list_episodes", "my_alerts", "my_labels"}

    def __init__(
        self,
        actions: Actions,
        chat: Callable[[list[dict], list[dict]], dict],
        max_rounds: int = MAX_ROUNDS,
    ) -> None:
        self.actions = actions
        self.chat = chat
        self.max_rounds = max_rounds

    # --- dispatch ------------------------------------------------------------

    def _call(self, name: str, arguments: dict) -> Any:
        handler = {
            "list_episodes": self.actions.list_episodes,
            "my_alerts": self.actions.my_alerts,
            "my_labels": self.actions.my_labels,
            "create_alert": self.actions.create_alert,
            "update_alert": self.actions.update_alert,
            "delete_alert": self.actions.delete_alert,
            "submit_episode_label": self.actions.submit_episode_label,
        }.get(name)
        if handler is None:
            # A model asking for a tool that does not exist is told so, rather
            # than the loop breaking. It usually recovers on the next round.
            return {"error": f"There is no tool called {name}."}
        return handler(**arguments)

    @staticmethod
    def _as_text(name: str, result: Any) -> str:
        """What goes back to the model as the tool's result.

        An ActionResult becomes its status and its message, not its row: the
        model does not need the row to answer, and feeding it back invites it to
        quote a primary key at somebody.
        """
        if hasattr(result, "status"):
            payload = {"status": result.status, "message": result.message}
            if result.row and name != "delete_alert":
                payload["saved"] = {
                    key: str(value)
                    for key, value in result.row.items()
                    if key in {"id", "zone", "direction", "threshold_eur_mwh",
                               "episode_key", "true_cause", "confidence", "active"}
                }
            return json.dumps(payload, default=str)
        return json.dumps(result, default=str)[:4000]

    # --- the loop ------------------------------------------------------------

    def ask(self, question: str, history: list[dict] | None = None) -> Turn:
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(causes=", ".join(CAUSES))}
        ]
        messages.extend(history or [])
        messages.append({"role": "user", "content": question})

        results: list[Any] = []

        for round_number in range(1, self.max_rounds + 1):
            reply = self.chat(messages, TOOLS)
            calls = reply.get("tool_calls") or []

            if not calls:
                return Turn(
                    reply=(reply.get("content") or "").strip(),
                    results=results,
                    rounds=round_number,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.get("content") or "",
                    "tool_calls": calls,
                }
            )

            for call in calls:
                function = call.get("function", {})
                name = function.get("name", "")
                raw = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except json.JSONDecodeError:
                    # Malformed arguments are the model's mistake, and telling it
                    # so is more useful than failing the turn.
                    outcome: Any = {"error": "The arguments were not valid JSON."}
                else:
                    outcome = self._call(name, arguments)

                results.append(outcome)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": self._as_text(name, outcome),
                    }
                )

        # Out of rounds. Whatever was written has been written and is in
        # `results`, so the interface can still show it: the failure is that the
        # model never summarised, not that nothing happened.
        return Turn(
            reply=(
                "I did not manage to finish that. What I did is listed below, "
                "and it is worth checking before asking again."
            ),
            results=results,
            rounds=self.max_rounds,
            stopped_early=True,
        )


def databricks_chat(
    endpoint: str = "databricks-claude-haiku-4-5",
    max_tokens: int = 700,
    temperature: float = 0.0,
) -> Callable[[list[dict], list[dict]], dict]:
    """A `chat` backed by a Databricks serving endpoint.

    Raw HTTP against the OpenAI compatible invocations route rather than the
    SDK's typed client. Function calling on these endpoints is documented as
    OpenAI compatible, and the JSON shape for tool calls is stable across SDK
    versions in a way the dataclasses are not. The SDK is still what provides
    the host and the authentication headers, so the service principal
    configuration has one source.

    That client comes from `workspace.py`, the same one Lakebase uses. An
    application that reaches its database as itself and its model endpoint as
    whoever deployed it would work until the day somebody else deploys it.

    Temperature at zero: this agent chooses tools and restates their results,
    and there is nothing here that sampling improves.
    """

    def chat(messages: list[dict], tools: list[dict]) -> dict:
        import requests

        from iberian.app.workspace import workspace_client

        workspace = workspace_client()
        headers = {"Content-Type": "application/json"}
        headers.update(workspace.config.authenticate())

        response = requests.post(
            f"{workspace.config.host}/serving-endpoints/{endpoint}/invocations",
            headers=headers,
            json={
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=60,
        )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        return choices[0].get("message", {}) if choices else {}

    return chat