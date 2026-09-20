"""The explanations as a table: shape, types, and the derived columns.

The table is what anybody other than this project will actually query, so the
things worth pinning are the ones a query breaks on: a column that moves, a
list column that arrives as a string, a timestamp that is not a timestamp, and
a rejected explanation whose text leaks into the column a reader would read.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.table import (  # noqa: E402
    ARRAY_COLUMNS,
    COLUMNS,
    column_comments,
    episode_start,
    explanation_rows,
    explanations_frame,
    summarise_table,
)

STAMP = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)


def grounded(key: str = "2026-08-18T0745") -> dict:
    return {
        "episode_key": key,
        "model": "databricks-claude-haiku-4-5",
        "grounded": True,
        "attempts": 1,
        "unsupported": [],
        "missing_sources": False,
        "wrong_dates": [],
        "numeric_claims": 6,
        "text": "Portugal paid 109.84 EUR/MWh more (ENTSO-E A44 day-ahead).",
        "final_draft": "",
        "rejected_drafts": [],
        "true_cause": "saturation_planned",
        "sources": ["ENTSO-E A44 day-ahead", "ENTSO-E A61 day-ahead capacity"],
    }


def rejected(key: str = "2026-08-01T1100") -> dict:
    record = grounded(key)
    record.update(
        grounded=False,
        attempts=2,
        unsupported=["412"],
        text="",
        final_draft="Portugal paid 109.84 more, with 412 MW lost (A44).",
        rejected_drafts=["an earlier attempt"],
    )
    return record


# --- the key, and what is derived from it -----------------------------------


def test_the_start_is_recovered_from_the_key():
    assert episode_start("2026-08-01T1100") == datetime(
        2026, 8, 1, 11, 0, tzinfo=timezone.utc
    )


def test_the_market_day_comes_from_the_key_too():
    frame = explanations_frame([grounded("2026-08-01T1100")], STAMP)
    assert frame.loc[0, "market_day"] == date(2026, 8, 1)


def test_a_midnight_episode_keeps_its_hour():
    assert episode_start("2026-12-31T0000").hour == 0


# --- shape ------------------------------------------------------------------


def test_the_column_order_is_fixed():
    frame = explanations_frame([grounded()], STAMP)
    assert tuple(frame.columns) == COLUMNS


def test_an_empty_set_of_records_still_has_the_columns():
    # The first run on a fresh workspace has nothing to write, and a table
    # created with no columns can never be appended to.
    frame = explanations_frame([], STAMP)
    assert tuple(frame.columns) == COLUMNS
    assert frame.empty


def test_rows_come_out_in_time_order():
    frame = explanations_frame(
        [grounded("2026-09-03T1800"), grounded("2026-08-01T1100")], STAMP
    )
    assert list(frame["episode_key"]) == ["2026-08-01T1100", "2026-09-03T1800"]


def test_every_list_column_is_a_list_even_when_empty():
    frame = explanations_frame([grounded()], STAMP)
    for column in ARRAY_COLUMNS:
        assert isinstance(frame.loc[0, column], list), column


def test_a_string_where_a_list_was_expected_is_not_exploded_into_characters():
    # An older record wrote `unsupported` as a single string. Reading it as an
    # iterable would produce one row of single characters and no error.
    record = grounded()
    record["unsupported"] = "412"
    frame = explanations_frame([record], STAMP)
    assert frame.loc[0, "unsupported"] == ["412"]


def test_a_record_missing_the_newer_keys_still_becomes_a_row():
    # Records written before `wrong_dates` and `sources` existed are in the
    # file already, and the table has to be buildable from them.
    old = {"episode_key": "2026-08-18T0745", "grounded": True, "attempts": 1}
    frame = explanations_frame([old], STAMP)
    assert frame.loc[0, "wrong_dates"] == []
    assert frame.loc[0, "sources"] == []
    assert frame.loc[0, "numeric_claims"] == 0


# --- what the columns are allowed to contain --------------------------------


def test_a_rejected_explanation_has_no_text_and_keeps_its_draft():
    frame = explanations_frame([rejected()], STAMP)
    assert frame.loc[0, "text"] == "", "an unverified answer must never be readable"
    assert "412" in frame.loc[0, "final_draft"]
    assert frame.loc[0, "unsupported"] == ["412"]


def test_generated_at_is_when_the_table_was_built():
    frame = explanations_frame([grounded()], STAMP)
    assert frame.loc[0, "generated_at"] == STAMP


def test_the_timestamps_are_timestamps_rather_than_strings():
    frame = explanations_frame([grounded()], STAMP)
    assert isinstance(frame.loc[0, "start_utc"], (datetime, pd.Timestamp))


# --- what Spark is handed ---------------------------------------------------


def test_the_rows_are_built_in_types_rather_than_numpy_ones():
    """`createDataFrame` with an explicit IntegerType refuses `numpy.int64`.

    The failure that would cause happens at the write, on a cluster, with an
    error naming a type nobody wrote, so it is worth catching here.
    """
    rows = explanation_rows([grounded()], STAMP)
    assert type(rows[0]["attempts"]) is int
    assert type(rows[0]["numeric_claims"]) is int
    assert type(rows[0]["grounded"]) is bool
    assert type(rows[0]["episode_key"]) is str


def test_the_rows_are_sorted_the_same_way_the_frame_is():
    keys = [
        row["episode_key"]
        for row in explanation_rows(
            [grounded("2026-09-03T1800"), grounded("2026-08-01T1100")], STAMP
        )
    ]
    assert keys == ["2026-08-01T1100", "2026-09-03T1800"]


def test_every_row_has_every_column_and_no_others():
    rows = explanation_rows([grounded(), rejected()], STAMP)
    for row in rows:
        assert tuple(row) == COLUMNS


# --- the summary line -------------------------------------------------------


def test_the_summary_counts_grounded_and_first_attempt_separately():
    line = summarise_table(
        explanations_frame([grounded(), rejected(), grounded("2026-08-19T0730")], STAMP)
    )
    assert "3 explanations" in line
    assert "2 grounded" in line
    assert "2 on the first attempt" in line


def test_the_summary_of_nothing_says_nothing_rather_than_failing():
    assert summarise_table(explanations_frame([], STAMP)) == "0 explanations"


# --- the documentation that ships with the table ----------------------------


def test_every_column_carries_a_comment():
    # A gold table a stranger is expected to query should explain itself, and
    # the gap this catches is a column added here and documented nowhere.
    assert set(column_comments()) == set(COLUMNS)