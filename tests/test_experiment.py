"""The evaluation metrics, and the promise that recording never breaks a run.

`summarise` is the part worth testing for correctness: those four numbers are
what a future run gets compared against, and a rate computed over the wrong
denominator is the kind of error that survives for months because it still looks
plausible.

The rest is testing a promise rather than a calculation. A tracking server that
is down, or absent, must cost nothing: the numbers are the deliverable and the
recording is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.experiment import EvaluationRun, summarise  # noqa: E402


def record(key: str, grounded: bool, attempts: int = 1, claims: int = 3) -> dict:
    return {
        "episode_key": key,
        "grounded": grounded,
        "attempts": attempts,
        "numeric_claims": claims,
    }


# --- summarise --------------------------------------------------------------


def test_no_records_produce_no_metrics():
    # Rather than a division by zero, or worse a rate of 0.0 that reads as a
    # terrible run when nothing ran at all.
    assert summarise([]) == {}


def test_a_clean_run():
    metrics = summarise([record("a", True), record("b", True)])
    assert metrics["grounded_rate"] == 1.0
    assert metrics["first_attempt_rate"] == 1.0
    assert metrics["episodes"] == 2.0
    assert metrics["claims_per_explanation"] == 3.0


def test_a_retry_is_grounded_but_not_first_attempt():
    metrics = summarise([record("a", True, attempts=2), record("b", True)])
    assert metrics["grounded_rate"] == 1.0
    assert metrics["first_attempt_rate"] == 0.5


def test_a_rejection_lowers_the_rate():
    metrics = summarise([record("a", True), record("b", False, attempts=2)])
    assert metrics["grounded_rate"] == 0.5
    assert metrics["grounded"] == 1.0


def test_claims_per_explanation_counts_every_record():
    # Including the ungrounded one. A rejected draft still made claims, and
    # dividing only by the passing ones would flatter the number.
    metrics = summarise([record("a", True, claims=4), record("b", False, claims=0)])
    assert metrics["claims_per_explanation"] == 2.0
    assert metrics["numeric_claims"] == 4.0


def test_a_missing_claim_count_is_treated_as_none_rather_than_crashing():
    metrics = summarise([{"episode_key": "a", "grounded": True, "attempts": 1}])
    assert metrics["claims_per_explanation"] == 0.0


# --- the run itself ---------------------------------------------------------


class BrokenMlflow:
    """Everything raises, which is what an unreachable tracking server does."""

    def set_experiment(self, *_a, **_k):
        raise RuntimeError("tracking server unreachable")

    def start_run(self, *_a, **_k):
        raise RuntimeError("tracking server unreachable")

    def log_params(self, *_a, **_k):
        raise RuntimeError("tracking server unreachable")


def test_a_broken_tracking_server_does_not_raise(monkeypatch, capsys):
    run = EvaluationRun(endpoint="e", attempts=2)
    monkeypatch.setattr(run, "mlflow", BrokenMlflow())
    with run as active:
        active.record([record("a", True)])
    assert run.active is False
    assert "MLflow run not started" in capsys.readouterr().out


def test_an_absent_mlflow_is_reported_rather_than_assumed(monkeypatch):
    run = EvaluationRun(endpoint="e", attempts=2)
    monkeypatch.setattr(run, "mlflow", None)
    assert run.available is False
    assert "not installed" in run.describe()
    with run as active:
        active.record([record("a", True)])  # must not raise


class RecordingMlflow:
    def __init__(self) -> None:
        self.params: dict = {}
        self.metrics: dict = {}
        self.tags: dict = {}
        self.artifacts: list = []
        self.ended = False

    def set_experiment(self, name):
        self.experiment = name

    def start_run(self):
        pass

    def log_params(self, params):
        self.params.update(params)

    def log_metrics(self, metrics):
        self.metrics.update(metrics)

    def log_artifact(self, path):
        self.artifacts.append(path)

    def set_tag(self, key, value):
        self.tags[key] = value

    def end_run(self):
        self.ended = True


def test_a_working_run_logs_params_metrics_and_the_failures_tag(monkeypatch, tmp_path):
    fake = RecordingMlflow()
    artifact = tmp_path / "explanations.jsonl"
    artifact.write_text("{}\n")

    run = EvaluationRun(endpoint="haiku", attempts=2, extra_params={"episodes": 2})
    monkeypatch.setattr(run, "mlflow", fake)
    with run as active:
        active.record([record("a", True), record("b", False)], artifact=artifact)

    assert fake.params["endpoint"] == "haiku"
    assert fake.params["max_attempts"] == 2
    assert fake.metrics["grounded_rate"] == 0.5
    assert fake.tags["failures"] == "b"
    assert fake.artifacts == [str(artifact)]
    assert fake.ended is True


def test_a_clean_run_tags_failures_as_none(monkeypatch):
    fake = RecordingMlflow()
    run = EvaluationRun(endpoint="haiku", attempts=2)
    monkeypatch.setattr(run, "mlflow", fake)
    with run as active:
        active.record([record("a", True)])
    assert fake.tags["failures"] == "none"


# --- Unity Catalog trace storage --------------------------------------------


def test_unity_catalog_needs_all_three_values(monkeypatch):
    # `mlflow` is stubbed rather than required: what is under test is the
    # storage decision, and the suite has to give the same answer on a laptop
    # that never installed MLflow as it does in CI, which does.
    monkeypatch.delenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID", raising=False)

    partial = EvaluationRun(
        endpoint="e", attempts=2, trace_catalog="c", trace_schema="s"
    )
    monkeypatch.setattr(partial, "mlflow", RecordingMlflow())
    assert partial.unity_catalog is False
    assert "experiment's own storage" in partial.describe()

    complete = EvaluationRun(
        endpoint="e",
        attempts=2,
        trace_catalog="c",
        trace_schema="s",
        warehouse_id="w",
    )
    monkeypatch.setattr(complete, "mlflow", RecordingMlflow())
    assert complete.unity_catalog is True
    assert "Unity Catalog, c.s" in complete.describe()


def test_the_warehouse_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID", "from-env")
    run = EvaluationRun(endpoint="e", attempts=2, trace_catalog="c", trace_schema="s")
    assert run.warehouse_id == "from-env"
    assert run.unity_catalog is True


def test_the_storage_choice_is_logged_as_a_parameter(monkeypatch):
    fake = RecordingMlflow()
    run = EvaluationRun(
        endpoint="e",
        attempts=2,
        trace_catalog="bootcamp_students",
        trace_schema="doriel",
        warehouse_id="w",
    )
    monkeypatch.setattr(run, "mlflow", fake)
    # _set_experiment would import mlflow.entities for real, so stub the binding
    # itself: what is under test is that the parameter records the choice.
    monkeypatch.setattr(run, "_set_experiment", lambda: None)
    with run:
        pass
    assert fake.params["trace_storage"] == "uc:bootcamp_students.doriel"


def test_a_failed_unity_catalog_binding_records_nothing_and_explains(monkeypatch, capsys):
    run = EvaluationRun(
        endpoint="e", attempts=2, trace_catalog="c", trace_schema="s", warehouse_id="w"
    )
    monkeypatch.setattr(run, "mlflow", RecordingMlflow())
    monkeypatch.setattr(
        run, "_set_experiment", lambda: (_ for _ in ()).throw(PermissionError("denied"))
    )
    with run as active:
        active.record([record("a", True)])
    assert run.active is False
    out = capsys.readouterr().out
    assert "Unity Catalog trace storage was requested" in out
    assert "CREATE TABLE" in out