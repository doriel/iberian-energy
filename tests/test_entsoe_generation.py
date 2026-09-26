"""Parsing actual generation per unit, against documents rather than hope.

Seven hundred files are going through this. Every awkward case here is one that
would otherwise be found by an ingestion that ran for an hour and produced rows
nobody could explain.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.parsing.entsoe_generation import (  # noqa: E402
    generation_points,
    generation_rows,
    is_acknowledgement,
    units_in,
)

NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"

#: The Portuguese control area, as the real documents write it. Both the
#: generation and the consumption series carry this same value, which is the
#: point: which element holds it is the information, not what it says.
PT_ZONE = "10YPT-REN------W"


def document(*series: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<GL_MarketDocument xmlns="{NS}">{"".join(series)}</GL_MarketDocument>'
    )


def series(
    unit="48W000000ALMA-1F",
    name="Almaraz I",
    psr="B14",
    start="2026-09-23T00:00Z",
    end="2026-09-24T00:00Z",
    resolution="PT60M",
    quantities=(950.0, 948.0),
    first_position=1,
    direction="in",
    business_type="A01",
    curve_type="A03",
) -> str:
    """One TimeSeries, shaped like the ones ENTSO-E actually sends.

    `direction` is "in", "out" or "none": the real documents carry exactly one
    of the two bidding zone elements, and "none" exists to pin down what this
    module does with a document that carries neither.
    """
    points = "".join(
        f"<Point><position>{first_position + index}</position>"
        f"<quantity>{value}</quantity></Point>"
        for index, value in enumerate(quantities)
    )
    zone_element = {
        "in": f'<inBiddingZone_Domain.mRID codingScheme="A01">{PT_ZONE}'
              "</inBiddingZone_Domain.mRID>",
        "out": f'<outBiddingZone_Domain.mRID codingScheme="A01">{PT_ZONE}'
               "</outBiddingZone_Domain.mRID>",
        "none": "",
    }[direction]
    return (
        "<TimeSeries>"
        f"<mRID>1</mRID><businessType>{business_type}</businessType>"
        "<objectAggregation>A06</objectAggregation>"
        f"{zone_element}"
        "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>"
        f"<curveType>{curve_type}</curveType>"
        f"<MktPSRType><psrType>{psr}</psrType>"
        f"<PowerSystemResources><mRID>{unit}</mRID><name>{name}</name>"
        "</PowerSystemResources></MktPSRType>"
        f"<Period><timeInterval><start>{start}</start>"
        f"<end>{end}</end></timeInterval>"
        f"<resolution>{resolution}</resolution>{points}</Period>"
        "</TimeSeries>"
    )


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=timezone.utc)


# --- the ordinary case ---------------------------------------------------------


def test_a_unit_becomes_one_row_per_point():
    rows = generation_points(document(series()), zone="ES")

    assert len(rows) == 2
    assert rows[0].unit_eic == "48W000000ALMA-1F"
    assert rows[0].unit_name == "Almaraz I"
    assert rows[0].quantity_mw == 950.0
    assert rows[1].quantity_mw == 948.0


def test_the_production_type_is_labelled_as_well_as_coded():
    """B14 means nothing to a person reading the gold table."""
    rows = generation_points(document(series(psr="B14")), zone="ES")
    assert rows[0].psr_type == "B14"
    assert rows[0].psr_label == "Nuclear"


def test_an_unknown_production_type_is_carried_through_not_dropped():
    """A new code appearing is information, not an error."""
    rows = generation_points(document(series(psr="B99")), zone="ES")
    assert rows[0].psr_type == "B99"
    assert rows[0].psr_label == "B99"


def test_the_zone_comes_from_the_caller():
    rows = generation_points(document(series()), zone="PT")
    assert {row.zone for row in rows} == {"PT"}


# --- generation and consumption, which is where the duplicates came from -------


def test_a_series_flowing_into_the_zone_is_generation():
    rows = generation_points(document(series(direction="in")), zone="PT")
    assert {row.flow_direction for row in rows} == {"generation"}


def test_a_series_flowing_out_of_the_zone_is_consumption():
    rows = generation_points(document(series(direction="out")), zone="PT")
    assert {row.flow_direction for row in rows} == {"consumption"}


def test_a_unit_publishing_both_directions_gives_two_distinguishable_rows():
    """The bug this column exists for.

    Aguieira publishes generation and consumption for the same unit, the same
    production type and the same hour, as two TimeSeries. Without the direction
    they collapse onto one key, and deduplicating them would erase half of what
    pumped storage does.
    """
    xml = document(
        series(unit="16WAGUIE1------O", psr="B10", direction="out", quantities=(2.4,)),
        series(unit="16WAGUIE1------O", psr="B10", direction="in", quantities=(45.2,)),
    )
    rows = generation_points(xml, zone="PT")

    assert len(rows) == 2
    assert {row.ts_utc for row in rows} == {at(0)}
    assert {row.flow_direction for row in rows} == {"consumption", "generation"}

    keys = {
        (row.zone, row.unit_eic, row.psr_type, row.flow_direction, row.ts_utc)
        for row in rows
    }
    assert len(keys) == 2, "the direction has to make the key unique"


def test_consumption_is_not_limited_to_pumped_storage():
    """Measured, not assumed. On 2025-09-26 the Lares combined cycle and the
    Carrapatelo run-of-river station both published their own consumption."""
    xml = document(
        series(unit="16WLARES2------3", psr="B04", direction="out", quantities=(2.6,)),
        series(unit="16WCARRA3------X", psr="B11", direction="out", quantities=(0.0,)),
    )
    rows = generation_points(xml, zone="PT")

    assert [row.flow_direction for row in rows] == ["consumption", "consumption"]


def test_the_business_type_is_not_what_decides():
    """Both series carry A01, checked against a real document. A parser that
    keyed on businessType would look right and change nothing."""
    xml = document(
        series(direction="out", business_type="A01", quantities=(1.0,)),
        series(direction="in", business_type="A01", quantities=(2.0,)),
    )
    rows = generation_points(xml, zone="PT")
    assert {row.flow_direction for row in rows} == {"consumption", "generation"}


def test_consumption_keeps_the_sign_the_document_published():
    """It arrives positive in its own series, and is left positive.

    Flipping it to negative would be this module inventing an encoding the
    publisher did not use, and it would make a naive SUM look correct while
    hiding that two different quantities are being added together.
    """
    rows = generation_points(
        document(series(psr="B10", direction="out", quantities=(45.2,))), zone="PT"
    )
    assert rows[0].quantity_mw == 45.2
    assert rows[0].flow_direction == "consumption"


def test_a_series_with_neither_element_is_read_as_generation():
    """A documented default rather than a reading, and the safe one: a null in
    a key column is worse, and the duplicate check would surface it."""
    rows = generation_points(document(series(direction="none")), zone="ES")
    assert {row.flow_direction for row in rows} == {"generation"}


def test_the_dot_in_the_element_name_does_not_defeat_the_lookup():
    """`inBiddingZone_Domain.mRID` contains a dot, and ElementTree's path syntax
    gives a dot its own meaning. This is here because a `find` would silently
    match nothing and every row would come back as generation."""
    rows = generation_points(document(series(direction="out")), zone="PT")
    assert rows[0].flow_direction == "consumption"


def test_the_curve_type_is_carried_so_the_gap_rule_is_visible():
    """A03 is a variable sized block: a published point holds until the next
    position. Nothing here expands those, so the column has to say so."""
    rows = generation_points(document(series(curve_type="A03")), zone="ES")
    assert rows[0].curve_type == "A03"


def test_the_period_end_is_carried_on_every_row():
    """The last A03 block of a period holds until this instant, and a consumer
    without it has to guess where the day stops."""
    rows = generation_points(document(series()), zone="ES")
    assert all(
        row.period_end_utc == datetime(2026, 9, 24, tzinfo=timezone.utc) for row in rows
    )


def test_a_period_that_stops_early_says_so_rather_than_running_to_midnight():
    """The case the column exists for.

    A unit that stopped reporting at noon has a period that ends at noon. The
    two guesses somebody would otherwise make, one resolution step or the end
    of the calendar day, disagree by twelve hours, and one of them would show a
    plant producing through an afternoon it published nothing for.
    """
    rows = generation_points(
        document(series(end="2026-09-23T12:00Z", quantities=(950.0,))), zone="ES"
    )
    assert rows[0].period_end_utc == at(12)


def test_a_period_with_no_end_leaves_it_null_rather_than_inventing_one():
    xml = document(
        "<TimeSeries><curveType>A03</curveType>"
        "<MktPSRType><psrType>B16</psrType>"
        "<PowerSystemResources><mRID>48W0000000SOL-1</mRID><name>Solar</name>"
        "</PowerSystemResources></MktPSRType>"
        "<Period><timeInterval><start>2026-09-23T00:00Z</start></timeInterval>"
        "<resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>5</quantity></Point></Period>"
        "</TimeSeries>"
    )
    rows = generation_points(xml, zone="ES")

    assert len(rows) == 1
    assert rows[0].period_end_utc is None


# --- timestamps, which is where a silent error would live ----------------------


def test_positions_become_timestamps_at_the_hourly_step():
    rows = generation_points(document(series(resolution="PT60M")), zone="ES")
    assert rows[0].ts_utc == at(0)
    assert rows[1].ts_utc == at(1)


def test_positions_become_timestamps_at_the_quarter_hourly_step():
    """Spain publishes at this resolution and it is most of the data."""
    rows = generation_points(
        document(series(resolution="PT15M", quantities=(10, 11, 12, 13, 14))),
        zone="ES",
    )
    assert [row.ts_utc for row in rows[:5]] == [
        at(0, 0), at(0, 15), at(0, 30), at(0, 45), at(1, 0)
    ]


def test_a_period_that_does_not_start_at_midnight_is_respected():
    rows = generation_points(
        document(series(start="2026-09-23T13:00Z", resolution="PT60M")), zone="ES"
    )
    assert rows[0].ts_utc == at(13)


def test_a_sparse_first_position_is_not_treated_as_the_start():
    """Position is what places a reading in time, not its order in the file."""
    rows = generation_points(
        document(series(resolution="PT60M", quantities=(500,), first_position=7)),
        zone="ES",
    )
    assert rows[0].position == 7
    assert rows[0].ts_utc == at(6)


def test_a_gap_stays_a_gap():
    """The documents carry curveType A03, where a point holds until the next
    published position. This does not expand those blocks.

    A repeated value and a measured one are different things, and the
    difference matters to anybody counting how much a plant ran. Carrying a
    value forward is a decision for whoever aggregates, made in the open with a
    window function, not one inherited silently from the parser.
    """
    xml = document(
        series(resolution="PT60M", quantities=(100,), first_position=1),
        series(resolution="PT60M", quantities=(300,), first_position=5),
    )
    rows = generation_points(xml, zone="ES")

    assert len(rows) == 2
    assert [row.position for row in rows] == [1, 5]


def test_an_unrecognised_resolution_leaves_the_step_null_rather_than_guessing():
    """Visibly wrong beats silently shifted."""
    rows = generation_points(document(series(resolution="PT7M")), zone="ES")

    assert rows[0].resolution_minutes is None
    assert rows[0].ts_utc == at(0)
    assert rows[1].ts_utc == at(0)


def test_every_timestamp_is_utc_aware():
    """A naive timestamp joined against the market intervals is an outage."""
    rows = generation_points(document(series()), zone="ES")
    assert all(row.ts_utc.tzinfo is not None for row in rows)


# --- the two zones publish differently -----------------------------------------


def test_the_resolution_is_kept_rather_than_resampled():
    """Portugal is hourly and Spain is quarter hourly, and neither is converted.

    Upsampling the Portuguese hour into four quarters would invent three
    readings nobody measured, in a project whose claim is that every figure
    traces to a published document.
    """
    spain = generation_points(document(series(resolution="PT15M")), zone="ES")
    portugal = generation_points(document(series(resolution="PT60M")), zone="PT")

    assert spain[0].resolution_minutes == 15
    assert portugal[0].resolution_minutes == 60


# --- what the real documents actually contain ----------------------------------


def test_a_unit_appearing_twice_is_not_deduplicated():
    """Different production types, or a period split. The key is not the unit."""
    xml = document(
        series(psr="B04", quantities=(10,)),
        series(psr="B10", quantities=(20,)),
    )
    rows = generation_points(xml, zone="ES")

    assert len(rows) == 2
    assert {row.psr_type for row in rows} == {"B04", "B10"}


def test_a_negative_quantity_is_kept_rather_than_clamped():
    """Consumption arrives as its own positive series, so a negative here is
    something else. Whatever it is, it is what the document said."""
    rows = generation_points(document(series(psr="B10", quantities=(-45.5,))), zone="ES")
    assert rows[0].quantity_mw == -45.5


def test_a_series_with_no_unit_is_dropped_rather_than_given_one():
    xml = document(
        "<TimeSeries><MktPSRType><psrType>B16</psrType></MktPSRType>"
        "<Period><timeInterval><start>2026-09-23T00:00Z</start>"
        "<end>2026-09-24T00:00Z</end></timeInterval><resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>5</quantity></Point></Period>"
        "</TimeSeries>",
        series(),
    )
    rows = generation_points(xml, zone="ES")

    assert len(rows) == 2
    assert all(row.unit_eic for row in rows)


def test_a_point_missing_its_quantity_is_skipped_not_zeroed():
    xml = document(
        "<TimeSeries><MktPSRType><psrType>B16</psrType>"
        "<PowerSystemResources><mRID>48W0000000SOL-1</mRID><name>Solar</name>"
        "</PowerSystemResources></MktPSRType>"
        "<Period><timeInterval><start>2026-09-23T00:00Z</start>"
        "<end>2026-09-24T00:00Z</end></timeInterval><resolution>PT60M</resolution>"
        "<Point><position>1</position></Point>"
        "<Point><position>2</position><quantity>8</quantity></Point></Period>"
        "</TimeSeries>"
    )
    rows = generation_points(xml, zone="ES")

    assert len(rows) == 1
    assert rows[0].quantity_mw == 8.0


# --- what ENTSO-E sends when it will not answer --------------------------------


def test_an_acknowledgement_is_recognised_and_its_reason_read():
    """It refuses with a document, not a status code.

    A caller that only catches exceptions treats "no data for this day" as a
    success and lands an empty file that looks like a quiet day.
    """
    xml = (
        '<?xml version="1.0"?><Acknowledgement_MarketDocument '
        'xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:8:0">'
        "<Reason><code>999</code><text>No matching data found</text></Reason>"
        "</Acknowledgement_MarketDocument>"
    )
    declined, reason = is_acknowledgement(xml)

    assert declined is True
    assert reason == "No matching data found"
    assert generation_points(xml, zone="ES") == []


def test_an_ordinary_document_is_not_an_acknowledgement():
    declined, _ = is_acknowledgement(document(series()))
    assert declined is False


def test_an_empty_response_is_no_rows_rather_than_an_exception():
    assert generation_points("", zone="ES") == []
    assert generation_points("   ", zone="ES") == []


def test_a_document_with_a_different_schema_version_still_parses():
    """The namespace is read off the root, so a version bump does not silently
    return nothing."""
    xml = document(series()).replace(
        "generationloaddocument:3:0", "generationloaddocument:4:2"
    )
    assert len(generation_points(xml, zone="ES")) == 2


def test_the_direction_survives_a_schema_version_bump():
    """The direction is read by local name, so it does not depend on the URI."""
    xml = document(series(direction="out")).replace(
        "generationloaddocument:3:0", "generationloaddocument:4:2"
    )
    assert generation_points(xml, zone="ES")[0].flow_direction == "consumption"


# --- the shapes handed on ------------------------------------------------------


def test_rows_carry_every_column_the_silver_table_needs():
    row = generation_rows(document(series()), zone="ES")[0]
    assert set(row) == {
        "zone", "unit_eic", "unit_name", "psr_type", "psr_label",
        "flow_direction", "ts_utc", "resolution_minutes", "quantity_mw",
        "position", "curve_type", "period_end_utc",
    }


def test_the_unit_dimension_is_discovered_from_the_data():
    """Rather than maintained by hand, which is the only way it stays right as
    plants are commissioned and retired."""
    xml = document(
        series(unit="48W000000ALMA-1F", name="Almaraz I"),
        series(unit="16W-ALQUE1-----X", name="Alqueva - G1"),
    )
    assert units_in(xml, zone="ES") == {
        "48W000000ALMA-1F": "Almaraz I",
        "16W-ALQUE1-----X": "Alqueva - G1",
    }


# --- documents that are not quite documents ------------------------------------


def test_a_byte_order_mark_does_not_stop_it():
    """ENTSO-E sends them. The older parser strips one, which is how we know."""
    rows = generation_points("﻿" + document(series()), zone="ES")
    assert len(rows) == 2


def test_leading_whitespace_does_not_stop_it():
    """ElementTree refuses a declaration that is not at the very start."""
    rows = generation_points("\n  " + document(series()), zone="ES")
    assert len(rows) == 2


def test_an_acknowledgement_behind_a_byte_order_mark_is_still_recognised():
    xml = "﻿" + (
        '<?xml version="1.0"?><Acknowledgement_MarketDocument>'
        "<Reason><text>No matching data found</text></Reason>"
        "</Acknowledgement_MarketDocument>"
    )
    declined, reason = is_acknowledgement(xml)
    assert declined is True
    assert reason == "No matching data found"


def test_a_fragment_says_what_probably_happened():
    """The failure that cost an afternoon, turned into a message.

    Reading the files a line at a time instead of whole gives the parser an XML
    declaration on its own. ElementTree's own message is "no element found",
    which is true and points nowhere.
    """
    import pytest

    with pytest.raises(ValueError) as caught:
        generation_points('<?xml version="1.0" encoding="UTF-8"?>', zone="ES")

    message = str(caught.value)
    assert "wholetext" in message
    assert "line at a time" in message


def test_a_truncated_document_is_reported_rather_than_returning_nothing():
    """Silently returning no rows for a broken file loses a day without saying so."""
    import pytest

    with pytest.raises(ValueError):
        generation_points(document(series())[:120], zone="ES")