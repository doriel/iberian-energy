"""Log the explanation agent and register it in Unity Catalog.

    python scripts/register_agent.py
    python scripts/register_agent.py --episode 2026-08-18T0745

Three steps, and the third is the one that matters.

1. Build a real fact sheet for one episode, the same way the Job does, so the
   input example is evidence rather than a made-up shape.
2. Log `agents/mibel_agent.py` as a models-from-code `ResponsesAgent`, with the
   library packaged beside it, and register it as
   `bootcamp_students.doriel.mibel_agent`.
3. Load the registered model back in a separate process, from a directory that
   is not this repository, and ask it for one explanation.

Step 3 is separate on purpose. This script imports the library to build the
fact sheet, so in this process `import iberian` would find the repository copy
and a model that was packaged without its code would still appear to work. The
child process has not imported anything, and it prints where `iberian` was
loaded from. A path inside the model's own `code/` directory is the evidence
that the packaging is right. A path inside the repository means the check
proved less than it looks, and the output says so.

Registering is not deploying. Serving this model behind an endpoint is a
separate step with its own permissions, and it is not attempted here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iberian.agent.batch import EvidenceUnavailable, episode_key, sheet_builder  # noqa: E402
from iberian.agent.experiment import DEFAULT_EXPERIMENT  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_outages_response,
)

DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)
DEFAULT_MODEL = "bootcamp_students.doriel.mibel_agent"

#: Run in the child process. Kept as a string so the child imports nothing
#: from this repository before the model is loaded.
VALIDATE = r"""
import json, sys
import mlflow

mlflow.set_tracking_uri("databricks")
mlflow.set_registry_uri("databricks-uc")

model = mlflow.pyfunc.load_model(sys.argv[1])
request = json.loads(open(sys.argv[2]).read())
response = model.predict(request)

import iberian
location = iberian.__file__
print("iberian loaded from:", location)
packaged = "/code/iberian/" in location.replace("\\", "/")
print("packaged with the model:", packaged)

if hasattr(response, "model_dump"):
    response = response.model_dump()
custom = response.get("custom_outputs") or {}
print("grounded:", custom.get("grounded"))
print("attempts:", custom.get("attempts"))
texts = []
for item in response.get("output") or []:
    for part in item.get("content") or []:
        if part.get("text"):
            texts.append(part["text"])
print()
print(" ".join(texts)[:1200])
"""


def load(name: str) -> pd.DataFrame:
    path = ROOT / "data" / "lakehouse" / "gold" / f"{name}.parquet"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run scripts/build_medallion.py first.")
    return pd.read_parquet(path)


def build_example(key: str | None) -> tuple[str, dict]:
    episodes = load("gold_split_episodes").sort_values("max_abs_spread", ascending=False)
    intervals = load("gold_interval_premium")
    episodes = episodes.assign(
        episode_key=[episode_key(row) for _, row in episodes.iterrows()]
    )
    if key:
        chosen = episodes[episodes["episode_key"] == key]
        if chosen.empty:
            raise SystemExit(f"No such episode: {key}")
    else:
        # The worst episode by default: the one a reviewer is most likely to
        # ask about, and the one with the most evidence in its sheet.
        chosen = episodes.head(1)
    episode = chosen.iloc[0]

    client = EntsoeClient(Settings.from_env().require_entsoe_token())
    build = sheet_builder(
        client, intervals, DIRECTION, parse_outages_response, binding_assets
    )
    try:
        sheet = build(episode)
    except EvidenceUnavailable as exc:
        raise SystemExit(f"Could not build the example: {exc}. Try another --episode.")

    request = {
        "input": [{"role": "user", "content": "Explain this market splitting episode."}],
        "custom_inputs": {"fact_sheet": sheet.to_dict()},
    }
    return episode["episode_key"], request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", help="episode key for the input example")
    parser.add_argument("--model-name", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default="databricks-claude-haiku-4-5")
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="register only, without loading the model back",
    )
    args = parser.parse_args()

    # Checked first, because the failure otherwise comes last: after the model
    # has been called on the example and after Unity Catalog has created a
    # version, which is left behind half made. This workspace keeps model
    # files in S3, and the upload needs boto3, which plain mlflow does not
    # bring. `mlflow[databricks]` would, but it also pulls databricks-agents,
    # whose dependency `whenever` has no wheel for Python 3.14 and needs Rust.
    import importlib.util

    if importlib.util.find_spec("boto3") is None:
        raise SystemExit(
            "boto3 is not installed, and Unity Catalog needs it to upload the "
            "model files. Run: pip install boto3"
        )

    import mlflow
    from mlflow.models.resources import DatabricksServingEndpoint

    key, request = build_example(args.episode)
    print(f"Input example: episode {key}")

    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")
    mlflow.set_experiment(args.experiment)

    with mlflow.start_run(run_name=f"register {args.model_name.split('.')[-1]}"):
        mlflow.set_tags({"purpose": "register agent", "example_episode": key})
        info = mlflow.pyfunc.log_model(
            name="mibel_agent",
            python_model=str(ROOT / "agents" / "mibel_agent.py"),
            # The library travels with the model. Without it a serving
            # container would have the agent file and nothing it imports.
            code_paths=[str(ROOT / "src" / "iberian")],
            input_example=request,
            # Declared so a later deployment can authenticate to the model it
            # calls without a personal token baked into the container.
            resources=[DatabricksServingEndpoint(endpoint_name=args.endpoint)],
            pip_requirements=[
                f"mlflow=={mlflow.__version__}",
                "databricks-sdk>=0.30",
                "pandas>=2.0",
                "requests>=2.32",
            ],
            registered_model_name=args.model_name,
        )

    version = getattr(info, "registered_model_version", None)
    print(f"\nLogged:     {info.model_uri}")
    print(f"Registered: {args.model_name}" + (f" version {version}" if version else ""))

    if args.skip_validation:
        return 0

    uri = f"models:/{args.model_name}/{version}" if version else info.model_uri
    print(f"\nValidating {uri} in a clean process...\n")
    with tempfile.TemporaryDirectory() as scratch:
        example = Path(scratch) / "request.json"
        example.write_text(json.dumps(request))
        result = subprocess.run(
            [sys.executable, "-c", VALIDATE, uri, str(example)],
            cwd=scratch,
            capture_output=True,
            text=True,
        )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        print("Validation failed. The model is registered but did not answer.")
        return 1

    if "packaged with the model: False" in result.stdout:
        print(
            "Note: iberian was imported from outside the model, probably an "
            "installed copy in this environment. The model answered, but this "
            "run does not prove the packaged code is complete."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())