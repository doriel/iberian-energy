"""The tracing shim has to be invisible, and that is worth a test.

Its whole job is to change nothing about the decorated function. A shim that
quietly drops a return value or breaks on a keyword argument would be found on
Databricks, in an agent run, which is the worst place to find it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent import tracing  # noqa: E402


# --- the no-op decorator ----------------------------------------------------


def test_the_bare_form_returns_the_function_unchanged():
    def original(a, b=2):
        return a + b

    decorated = tracing._identity_decorator(original)
    assert decorated is original
    assert decorated(1, b=3) == 4


def test_the_called_form_preserves_behaviour():
    @tracing._identity_decorator(span_type="AGENT", name="x")
    def add(a, *rest, **keywords):
        return (a, rest, keywords)

    assert add(1, 2, three=3) == (1, (2,), {"three": 3})


def test_an_exception_is_not_swallowed():
    @tracing._identity_decorator(span_type="AGENT")
    def boom():
        raise ValueError("expected")

    with pytest.raises(ValueError, match="expected"):
        boom()


# --- resolution -------------------------------------------------------------


def test_mlflow_absent_means_a_no_op(monkeypatch):
    # None in sys.modules makes `import mlflow` raise ImportError, which is the
    # state this shim exists for and is cheaper to arrange than uninstalling it.
    monkeypatch.setitem(sys.modules, "mlflow", None)
    decorator, available = tracing._resolve()
    assert available is False
    assert decorator is tracing._identity_decorator


def test_mlflow_present_means_its_decorator(monkeypatch):
    class FakeMlflow:
        def trace(self, *args, **kwargs):  # pragma: no cover - identity only
            return lambda f: f

    fake = FakeMlflow()
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    decorator, available = tracing._resolve()
    assert available is True
    assert decorator == fake.trace


# --- the span type names ----------------------------------------------------


def test_the_span_types_used_by_this_project_exist():
    # Named rather than looped, so removing one is a failing test rather than a
    # silently shorter loop.
    assert tracing.SpanType.AGENT == "AGENT"
    assert tracing.SpanType.RETRIEVER == "RETRIEVER"
    assert tracing.SpanType.PARSER == "PARSER"
    assert tracing.SpanType.LLM == "LLM"


def test_describe_says_which_state_it_is_in():
    assert "tracing" in tracing.describe()


# --- against the real MLflow, when it is installed --------------------------

mlflow = pytest.importorskip("mlflow", reason="tracing degrades to a no-op without it")


def test_the_real_decorator_preserves_the_return_value():
    """The shim's whole promise, checked against the library rather than a fake.

    This is the test that fails when an MLflow release changes how `trace`
    wraps a function. Without it the first sign would be an agent run on
    Databricks returning something subtly different from what it returned here.
    """

    @tracing.trace(span_type=tracing.SpanType.AGENT)
    def add(a, b=2, *, c=0):
        return {"total": a + b + c}

    assert add(1, 2, c=3) == {"total": 6}


def test_the_real_decorator_lets_an_exception_through():
    @tracing.trace(span_type=tracing.SpanType.PARSER)
    def boom():
        raise ValueError("expected")

    with pytest.raises(ValueError, match="expected"):
        boom()


def test_the_span_type_strings_are_accepted_by_mlflow():
    # A string is documented as acceptable wherever a SpanType is, and this
    # project passes strings so the module imports without MLflow. If that ever
    # stops being true, it stops here rather than in a trace that is silently
    # typed UNKNOWN.
    from mlflow.entities import SpanType as MlflowSpanType

    for name in ("AGENT", "RETRIEVER", "PARSER", "LLM"):
        assert getattr(MlflowSpanType, name) == getattr(tracing.SpanType, name)