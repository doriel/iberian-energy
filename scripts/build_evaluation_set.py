"""Assemble the labelling sheet for the cause attribution evaluation.

The north star metric is the share of significant price anomalies that receive
a correct, grounded explanation. "Correct" requires a human to have decided,
beforehand, what the right answer was. That decision cannot be automated: it is
the ground truth the agent is scored against, and if the agent's own output
were used to produce it the metric would measure nothing.

What can be automated is everything around the decision. This script takes the
episodes in gold, retrieves the evidence that was available *before* each one
began, proposes the cause the current rules would pick, and writes a sheet with
three empty columns for a person to fill in.

    python scripts/build_evaluation_set.py
    python scripts/build_evaluation_set.py --limit 10

Output is `evaluation/episodes.csv`. Open it in a spreadsheet, read the
evidence columns, and fill in `true_cause`, `confidence` and `notes`. Existing
labels are preserved when the sheet is rebuilt, so this is safe to re-run after
adding more market days.

Point in time correctness is enforced here, not bolted on afterwards: only
notices published before an episode started are retrieved for it. Without that,
the accuracy figure quietly includes hindsight the agent would never have had.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import EIC_PORTUGAL, EIC_SPAIN, Settings  # noqa: E402
from iberian.ingestion.entsoe import EntsoeClient  # noqa: E402
from iberian.market_time import market_day_window  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_outages_response,
)

#: The vocabulary a labeller picks from. Deliberately small: a taxonomy with
#: twenty categories produces inconsistent labels, and every category here is
#: one the available evidence can actually distinguish.
CAUSES = {
    "saturation_planned": "Border at its limit, explained by a planned outage notice",
    "saturation_unplanned": "Border at its limit, explained by an unplanned outage",
    "saturation_no_notice": "Border at its limit, no notice accounts for it",
    "not_saturated": "Priced apart with headroom on the border, cause unknown",
    "threshold_artifact": "Spread at the rounding epsilon, not a real event",
    "unclear": "The available evidence does not settle it",
}

LABEL_COLUMNS = ["true_cause", "confidence", "notes"]

# The direction that matters: why Spanish power could not reach Portugal.
DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)


def propose(row: pd.Series) -> str:
    """What the current rules would answer, so labelling is agree or disagree.

    This is a candidate, never the label. Its value is that reviewing a
    proposal is several times faster than writing one from scratch, and the
    cases where a human disagrees are exactly the interesting ones.
    """
    if row["peak_abs_spread"] <= 0.01:
        return "threshold_artifact"
    if row["share_saturated"] is None or pd.isna(row["share_saturated"]):
        return "unclear"
    if row["share_saturated"] < 0.5:
        return "not_saturated"
    if row["notices"] == 0:
        return "saturation_no_notice"
    if row["tightest_status"] == "unplanned":
        return "saturation_unplanned"
    return "saturation_planned"


def load_existing(path: Path) -> pd.DataFrame:
    """Previous labels, so rebuilding the sheet never destroys an afternoon."""
    if not path.exists():
        return pd.DataFrame(columns=["episode_key"] + LABEL_COLUMNS)
    previous = pd.read_csv(path, dtype=str)
    keep = ["episode_key"] + [c for c in LABEL_COLUMNS if c in previous.columns]
    return previous[keep] if "episode_key" in previous.columns else pd.DataFrame(
        columns=["episode_key"] + LABEL_COLUMNS
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/lakehouse")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--out", default="evaluation/episodes.csv")
    parser.add_argument("--limit", type=int, help="only the N largest episodes")
    args = parser.parse_args()

    root = Path(args.root)
    episodes = pd.read_parquet(root / "gold" / "gold_split_episodes.parquet")
    intervals = pd.read_parquet(root / "gold" / "gold_interval_premium.parquet")
    if episodes.empty:
        print("No episodes in gold. Nothing to label.")
        return 0

    episodes = episodes.sort_values("max_abs_spread", ascending=False)
    if args.limit:
        episodes = episodes.head(args.limit)

    client = EntsoeClient(Settings.from_env().require_entsoe_token())

    # One A78 request per market day rather than per episode. Several episodes
    # share a day, and their terms of use are not the only reason not to ask
    # for the same document five times.
    curves_by_day: dict[object, list] = {}

    rows: list[dict] = []
    print(f"Assembling evidence for {len(episodes)} episode(s)\n")

    for _, episode in episodes.iterrows():
        start = pd.Timestamp(episode["start_utc"]).to_pydatetime()
        end = pd.Timestamp(episode["end_utc"]).to_pydatetime()
        day = episode["market_day"]

        window = intervals[
            (intervals["ts_utc"] >= episode["start_utc"])
            & (intervals["ts_utc"] < episode["end_utc"])
        ]

        if day not in curves_by_day:
            day_start, day_end = market_day_window(day)
            response = client.transmission_unavailability(
                *DIRECTION, day_start, day_end
            )
            curves_by_day[day] = (
                [] if response.is_empty else parse_outages_response(response)
            )
            target = Path(args.raw_dir) / "entsoe" / "transmission_unavailability"
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{day}{response.suggested_extension}").write_bytes(
                response.content
            )

        # published_before=start is the whole point. A notice issued after the
        # episode began was not retrievable when the explanation was needed.
        assets = binding_assets(
            curves_by_day[day],
            start,
            end,
            published_before=start,
            direction=DIRECTION,
        )
        tightest = assets[0] if assets else None

        capacity = window["capacity_mw"].dropna()
        utilisation = window["utilisation"].dropna()

        row = {
            "episode_key": f"{day}T{start:%H%M}",
            "market_day": str(day),
            "start_utc": f"{start:%Y-%m-%d %H:%M}Z",
            "duration_hours": round(float(episode["duration_hours"]), 2),
            "intervals": int(episode["intervals"]),
            "peak_spread": round(float(episode["peak_spread"]), 2),
            "peak_abs_spread": round(float(episode["max_abs_spread"]), 2),
            "premium_side": episode["premium_side"],
            "severity": episode["max_severity"],
            "extra_cost_eur": (
                round(float(episode["extra_cost_eur"]), 0)
                if pd.notna(episode.get("extra_cost_eur"))
                else None
            ),
            "share_saturated": (
                round(float(episode["share_saturated"]), 2)
                if pd.notna(episode.get("share_saturated"))
                else None
            ),
            "mean_utilisation": (
                round(float(utilisation.mean()), 3) if not utilisation.empty else None
            ),
            "min_capacity_mw": (
                round(float(capacity.min()), 0) if not capacity.empty else None
            ),
            "notices": len(assets),
            "tightest_asset": tightest["asset"] if tightest else "",
            "tightest_available_mw": (
                round(float(tightest["available_mw"]), 0) if tightest else None
            ),
            "tightest_status": tightest["status"] if tightest else "",
            "tightest_published": (
                f"{tightest['published_at']:%Y-%m-%d}"
                if tightest and tightest.get("published_at")
                else ""
            ),
        }

        # The honest column. A78 is asset level, A61 is the net border figure
        # after the operator's security assessment, so a positive gap is the
        # part the notices do not explain rather than an error.
        if tightest and row["min_capacity_mw"] is not None:
            row["unexplained_mw"] = round(
                row["tightest_available_mw"] - row["min_capacity_mw"], 0
            )
        else:
            row["unexplained_mw"] = None

        row["candidate_cause"] = propose(pd.Series(row))
        rows.append(row)
        print(
            f"  {row['episode_key']}  spread {row['peak_abs_spread']:>7.2f}  "
            f"{row['notices']} notice(s)  -> {row['candidate_cause']}"
        )

    sheet = pd.DataFrame(rows)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing(out_path)
    sheet = sheet.merge(existing, on="episode_key", how="left")
    for column in LABEL_COLUMNS:
        if column not in sheet.columns:
            sheet[column] = ""
    sheet[LABEL_COLUMNS] = sheet[LABEL_COLUMNS].fillna("")

    sheet.to_csv(out_path, index=False)

    legend = out_path.parent / "cause_vocabulary.md"
    legend.write_text(
        "# Cause vocabulary\n\n"
        "Pick exactly one value for `true_cause`. Set `confidence` to high,\n"
        "medium or low, and use `notes` for anything that made it hard.\n\n"
        "A label that disagrees with `candidate_cause` is the most valuable\n"
        "row in the sheet. Write down why in `notes`.\n\n"
        + "\n".join(f"- `{key}`: {text}" for key, text in CAUSES.items())
        + "\n"
    )

    labelled = int((sheet["true_cause"].astype(str).str.strip() != "").sum())
    print(f"\n  {out_path}  {len(sheet)} episodes, {labelled} already labelled")
    print(f"  {legend}  the vocabulary to pick from")
    if labelled < len(sheet):
        remaining = len(sheet) - labelled
        print(f"\n  {remaining} left. Five a day is twenty minutes and finishes")
        print("  this with time to spare.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())