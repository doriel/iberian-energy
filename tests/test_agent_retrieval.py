"""The vector path against the direct path, on the same notices.

Every test here exists to answer one question: if the evaluation reports that
the two paths disagree, is that a finding about retrieval or a bug in the
translation? These pin the translation, so a disagreement in the evaluation has
only one explanation left.

The fake index is deliberately literal. It applies the filters the way the
endpoint documents them, returns rows as `data_array` with a manifest and a
trailing score, and orders them by nothing in particular, because a real vector
index orders by similarity and this code must not depend on that order.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.notices import notice_row  # noqa: E402
from iberian.agent.retrieval import (  # noqa: E402
    COLUMNS,
    assets_from_rows,
    binding_from_index,
    comparable,
    filters,
    rows_from_response,
    vector_assets,
)
from iberian.parsing.entsoe_outages import OutageCurve, binding_assets  # noqa: E402

SPAIN = "10YES-REE------0"
PORTUGAL = "10YPT-REN------W"
DIRECTION = (SPAIN, PORTUGAL)

DAY = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
EPISODE_START = DAY + timedelta(hours=10)
EPISODE_END = DAY + timedelta(hours=14)


def curve(**overrides) -> OutageCurve:
    row = dict(
        document_mrid="doc-1",
        series_mrid="series-1",
        created_at=DAY - timedelta(days=9),
        business_type="A53",
        asset_mrid="asset-1",
        asset_name="Alcochete-Palmela",
        asset_location="Palmela",
        psr_type="B21",
        in_domain=PORTUGAL,
        out_domain=SPAIN,
        start_utc=DAY,
        end_utc=DAY + timedelta(days=3),
        resolution_minutes=60,
        breakpoints=[
            (DAY, 3100.0),
            (DAY + timedelta(hours=12), 900.0),
            (DAY + timedelta(hours=20), 3400.0),
        ],
    )
    row.update(overrides)
    return OutageCurve(**row)


class FakeIndex:
    """The notices, filtered the way the endpoint says it filters them."""

    def __init__(self, curves):
        self.rows = [notice_row(one) for one in curves]
        self.last_call: dict = {}

    @staticmethod
    def _matches(row: dict, clauses: dict) -> bool:
        for clause, wanted in clauses.items():
            column, _, operator = clause.partition(" ")
            value = row.get(column)
            if value is None:
                return False
            if operator == "<=" and not value <= wanted:
                return False
            if operator == ">" and not value > wanted:
                return False
            if operator == "" and value != wanted:
                return False
        return True

    def similarity_search(self, query_text, columns, num_results, filters):
        self.last_call = {
            "query_text": query_text,
            "columns": columns,
            "num_results": num_results,
            "filters": filters,
        }
        kept = [row for row in self.rows if self._matches(row, filters)]
        # Reversed, so nothing downstream can be relying on insertion order.
        kept = list(reversed(kept))[:num_results]
        return {
            "manifest": {
                "columns": [{"name": name} for name in columns] + [{"name": "score"}]
            },
            "result": {
                "data_array": [
                    [row.get(name) for name in columns] + [0.61] for row in kept
                ]
            },
        }


def direct(curves, start=EPISODE_START, end=EPISODE_END, published_before=None):
    return binding_assets(
        curves, start, end, published_before=published_before, direction=DIRECTION
    )


def through_index(curves, start=EPISODE_START, end=EPISODE_END, published_before=None):
    return vector_assets(
        FakeIndex(curves),
        start,
        end,
        published_before=published_before,
        direction=DIRECTION,
    )


# --- the two paths agree ------------------------------------------------------


def test_one_notice_reads_the_same_on_both_paths():
    curves = [curve()]
    assert [comparable(row) for row in through_index(curves)] == [
        comparable(row) for row in direct(curves)
    ]


def test_the_window_minimum_is_used_not_the_notice_minimum():
    # The episode runs 10:00 to 14:00 and the step down to 900 happens at 12:00,
    # so the binding value is 900. An episode earlier in the day would see 3100.
    assert through_index([curve()])[0]["available_mw"] == 900.0

    early = through_index(
        [curve()], start=DAY + timedelta(hours=2), end=DAY + timedelta(hours=4)
    )
    assert early[0]["available_mw"] == 3100.0


def test_the_tightest_asset_is_first_on_both_paths():
    tight = curve(
        document_mrid="doc-2",
        series_mrid="series-2",
        asset_mrid="asset-2",
        asset_name="Cedillo-Falagueira",
        breakpoints=[(DAY, 400.0)],
    )
    curves = [curve(), tight]

    assert direct(curves)[0]["asset"] == "Cedillo-Falagueira"
    assert through_index(curves)[0]["asset"] == "Cedillo-Falagueira"


def test_an_unidentified_asset_reads_the_same_on_both_paths():
    """The placeholder label has to match, or the fact sheet branches wrongly."""
    curves = [curve(asset_name=None, asset_mrid=None)]
    through = through_index(curves)[0]

    assert through["asset"] == direct(curves)[0]["asset"] == "unnamed asset"
    assert through["asset_named"] is False


# --- the point in time guarantee ---------------------------------------------


def test_a_notice_published_after_the_episode_is_not_retrieved():
    """The leak this whole design exists to prevent.

    Not a hypothetical: on the real endpoint, without the filter, the notice
    published after the episode came back ranked first.
    """
    later = curve(
        document_mrid="doc-3",
        series_mrid="series-3",
        created_at=EPISODE_START + timedelta(days=7),
        asset_name="Published-Afterwards",
        breakpoints=[(DAY, 100.0)],
    )
    curves = [curve(), later]

    assets = through_index(curves, published_before=EPISODE_START)
    assert [row["asset"] for row in assets] == ["Alcochete-Palmela"]

    # And without the filter it is retrieved, and would have been the tightest.
    unfiltered = through_index(curves)
    assert unfiltered[0]["asset"] == "Published-Afterwards"


def test_a_notice_published_in_the_same_second_is_excluded():
    # A tie goes to exclusion: a notice published at the instant of the anomaly
    # is not evidence of its cause.
    simultaneous = curve(created_at=EPISODE_START)
    assert through_index([simultaneous], published_before=EPISODE_START) == []


def test_the_published_filter_is_absent_when_no_cutoff_is_given():
    # Publishing an unbounded filter as `<= None` would fail at the endpoint
    # rather than in a test, so the absence is asserted here.
    assert "published_epoch <=" not in filters(EPISODE_START, EPISODE_END)


# --- the other filters --------------------------------------------------------


def test_the_other_direction_is_not_retrieved():
    reversed_border = curve(
        document_mrid="doc-4",
        series_mrid="series-4",
        out_domain=PORTUGAL,
        in_domain=SPAIN,
        asset_name="Wrong-Direction",
        breakpoints=[(DAY, 50.0)],
    )
    curves = [curve(), reversed_border]
    assert [row["asset"] for row in through_index(curves)] == ["Alcochete-Palmela"]
    assert [row["asset"] for row in direct(curves)] == ["Alcochete-Palmela"]


def test_a_notice_that_ended_before_the_episode_is_not_retrieved():
    earlier = curve(
        document_mrid="doc-5",
        series_mrid="series-5",
        start_utc=DAY - timedelta(days=5),
        end_utc=DAY - timedelta(days=4),
        breakpoints=[(DAY - timedelta(days=5), 10.0)],
    )
    assert through_index([earlier]) == []
    assert direct([earlier]) == []


def test_a_row_that_slips_past_the_filter_is_still_dropped():
    """The guard in `assets_from_rows`, exercised without the index.

    If the endpoint's boundary semantics turn out to differ from the documented
    ones, this is what stops a capacity from outside the window being reported
    as the binding one.
    """
    outside = curve(
        start_utc=DAY - timedelta(days=5),
        end_utc=DAY - timedelta(days=4),
        breakpoints=[(DAY - timedelta(days=5), 10.0)],
    )
    assert assets_from_rows([notice_row(outside)], EPISODE_START, EPISODE_END) == []


# --- the mechanics ------------------------------------------------------------


def test_the_response_is_read_by_the_manifest_not_by_position():
    """The endpoint appends a score. Assuming the requested order loses a column."""
    response = {
        "manifest": {"columns": [{"name": "asset"}, {"name": "score"}]},
        "result": {"data_array": [["Alcochete-Palmela", 0.64]]},
    }
    assert rows_from_response(response) == [
        {"asset": "Alcochete-Palmela", "score": 0.64}
    ]


def test_an_empty_result_is_no_assets_rather_than_an_error():
    # A day with no transmission outages is ordinary, and the fact sheet already
    # knows how to say that nothing was in force.
    assert rows_from_response({"result": {"data_array": []}}) == []
    assert through_index([]) == []


def test_the_query_asks_for_the_columns_the_translation_needs():
    index = FakeIndex([curve()])
    vector_assets(index, EPISODE_START, EPISODE_END, direction=DIRECTION)
    assert index.last_call["columns"] == list(COLUMNS)
    assert "breakpoints_json" in index.last_call["columns"], (
        "without the curve the window minimum cannot be computed"
    )


def test_the_index_binding_has_the_direct_functions_signature():
    """What lets `sheet_builder` take either one without knowing which."""
    curves = [curve()]
    binding = binding_from_index(FakeIndex(curves))

    from_index = binding(
        curves,
        EPISODE_START,
        EPISODE_END,
        published_before=EPISODE_START,
        direction=DIRECTION,
    )
    from_api = binding_assets(
        curves,
        EPISODE_START,
        EPISODE_END,
        published_before=EPISODE_START,
        direction=DIRECTION,
    )
    assert [comparable(row) for row in from_index] == [
        comparable(row) for row in from_api
    ]