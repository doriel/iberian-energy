"""Keep one row per settlement interval when a document was published twice.

The local build parses the responses it just requested, so it never sees the
same interval twice. A pipeline reading a landing zone does: every backfill
leaves its files behind, ranges overlap, and a day requested in a seven day run
and again in a thirty one day run arrives as two documents.

That is not corruption. ENTSO-E republishes corrected documents, and the
transparency platform's own semantics are that a later publication supersedes
an earlier one for the same interval. So the rule is the market's rule, not a
convenience: keep the most recently landed row per key.

Deciding this here rather than inside the pipeline keeps one code path and lets
the rule be tested, including the case that matters, where the two publications
disagree and the newer one has to win.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

PRICE_KEYS = ("zone_eic", "ts_utc", "market")
QUANTITY_KEYS = ("ts_utc", "in_domain", "out_domain", "label")


def conflicts(
    frame: pd.DataFrame, keys: Sequence[str], value_column: str
) -> pd.DataFrame:
    """Keys carrying more than one distinct value across publications.

    Worth reporting rather than swallowing: a key that was republished with the
    same value is routine, and one republished with a different value is a
    correction someone may want to know about.
    """
    present = [key for key in keys if key in frame.columns]
    if frame.empty or not present or value_column not in frame.columns:
        return frame.iloc[0:0]

    counts = frame.groupby(list(present))[value_column].nunique()
    return counts[counts > 1].reset_index(name="distinct_values")


def latest_per_key(
    frame: pd.DataFrame,
    keys: Sequence[str],
    published_column: str = "landed_at",
) -> pd.DataFrame:
    """One row per key, from the most recent publication.

    Rows missing the publication timestamp sort oldest, so a row that does
    carry one always wins over a row that does not. Without the column at all
    the last occurrence wins, which is the order the files were read in.
    """
    present = [key for key in keys if key in frame.columns]
    if frame.empty or not present:
        return frame

    ordered = frame
    if published_column in frame.columns:
        ordered = frame.sort_values(
            published_column, ascending=True, na_position="first", kind="stable"
        )

    return (
        ordered.drop_duplicates(subset=present, keep="last")
        .sort_values(present[1] if len(present) > 1 else present[0], kind="stable")
        .reset_index(drop=True)
    )