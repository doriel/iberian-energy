"""The write actions: what they refuse, what they record, and what they return.

A fake store rather than a database, so every branch runs in milliseconds and
the interesting ones get covered. The interesting ones are the refusals: a
threshold with an extra zero, an episode key that does not exist, a delete
nobody confirmed. Those are the paths a real user finds and the ones a test
against a live database is least likely to reach.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.app.actions import (  # noqa: E402
    MAX_THRESHOLD_EUR_MWH,
    MAX_WRITES_PER_SESSION,
    Actions,
)


class FakeStore:
    """Answers queries from canned rows and remembers every statement."""

    def __init__(self, rows=None, fail_on=None):
        self.rows = rows if rows is not None else {}
        self.fail_on = fail_on
        self.statements: list[tuple[str, tuple]] = []

    def _answer(self, statement, args):
        self.statements.append((statement, tuple(args)))
        if self.fail_on and self.fail_on in statement:
            raise RuntimeError("connection reset by peer")
        for marker, rows in self.rows.items():
            if marker in statement:
                return list(rows)
        return []

    def query(self, statement, args=()):
        return self._answer(statement, args)

    def execute(self, statement, args=()):
        rows = self._answer(statement, args)
        return rows[0] if rows else None


EPISODE = {"episode_key": "2026-08-18T0745"}


def build(rows=None, **kwargs):
    store = FakeStore(rows=rows, **kwargs)
    return store, Actions(store=store, session_id="s-1", created_by="doriel")


def logged(store):
    """Every row that went to agent_actions, as (tool, status, detail)."""
    return [
        (args[2], args[4], args[5])
        for statement, args in store.statements
        if "agent_actions" in statement
    ]


# --- alerts, the happy path ---------------------------------------------------


def test_creating_an_alert_returns_the_stored_row_and_says_what_it_means():
    store, actions = build(rows={"INSERT INTO iberian.alerts": [{"id": 3}]})
    result = actions.create_alert("PT", "above", 20)

    assert result.ok
    assert result.row == {"id": 3}
    assert "PT" in result.message and "above" in result.message
    assert logged(store) == [("create_alert", "ok", result.message)]


def test_creating_the_same_alert_twice_upserts_rather_than_duplicating():
    store, actions = build(rows={"INSERT INTO iberian.alerts": [{"id": 3}]})
    actions.create_alert("PT", "above", 20)
    actions.create_alert("PT", "above", 20)

    writes = [s for s, _ in store.statements if "INSERT INTO iberian.alerts" in s]
    assert all("ON CONFLICT" in statement for statement in writes)


# --- alerts, the refusals -----------------------------------------------------


def test_a_threshold_with_an_extra_zero_is_refused():
    # 20000 rather than 2000. The market cap makes this decidable.
    store, actions = build()
    result = actions.create_alert("PT", "above", 20000)

    assert result.status == "rejected"
    assert "market cap" in result.message
    assert not [s for s, _ in store.statements if "INSERT INTO iberian.alerts" in s]


def test_a_negative_threshold_is_refused():
    _, actions = build()
    assert actions.create_alert("PT", "above", -5).status == "rejected"


def test_a_threshold_that_is_not_a_number_is_refused_with_the_value_quoted():
    _, actions = build()
    result = actions.create_alert("PT", "above", "vinte")

    assert result.status == "rejected"
    assert "vinte" in result.message


def test_an_unknown_zone_is_refused_and_the_options_are_listed():
    _, actions = build()
    result = actions.create_alert("FR", "above", 20)

    assert result.status == "rejected"
    assert "PT" in result.message and "ES" in result.message


def test_the_threshold_at_exactly_the_cap_is_allowed():
    # The boundary belongs to the allowed side, and a test says so rather than
    # leaving the next reader to work it out from the comparison.
    _, actions = build(rows={"INSERT INTO iberian.alerts": [{"id": 1}]})
    assert actions.create_alert("PT", "above", MAX_THRESHOLD_EUR_MWH).ok


# --- updating and deleting ----------------------------------------------------


def test_updating_an_alert_that_is_not_yours_is_refused():
    store, actions = build(rows={})  # the UPDATE returns nothing
    result = actions.update_alert(99, threshold_eur_mwh=30)

    assert result.status == "rejected"
    assert "belonging to you" in result.message


def test_updating_nothing_is_refused_rather_than_run():
    store, actions = build()
    result = actions.update_alert(1)

    assert result.status == "rejected"
    assert not [s for s, _ in store.statements if "UPDATE iberian.alerts" in s]


def test_a_delete_without_confirmation_describes_what_would_go_and_does_not_go():
    store, actions = build(
        rows={
            "SELECT * FROM iberian.alerts": [
                {"id": 4, "zone": "PT", "direction": "above", "threshold_eur_mwh": 20}
            ]
        }
    )
    result = actions.delete_alert(4)

    assert result.status == "rejected"
    assert "permanently delete" in result.message
    assert "20" in result.message, "the person should see what they are confirming"
    assert not [s for s, _ in store.statements if s.strip().startswith("DELETE")]


def test_a_confirmed_delete_goes_ahead():
    store, actions = build(
        rows={
            "SELECT * FROM iberian.alerts": [
                {"id": 4, "zone": "PT", "direction": "above", "threshold_eur_mwh": 20}
            ],
            "DELETE FROM iberian.alerts": [{"id": 4}],
        }
    )
    result = actions.delete_alert(4, confirmed=True)

    assert result.ok
    assert logged(store)[-1][:2] == ("delete_alert", "ok")


# --- labels -------------------------------------------------------------------


def test_a_label_for_an_episode_that_does_not_exist_is_refused():
    store, actions = build(rows={})  # the episode lookup finds nothing
    result = actions.submit_episode_label("not-a-key", "saturation_planned", "high")

    assert result.status == "rejected"
    assert "No episode" in result.message
    assert not [s for s, _ in store.statements if "INSERT INTO iberian.episode_labels" in s]


def test_a_cause_outside_the_vocabulary_is_refused():
    """The closed vocabulary is what keeps the ground truth aggregatable."""
    store, actions = build(rows={"FROM iberian.episodes WHERE": [EPISODE]})
    result = actions.submit_episode_label(
        "2026-08-18T0745", "the border was busy", "high"
    )

    assert result.status == "rejected"
    assert "saturation_planned" in result.message


def test_a_label_is_saved_and_repeating_it_updates_rather_than_duplicates():
    store, actions = build(
        rows={
            "FROM iberian.episodes WHERE": [EPISODE],
            "INSERT INTO iberian.episode_labels": [{"id": 11}],
        }
    )
    result = actions.submit_episode_label(
        "2026-08-18T0745", "saturation_ordinary_capacity", "medium", " notes here "
    )

    assert result.ok
    statement, args = [
        (s, a) for s, a in store.statements if "INSERT INTO iberian.episode_labels" in s
    ][0]
    assert "ON CONFLICT (episode_key, created_by) DO UPDATE" in statement
    assert args[-1] == "notes here", "whitespace should not become a note"


def test_an_empty_note_is_stored_as_nothing_rather_than_an_empty_string():
    store, actions = build(
        rows={
            "FROM iberian.episodes WHERE": [EPISODE],
            "INSERT INTO iberian.episode_labels": [{"id": 11}],
        }
    )
    actions.submit_episode_label("2026-08-18T0745", "unclear", "low", "   ")

    args = [a for s, a in store.statements if "INSERT INTO iberian.episode_labels" in s][0]
    assert args[-1] is None


# --- the audit log ------------------------------------------------------------


def test_a_rejection_is_recorded_as_rejected_and_not_as_an_error():
    """The distinction is what makes a tool success rate mean anything.

    A rate that counts refused input as failure goes down when validation gets
    stricter, which is exactly backwards.
    """
    store, actions = build()
    actions.create_alert("PT", "above", -5)

    assert logged(store)[0][:2] == ("create_alert", "rejected")


def test_a_database_failure_is_recorded_as_an_error_and_not_raised():
    store, actions = build(fail_on="INSERT INTO iberian.alerts")
    result = actions.create_alert("PT", "above", 20)

    assert result.status == "error"
    assert "RuntimeError" in result.message
    assert logged(store)[0][:2] == ("create_alert", "error")


def test_a_failing_audit_log_does_not_break_the_action_that_succeeded():
    """The alert was created. Telling the user otherwise because the logging
    failed would be the worse of the two lies."""
    store, actions = build(
        rows={"INSERT INTO iberian.alerts": [{"id": 3}]},
        fail_on="agent_actions",
    )
    result = actions.create_alert("PT", "above", 20)

    assert result.ok


def test_every_action_carries_how_long_it_took():
    ticks = iter([0.0, 0.25, 0.25, 0.25])
    store = FakeStore(rows={"INSERT INTO iberian.alerts": [{"id": 1}]})
    actions = Actions(
        store=store, session_id="s", created_by="d", clock=lambda: next(ticks)
    )
    assert actions.create_alert("PT", "above", 20).latency_ms == 250


# --- the session limit --------------------------------------------------------


def test_a_session_cannot_write_forever():
    # The application is public. A limit is the difference between a demo and
    # an open invitation.
    store, actions = build(rows={"INSERT INTO iberian.alerts": [{"id": 1}]})
    for index in range(MAX_WRITES_PER_SESSION):
        assert actions.create_alert("PT", "above", index + 1).ok

    stopped = actions.create_alert("PT", "above", 1)
    assert stopped.status == "rejected"
    assert "limit" in stopped.message


def test_refusals_do_not_count_towards_the_limit():
    # Otherwise a user who mistypes a few times loses writes they never made.
    store, actions = build()
    for _ in range(10):
        actions.create_alert("PT", "above", -1)

    assert actions._writes == 0


# --- reads --------------------------------------------------------------------


def test_the_unlabelled_listing_is_scoped_to_the_person_asking():
    """What makes the application usable by a visitor: it offers them episodes
    nobody has judged rather than a list that is already answered."""
    store, actions = build(rows={"FROM iberian.episodes e": [EPISODE]})
    actions.list_episodes(unlabelled_only=True)

    statement, args = store.statements[0]
    assert "NOT EXISTS" in statement
    assert args[0] == "doriel"


def test_the_listing_limit_is_clamped_rather_than_trusted():
    store, actions = build()
    actions.list_episodes(limit=100000)

    assert store.statements[0][1][-1] == 100