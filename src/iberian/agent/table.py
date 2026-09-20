"""The agent's explanations as a table rather than as a file.

The explanations are the distinctive part of this project and they were the one
thing in the system that was not a table. They live as JSONL in a Unity Catalog
Volume, which is governed storage, but a file cannot be joined to
`gold_split_episodes`, cannot be queried in SQL, and does not appear in
lineage. A journalist asking "which episodes have an explanation naming an
asset" has no way to ask it.

So the same records are also written as Delta. The JSONL stays the record of
work, because it is what the publish task reads and what the evaluation merges
into, and the table is a materialisation of it. That direction matters: if the
two ever disagree, the file is right and the table is stale, and rebuilding the
table costs one overwrite of a few hundred rows.

Nothing here touches Spark. The frame is assembled in pandas so the shape,
the types and the derived columns are covered by the test suite in
milliseconds, and the notebook only has to hand it to `createDataFrame` with an
explicit schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Sequence

import pandas as pd

#: Column order, fixed here rather than left to whatever the first record
#: happens to contain. A table whose columns move when a key is added to a
#: record is a table nobody can write a query against.
COLUMNS: tuple[str, ...] = (
    "episode_key",
    "market_day",
    "start_utc",
    "grounded",
    "attempts",
    "model",
    "text",
    "numeric_claims",
    "unsupported",
    "missing_sources",
    "wrong_dates",
    "sources",
    "final_draft",
    "rejected_drafts",
    "true_cause",
    "generated_at",
)

#: The columns holding lists. Kept as a name so the notebook's Spark schema and
#: these tests cannot drift apart silently.
ARRAY_COLUMNS: tuple[str, ...] = (
    "unsupported",
    "wrong_dates",
    "sources",
    "rejected_drafts",
)


def episode_start(key: str) -> datetime:
    """The episode's start, recovered from its key.

    The key is `2026-08-01T1100`, which is the market day and the UTC start
    time. Deriving the timestamp here rather than carrying it in the record
    keeps one definition of the key: a record and a table that disagreed about
    when an episode started would be worse than not having the column.
    """
    day, clock = key.split("T")
    return datetime(
        int(day[0:4]), int(day[5:7]), int(day[8:10]),
        int(clock[0:2]), int(clock[2:4]),
        tzinfo=timezone.utc,
    )


def _strings(value) -> list[str]:
    """A list of strings, whatever shape the record stored."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value]


def to_row(record: dict, generated_at: datetime) -> dict:
    key = str(record["episode_key"])
    start = episode_start(key)
    return {
        "episode_key": key,
        "market_day": start.date(),
        "start_utc": start,
        "grounded": bool(record.get("grounded")),
        "attempts": int(record.get("attempts") or 0),
        "model": str(record.get("model") or ""),
        # A rejected explanation has no text on purpose: the guarantee is that
        # an unverified answer is never shown, and a table that carried it
        # would be the obvious place for somebody to read it from anyway.
        "text": str(record.get("text") or ""),
        "numeric_claims": int(record.get("numeric_claims") or 0),
        "unsupported": _strings(record.get("unsupported")),
        "missing_sources": bool(record.get("missing_sources")),
        "wrong_dates": _strings(record.get("wrong_dates")),
        "sources": _strings(record.get("sources")),
        "final_draft": str(record.get("final_draft") or ""),
        "rejected_drafts": _strings(record.get("rejected_drafts")),
        "true_cause": str(record.get("true_cause") or ""),
        "generated_at": generated_at,
    }


def explanation_rows(
    records: Iterable[dict], generated_at: datetime | None = None
) -> list[dict]:
    """The rows as plain Python, which is what Spark should be handed.

    Deliberately not the pandas frame. Going through pandas turns the integers
    into `numpy.int64`, and `createDataFrame` with an explicit `IntegerType`
    refuses those, so the notebook would fail at the write with an error about
    a type nobody wrote. Plain dicts of built-in types cross that boundary
    without an opinion.

    `generated_at` is when the table was built, not when the explanation was
    written: the records do not carry their own timestamp and inventing one
    from the file's mtime would be a figure that looks like evidence and is
    not. It is passed in rather than read from the clock so a test can assert
    the rows without freezing time.
    """
    stamp = generated_at or datetime.now(timezone.utc)
    rows = [to_row(record, stamp) for record in records]
    rows.sort(key=lambda row: row["start_utc"])
    return rows


def explanations_frame(
    records: Iterable[dict], generated_at: datetime | None = None
) -> pd.DataFrame:
    """The same rows as a pandas frame, for local work and for the tests."""
    frame = pd.DataFrame(
        explanation_rows(records, generated_at), columns=list(COLUMNS)
    )
    if frame.empty:
        # An empty frame still needs the right columns, or the first write of
        # the table would create it with no schema at all.
        return frame.astype({"attempts": "int64", "numeric_claims": "int64"})
    return frame


def summarise_table(frame: pd.DataFrame) -> str:
    """One line for the notebook to print, so a run says what it wrote."""
    if frame.empty:
        return "0 explanations"
    grounded = int(frame["grounded"].sum())
    first = int(((frame["attempts"] == 1) & frame["grounded"]).sum())
    return (
        f"{len(frame)} explanations, {grounded} grounded, "
        f"{first} on the first attempt, "
        f"{frame['market_day'].min()} to {frame['market_day'].max()}"
    )


def spark_schema():
    """The Spark schema, built only when Spark is present.

    Imported inside the function so this module stays importable on a laptop
    with no PySpark, which is the rule the whole library follows: the test
    suite must not need a cluster.

    The schema is explicit rather than inferred because inference on an empty
    array column produces a null typed array, and the first run with no
    rejections anywhere would create a table whose `unsupported` column can
    never hold a string.
    """
    from pyspark.sql.types import (
        ArrayType,
        BooleanType,
        DateType,
        IntegerType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    strings = ArrayType(StringType())
    return StructType(
        [
            StructField("episode_key", StringType(), False),
            StructField("market_day", DateType(), False),
            StructField("start_utc", TimestampType(), False),
            StructField("grounded", BooleanType(), False),
            StructField("attempts", IntegerType(), False),
            StructField("model", StringType(), True),
            StructField("text", StringType(), True),
            StructField("numeric_claims", IntegerType(), False),
            StructField("unsupported", strings, True),
            StructField("missing_sources", BooleanType(), False),
            StructField("wrong_dates", strings, True),
            StructField("sources", strings, True),
            StructField("final_draft", StringType(), True),
            StructField("rejected_drafts", strings, True),
            StructField("true_cause", StringType(), True),
            StructField("generated_at", TimestampType(), False),
        ]
    )


def column_comments() -> dict[str, str]:
    """What each column means, for `COMMENT ON COLUMN`.

    A gold table a journalist or a grader is expected to query should say what
    it holds without anybody having to read this file.
    """
    return {
        "episode_key": "Market day and UTC start time, the key used everywhere",
        "market_day": "Iberian market day, which begins at local midnight CET",
        "start_utc": "First interval of the episode, UTC",
        "grounded": "Every figure traced to a retrieved document, and a source named",
        "attempts": "Generation attempts, 2 means the first draft was rejected",
        "model": "Serving endpoint that wrote it",
        "text": "The explanation. Empty when not grounded: an unverified answer is never shown",
        "numeric_claims": "Figures the verifier checked in the final draft",
        "unsupported": "Figures that matched no retrieved value, when rejected",
        "missing_sources": "True when no retrieved document was named",
        "wrong_dates": "Dates stated that are in no retrieved fact and no part of the window",
        "sources": "Documents behind the fact sheet this explanation was given",
        "final_draft": "The rejected text, kept for diagnosis. Empty when grounded",
        "rejected_drafts": "Earlier drafts rejected before the final one",
        "true_cause": "Human label from evaluation/episodes.csv, blank if unlabelled",
        "generated_at": "When this table was built, not when the explanation was written",
    }