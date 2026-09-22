"""A78 notices on their way into a table, and back out of it.

The thing worth pinning is that the trip does not change the answer. If the
vector path and the direct path can disagree about which asset was tightest,
the evaluation that compares them measures our round trip rather than the
retrieval.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.notices import (  # noqa: E402
    COLUMN_TYPES,
    COLUMNS,
    breakpoints_from_json,
    notice_id,
    notice_row,
    notice_rows,
    notice_text,
)
from iberian.parsing.entsoe_outages import (  # noqa: E402
    OutageCurve,
    minimum_capacity,
)

START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


def curve(**overrides) -> OutageCurve:
    row = dict(
        document_mrid="doc-1",
        series_mrid="series-1",
        created_at=datetime(2026, 7, 23, 17, 13, tzinfo=timezone.utc),
        business_type="A53",
        asset_mrid="asset-1",
        asset_name="Alcochete-Palmela",
        asset_location="Palmela",
        psr_type="B21",
        in_domain="10YPT-REN------W",
        out_domain="10YES-REE------0",
        start_utc=START,
        end_utc=START + timedelta(days=10),
        resolution_minutes=60,
        breakpoints=[
            (START, 3100.0),
            (START + timedelta(days=2), 2800.0),
            (START + timedelta(days=5), 3400.0),
        ],
    )
    row.update(overrides)
    return OutageCurve(**row)


# --- the key ----------------------------------------------------------------


def test_the_id_holds_the_document_the_series_and_the_period():
    assert notice_id(curve()) == "doc-1:series-1:20260801T0000"


def test_two_publications_of_the_same_notice_are_two_rows():
    """A republished notice is a new document, and the old one still existed.

    Keeping only the latest would hand an episode a version published after it,
    which is the leak the publication filter exists to stop.
    """
    first = curve()
    second = curve(
        document_mrid="doc-2",
        created_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        breakpoints=[(START, 2000.0)],
    )
    rows = notice_rows([first, second])
    assert len(rows) == 2
    assert {row["published_epoch"] for row in rows} == {
        int(first.created_at.timestamp()),
        int(second.created_at.timestamp()),
    }


def test_the_same_notice_seen_on_many_days_is_one_row():
    # The window is fetched day by day, so a notice spanning ten days comes
    # back ten times. The count should be notices, not notice-days.
    assert len(notice_rows([curve(), curve(), curve()])) == 1


# --- the shape ---------------------------------------------------------------


def test_every_row_has_every_column_in_order():
    for row in notice_rows([curve()]):
        assert tuple(row) == COLUMNS


def test_the_declared_types_cover_exactly_the_columns():
    """The Spark schema cannot be built here, so this is what guards it.

    A column added to `COLUMNS` and forgotten here would reach the cluster as a
    MERGE that fails on a schema mismatch, which is a slow way to find out.
    """
    assert tuple(COLUMN_TYPES) == COLUMNS


def test_the_epochs_match_the_timestamps():
    row = notice_row(curve())
    assert row["published_epoch"] == int(row["published_at"].timestamp())
    assert row["outage_start_epoch"] == int(row["outage_start"].timestamp())
    assert row["outage_end_epoch"] == int(row["outage_end"].timestamp())


# --- the text that gets embedded ---------------------------------------------


def test_the_text_names_the_asset_the_border_and_the_source():
    text = notice_text(curve())
    assert "Alcochete-Palmela" in text
    assert "Spain to Portugal border" in text
    assert "ENTSO-E A78" in text
    assert "2,800 MW" in text, "the lowest capacity in the notice"


def test_the_text_says_the_operator_did_not_identify_the_asset():
    # Same rule as the fact sheet: "unnamed asset" reads as though the name was
    # lost here, when the publisher never gave one.
    text = notice_text(curve(asset_name=None, asset_mrid=None))
    assert "did not identify" in text
    assert "unnamed asset" not in text


def test_the_text_uses_readable_zone_names_rather_than_eic_codes():
    assert "10YES" not in notice_text(curve())


# --- the round trip, which is the point --------------------------------------


def test_the_curve_survives_the_trip_through_json():
    original = curve()
    restored = breakpoints_from_json(notice_row(original)["breakpoints_json"])
    assert restored == original.breakpoints


def test_the_tightest_capacity_is_the_same_on_both_paths():
    """The direct path asks the curve, the vector path asks the stored JSON."""
    subject = curve()
    window_start = START + timedelta(days=3)
    window_end = START + timedelta(days=4)

    direct = subject.minimum_between(window_start, window_end)
    restored = breakpoints_from_json(notice_row(subject)["breakpoints_json"])
    through_table = minimum_capacity(restored, window_start, window_end)

    assert direct == through_table == 2800.0


def test_a_window_before_the_reduction_sees_the_earlier_value():
    # The window opens while the first step is still in force, and no
    # breakpoint falls inside it. The answer is the value at the start.
    subject = curve()
    early = minimum_capacity(
        breakpoints_from_json(notice_row(subject)["breakpoints_json"]),
        START + timedelta(hours=1),
        START + timedelta(hours=2),
    )
    assert early == 3100.0
    assert early == subject.minimum_between(
        START + timedelta(hours=1), START + timedelta(hours=2)
    )


def test_the_notice_wide_minimum_is_not_the_window_minimum():
    """Why the breakpoints have to travel with the row.

    Storing only `min_available_mw` would make the vector path answer 2800 for
    a window where the border actually had 3400, and the disagreement would
    look like a finding about retrieval.
    """
    subject = curve()
    row = notice_row(subject)
    late_start = START + timedelta(days=6)
    late_end = START + timedelta(days=7)

    assert row["min_available_mw"] == 2800.0
    assert subject.minimum_between(late_start, late_end) == 3400.0