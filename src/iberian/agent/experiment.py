"""Record an evaluation run as an MLflow experiment, where MLflow is present.

The evaluation has always produced the right numbers and put them in a JSONL
file next to the code. That is fine for one run and useless for comparing six:
which endpoint, how many attempts, what changed in the verifier between Tuesday
and Thursday. Those questions are what an experiment tracker answers, and
answering them from a directory of files is a job nobody does twice.

Like `tracing`, this degrades to nothing rather than becoming a dependency. A
laptop without MLflow runs the evaluation exactly as before and says so.

    from iberian.agent.experiment import EvaluationRun

    with EvaluationRun(endpoint="databricks-claude-haiku-4-5", attempts=2) as run:
        ...
        run.record(records)

What gets logged is deliberately small. Parameters are the things that change
the answer, metrics are the north star and the two numbers that explain it, and
the artifact is the record file itself, because a metric without the drafts
behind it cannot be argued with.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

#: The experiment every run of this project lands in unless told otherwise.
#: A path rather than a name, because that is what a Databricks workspace wants
#: and a local MLflow accepts.
DEFAULT_EXPERIMENT = "/Shared/iberian-energy-agent"


def _mlflow():
    try:
        import mlflow
    except Exception:
        return None
    return mlflow


def summarise(records: Sequence[dict]) -> dict[str, float]:
    """The numbers worth comparing across runs, from the evaluation records."""
    total = len(records)
    if not total:
        return {}
    grounded = sum(1 for r in records if r.get("grounded"))
    first_try = sum(1 for r in records if r.get("grounded") and r.get("attempts") == 1)
    claims = sum(int(r.get("numeric_claims") or 0) for r in records)
    return {
        # The north star, as a share rather than a count, so runs over different
        # numbers of episodes stay comparable.
        "grounded_rate": grounded / total,
        "grounded": float(grounded),
        "episodes": float(total),
        # A model that needs a retry is not ungrounded, but it is worse, and the
        # distinction disappears if only the final verdict is kept.
        "first_attempt_rate": first_try / total,
        # Claims per explanation says how much the verifier actually checked. A
        # high grounded rate over prose containing no figures would be vacuous,
        # and this is the number that would expose it.
        "claims_per_explanation": claims / total,
        "numeric_claims": float(claims),
    }


class EvaluationRun:
    """A context manager that is an MLflow run, or politely nothing."""

    def __init__(
        self,
        endpoint: str,
        attempts: int,
        experiment: str = DEFAULT_EXPERIMENT,
        extra_params: dict[str, Any] | None = None,
        tracking_uri: str | None = None,
        trace_catalog: str | None = None,
        trace_schema: str | None = None,
        warehouse_id: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.attempts = attempts
        self.experiment = experiment
        self.extra_params = extra_params or {}
        self.tracking_uri = tracking_uri
        self.trace_catalog = trace_catalog
        self.trace_schema = trace_schema
        self.warehouse_id = warehouse_id or os.environ.get(
            "MLFLOW_TRACING_SQL_WAREHOUSE_ID"
        )
        self.mlflow = _mlflow()
        self.active = False

    @property
    def available(self) -> bool:
        return self.mlflow is not None

    @property
    def unity_catalog(self) -> bool:
        """Whether this run has everything it needs to store traces in UC."""
        return bool(self.trace_catalog and self.trace_schema and self.warehouse_id)

    def describe(self) -> str:
        if not self.available:
            return "MLflow not installed, this run is not being recorded"
        where = (
            f"Unity Catalog, {self.trace_catalog}.{self.trace_schema}"
            if self.unity_catalog
            else "the experiment's own storage"
        )
        return f"Recording to {self.experiment}, traces in {where}"

    def _set_experiment(self) -> None:
        """Bind the experiment, to Unity Catalog where that was asked for.

        A UC trace location is permanent: once an experiment is bound it cannot
        be pointed elsewhere. So when UC was asked for and cannot be had, this
        run records nothing and says why. It never quietly binds to the legacy
        store instead, which would leave somebody believing the traces are in
        Delta when they are not, and would burn the experiment name doing it.
        """
        if not self.unity_catalog:
            self.mlflow.set_experiment(self.experiment)
            return

        from mlflow.entities.trace_location import UnityCatalog

        os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] = self.warehouse_id
        self.mlflow.set_experiment(
            experiment_name=self.experiment,
            trace_location=UnityCatalog(
                catalog_name=self.trace_catalog,
                schema_name=self.trace_schema,
            ),
        )

    def __enter__(self) -> "EvaluationRun":
        if not self.available:
            return self
        try:
            if self.tracking_uri:
                self.mlflow.set_tracking_uri(self.tracking_uri)
            self._set_experiment()
            self.mlflow.start_run()
            self.mlflow.log_params(
                {
                    "endpoint": self.endpoint,
                    "max_attempts": self.attempts,
                    "trace_storage": (
                        f"uc:{self.trace_catalog}.{self.trace_schema}"
                        if self.unity_catalog
                        else "experiment"
                    ),
                    **self.extra_params,
                }
            )
            self.active = True
        except Exception as exc:
            # A tracking server that is unreachable must not take the evaluation
            # with it. The numbers are the deliverable; the recording is not.
            print(f"  MLflow run not started ({type(exc).__name__}: {exc})")
            if self.unity_catalog:
                print(
                    "  Unity Catalog trace storage was requested and not"
                    " established, so nothing was recorded. Check CREATE TABLE on"
                    f" {self.trace_catalog}.{self.trace_schema}, that warehouse"
                    f" {self.warehouse_id} is reachable, and whether this"
                    " experiment is already bound to a different location."
                )
            self.active = False
        return self

    def record(self, records: Sequence[dict], artifact: Path | None = None) -> None:
        if not self.active:
            return
        try:
            metrics = summarise(records)
            if metrics:
                self.mlflow.log_metrics(metrics)
            if artifact and Path(artifact).exists():
                self.mlflow.log_artifact(str(artifact))
            # One tag per failing episode would be noise; the count is the thing
            # a run list should show without opening anything.
            failures = [r["episode_key"] for r in records if not r.get("grounded")]
            self.mlflow.set_tag("failures", ", ".join(failures) if failures else "none")
        except Exception as exc:
            print(f"  MLflow logging failed ({type(exc).__name__}: {exc})")

    def __exit__(self, *exc_info) -> None:
        if self.active:
            try:
                self.mlflow.end_run()
            except Exception:
                pass
        self.active = False