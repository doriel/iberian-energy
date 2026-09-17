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

`--dry-run` prints the fact sheets without calling a model, which is the way to
read what the agent will be given before spending a token on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.explain import databricks_completer, explain  # noqa: E402
from iberian.agent.facts import episode_facts  # noqa: E402
from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import market_day_window  # noqa: E402
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
        "--dry-run",
        action="store_true",
        help="print the fact sheets and stop, without calling a model",
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
        episode_key=[
            f"{row['market_day']}T{pd.Timestamp(row['start_utc']):%H%M}"
            for _, row in episodes.iterrows()
        ]
    )

    if args.labelled_only and not labels.empty:
        known = set(labels.loc[labels["true_cause"].str.strip() != "", "episode_key"])
        episodes = episodes[episodes["episode_key"].isin(known)]
        if episodes.empty:
            print("No labelled episodes yet. Run scripts/label_episodes.py first.")
            return 0

    if args.limit:
        episodes = episodes.head(args.limit)

    # Retrieval happens in a dry run too. Skipping it would show fact sheets
    # that differ from the real ones, and a preview that lies about what the
    # agent will be given is worse than no preview. Only the model call is
    # skipped, because only the model call costs anything.
    client = EntsoeClient(Settings.from_env().require_entsoe_token())
    complete = None if args.dry_run else databricks_completer(endpoint=args.endpoint)

    curves_by_day: dict[object, list] = {}
    records: list[dict] = []

    print(f"{len(episodes)} episode(s)"
          + ("" if args.dry_run else f", endpoint {args.endpoint}") + "\n")

    for _, episode in episodes.iterrows():
        window = intervals[
            (intervals["ts_utc"] >= episode["start_utc"])
            & (intervals["ts_utc"] < episode["end_utc"])
        ]

        day = episode["market_day"]
        if day not in curves_by_day:
            day_start, day_end = market_day_window(day)
            response = client.transmission_unavailability(
                *DIRECTION, day_start, day_end
            )
            curves_by_day[day] = (
                [] if response.is_empty else parse_outages_response(response)
            )
        assets = binding_assets(
            curves_by_day[day],
            pd.Timestamp(episode["start_utc"]).to_pydatetime(),
            pd.Timestamp(episode["end_utc"]).to_pydatetime(),
            published_before=pd.Timestamp(episode["start_utc"]).to_pydatetime(),
            direction=DIRECTION,
        )

        sheet = episode_facts(episode, window, assets or None)

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

        records.append(
            {
                "episode_key": episode["episode_key"],
                "model": args.endpoint,
                "grounded": result.ok,
                "attempts": result.attempts,
                "unsupported": [c.text for c in result.verdict.unsupported],
                "missing_sources": result.verdict.missing_sources,
                "numeric_claims": len(result.verdict.claims),
                "text": result.text if result.ok else "",
                "rejected_drafts": result.rejected,
                "true_cause": truth,
            }
        )

    if args.dry_run or not records:
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str) + "\n")

    grounded = sum(1 for record in records if record["grounded"])
    first_try = sum(
        1 for record in records if record["grounded"] and record["attempts"] == 1
    )
    labelled = [record for record in records if record["true_cause"]]

    print("=" * 72)
    print(f"Grounded explanations: {grounded} of {len(records)}"
          f"  ({grounded / len(records):.0%})")
    print(f"  passed without a retry: {first_try}")
    if grounded < len(records):
        print("\n  Failures, which are the interesting rows:")
        for record in records:
            if not record["grounded"]:
                reason = (
                    ", ".join(record["unsupported"])
                    if record["unsupported"]
                    else "no source named"
                )
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