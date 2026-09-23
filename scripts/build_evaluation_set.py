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
    "saturation_planned": "Capacity unusually low, a planned outage is consistent with it",
    "saturation_unplanned": "Capacity unusually low, an unplanned outage is consistent",
    "saturation_no_notice": "Capacity unusually low, no notice accounts for it",
    "saturation_ordinary_capacity": "Border full at ordinary capacity, nothing reduced it",
    "not_saturated": "Priced apart with headroom on the border, cause unknown",
    "threshold_artifact": "Spread at the rounding epsilon, not a real event",
    "unclear": "The available evidence does not settle it",
}

#: Below this percentile of the window's own capacity distribution, the border
#: was carrying unusually little and something reduced it. At or above it, the
#: border was at an ordinary level and there is no reduction to explain.
#:
#: There is no published "normal" capacity for this border: the operator
#: recomputes the net transfer capacity every day from the whole system state,
#: and the observed values run from 210 to 7020 MW with no mode. A percentile of
#: the window is the only reference the public data supports.
#:
#: The quartile rather than the median, because "unusually low" has to mean
#: something. Half of all quarter hours are below the median by construction, so
#: a median cut calls an utterly ordinary capacity a reduction. On the first
#: sixty two episodes the choice moves twelve of them:
#:
#:     p25 -> 21 ordinary capacity, 32 outage consistent
#:     p50 ->  8 ordinary capacity, 45 outage consistent
#:
#: One named constant rather than a figure buried in a condition, because it is
#: a judgement about what "unusual" means and somebody should be able to find it
#: and argue with it.
ORDINARY_CAPACITY_PERCENTILE = 25.0

LABEL_COLUMNS = ["true_cause", "confidence", "notes"]

# The direction that matters: why Spanish power could not reach Portugal.
DIRECTION = (EIC_SPAIN, EIC_PORTUGAL)


def propose(row: pd.Series) -> str:
    """What the current rules would answer, so labelling is agree or disagree.

    This is a candidate, never the label. It is not shown to the labeller before
    they answer, for reasons the labelling tool explains at length.

    The order matters and follows the evidence rather than convenience. The
    first version asked "is there a notice" before asking "was there anything to
    explain", and so proposed `saturation_planned` for sixty of sixty two
    episodes, including one where the border was at its 74th percentile and the
    notice covered half of it. A notice sitting in the evidence is not a cause.
    """
    if row["peak_abs_spread"] <= 0.01:
        return "threshold_artifact"
    if row["share_saturated"] is None or pd.isna(row["share_saturated"]):
        return "unclear"
    if row["share_saturated"] < 0.5:
        # Not expected to fire. Zones decouple because the interconnection
        # binds, so a split with headroom would be a finding about the pipeline
        # rather than about the market.
        return "not_saturated"

    percentile = row.get("capacity_percentile")
    if percentile is None or pd.isna(percentile):
        return "unclear"
    if percentile >= ORDINARY_CAPACITY_PERCENTILE:
        # The border was full at a level it reaches routinely. Nothing was
        # taken away, so no outage explains anything: the interconnection is
        # simply smaller than the flow the price difference would justify.
        return "saturation_ordinary_capacity"

    if row["notices"] == 0:
        return "saturation_no_notice"
    if row["unexplained_mw"] is not None and not pd.isna(row["unexplained_mw"]):
        if float(row["unexplained_mw"]) > 0:
            # The notice permits more than the border carried, so whatever cut
            # the capacity, it was not this.
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

    # The reference the sheet was missing, and without which the central
    # question cannot be answered at all.
    #
    # "Does this outage explain the capacity" needs something to compare the
    # capacity against, and this border has no published normal: the operator
    # recalculates it daily and the values run from 210 to 7020 MW. What can be
    # said is where a given figure sits among the others, so the reference is
    # the window's own distribution.
    #
    # This is a relative measure and it is worth being honest about the limit:
    # it says the border was carrying unusually little, not why.
    capacity_series = intervals["capacity_mw"].dropna().to_numpy()
    capacity_series.sort()

    def capacity_percentile(value) -> float | None:
        if value is None or pd.isna(value) or capacity_series.size == 0:
            return None
        return round(
            float((capacity_series < float(value)).mean() * 100), 1
        )

    median_capacity = (
        round(float(pd.Series(capacity_series).median()), 0)
        if capacity_series.size
        else None
    )

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

        # Where this episode's capacity sits among every quarter hour in the
        # window. Low means the border was carrying unusually little.
        row["capacity_percentile"] = capacity_percentile(row["min_capacity_mw"])
        row["median_capacity_mw"] = median_capacity

        # How much of the border that one asset's remaining capacity amounts
        # to. A ratio near 1 means the asset under notice is most of the
        # border, so its outage plausibly set the limit. A ratio near 0.5 means
        # the border had roughly as much again elsewhere, and that notice is
        # not what constrained it.
        #
        # A heuristic, not physics: the border total is not the sum of the
        # assets, and the operator's security assessment is not published.
        if row["tightest_available_mw"] and row["min_capacity_mw"]:
            row["notice_share_of_border"] = round(
                float(row["tightest_available_mw"]) / float(row["min_capacity_mw"]), 2
            )
        else:
            row["notice_share_of_border"] = None

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