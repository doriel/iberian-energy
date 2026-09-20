"""Which episodes get explained, and what happens to the ones already done.

This is the logic that decides whether a daily task costs three model calls or
a hundred and twenty-six, and whether an explanation a human has read gets
quietly rewritten. Both are worth a test rather than a careful reading.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.batch import (  # noqa: E402
    episode_key,
    sheet_builder,
    explain_episodes,
    load_records,
    merge,
    pending,
    write_records,
)
from iberian.agent.facts import Fact, FactSheet  # noqa: E402


def episodes(*keys: str) -> pd.DataFrame:
    rows = []
    for key in keys:
        day, time = key.split("T")
        rows.append(
            {
                "market_day": date.fromisoformat(day),
                "start_utc": pd.Timestamp(f"{day}T{time[:2]}:{time[2:]}:00Z"),
                "end_utc": pd.Timestamp(f"{day}T{time[:2]}:{time[2:]}:00Z")
                + pd.Timedelta(hours=1),
            }
        )
    return pd.DataFrame(rows)


def sheet_for(_episode) -> FactSheet:
    return FactSheet(
        subject="episode",
        start_utc=datetime(2026, 8, 18, 7, 45, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 18, 15, 15, tzinfo=timezone.utc),
        market_day=date(2026, 8, 18),
        facts=[Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead")],
    )


GROUNDED = "Portugal paid 109.84 EUR/MWh more (ENTSO-E A44 day-ahead)."
INVENTED = "Portugal paid 109.84 more, with 412 MW lost (ENTSO-E A44 day-ahead)."


# --- the key ----------------------------------------------------------------


def test_the_key_is_the_market_day_and_the_start_time():
    frame = episodes("2026-08-18T0745")
    assert episode_key(frame.iloc[0]) == "2026-08-18T0745"


# --- what still needs doing -------------------------------------------------


def test_everything_is_pending_when_nothing_has_been_explained():
    frame = episodes("2026-08-18T0745", "2026-08-19T0730")
    assert len(pending(frame, [])) == 2


def test_an_explained_episode_is_not_re_explained():
    frame = episodes("2026-08-18T0745", "2026-08-19T0730")
    existing = [{"episode_key": "2026-08-18T0745", "grounded": True}]
    remaining = pending(frame, existing)
    assert len(remaining) == 1
    assert episode_key(remaining.iloc[0]) == "2026-08-19T0730"


def test_a_rejected_episode_counts_as_done():
    # At temperature zero a re-run produces the same rejection, so retrying it
    # every day would spend a model call a day to learn nothing.
    frame = episodes("2026-08-18T0745")
    existing = [{"episode_key": "2026-08-18T0745", "grounded": False}]
    assert pending(frame, existing).empty


def test_no_episodes_at_all_is_not_an_error():
    assert pending(pd.DataFrame(), []).empty


# --- merging ----------------------------------------------------------------


def test_merge_keeps_what_was_there_and_adds_what_is_new():
    merged = merge(
        [{"episode_key": "a", "text": "old"}],
        [{"episode_key": "b", "text": "new"}],
    )
    assert {r["episode_key"] for r in merged} == {"a", "b"}


def test_a_deliberate_re_explanation_wins():
    merged = merge(
        [{"episode_key": "a", "text": "old"}],
        [{"episode_key": "a", "text": "corrected"}],
    )
    assert len(merged) == 1
    assert merged[0]["text"] == "corrected"


def test_a_round_trip_through_the_file_keeps_every_record(tmp_path):
    path = tmp_path / "explanations.jsonl"
    records = [{"episode_key": "a", "text": "one"}, {"episode_key": "b", "text": "two"}]
    write_records(path, records)
    assert load_records(path) == records


def test_a_missing_file_reads_as_nothing_rather_than_failing(tmp_path):
    assert load_records(tmp_path / "does-not-exist.jsonl") == []


# --- the loop ---------------------------------------------------------------


def test_each_episode_produces_one_record():
    frame = episodes("2026-08-18T0745", "2026-08-19T0730")
    records = list(
        explain_episodes(frame, sheet_for, lambda s, u: GROUNDED, model="stub")
    )
    assert [r["episode_key"] for r in records] == ["2026-08-18T0745", "2026-08-19T0730"]
    assert all(r["grounded"] for r in records)


def test_a_rejected_draft_is_kept_rather_than_discarded():
    frame = episodes("2026-08-18T0745")
    records = list(
        explain_episodes(frame, sheet_for, lambda s, u: INVENTED, model="stub")
    )
    record = records[0]
    assert record["grounded"] is False
    assert record["text"] == "", "a rejected draft must never be published"
    assert "412" in record["final_draft"], "and must still be kept for diagnosis"
    assert any("412" in claim for claim in record["unsupported"])


def test_records_arrive_one_at_a_time():
    # Yielded rather than returned, so a batch that dies on episode forty does
    # not lose the first thirty-nine.
    frame = episodes("2026-08-18T0745", "2026-08-19T0730", "2026-08-11T0730")
    stream = explain_episodes(frame, sheet_for, lambda s, u: GROUNDED, model="stub")
    first = next(stream)
    assert first["episode_key"] == "2026-08-18T0745"


def test_the_label_is_attached_when_one_exists():
    frame = episodes("2026-08-18T0745")
    records = list(
        explain_episodes(
            frame,
            sheet_for,
            lambda s, u: GROUNDED,
            model="stub",
            truth_for=lambda key: "saturation_planned",
        )
    )
    assert records[0]["true_cause"] == "saturation_planned"


def test_the_sources_are_recorded_with_the_explanation():
    frame = episodes("2026-08-18T0745")
    records = list(
        explain_episodes(frame, sheet_for, lambda s, u: GROUNDED, model="stub")
    )
    assert records[0]["sources"] == ["ENTSO-E A44 day-ahead"]


def test_the_callback_sees_every_episode():
    seen = []
    frame = episodes("2026-08-18T0745", "2026-08-19T0730")
    list(
        explain_episodes(
            frame,
            sheet_for,
            lambda s, u: GROUNDED,
            model="stub",
            on_each=lambda key, result: seen.append(key),
        )
    )
    assert seen == ["2026-08-18T0745", "2026-08-19T0730"]


# --- the shared retrieval closure -------------------------------------------


class FakeResponse:
    is_empty = False


class FakeClient:
    """Counts calls, so the day-level caching can be asserted rather than hoped."""

    def __init__(self) -> None:
        self.days: list = []

    def transmission_unavailability(self, *args):
        self.days.append(args[2])
        return FakeResponse()


def intervals_for(*keys: str) -> pd.DataFrame:
    rows = []
    for key in keys:
        day, time = key.split("T")
        rows.append(
            {
                "ts_utc": pd.Timestamp(f"{day}T{time[:2]}:{time[2:]}:00Z"),
                "price_pt_eur_mwh": 110.0,
                "price_es_eur_mwh": 1.0,
                "premium_eur_mwh": 109.0,
                "abs_premium_eur_mwh": 109.0,
                "is_decoupled": True,
                "utilisation": 1.0,
                "market_day": date.fromisoformat(day),
            }
        )
    return pd.DataFrame(rows)


def test_the_days_notices_are_fetched_once_per_market_day():
    client = FakeClient()
    frame = episodes("2026-08-18T0745", "2026-08-18T1000", "2026-08-19T0730")
    build = sheet_builder(
        client,
        intervals_for("2026-08-18T0745", "2026-08-18T1000", "2026-08-19T0730"),
        ("ES", "PT"),
        parse=lambda response: [],
        binding=lambda *a, **k: [],
    )
    for _, episode in frame.iterrows():
        build(episode)
    assert len(client.days) == 2, "three episodes over two days is two fetches"


def test_only_notices_published_before_the_episode_are_considered():
    # The point in time filter is what stops the evaluation leaking information
    # from the future, so the argument reaching `binding` is worth asserting.
    seen = {}

    def binding(curves, start, end, published_before, direction):
        seen["published_before"] = published_before
        seen["start"] = start
        return []

    frame = episodes("2026-08-18T0745")
    build = sheet_builder(
        FakeClient(),
        intervals_for("2026-08-18T0745"),
        ("ES", "PT"),
        parse=lambda response: [],
        binding=binding,
    )
    build(frame.iloc[0])
    assert seen["published_before"] == seen["start"]