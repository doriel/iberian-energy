"""MLflow tracing, where MLflow is present, and nothing where it is not.

The rest of `iberian/` is plain Python with no platform imports, which is what
lets the test suite run in under two seconds on a laptop with no credentials.
Decorating the agent with `@mlflow.trace` directly would end that: every test
would need MLflow installed, and importing it costs more than the tests do.

So the decorator is resolved once, here. On Databricks it is the real one and
the agent's retrieval, generation and verification appear as nested spans in the
experiment. Anywhere else it is the identity function and the decorated code is
byte for byte what it was.

    from iberian.agent.tracing import trace, SpanType

    @trace(span_type=SpanType.RETRIEVER)
    def episode_facts(...): ...

The span types are the ones MLflow defines, restated as plain strings so this
module imports with or without MLflow and the names still read correctly in a
call site. MLflow accepts a string wherever it accepts a SpanType.
"""

from __future__ import annotations

from typing import Any, Callable


class SpanType:
    """MLflow's span types as literals, so call sites read the same either way."""

    AGENT = "AGENT"
    CHAIN = "CHAIN"
    CHAT_MODEL = "CHAT_MODEL"
    LLM = "LLM"
    PARSER = "PARSER"
    RERANKER = "RERANKER"
    RETRIEVER = "RETRIEVER"
    TOOL = "TOOL"
    UNKNOWN = "UNKNOWN"


def _identity_decorator(*_args: Any, **_kwargs: Any) -> Callable:
    """A `@trace(...)` that does nothing, and also a bare `@trace`."""

    # Supports both `@trace` and `@trace(span_type=...)`, because a decorator
    # that only works one of those two ways is a trap for whoever writes the
    # next call site.
    if len(_args) == 1 and not _kwargs and callable(_args[0]):
        return _args[0]

    def decorate(function: Callable) -> Callable:
        return function

    return decorate


def _resolve() -> tuple[Callable, bool]:
    try:
        import mlflow
    except Exception:
        return _identity_decorator, False
    return mlflow.trace, True


trace, TRACING_AVAILABLE = _resolve()
"""`trace` is `mlflow.trace` where MLflow is installed, otherwise a no-op."""


def describe() -> str:
    """One line for a script to print, so the state is never a guess."""
    return (
        "MLflow tracing active"
        if TRACING_AVAILABLE
        else "MLflow not installed, tracing disabled"
    )