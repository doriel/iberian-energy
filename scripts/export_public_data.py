"""Publish the gold tables as one file the web service can serve.

The web service cannot query the lakehouse. The workspace issues OAuth app
integrations rather than service principal secrets, so anything behind a sign in
needs a Databricks account, and none of the three target users has one. REE's
terms say the same thing from the other direction: what gets published has to be
served from our own infrastructure rather than by calling theirs.

So the data is published. This reads the gold tables, recomputes the two
validations from what is already on disk, and writes a single JSON document.
Nothing here calls an API and nothing needs a warehouse, which means the export
is reproducible on a laptop and in CI, and the file is small enough to deploy
with the application.

    python scripts/export_public_data.py
    python scripts/export_public_data.py --out app/public/data.json

The interval series is written as parallel arrays rather than a list of objects.
Sixty six market days is about 6,300 intervals, and repeating six key names on
every one of them triples the file for nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.analysis.market_splitting import build_spread_series  # noqa: E402
from iberian.analysis.validation import (  # noqa: E402
    cost_validation,
    price_source_agreement,
)
from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.ingestion.esios import (  # noqa: E402
    IndicatorResponse,
    congestion_rent_by_day,
    to_frame,
)

#: Document codes as a reader would check them. Extracted from the explanation
#: text rather than stored separately, because the agent is required to name its
#: source inline and these are exactly the tokens it named.
_SOURCE = re.compile(r"\bA\d{2}\b")


def read_gold(root: Path, name: str) -> pd.DataFrame:
    path = root / "gold" / f"{name}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def read_silver(root: Path, name: str) -> pd.DataFrame:
    path = root / "silver" / f"{name}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def esios_frame(raw_dir: Path) -> pd.DataFrame:
    """Every ESIOS payload on disk, parsed. No network, no token."""
    responses = []
    for path in sorted(raw_dir.glob("esios/indicator=*/*.json")):
        indicator_id = int(path.parent.name.split("=", 1)[1])
        responses.append(
            IndicatorResponse(indicator_id=indicator_id, content=path.read_bytes())
        )
    return to_frame(responses) if responses else pd.DataFrame()


def number(value, digits: int = 2):
    """A JSON number, or null. NaN is not valid JSON and json.dump emits it anyway."""
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def load_explanations(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for line in path.open():
        record = json.loads(line)
        if not record.get("grounded") or not record.get("text"):
            continue
        out[record["episode_key"]] = {
            "text": record["text"],
            "model": record.get("model", ""),
            "claims": record.get("numeric_claims", 0),
            "attempts": record.get("attempts", 1),
            "sources": sorted(set(_SOURCE.findall(record["text"]))),
        }
    return out


def load_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    sheet = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {
        row["episode_key"]: {
            "cause": row.get("true_cause", "").strip(),
            "confidence": row.get("confidence", "").strip(),
        }
        for _, row in sheet.iterrows()
        if row.get("true_cause", "").strip()
    }


def build(root: Path, raw_dir: Path, evaluation: Path) -> dict:
    intervals = read_gold(root, "gold_interval_premium")
    episodes = read_gold(root, "gold_split_episodes")
    profile = read_gold(root, "gold_daily_profile")

    if intervals.empty or episodes.empty:
        raise SystemExit(
            f"No gold tables under {root}. Run scripts/build_medallion.py first."
        )

    intervals = intervals.sort_values("ts_utc").reset_index(drop=True)
    explanations = load_explanations(evaluation / "explanations.jsonl")
    labels = load_labels(evaluation / "episodes.csv")

    # --- the two validations, recomputed from what is on disk ---------------
    prices = read_silver(root, "silver_entsoe_prices")
    omie = read_silver(root, "silver_omie_prices")
    agreement = pd.DataFrame()
    if not prices.empty and not omie.empty:
        agreement = price_source_agreement(
            build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN), omie
        )

    rent = esios_frame(raw_dir)
    cost = pd.DataFrame()
    if not rent.empty:
        cost = cost_validation(episodes, congestion_rent_by_day(rent))

    # --- episodes, newest and worst first ------------------------------------
    episode_rows = []
    for _, row in episodes.sort_values("start_utc", ascending=False).iterrows():
        start = pd.Timestamp(row["start_utc"])
        key = f"{row['market_day']}T{start:%H%M}"
        explanation = explanations.get(key, {})
        label = labels.get(key, {})
        episode_rows.append(
            {
                "key": key,
                "day": str(row["market_day"]),
                "start": start.isoformat(),
                "end": pd.Timestamp(row["end_utc"]).isoformat(),
                "hours": number(row.get("duration_hours")),
                "intervals": int(row.get("intervals", 0) or 0),
                "peak_spread": number(row.get("peak_spread")),
                "side": row.get("premium_side", ""),
                "severity": row.get("max_severity", ""),
                "cost_eur": number(row.get("extra_cost_eur"), 0),
                "share_saturated": number(row.get("share_saturated"), 3),
                "cause": label.get("cause", ""),
                "confidence": label.get("confidence", ""),
                "explanation": explanation.get("text", ""),
                "sources": explanation.get("sources", []),
                "model": explanation.get("model", ""),
                "claims": explanation.get("claims", 0),
            }
        )

    # --- per market day, for the timeline ------------------------------------
    by_day = (
        intervals.assign(day=intervals["market_day"].astype(str))
        .groupby("day")
        .agg(
            intervals=("ts_utc", "count"),
            decoupled=("is_decoupled", "sum"),
            worst_premium=("abs_premium_eur_mwh", "max"),
            mean_utilisation=("utilisation", "mean"),
            max_utilisation=("utilisation", "max"),
        )
        .reset_index()
    )
    cost_by_day = (
        episodes.assign(day=episodes["market_day"].astype(str))
        .groupby("day")["extra_cost_eur"]
        .sum()
    )
    by_day["cost_eur"] = by_day["day"].map(cost_by_day).fillna(0.0)

    days = [
        {
            "day": row["day"],
            "decoupled": int(row["decoupled"]),
            "intervals": int(row["intervals"]),
            "worst_premium": number(row["worst_premium"]),
            "mean_utilisation": number(row["mean_utilisation"], 4),
            "max_utilisation": number(row["max_utilisation"], 4),
            "cost_eur": number(row["cost_eur"], 0),
        }
        for _, row in by_day.sort_values("day").iterrows()
    ]

    hours = [
        {
            "hour": int(row["hour_of_day_utc"]),
            "split_probability": number(row.get("split_probability"), 4),
            "mean_premium": number(row.get("mean_premium_eur_mwh"), 3),
            "worst_premium": number(row.get("worst_premium_eur_mwh")),
            "mean_utilisation": number(row.get("mean_utilisation"), 4),
        }
        for _, row in profile.sort_values("hour_of_day_utc").iterrows()
    ] if not profile.empty else []

    # --- the interval series, as parallel arrays ------------------------------
    series = {
        "ts": [int(pd.Timestamp(t).timestamp()) for t in intervals["ts_utc"]],
        "pt": [number(v) for v in intervals["price_pt_eur_mwh"]],
        "es": [number(v) for v in intervals["price_es_eur_mwh"]],
        "premium": [number(v) for v in intervals["premium_eur_mwh"]],
        "utilisation": [number(v, 4) for v in intervals.get("utilisation", [])],
        "decoupled": [bool(v) for v in intervals["is_decoupled"]],
    }

    headline = {
        "episodes": int(len(episodes)),
        "explained": sum(1 for row in episode_rows if row["explanation"]),
        "total_cost_eur": number(episodes["extra_cost_eur"].sum(), 0),
        "worst_spread": number(episodes["peak_spread"].abs().max()),
        "decoupled_share": number(
            intervals["is_decoupled"].mean() * 100 if len(intervals) else 0, 2
        ),
    }
    if not agreement.empty:
        headline["price_intervals"] = int(len(agreement))
        headline["price_agreeing"] = int(agreement["agrees"].sum())
        headline["worst_price_difference"] = number(
            agreement[["pt_difference", "es_difference"]].max().max(), 4
        )
    if not cost.empty:
        ours = cost["our_cost_eur"].sum()
        theirs = cost["congestion_rent_eur"].sum()
        headline["ree_rent_eur"] = number(theirs, 0)
        headline["cost_difference_pct"] = (
            number((ours - theirs) / theirs * 100.0, 4) if theirs else None
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coverage": {
            "first_day": days[0]["day"] if days else None,
            "last_day": days[-1]["day"] if days else None,
            "days": len(days),
            "intervals": int(len(intervals)),
        },
        "headline": headline,
        "hours": hours,
        "days": days,
        "episodes": episode_rows,
        "series": series,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/lakehouse")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--evaluation", default="evaluation")
    parser.add_argument("--out", default="app/public/data.json")
    args = parser.parse_args()

    payload = build(Path(args.root), Path(args.raw_dir), Path(args.evaluation))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Separators without spaces, because this file is shipped rather than read.
    out.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False))

    head = payload["headline"]
    coverage = payload["coverage"]
    print(f"{out}  {out.stat().st_size / 1024:.0f} KB")
    print(f"  {coverage['days']} market days, {coverage['intervals']} intervals")
    print(f"  {head['episodes']} episodes, {head['explained']} with an explanation")
    if "price_agreeing" in head:
        print(
            f"  OMIE agreement: {head['price_agreeing']}/{head['price_intervals']}, "
            f"worst difference {head['worst_price_difference']}"
        )
    if "cost_difference_pct" in head:
        print(
            f"  REE congestion rent: {head['ree_rent_eur']:,.0f} EUR against "
            f"{head['total_cost_eur']:,.0f}, {head['cost_difference_pct']}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())