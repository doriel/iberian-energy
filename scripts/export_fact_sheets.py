"""Export the fact sheet for each labelled episode, once, so the adversarial
evaluation can run on a laptop forever after.

Run this in Databricks. It is the only step of the adversarial evaluation that
needs the workspace, and that is the point: after it, `adversarial_eval.py`
reads two files out of the repository and needs no cluster, no credentials and
no network, which is the same property the test suite has and the reason the
test suite is worth running.

    %run this notebook, or:
    python scripts/export_fact_sheets.py --catalog bootcamp_students --schema doriel

It writes `evaluation/fact_sheets.jsonl`, one sheet per line, keyed by episode.
Commit that file. It is retrieved evidence about a public market, there is
nothing in it that is not already in the published explanations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from iberian.agent.facts import episode_facts  # noqa: E402


def labelled_keys(path: Path) -> list[str]:
    """The episodes the evaluation set covers, in the order they were labelled."""
    import pandas as pd

    frame = pd.read_csv(path)
    column = "episode_key" if "episode_key" in frame.columns else frame.columns[0]
    return [str(key) for key in frame[column].dropna().unique()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="bootcamp_students")
    parser.add_argument("--schema", default="doriel")
    parser.add_argument("--out", default=str(REPO / "evaluation" / "fact_sheets.jsonl"))
    parser.add_argument("--labels", default=str(REPO / "evaluation" / "episodes.csv"))
    args = parser.parse_args()

    try:
        from pyspark.sql import SparkSession
    except ImportError:
        print("This runs in Databricks: pyspark is not installed here.")
        return 1

    spark = SparkSession.builder.getOrCreate()
    prefix = f"{args.catalog}.{args.schema}"

    episodes = spark.table(f"{prefix}.gold_split_episodes").toPandas()
    intervals = spark.table(f"{prefix}.gold_interval_premiums").toPandas()
    notices = spark.table(f"{prefix}.gold_transmission_notices").toPandas()

    keys = labelled_keys(Path(args.labels))
    print(f"{len(keys)} labelled episode(s)")

    written = 0
    missing: list[str] = []
    with open(args.out, "w") as handle:
        for key in keys:
            rows = episodes[episodes["episode_key"] == key]
            if rows.empty:
                missing.append(key)
                continue
            episode = rows.iloc[0]
            window = intervals[
                (intervals["ts_utc"] >= episode["start_utc"])
                & (intervals["ts_utc"] < episode["end_utc"])
            ]
            # The assets in force are the ones the explanation was given, so
            # they are read the same way the explain task reads them rather
            # than recomputed here. A sheet that differs from the one the model
            # saw would make the evaluation measure the wrong thing.
            assets = [
                row.to_dict()
                for _, row in notices[
                    (notices["start_utc"] <= episode["start_utc"])
                    & (notices["end_utc"] >= episode["end_utc"])
                ].iterrows()
            ]
            sheet = episode_facts(episode, window, assets=assets)
            handle.write(
                json.dumps({"episode_key": key, "sheet": sheet.to_dict()}) + "\n"
            )
            written += 1

    print(f"wrote {written} sheet(s) to {args.out}")
    if missing:
        print(f"{len(missing)} labelled episode(s) not found in gold: {missing[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())