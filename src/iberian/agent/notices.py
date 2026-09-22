"""A78 notices as rows of a table, and as text a vector index can search.

The agent asks the transparency platform for notices every time it explains an
episode. That works and is point in time correct, but it keeps the evidence
layer outside the lakehouse: the notices are in nobody's table, so nobody can
query them, join them, or see them in lineage, and the retrieval cannot be
compared against anything.

This module turns the parsed curves into rows for `gold_transmission_notices`,
which is the source of the notice vector index. It knows nothing about Spark or
about the index. It converts, and the conversion is tested in milliseconds.

Two decisions are worth stating because they are what makes the index honest.

**Every published version is a row.** A republished notice is a new document
with a new publication time, and the old version was the one in force before it.
Keeping only the latest would mean an episode retrieves a version that did not
exist when it happened, which is the same leak the publication filter exists to
prevent, arriving through the back door.

**The curve travels with the row.** The available capacity of an asset changes
through an outage, and what matters for an episode is the lowest value inside
that episode's window, not the lowest in the notice. Without the breakpoints the
index could only offer the notice-wide minimum, and it would disagree with the
direct query for a reason that is ours rather than the search's.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

from iberian.parsing.entsoe_outages import minimum_capacity

#: Readable names for the two EIC codes this project uses. The index text is
#: read by a model and by a person, and "10YES-REE------0 to 10YPT-REN------W"
#: is neither searchable nor legible.
ZONE_NAMES = {
    "10YES-REE------0": "Spain",
    "10YPT-REN------W": "Portugal",
}


def _epoch(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def zone(eic: str | None) -> str:
    return ZONE_NAMES.get(eic or "", eic or "unknown")


def notice_id(curve) -> str:
    """Stable across runs, unique per published version and per period.

    The document identifies one publication, the series identifies the notice
    inside it, and the period start separates the availability blocks that the
    parser expands into one curve each.
    """
    return ":".join(
        [
            curve.document_mrid or "nodoc",
            curve.series_mrid or "noseries",
            f"{curve.start_utc:%Y%m%dT%H%M}",
        ]
    )


def notice_text(curve) -> str:
    """The sentence that gets embedded, and that a person can read.

    Written as prose rather than as a record dump because it is what the
    embedding sees. It names the asset when the operator published one, and
    says plainly that it did not when it did not, for the same reason the fact
    sheet does: "unnamed asset" reads as though this project lost the name.
    """
    named = bool(curve.asset_name or curve.asset_mrid)
    asset = (
        f"on {curve.label}"
        if named
        else "on an asset the operator did not identify"
    )
    where = f"{zone(curve.out_domain)} to {zone(curve.in_domain)} border"
    lowest = minimum_capacity(curve.breakpoints, curve.start_utc, curve.end_utc)
    capacity = (
        f"Capacity remaining available falls to {lowest:,.0f} MW. "
        if lowest is not None
        else ""
    )
    published = (
        f"Published {curve.created_at:%Y-%m-%d}. " if curve.created_at else ""
    )
    return (
        f"{curve.status.capitalize()} transmission unavailability {asset}, "
        f"{where}. In force from {curve.start_utc:%Y-%m-%d %H:%M}Z to "
        f"{curve.end_utc:%Y-%m-%d %H:%M}Z. {capacity}{published}"
        "Source: ENTSO-E A78 transmission unavailability."
    )


def breakpoints_json(curve) -> str:
    """The step function, as JSON, so it survives a round trip through Delta."""
    return json.dumps(
        [[_epoch(moment), value] for moment, value in curve.breakpoints]
    )


def breakpoints_from_json(raw: str | None) -> list[tuple[datetime, float]]:
    if not raw:
        return []
    return [
        (datetime.fromtimestamp(int(moment), tz=timezone.utc), float(value))
        for moment, value in json.loads(raw)
    ]


def notice_row(curve) -> dict:
    lowest = minimum_capacity(curve.breakpoints, curve.start_utc, curve.end_utc)
    return {
        "notice_id": notice_id(curve),
        "text": notice_text(curve),
        "published_epoch": _epoch(curve.created_at),
        "outage_start_epoch": _epoch(curve.start_utc),
        "outage_end_epoch": _epoch(curve.end_utc),
        "out_domain": curve.out_domain,
        "in_domain": curve.in_domain,
        "asset": curve.asset_name or curve.asset_mrid,
        "asset_named": bool(curve.asset_name or curve.asset_mrid),
        "status": curve.status,
        "business_type": curve.business_type,
        "min_available_mw": lowest,
        "breakpoints_json": breakpoints_json(curve),
        "published_at": curve.created_at,
        "outage_start": curve.start_utc,
        "outage_end": curve.end_utc,
    }


#: Column order, fixed, matching `pipelines/00_setup_notice_index.py`.
COLUMNS: tuple[str, ...] = (
    "notice_id",
    "text",
    "published_epoch",
    "outage_start_epoch",
    "outage_end_epoch",
    "out_domain",
    "in_domain",
    "asset",
    "asset_named",
    "status",
    "business_type",
    "min_available_mw",
    "breakpoints_json",
    "published_at",
    "outage_start",
    "outage_end",
)


#: Column name to Spark type name, in the order the table declares them. Kept
#: as names rather than as type objects so this module stays importable without
#: PySpark, which is the rule the whole library follows: the test suite must not
#: need a cluster. A test asserts these keys are exactly `COLUMNS`.
COLUMN_TYPES: dict[str, str] = {
    "notice_id": "StringType",
    "text": "StringType",
    "published_epoch": "LongType",
    "outage_start_epoch": "LongType",
    "outage_end_epoch": "LongType",
    "out_domain": "StringType",
    "in_domain": "StringType",
    "asset": "StringType",
    "asset_named": "BooleanType",
    "status": "StringType",
    "business_type": "StringType",
    "min_available_mw": "DoubleType",
    "breakpoints_json": "StringType",
    "published_at": "TimestampType",
    "outage_start": "TimestampType",
    "outage_end": "TimestampType",
}


def spark_schema():
    """The Spark schema, built only when Spark is present.

    Explicit rather than inferred: a window with no unnamed assets and no
    missing capacities would otherwise produce null typed columns, and the
    MERGE into a table that declares BOOLEAN and DOUBLE would fail on a day
    that has nothing unusual in it.

    Only `notice_id` is non nullable, matching the table.
    """
    from pyspark.sql import types

    return types.StructType(
        [
            types.StructField(
                column, getattr(types, kind)(), column != "notice_id"
            )
            for column, kind in COLUMN_TYPES.items()
        ]
    )


def notice_rows(curves: Iterable) -> list[dict]:
    """One row per notice, de-duplicated by id, newest publication last.

    Fetching a window day by day returns the same notice once per day it
    covers. The rows are identical, so the last one wins and the count stays
    the number of notices rather than the number of days they span.
    """
    by_id: dict[str, dict] = {}
    for curve in curves:
        row = notice_row(curve)
        by_id[row["notice_id"]] = row
    return [{column: row[column] for column in COLUMNS} for row in by_id.values()]