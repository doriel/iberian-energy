"""The explanation agent, as an MLflow ResponsesAgent.

This is the model code MLflow loads. It is a file rather than a pickled object
because models-from-code is what Databricks recommends, and because a file can
be read by whoever has to trust what the served endpoint does.

It lives outside `src/iberian/` on purpose. The library has no platform imports,
which is what keeps its test suite at two seconds on a laptop with no
credentials. This module is the platform integration, so it sits with the other
platform code and imports the library rather than the other way round.

What it does is exactly what `scripts/explain_episodes.py` does for one episode:

    fact sheet  ->  generate  ->  verify  ->  return, or refuse

The model never sees market data. It sees a sheet of retrieved facts, each
carrying the document it came from, and every figure it writes is matched back
against that sheet before the response leaves this process. An explanation
containing a number that was not retrieved is not returned.

Request shape:

    {
      "input": [{"role": "user", "content": "explain this episode"}],
      "custom_inputs": {
        "fact_sheet": { ... FactSheet.to_dict() ... },
        "endpoint": "databricks-claude-haiku-4-5",   # optional
        "max_attempts": 2                             # optional
      }
    }

The response carries the prose, and `custom_outputs` carries the verdict, so a
caller can tell a grounded explanation from a refusal without parsing English.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import mlflow
from mlflow.entities import SpanType
from mlflow.models import set_model
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

# The library ships beside this file when logged, via code_paths. Locally it is
# one level up. Both are tried rather than assumed, because guessing where a
# checkout lives has already cost this project three failed Job runs.
for candidate in (Path(__file__).resolve().parent, Path(__file__).resolve().parents[1]):
    source = candidate / "src"
    if (source / "iberian").is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))

from iberian.agent.explain import databricks_completer, explain  # noqa: E402
from iberian.agent.facts import FactSheet  # noqa: E402

DEFAULT_ENDPOINT = "databricks-claude-haiku-4-5"
DEFAULT_ATTEMPTS = 2

#: What a caller gets when the explanation did not survive the check. Deliberately
#: not an empty string and not an apology: it says what happened and that the
#: numbers are the reason, so a UI can show it without dressing it up.
REFUSAL = (
    "No explanation is available for this episode. A draft was produced and "
    "rejected because it contained a figure that was not in the retrieved "
    "evidence. Showing it would defeat the purpose of retrieving anything."
)


@mlflow.trace(span_type=SpanType.RETRIEVER)
def resolve_fact_sheet(custom_inputs: dict[str, Any]) -> FactSheet:
    """Get the evidence this request is about.

    **This function is the seam.** Today the caller assembles the sheet from the
    gold tables and sends it, because the gold tables do not exist inside a
    serving container and reaching them from one runs into the same workspace
    permissions that blocked the Lakebase read models.

    The alternative, where the agent receives an episode key and fetches the
    facts itself, changes this function and nothing else: generation,
    verification, the ResponsesAgent contract and the registered model are all
    identical either way. When that access exists, add the branch here.
    """
    raw = custom_inputs.get("fact_sheet")
    if not raw:
        raise ValueError(
            "custom_inputs.fact_sheet is required. The caller assembles it from "
            "the gold tables with iberian.agent.facts.episode_facts and sends "
            "FactSheet.to_dict(). This agent does not read the lakehouse."
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"custom_inputs.fact_sheet must be an object, got {type(raw).__name__}"
        )
    return FactSheet.from_dict(raw)


class MibelExplanationAgent(ResponsesAgent):
    """Explains one market splitting episode, or declines to."""

    def __init__(self, completer_factory=databricks_completer) -> None:
        # Injected so a test can drive this without a serving endpoint, and so
        # the endpoint stays a request-time choice rather than being baked into
        # the registered model.
        self._completer_factory = completer_factory
        self._completers: dict[str, Any] = {}

    def _completer(self, endpoint: str):
        # Cached per endpoint: building one opens a client, and a served model
        # answers many requests.
        if endpoint not in self._completers:
            self._completers[endpoint] = self._completer_factory(endpoint=endpoint)
        return self._completers[endpoint]

    # No @mlflow.trace here: ResponsesAgent traces predict itself, and adding
    # one makes MLflow warn about the duplicate. resolve_fact_sheet above is
    # decorated because nothing traces it for us.
    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        custom = dict(request.custom_inputs or {})
        endpoint = str(
            custom.get("endpoint") or os.environ.get("IBERIAN_ENDPOINT") or DEFAULT_ENDPOINT
        )
        attempts = int(custom.get("max_attempts") or DEFAULT_ATTEMPTS)

        sheet = resolve_fact_sheet(custom)
        result = explain(
            sheet, self._completer(endpoint), max_attempts=attempts, model=endpoint
        )

        text = result.text if result.ok else REFUSAL
        return ResponsesAgentResponse(
            output=[
                self.create_text_output_item(text=text, id=f"msg_{uuid.uuid4().hex[:12]}")
            ],
            # The verdict travels as data. A caller that has to read the prose to
            # find out whether it was verified has no way to act on the answer.
            custom_outputs={
                "grounded": result.ok,
                "attempts": result.attempts,
                "model": endpoint,
                "numeric_claims": len(result.verdict.claims),
                "unsupported": [claim.text for claim in result.verdict.unsupported],
                "wrong_dates": [claim.text for claim in result.verdict.wrong_dates],
                "missing_sources": result.verdict.missing_sources,
                "sources": sheet.sources(),
                "subject": sheet.subject,
            },
        )


set_model(MibelExplanationAgent())