"""Run the agent over the labelled episodes and report the north star metric.

The metric the proposal commits to is the share of significant price anomalies
that receive a correct, grounded explanation. Both halves are measured here and
they measure different things.

Correct means the cause matches the human label in `evaluation/episodes.csv`.
The rule based classifier already does this without a model, so the agent is
not being asked to improve on it. That number is reported as the baseline it is.

Grounded means every figure in the prose came from a retrieved document and the
document is named. That is where a model can fail, it is checked
programmatically by `agent.verify`, and it is the number worth arguing about.

    python scripts/explain_episodes.py --limit 5
    python scripts/explain_episodes.py --endpoint databricks-claude-opus-4-5
    python scripts/explain_episodes.py --dry-run
    python scripts/explain_episodes.py --episode 2026-08-01T1100 --dry-run

`--dry-run` prints the fact sheets without calling a model, which is the way to
read what the agent will be given before spending a token on it.

`--episode` narrows to one episode, which is what a rejection needs: the sheet
and the draft side by side decide whether the verifier caught an invention or
produced another false positive.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.batch import (  # noqa: E402
    EvidenceUnavailable,
    episode_key,
    load_records,
    merge,
    pending,
    sheet_builder,
    to_record,
    write_records,
)
from iberian.agent.experiment import EvaluationRun  # noqa: E402
from iberian.agent.explain import databricks_completer, explain  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_outages_response,
)

DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)


def load(root: Path, name: str) -> pd.DataFrame:
    path = root / "gold" / f"{name}.parquet"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run scripts/build_medallion.py first.")
    return pd.read_parquet(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/lakehouse")
    parser.add_argument("--labels", default="evaluation/episodes.csv")
    parser.add_argument("--out", default="evaluation/explanations.jsonl")
    parser.add_argument("--endpoint", default="databricks-claude-haiku-4-5")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument(
        "--episode",
        action="append",
        default=[],
        metavar="KEY",
        help="only this episode, by key (2026-08-01T1100). Repeatable. Naming "
        "an episode re-runs it even if it is already on file, because that is "
        "the only reason to name one.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the fact sheets and stop, without calling a model",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="re-explain every episode, including ones already on file",
    )
    parser.add_argument(
        "--experiment",
        default="",
        help="MLflow experiment path; blank uses the project default",
    )
    parser.add_argument(
        "--tracking-uri",
        default="",
        help='where to record; "databricks" sends runs and traces to the workspace',
    )
    parser.add_argument(
        "--trace-catalog",
        default="",
        help="Unity Catalog catalog for trace storage; needs --trace-schema too",
    )
    parser.add_argument(
        "--trace-schema",
        default="",
        help="Unity Catalog schema for trace storage, plus a SQL warehouse in "
        "MLFLOW_TRACING_SQL_WAREHOUSE_ID. Binding is permanent.",
    )
    parser.add_argument(
        "--no-mlflow",
        action="store_true",
        help="do not record this run, even if MLflow is available",
    )
    parser.add_argument(
        "--labelled-only",
        action="store_true",
        help="only episodes a human has labelled, which is what the metric needs",
    )
    args = parser.parse_args()

    episodes = load(Path(args.root), "gold_split_episodes")
    intervals = load(Path(args.root), "gold_interval_premium")
    if episodes.empty:
        print("No episodes in gold.")
        return 0

    labels = pd.DataFrame()
    labels_path = Path(args.labels)
    if labels_path.exists():
        labels = pd.read_csv(labels_path, dtype=str, keep_default_na=False)

    episodes = episodes.sort_values("max_abs_spread", ascending=False)
    episodes = episodes.assign(
        episode_key=[episode_key(row) for _, row in episodes.iterrows()]
    )

    # Existing work is read before anything is filtered, because both the
    # incremental decision and the final merge need it.
    out_path = Path(args.out)
    already = load_records(out_path)

    if args.episode:
        wanted = set(args.episode)
        episodes = episodes[episodes["episode_key"].isin(wanted)]
        missing = wanted - set(episodes["episode_key"])
        if missing:
            print(f"No such episode: {', '.join(sorted(missing))}")
        if episodes.empty:
            return 1

    if args.labelled_only and not labels.empty:
        known = set(labels.loc[labels["true_cause"].str.strip() != "", "episode_key"])
        episodes = episodes[episodes["episode_key"].isin(known)]
        if episodes.empty:
            print("No labelled episodes yet. Run scripts/label_episodes.py first.")
            return 0

    if not args.all and not args.episode:
        before = len(episodes)
        episodes = pending(episodes, already)
        done = before - len(episodes)
        if done:
            print(f"{done} episode(s) already explained, skipping. --all re-runs them.")
        if episodes.empty:
            print("Nothing new to explain.")
            return 0

    if args.limit:
        episodes = episodes.head(args.limit)

    # Retrieval happens in a dry run too. Skipping it would show fact sheets
    # that differ from the real ones, and a preview that lies about what the
    # agent will be given is worse than no preview. Only the model call is
    # skipped, because only the model call costs anything.
    client = EntsoeClient(Settings.from_env().require_entsoe_token())
    complete = None if args.dry_run else databricks_completer(endpoint=args.endpoint)

    # The same retrieval the Job's explain task uses. This loop used to fetch
    # the notices itself, and a copy is how the two drift: the local run and
    # the published run must be given identical evidence.
    build_sheet = sheet_builder(
        client, intervals, DIRECTION, parse_outages_response, binding_assets
    )
    records: list[dict] = []
    skipped: list[str] = []

    print(f"{len(episodes)} episode(s)"
          + ("" if args.dry_run else f", endpoint {args.endpoint}") + "\n")

    for _, episode in episodes.iterrows():
        try:
            sheet = build_sheet(episode)
        except EvidenceUnavailable as exc:
            print(f"{episode['episode_key']}   SKIPPED, evidence unavailable")
            print(f"    {exc}\n")
            skipped.append(episode["episode_key"])
            continue

        print("=" * 72)
        print(f"{episode['episode_key']}   {sheet.subject}")
        print("=" * 72)

        if args.dry_run:
            print(sheet.render())
            print()
            continue

        result = explain(sheet, complete, max_attempts=args.attempts,
                         model=args.endpoint)
        print(result.render())
        print()

        truth = ""
        if not labels.empty:
            match = labels[labels["episode_key"] == episode["episode_key"]]
            if not match.empty:
                truth = match.iloc[0]["true_cause"].strip()

        # Built by the same function the Job's explain task uses. Writing the
        # dict here as well is how the two drifted: this copy had no `sources`
        # key, so a record written locally and a record written on Databricks
        # were different shapes in the same file.
        records.append(to_record(episode["episode_key"], result, sheet, truth))

        # Written as each one arrives, merged, never overwritten. Writing only
        # at the end lost a paid-for Opus explanation when the next episode's
        # evidence fetch failed, and at forty-eight model calls a crash near
        # the end would lose nearly all of them.
        write_records(out_path, merge(already, records))

    if skipped:
        # Named rather than counted, and as a command, because a re-run with
        # --all would repeat every episode, and a re-run without it would skip
        # these too whenever an older record for them is already on file.
        print(f"\n{len(skipped)} episode(s) skipped because their evidence could "
              "not be retrieved. Nothing was written for them. To retry just those:")
        wanted = " ".join(f"--episode {key}" for key in skipped)
        print(f"  python scripts/explain_episodes.py {wanted} "
              f"--endpoint {args.endpoint} --out {args.out}\n")

    if args.dry_run or not records:
        return 0

    # Recorded after the file is written, so the artifact logged is the one on
    # disk rather than a second serialisation that could differ from it.
    if not args.no_mlflow:
        run = EvaluationRun(
            endpoint=args.endpoint,
            attempts=args.attempts,
            **({"experiment": args.experiment} if args.experiment else {}),
            tracking_uri=args.tracking_uri or None,
            trace_catalog=args.trace_catalog or None,
            trace_schema=args.trace_schema or None,
            extra_params={
                "labelled_only": args.labelled_only,
                "episodes": len(records),
            },
        )
        print(f"\n{run.describe()}")
        with run as active:
            active.record(records, artifact=out_path)

    grounded = sum(1 for record in records if record["grounded"])
    first_try = sum(
        1 for record in records if record["grounded"] and record["attempts"] == 1
    )
    labelled = [record for record in records if record["true_cause"]]

    print("=" * 72)
    print(f"Grounded explanations: {grounded} of {len(records)}"
          f"  ({grounded / len(records):.0%})")
    print(f"  passed without a retry: {first_try}")
    if skipped:
        print(f"  skipped, evidence unavailable, not counted above: {len(skipped)}")
    if grounded < len(records):
        print("\n  Failures, which are the interesting rows:")
        for record in records:
            if not record["grounded"]:
                reasons = list(record["unsupported"]) + [
                    f"date {text}" for text in record.get("wrong_dates", [])
                ]
                reason = ", ".join(reasons) if reasons else "no source named"
                print(f"    {record['episode_key']}: {reason}")

    print(f"\nHuman labelled episodes in this run: {len(labelled)}")
    if labelled:
        print("  Cause accuracy is not measured here: the rule based classifier")
        print("  already matches the labels without a model, so the agent adds")
        print("  nothing to that half of the metric. What it adds, and what is")
        print("  measured above, is whether the prose stays inside the evidence.")

    print(f"\n  {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())