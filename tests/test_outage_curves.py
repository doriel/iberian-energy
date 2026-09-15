"""A78 capacity curves and the point in time filter.

Two things these pin down. First, an A78 notice is a sparse step function at
PT1M over months, so it must stay sparse: expanding it per minute would be
half a million rows for one asset. Second, cause attribution must not be able
to retrieve a notice published after the anomaly it claims to explain.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.config import EIC_PORTUGAL, EIC_SPAIN  # noqa: E402
from iberian.parsing.entsoe_outages import (  # noqa: E402
    binding_assets,
    parse_transmission_outages,
)

NS = "urn:iec62325.351:tc57wg16:451-6:outagedocument:3:0"


def outage_document(
    points: list[tuple[int, float]],
    created: str = "2026-09-01T10:00:00Z",
    business_type: str = "A53",
    asset_name: str = "Pereiros-Rio Maior 1",
    start: str = "2026-09-03T00:00Z",
    end: str = "2026-09-05T00:00Z",
) -> str:
    points_xml = "".join(
        f"<Point><position>{position}</position><quantity>{quantity}</quantity></Point>"
        for position, quantity in points
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Unavailability_MarketDocument xmlns="{NS}">
  <mRID>doc-1</mRID>
  <createdDateTime>{created}</createdDateTime>
  <TimeSeries>
    <mRID>1</mRID>
    <businessType>{business_type}</businessType>
    <in_Domain.mRID>{EIC_PORTUGAL}</in_Domain.mRID>
    <out_Domain.mRID>{EIC_SPAIN}</out_Domain.mRID>
    <Asset_RegisteredResource>
      <mRID>16TLPRRM1------J</mRID>
      <name>{asset_name}</name>
      <location><name>Pereiros</name></location>
      <asset_PSRType><psrType>B21</psrType></asset_PSRType>
    </Asset_RegisteredResource>
    <Available_Period>
      <timeInterval><start>{start}</start><end>{end}</end></timeInterval>
      <resolution>PT1M</resolution>
      {points_xml}
    </Available_Period>
  </TimeSeries>
</Unavailability_MarketDocument>"""


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def test_curve_stays_sparse_rather_than_expanding_every_minute():
    """Two days at PT1M is 2880 minutes. Three breakpoints must stay three."""
    curves = parse_transmission_outages(
        outage_document([(1, 4000.0), (1081, 434.0), (1261, 4000.0)])
    )

    assert len(curves) == 1
    assert len(curves[0].breakpoints) == 3


def test_capacity_is_a_step_function_between_breakpoints():
    curve = parse_transmission_outages(
        outage_document([(1, 4000.0), (1081, 434.0), (1261, 4000.0)])
    )[0]

    # Position 1081 is 1080 minutes after midnight on the 3rd, so 18:00.
    assert curve.capacity_at(utc(3, 17, 59)) == 4000.0
    assert curve.capacity_at(utc(3, 18, 0)) == 434.0
    assert curve.capacity_at(utc(3, 18, 30)) == 434.0
    assert curve.capacity_at(utc(3, 21, 0)) == 4000.0


def test_outside_the_window_returns_nothing():
    curve = parse_transmission_outages(outage_document([(1, 4000.0)]))[0]

    assert curve.capacity_at(utc(2, 23, 0)) is None
    assert curve.capacity_at(utc(6, 0, 0)) is None


def test_minimum_across_an_interval_is_what_binds():
    """A quarter hour spanning a drop is constrained by the lowest value in it."""
    curve = parse_transmission_outages(
        outage_document([(1, 4000.0), (1086, 434.0)])
    )[0]

    # 18:00 to 18:15 contains the drop at 18:05.
    assert curve.minimum_between(utc(3, 18, 0), utc(3, 18, 15)) == 434.0
    assert curve.minimum_between(utc(3, 17, 0), utc(3, 17, 15)) == 4000.0


def test_asset_and_status_are_extracted():
    curve = parse_transmission_outages(
        outage_document([(1, 100.0)], business_type="A54")
    )[0]

    assert curve.asset_name == "Pereiros-Rio Maior 1"
    assert curve.asset_location == "Pereiros"
    assert curve.psr_type == "B21"
    assert curve.status == "unplanned"
    assert curve.label == "Pereiros-Rio Maior 1"


def test_point_in_time_filter_excludes_notices_published_later():
    """The whole evaluation story: no hindsight may reach an explanation."""
    early = parse_transmission_outages(
        outage_document([(1, 500.0)], created="2026-09-01T10:00:00Z",
                        asset_name="Known before")
    )
    late = parse_transmission_outages(
        outage_document([(1, 200.0)], created="2026-09-04T10:00:00Z",
                        asset_name="Published after the fact")
    )
    curves = early + late

    without_filter = binding_assets(curves, utc(3, 18, 0), utc(3, 18, 15))
    assert len(without_filter) == 2

    with_filter = binding_assets(
        curves, utc(3, 18, 0), utc(3, 18, 15), published_before=utc(3, 18, 0)
    )
    assert len(with_filter) == 1
    assert with_filter[0]["asset"] == "Known before"


def test_assets_are_ranked_tightest_first():
    loose = parse_transmission_outages(
        outage_document([(1, 3000.0)], asset_name="Loose")
    )
    tight = parse_transmission_outages(
        outage_document([(1, 400.0)], asset_name="Tight")
    )

    ranked = binding_assets(loose + tight, utc(3, 18, 0), utc(3, 18, 15))

    assert [row["asset"] for row in ranked] == ["Tight", "Loose"]
    assert ranked[0]["available_mw"] == 400.0


def test_direction_filter_keeps_the_two_sides_of_the_border_apart():
    """A constraint the other way does not explain a Portuguese premium."""
    es_to_pt = parse_transmission_outages(
        outage_document([(1, 400.0)], asset_name="Southbound")
    )
    # Same document shape with the domains swapped.
    swapped = outage_document([(1, 100.0)], asset_name="Northbound").replace(
        f"<in_Domain.mRID>{EIC_PORTUGAL}</in_Domain.mRID>", "<in_Domain.mRID>TMP</in_Domain.mRID>"
    ).replace(
        f"<out_Domain.mRID>{EIC_SPAIN}</out_Domain.mRID>",
        f"<out_Domain.mRID>{EIC_PORTUGAL}</out_Domain.mRID>",
    ).replace("<in_Domain.mRID>TMP</in_Domain.mRID>", f"<in_Domain.mRID>{EIC_SPAIN}</in_Domain.mRID>")
    pt_to_es = parse_transmission_outages(swapped)

    curves = es_to_pt + pt_to_es
    assert len(binding_assets(curves, utc(3, 18, 0), utc(3, 18, 15))) == 2

    filtered = binding_assets(
        curves,
        utc(3, 18, 0),
        utc(3, 18, 15),
        direction=(EIC_SPAIN, EIC_PORTUGAL),
    )
    assert [row["asset"] for row in filtered] == ["Southbound"]


def test_notice_that_does_not_cover_the_interval_is_ignored():
    curve = parse_transmission_outages(
        outage_document([(1, 400.0)], start="2026-09-10T00:00Z",
                        end="2026-09-12T00:00Z")
    )

    assert binding_assets(curve, utc(3, 18, 0), utc(3, 18, 15)) == []
