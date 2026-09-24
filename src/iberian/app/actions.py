"""What the agent is allowed to do, and what happens when it tries.

Every write in the application goes through one of these. They are not thin
wrappers over SQL: each one validates first, records what happened whether it
worked or not, and returns something the interface can show a person.

**Validation is here, not in the agent's prompt.** A model asked nicely to
supply a threshold between 0 and 3000 will mostly do it. The database has a
constraint, which is the real guarantee, but a constraint violation arrives as a
Postgres error nobody can read. So the same rule is checked twice: here, to
produce a sentence a person understands, and in the schema, because the check
here can be bypassed by anything that talks to the database directly.

**A rejection is not an error.** A threshold of minus five is the system working
correctly, and a connection that dropped is not. They are separate states in
`agent_actions` so that a tool success rate means something: mixing them gives a
number that falls when validation gets stricter, which is backwards.

**Every action is recorded, including the ones that fail.** An audit log that
only holds successes cannot answer the question anybody actually asks, which is
what went wrong and how often.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

#: The labelling vocabulary, matching `sql/001_application_tables.sql` and
#: `evaluation/labelling-protocol.md`. Three copies is two too many, and the
#: schema is the one that cannot be skipped, so this list exists to give a
#: readable rejection rather than to be the authority.
CAUSES = (
    "saturation_planned",
    "saturation_unplanned",
    "saturation_no_notice",
    "saturation_ordinary_capacity",
    "not_saturated",
    "threshold_artifact",
    "unclear",
)

CONFIDENCES = ("high", "medium", "low")
ZONES = ("PT", "ES")
DIRECTIONS = ("above", "below")

#: The market price cap. A threshold above it is a typed extra zero, not an
#: intention, and rejecting it costs nothing while storing it produces an alert
#: that can never fire.
MAX_THRESHOLD_EUR_MWH = 3000.0

#: Enough for a demonstration and for real use, low enough that a stranger with
#: a script cannot fill the database. The application is public.
MAX_WRITES_PER_SESSION = 200


class Rejected(Exception):
    """Input the application refuses, in words a person can act on."""


@dataclass
class ActionResult:
    """What the interface renders, and what the transcript records."""

    status: str  # ok, rejected, error
    message: str
    row: dict | None = None
    tool: str = ""
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Actions:
    """The agent's tools, bound to one session and one person.

    `created_by` is whatever name the visitor gave the application. It is not an
    identity in any security sense and is not treated as one: it scopes a
    person's own rows in the interface, and it separates one labeller's
    judgements from another's in the evaluation. Nothing is authorised by it.
    """

    store: Any
    session_id: str
    created_by: str
    clock: Callable[[], float] = time.monotonic
    _writes: int = field(default=0, init=False)

    # --- recording -----------------------------------------------------------

    def _record(
        self,
        tool: str,
        arguments: dict,
        result: ActionResult,
        target_table: str | None = None,
    ) -> None:
        """Append to the audit log, and never let that append break the action.

        If the log write fails the action itself already happened, and raising
        here would tell the user their alert was not created when it was. The
        analytics lose a row; the person keeps the truth.
        """
        try:
            self.store.execute(
                """
                INSERT INTO iberian.agent_actions
                    (session_id, created_by, tool, arguments, status, detail,
                     target_table, target_id, latency_ms)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.session_id,
                    self.created_by,
                    tool,
                    json.dumps(arguments, default=str),
                    result.status,
                    result.message,
                    target_table,
                    (result.row or {}).get("id"),
                    result.latency_ms,
                ),
            )
        except Exception:
            pass

    def _run(self, tool: str, arguments: dict, target_table: str, work) -> ActionResult:
        """One write, timed, validated, recorded, and never raising at the caller.

        The interface gets a result object in every case. A tool that raises
        would have to be wrapped at every call site, and one of those wrappers
        would eventually be forgotten.
        """
        started = self.clock()

        def finish(status: str, message: str, row: dict | None = None) -> ActionResult:
            result = ActionResult(
                status=status,
                message=message,
                row=row,
                tool=tool,
                latency_ms=int((self.clock() - started) * 1000),
            )
            self._record(tool, arguments, result, target_table)
            return result

        if self._writes >= MAX_WRITES_PER_SESSION:
            return finish(
                "rejected",
                f"This session has made {MAX_WRITES_PER_SESSION} changes, which is "
                "the limit. Start a new session to continue.",
            )

        try:
            row, message = work()
        except Rejected as exc:
            return finish("rejected", str(exc))
        except Exception as exc:
            # Broad on purpose. Whatever the database did, the person gets a
            # sentence rather than a traceback, and the detail goes to the log.
            return finish("error", f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")

        self._writes += 1
        return finish("ok", message, row)

    # --- validation ----------------------------------------------------------

    @staticmethod
    def _one_of(value: Any, allowed: tuple[str, ...], field_name: str) -> str:
        text = str(value or "").strip()
        if text not in allowed:
            raise Rejected(
                f"{field_name} must be one of {', '.join(allowed)}. Got {text or 'nothing'}."
            )
        return text

    def _episode_must_exist(self, episode_key: str) -> str:
        key = str(episode_key or "").strip()
        if not key:
            raise Rejected("An episode key is required.")
        found = self.store.query(
            "SELECT episode_key FROM iberian.episodes WHERE episode_key = %s", (key,)
        )
        if not found:
            raise Rejected(
                f"No episode {key}. Ask for the list of episodes and use a key from it."
            )
        return key

    # --- alerts --------------------------------------------------------------

    def create_alert(self, zone: str, direction: str, threshold_eur_mwh: Any) -> ActionResult:
        arguments = {
            "zone": zone,
            "direction": direction,
            "threshold_eur_mwh": threshold_eur_mwh,
        }

        def work():
            checked_zone = self._one_of(zone, ZONES, "zone")
            checked_direction = self._one_of(direction, DIRECTIONS, "direction")
            try:
                threshold = float(threshold_eur_mwh)
            except (TypeError, ValueError):
                raise Rejected(f"The threshold must be a number. Got {threshold_eur_mwh!r}.")
            if not 0 < threshold <= MAX_THRESHOLD_EUR_MWH:
                raise Rejected(
                    f"The threshold must be above 0 and at most {MAX_THRESHOLD_EUR_MWH:,.0f} "
                    f"EUR/MWh, which is the market cap. Got {threshold:,.2f}."
                )

            row = self.store.execute(
                """
                INSERT INTO iberian.alerts
                    (created_by, zone, direction, threshold_eur_mwh)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (created_by, zone, direction, threshold_eur_mwh)
                DO UPDATE SET active = true, updated_at = now()
                RETURNING *
                """,
                (self.created_by, checked_zone, checked_direction, threshold),
            )
            return row, (
                f"Alert saved: tell you when the {checked_zone} price goes "
                f"{checked_direction} {threshold:,.2f} EUR/MWh."
            )

        return self._run("create_alert", arguments, "iberian.alerts", work)

    def update_alert(self, alert_id: Any, threshold_eur_mwh: Any = None, active: Any = None) -> ActionResult:
        arguments = {
            "alert_id": alert_id,
            "threshold_eur_mwh": threshold_eur_mwh,
            "active": active,
        }

        def work():
            if threshold_eur_mwh is None and active is None:
                raise Rejected("Nothing to change. Give a new threshold, or turn it on or off.")

            fields, values = [], []
            if threshold_eur_mwh is not None:
                try:
                    threshold = float(threshold_eur_mwh)
                except (TypeError, ValueError):
                    raise Rejected(f"The threshold must be a number. Got {threshold_eur_mwh!r}.")
                if not 0 < threshold <= MAX_THRESHOLD_EUR_MWH:
                    raise Rejected(
                        f"The threshold must be above 0 and at most "
                        f"{MAX_THRESHOLD_EUR_MWH:,.0f} EUR/MWh."
                    )
                fields.append("threshold_eur_mwh = %s")
                values.append(threshold)
            if active is not None:
                fields.append("active = %s")
                values.append(bool(active))

            # Scoped to the caller's own rows. Not a security boundary, a
            # correctness one: without it a mistyped id edits a stranger's alert.
            row = self.store.execute(
                f"UPDATE iberian.alerts SET {', '.join(fields)} "
                "WHERE id = %s AND created_by = %s RETURNING *",
                (*values, alert_id, self.created_by),
            )
            if row is None:
                raise Rejected(f"No alert {alert_id} belonging to you.")
            return row, "Alert updated."

        return self._run("update_alert", arguments, "iberian.alerts", work)

    def delete_alert(self, alert_id: Any, confirmed: bool = False) -> ActionResult:
        """The only irreversible action, so it asks first.

        The confirmation is a separate argument rather than a prompt
        instruction, so the agent cannot delete anything by being persuasive
        with itself. It has to come back with `confirmed=True`, which means the
        person said yes in between.
        """
        arguments = {"alert_id": alert_id, "confirmed": confirmed}

        def work():
            existing = self.store.query(
                "SELECT * FROM iberian.alerts WHERE id = %s AND created_by = %s",
                (alert_id, self.created_by),
            )
            if not existing:
                raise Rejected(f"No alert {alert_id} belonging to you.")

            if not confirmed:
                alert = existing[0]
                raise Rejected(
                    f"This will permanently delete the alert for {alert['zone']} "
                    f"{alert['direction']} {float(alert['threshold_eur_mwh']):,.2f} "
                    "EUR/MWh. Confirm to go ahead."
                )

            row = self.store.execute(
                "DELETE FROM iberian.alerts WHERE id = %s AND created_by = %s RETURNING *",
                (alert_id, self.created_by),
            )
            return row, "Alert deleted."

        return self._run("delete_alert", arguments, "iberian.alerts", work)

    # --- labels --------------------------------------------------------------

    def submit_episode_label(
        self, episode_key: str, true_cause: str, confidence: str, notes: str = ""
    ) -> ActionResult:
        """A human judgement of what caused an episode.

        What is written here leaves the application, travels back into Delta
        through the change feed, and becomes the ground truth the agent is
        scored against. That is why the vocabulary is closed: a free text cause
        cannot be aggregated, and a ground truth nobody can aggregate is a
        collection of opinions.
        """
        arguments = {
            "episode_key": episode_key,
            "true_cause": true_cause,
            "confidence": confidence,
            "notes": notes,
        }

        def work():
            key = self._episode_must_exist(episode_key)
            cause = self._one_of(true_cause, CAUSES, "true_cause")
            certainty = self._one_of(confidence, CONFIDENCES, "confidence")

            row = self.store.execute(
                """
                INSERT INTO iberian.episode_labels
                    (episode_key, created_by, true_cause, confidence, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (episode_key, created_by) DO UPDATE
                SET true_cause = EXCLUDED.true_cause,
                    confidence = EXCLUDED.confidence,
                    notes = EXCLUDED.notes
                RETURNING *
                """,
                (key, self.created_by, cause, certainty, (notes or "").strip() or None),
            )
            return row, f"Label saved for {key}: {cause}, confidence {certainty}."

        return self._run(
            "submit_episode_label", arguments, "iberian.episode_labels", work
        )

    # --- reads ---------------------------------------------------------------

    def list_episodes(self, limit: int = 20, unlabelled_only: bool = False) -> list[dict]:
        """Worst first, because that is the order anybody would look in.

        `unlabelled_only` is what makes the application usable by a visitor: it
        offers them something nobody has judged yet rather than a list where
        every row is already answered.
        """
        limit = max(1, min(int(limit or 20), 100))
        if unlabelled_only:
            return self.store.query(
                """
                SELECT e.* FROM iberian.episodes e
                WHERE NOT EXISTS (
                    SELECT 1 FROM iberian.episode_labels l
                    WHERE l.episode_key = e.episode_key AND l.created_by = %s
                )
                ORDER BY e.peak_abs_spread DESC
                LIMIT %s
                """,
                (self.created_by, limit),
            )
        return self.store.query(
            "SELECT * FROM iberian.episodes ORDER BY peak_abs_spread DESC LIMIT %s",
            (limit,),
        )

    def my_alerts(self) -> list[dict]:
        return self.store.query(
            "SELECT * FROM iberian.alerts WHERE created_by = %s ORDER BY created_at DESC",
            (self.created_by,),
        )

    def my_labels(self) -> list[dict]:
        return self.store.query(
            """
            SELECT * FROM iberian.episode_labels
            WHERE created_by = %s ORDER BY updated_at DESC
            """,
            (self.created_by,),
        )