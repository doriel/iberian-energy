"""Build the dashboard payload from the gold tables.

The web service cannot query the lakehouse. The workspace issues OAuth app
integrations rather than service principal secrets, so anything behind a sign in
needs a Databricks account, and none of the three target users has one. REE's
terms say the same thing from the other direction: what gets published has to be
served from our own infrastructure rather than by calling theirs.

So the data is published. This reads the gold tables and produces a single JSON
document. The file is small enough to deploy with the application, and nothing
in it needs a warehouse at request time.

There are two places the gold tables live, and one payload builder for both:

    LocalFiles      data/lakehouse/**.parquet, built by scripts/build_medallion.py
    UnityCatalog    Delta tables, written by the declarative pipeline

The local build does not produce the two validation tables, so on that path they
are recomputed from silver and from the stored ESIOS payloads. On Databricks the
pipeline already owns them as tables and they are read rather than recomputed.
Either way the numbers come out of the same functions in iberian.analysis.

This lives in the package rather than in scripts/ because both callers import
it: the command line entry point in scripts/export_public_data.py, and the
notebook the daily Job runs. A notebook that adds scripts/ to sys.path works
only as long as it guesses the checkout layout correctly, and it did not.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from iberian.analysis.market_splitting import build_spread_series
from iberian.analysis.validation import cost_validation, price_source_agreement
from iberian.config import EIC_PORTUGAL, EIC_SPAIN
from iberian.ingestion.esios import (
    IndicatorResponse,
    congestion_rent_by_day,
    to_frame,
)
from iberian.market_time import as_utc

#: Document codes as a reader would check them. Extracted from the explanation
#: text rather than stored separately, because the agent is required to name its
#: source inline and these are exactly the tokens it named.
_SOURCE = re.compile(r"\bA\d{2}\b")


class Source:
    """Where a named table comes from.

    A table that does not exist returns an empty frame rather than raising. The
    builder uses that to tell the two environments apart without being told
    which one it is running in.
    """

    def table(self, name: str) -> pd.DataFrame:
        raise NotImplementedError

    def esios_payloads(self) -> pd.DataFrame:
        """Raw ESIOS indicator readings, where they are reachable as files."""
        return pd.DataFrame()


class LocalFiles(Source):
    """The laptop build: Parquet under data/lakehouse, payloads under data/raw."""

    def __init__(self, root: Path, raw_dir: Path) -> None:
        self.root = root
        self.raw_dir = raw_dir

    def table(self, name: str) -> pd.DataFrame:
        for layer in ("gold", "silver", "bronze"):
            path = self.root / layer / f"{name}.parquet"
            if path.exists():
                return pd.read_parquet(path)
        return pd.DataFrame()

    def esios_payloads(self) -> pd.DataFrame:
        responses = []
        for path in sorted(self.raw_dir.glob("esios/indicator=*/*.json")):
            indicator_id = int(path.parent.name.split("=", 1)[1])
            responses.append(
                IndicatorResponse(indicator_id=indicator_id, content=path.read_bytes())
            )
        return to_frame(responses) if responses else pd.DataFrame()


class UnityCatalog(Source):
    """The workspace: Delta tables the declarative pipeline owns.

    Every table here is small. The largest is one row per settlement interval,
    which is a few thousand rows over the window this project covers, so
    collecting to pandas is the cheap option rather than the reckless one.
    """

    def __init__(self, spark, catalog: str, schema: str) -> None:
        self.spark = spark
        self.catalog = catalog
        self.schema = schema

    def table(self, name: str) -> pd.DataFrame:
        full = f"{self.catalog}.{self.schema}.{name}"
        if not self.spark.catalog.tableExists(full):
            return pd.DataFrame()
        return self.spark.table(full).toPandas()


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


def build(
    source: Source,
    evaluation: Path | str,
    explanations: Path | str | None = None,
) -> dict:
    """Assemble the published document.

    `evaluation` holds the human labels, which live in the repository because
    they are ground truth and a person made them. `explanations` defaults to
    sitting beside them, which is where the local build writes them, and is
    passed separately on Databricks because the explain task writes to the
    Volume: within one Job run the Git checkout is fixed at the commit the run
    started from, so a file written by one task cannot be read from the
    checkout by the next.
    """
    # Coerced rather than required, because the notebook builds this path with
    # os.path.join to match the block it shares with 01_build_medallion, and a
    # TypeError three cells in is a poor way to learn that.
    evaluation = Path(evaluation)
    explanations = (
        Path(explanations) if explanations else evaluation / "explanations.jsonl"
    )

    intervals = source.table("gold_interval_premium")
    episodes = source.table("gold_split_episodes")
    profile = source.table("gold_daily_profile")

    if intervals.empty or episodes.empty:
        raise SystemExit(
            "No gold tables found. Run scripts/build_medallion.py, or point this "
            "at a catalog where the pipeline has run."
        )

    # Spark hands back naive timestamps and Parquet hands back tz-aware ones.
    # Fixing that here means the rest of this function cannot tell the
    # difference, and the epoch seconds in the series are right either way.
    intervals = as_utc(intervals, "ts_utc").sort_values("ts_utc").reset_index(drop=True)
    episodes = as_utc(episodes, "start_utc", "end_utc")

    explained = load_explanations(explanations)
    labels = load_labels(evaluation / "episodes.csv")

    # --- the two validations ---------------------------------------------------
    # Read them where the pipeline owns them, recompute them where it does not.
    agreement = source.table("gold_price_source_agreement")
    if agreement.empty:
        prices = source.table("silver_entsoe_prices")
        omie = source.table("silver_omie_prices")
        if not prices.empty and not omie.empty:
            agreement = price_source_agreement(
                build_spread_series(prices, EIC_PORTUGAL, EIC_SPAIN), omie
            )

    cost = source.table("gold_cost_validation")
    if cost.empty:
        rent = source.esios_payloads()
        if not rent.empty:
            cost = cost_validation(episodes, congestion_rent_by_day(rent))

    # --- episodes, newest and worst first ------------------------------------
    episode_rows = []
    for _, row in episodes.sort_values("start_utc", ascending=False).iterrows():
        start = pd.Timestamp(row["start_utc"])
        key = f"{row['market_day']}T{start:%H%M}"
        explanation = explained.get(key, {})
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


def serialise(payload: dict) -> bytes:
    """The exact bytes that get written or pushed, from either entry point.

    Separators without spaces, because this file is shipped rather than read.
    allow_nan=False so a NaN that slipped through fails here instead of
    producing a file that every JSON parser except Python's rejects.
    """
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")


def summarise(payload: dict) -> list[str]:
    head, coverage = payload["headline"], payload["coverage"]
    lines = [
        f"{coverage['days']} market days, {coverage['intervals']} intervals",
        f"{head['episodes']} episodes, {head['explained']} with an explanation",
    ]
    if "price_agreeing" in head:
        lines.append(
            f"OMIE agreement: {head['price_agreeing']}/{head['price_intervals']}, "
            f"worst difference {head['worst_price_difference']}"
        )
    if "cost_difference_pct" in head:
        # Both sides labelled. The first version gave REE's figure a name and
        # ours none, and it was read the wrong way round: as this project
        # counting more than the operator, when it counts slightly less.
        lines.append(
            f"Extra import cost {head['total_cost_eur']:,.0f} EUR vs REE "
            f"congestion rent {head['ree_rent_eur']:,.0f} EUR "
            f"({head['cost_difference_pct']}%)"
        )
    return lines