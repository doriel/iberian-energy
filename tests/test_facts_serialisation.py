"""A fact sheet has to survive a round trip without changing what it permits.

The sheet is the verifier's allowlist. Every number in it is a number the model
is allowed to write, so a serialisation that adds one, loses one, or changes one
does not just move data, it moves what counts as a hallucination.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.facts import Fact, FactSheet  # noqa: E402


def sheet() -> FactSheet:
    return FactSheet(
        subject="Market splitting episode starting 2026-08-18 07:45Z",
        start_utc=datetime(2026, 8, 18, 7, 45, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 18, 15, 15, tzinfo=timezone.utc),
        market_day=date(2026, 8, 18),
        facts=[
            Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
            Fact("min_border_capacity", 3195.0, "MW", "ENTSO-E A61"),
            Fact("binding_asset", "AT 2 400/220 SRM", "", "ENTSO-E A78", "planned"),
            Fact("demand_forecast_error", None, "MW", "REE ESIOS 1775"),
        ],
        caveats=["Consistency is not causation."],
    )


def test_a_round_trip_changes_nothing():
    original = sheet()
    restored = FactSheet.from_dict(original.to_dict())
    assert restored == original


def test_it_survives_actual_json():
    # to_dict alone could return objects json.dumps refuses. The wire is JSON,
    # so the test has to be JSON.
    original = sheet()
    restored = FactSheet.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original


def test_the_allowlist_is_identical_after_a_round_trip():
    original = sheet()
    restored = FactSheet.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.numbers() == original.numbers()
    assert restored.numbers() == {109.84, 3195.0}


def test_the_rendered_block_is_identical_after_a_round_trip():
    # What the model is shown must not drift either, or a sheet sent over the
    # wire produces a different prompt from the same sheet used locally.
    original = sheet()
    restored = FactSheet.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.render() == original.render()


def test_timestamps_do_not_become_numbers():
    # An epoch integer in the payload would be a number, and every number in the
    # sheet is a number the verifier permits the model to write.
    payload = json.dumps(sheet().to_dict())
    assert "1755" not in payload
    assert "2026-08-18T07:45:00+00:00" in payload


def test_a_fact_with_no_value_survives():
    restored = FactSheet.from_dict(json.loads(json.dumps(sheet().to_dict())))
    missing = restored.get("demand_forecast_error")
    assert missing is not None
    assert missing.value is None
    assert missing.is_numeric is False


def test_a_string_fact_keeps_its_note_and_source():
    restored = FactSheet.from_dict(json.loads(json.dumps(sheet().to_dict())))
    asset = restored.get("binding_asset")
    assert asset.value == "AT 2 400/220 SRM"
    assert asset.source == "ENTSO-E A78"
    assert asset.note == "planned"


@pytest.mark.parametrize("field", ["subject", "start_utc", "end_utc", "market_day"])
def test_a_sheet_missing_its_window_is_refused(field):
    raw = sheet().to_dict()
    del raw[field]
    with pytest.raises(ValueError, match=field):
        FactSheet.from_dict(raw)


def test_an_empty_sheet_round_trips():
    empty = FactSheet(
        subject="nothing",
        start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 2, tzinfo=timezone.utc),
        market_day=date(2026, 1, 1),
    )
    assert FactSheet.from_dict(empty.to_dict()) == empty
    assert FactSheet.from_dict(empty.to_dict()).numbers() == set()