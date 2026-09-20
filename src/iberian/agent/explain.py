"""Turn a fact sheet into prose, and refuse to return prose that fails the check.

The model is given the retrieved facts and asked to phrase them. It is not
given the data, it is not asked to compute, and it is not trusted. What comes
back goes through `agent.verify` before anyone sees it, and an explanation
containing a number that was not retrieved is not returned at all.

One retry is allowed, and the retry is told exactly which figure was rejected.
That is worth having because the common failure is a model rounding or
combining values rather than fabricating them, and naming the offending number
fixes it. A second failure is reported as a failure. Silently returning
unverified text would defeat the point of building the check.

Transport is injected as a `complete(system, user) -> str` callable, so the
logic here is tested without a network and the Databricks specifics live in
one small function at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iberian.agent.facts import FactSheet
from iberian.agent.tracing import SpanType, trace
from iberian.agent.verify import Verdict, verify

SYSTEM_PROMPT = """\
You explain movements in the Iberian electricity market to a professional \
audience: an industrial energy buyer, a journalist, or a grid analyst.

You are given a list of facts retrieved from published market documents. Those \
facts are the only information you have and the only information you may use.

Rules, in order of importance:

1. Every number in your answer must appear in the facts you were given. Do not \
calculate, do not sum, do not average, do not convert units, and do not \
estimate. If a number you want is not in the list, do not write it.
2. Name the document behind each figure, using the source given with it, for \
example "ENTSO-E A44 day-ahead". A reader must be able to check you.
3. Include every caveat listed. They are there because the evidence does not \
support the stronger claim a reader would otherwise infer.
4. Do not assert that one thing caused another unless the facts say so. A full \
border and a price separation happening together is consistency, not proof.
5. If the facts do not explain the event, say that plainly. "The published \
notices do not account for this" is a better answer than a confident guess.

Write three to five sentences of plain prose. No headings, no bullet points, \
no preamble such as "Based on the facts provided". Start with what happened.\
"""

RETRY_SUFFIX = """\

Your previous answer was rejected because it contained {count} figure(s) that \
do not appear in the facts: {offending}. Those numbers were not retrieved from \
any document, so they cannot be used. Rewrite the explanation using only \
figures from the list, or leave the point out.\
"""

SOURCE_SUFFIX = """\

Your previous answer was rejected because it named no source. Every figure \
must be attributed to the document it came from.\
"""

DATE_SUFFIX = """\

Your previous answer was rejected because it stated {count} date(s) that do \
not appear in the facts: {offending}. The window and every publication date \
are in the list you were given. Use those exactly, or write no date at all.\
"""

EMPTY_SUFFIX = """\

Your previous answer was empty. Write the explanation.\
"""


@dataclass(frozen=True)
class Explanation:
    """What the agent produced, and whether it survived the check."""

    text: str
    verdict: Verdict
    attempts: int
    model: str = ""
    rejected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict.ok

    def render(self) -> str:
        head = self.text if self.ok else "REJECTED, not shown to a reader."
        lines = [head, "", f"  {self.verdict.describe()}", f"  attempts: {self.attempts}"]
        if self.model:
            lines.append(f"  model: {self.model}")
        if self.rejected:
            lines.append(f"  earlier drafts rejected: {len(self.rejected)}")
        return "\n".join(lines)


@trace(span_type=SpanType.AGENT)
def explain(
    sheet: FactSheet,
    complete,
    max_attempts: int = 2,
    model: str = "",
) -> Explanation:
    """Ask for an explanation, verify it, and allow one corrected retry."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    user = sheet.render()
    rejected: list[str] = []
    verdict = Verdict(ok=False)
    text = ""

    for attempt in range(1, max_attempts + 1):
        system = SYSTEM_PROMPT
        if attempt > 1:
            if not text:
                system += EMPTY_SUFFIX
            elif verdict.unsupported:
                system += RETRY_SUFFIX.format(
                    count=len(verdict.unsupported),
                    offending=", ".join(claim.text for claim in verdict.unsupported),
                )
            elif verdict.wrong_dates:
                system += DATE_SUFFIX.format(
                    count=len(verdict.wrong_dates),
                    offending=", ".join(claim.text for claim in verdict.wrong_dates),
                )
            elif verdict.missing_sources:
                system += SOURCE_SUFFIX

        text = (complete(system, user) or "").strip()

        # An empty answer invents nothing, so the numeric check passes it. That
        # is a vacuous pass: the task is to produce an explanation, and silence
        # is not one. Caught here rather than in verify, whose single question
        # is whether the figures in a text were retrieved.
        verdict = verify(text, sheet) if text else Verdict(ok=False)

        if verdict.ok:
            return Explanation(
                text=text,
                verdict=verdict,
                attempts=attempt,
                model=model,
                rejected=rejected,
            )
        rejected.append(text)

    return Explanation(
        text=text,
        verdict=verdict,
        attempts=max_attempts,
        model=model,
        rejected=rejected[:-1],
    )


def databricks_completer(
    endpoint: str = "databricks-claude-haiku-4-5",
    max_tokens: int = 400,
    temperature: float = 0.0,
    profile: str | None = None,
):
    """A `complete` backed by a Databricks Foundation Model endpoint.

    Temperature is zero because the task is to restate retrieved facts, and
    there is nothing here that creativity improves. A deterministic setting
    also makes a failed verification reproducible, which matters when the
    evaluation has to be repeatable.

    The endpoint defaults to a small model on purpose. If the grounding does
    the work, a small model should pass the check as often as a large one, and
    whether it does is a result worth reporting rather than an assumption.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    client = WorkspaceClient(profile=profile) if profile else WorkspaceClient()

    def complete(system: str, user: str) -> str:
        response = client.serving_endpoints.query(
            name=endpoint,
            messages=[
                ChatMessage(role=ChatMessageRole.SYSTEM, content=system),
                ChatMessage(role=ChatMessageRole.USER, content=user),
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    return complete