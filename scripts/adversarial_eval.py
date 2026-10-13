"""Measure what the numeric check actually catches.

Reads the published explanations and the fact sheets they were generated from,
corrupts one number in each explanation in four ways a language model plausibly
would, and reports how often the verifier fires.

    python scripts/adversarial_eval.py
    python scripts/adversarial_eval.py --json results.json

No cluster, no credentials, no network. `scripts/export_fact_sheets.py` does
the one step that needs the workspace and writes the file this reads.

**Read the misses, not the rate.** A detection rate near a hundred percent is
the expected result and says little on its own: these are corruptions of a
value against an allowlist of values, so the check ought to find them. What the
run is for is the residue. Every uncaught mutant is a sentence a model could
write that this check would publish, and each one is either a bug to fix or a
boundary to write down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from iberian.agent.adversarial import (  # noqa: E402
    BENIGN,
    FREE,
    MISSED,
    TOLERATED,
    foreign_mutants,
    mutate,
    score,
    summarise,
)
from iberian.agent.facts import FactSheet  # noqa: E402
from iberian.agent.verify import verify  # noqa: E402


def load_sheets(path: Path) -> dict[str, FactSheet]:
    if not path.exists():
        raise SystemExit(
            f"{path} is missing. Run scripts/export_fact_sheets.py in Databricks "
            "once and commit the result."
        )
    out = {}
    for line in path.open():
        record = json.loads(line)
        out[record["episode_key"]] = FactSheet.from_dict(record["sheet"])
    return out


def load_explanations(path: Path) -> dict[str, str]:
    out = {}
    for line in path.open():
        record = json.loads(line)
        if record.get("grounded") and record.get("text"):
            out[record["episode_key"]] = record["text"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheets", default=str(REPO / "evaluation/fact_sheets.jsonl"))
    parser.add_argument(
        "--explanations", default=str(REPO / "evaluation/explanations.jsonl")
    )
    parser.add_argument("--json", default="")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    sheets = load_sheets(Path(args.sheets))
    texts = load_explanations(Path(args.explanations))
    shared = sorted(set(sheets) & set(texts))
    if not shared:
        raise SystemExit("no episode has both a sheet and a grounded explanation")

    print(f"{len(shared)} episode(s) with both a fact sheet and an explanation\n")

    # The baseline first. A detection rate measured against explanations that
    # do not pass in the first place would be measuring nothing.
    baseline_failures = [
        key for key in shared if not verify(texts[key], sheets[key],
                                            require_sources=False).ok
    ]
    if baseline_failures:
        print(
            f"WARNING: {len(baseline_failures)} published explanation(s) do not "
            f"pass unmutated: {baseline_failures[:5]}"
        )
        print("Fix that before reading anything below.\n")

    outcomes = []
    for key in shared:
        sheet = sheets[key]
        others = [sheets[other] for other in shared if other != key]
        outcomes += score(mutate(texts[key], sheet, episode_key=key), sheet)
        outcomes += score(
            foreign_mutants(
                texts[key], sheet, others, episode_key=key, seed=args.seed
            ),
            sheet,
        )

    table = summarise(outcomes)

    harmful = [o for o in outcomes if not o.mutant.benign]
    caught = sum(1 for o in harmful if o.caught)
    tolerated = sum(1 for o in harmful if o.category == TOLERATED)
    free = sum(1 for o in harmful if o.category == FREE)
    misses = [o for o in harmful if o.category == MISSED]
    judged = caught + len(misses)
    rate = 100.0 * caught / judged if judged else 0.0

    header = f"{'mutation':<12} {'mutants':>8} {'caught':>7} {'tol':>5} {'free':>5} {'missed':>7} {'rate':>7}"
    print(header)
    print("-" * len(header))
    for kind in sorted(table):
        row = table[kind]
        tail = "  (accepted by design)" if kind in BENIGN else ""
        print(
            f"{kind:<12} {row['mutants']:>8} {row['caught']:>7} "
            f"{row[TOLERATED]:>5} {row[FREE]:>5} {row[MISSED]:>7} "
            f"{row['rate']:>6}%{tail}"
        )
    print("-" * len(header))
    print(
        f"{'HARMFUL':<12} {len(harmful):>8} {caught:>7} {tolerated:>5} {free:>5} "
        f"{len(misses):>7} {rate:>6.1f}%\n"
    )

    print("tol    inside the rounding tolerance the verifier documents")
    print("free   on the short allowlist of numbers that carry no claim")
    print("rate   caught / (caught + missed), excusing only the two above\n")

    if misses:
        print(f"{len(misses)} corruption(s) passed for no declared reason.")
        print("Each one is a sentence this check would publish:\n")
        for outcome in misses[:20]:
            m = outcome.mutant
            print(f"  {m.episode_key}  {m.kind:<10} {m.original} -> {m.replacement}")
        if len(misses) > 20:
            print(f"  ... and {len(misses) - 20} more")
    else:
        print("No corruption passed without a declared reason.")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "episodes": len(shared),
                    "harmful_mutants": len(harmful),
                    "caught": caught,
                    "tolerated": tolerated,
                    "free_number": free,
                    "missed": len(misses),
                    "detection_rate_pct": round(rate, 2),
                    "by_mutation": table,
                    "misses": [
                        {
                            "episode_key": o.mutant.episode_key,
                            "kind": o.mutant.kind,
                            "original": o.mutant.original,
                            "replacement": o.mutant.replacement,
                        }
                        for o in misses
                    ],
                },
                indent=2,
            )
        )
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())