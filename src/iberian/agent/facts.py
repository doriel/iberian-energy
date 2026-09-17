"""The facts an explanation may use, each one carrying where it came from.

`explain_interval.py` already assembles this evidence, but it prints it. A
model cannot be handed a terminal transcript and be expected to stay honest,
and a verifier cannot check prose against prose. So the same assembly returns
a structure instead: a flat list of named, sourced, typed facts.

Two properties make the rest of the layer possible.

Every number the explanation is allowed to contain is enumerable, because
`numbers()` returns exactly the set that was retrieved. Anything outside that
set in the generated text was invented, and `agent.verify` rejects it.

Every fact names its document. "3195 MW" on its own is a claim; "3195 MW,
ENTSO-E A61 day-ahead capacity" is evidence. The agent is asked to carry the
source through, and the sheet is what makes that possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd


@dataclass(frozen=True)
class Fact:
    """One retrieved value. Never computed by the model, never inferred."""

    key: str
    value: float | int | str | None
    unit: str = ""
    source: str = ""
    note: str = ""

    @property
    def is_numeric(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)

    def render(self) -> str:
        if self.value is None:
            return f"{self.key}: not published"
        if self.is_numeric:
            body = f"{self.key}: {self.value:,.2f}".rstrip("0").rstrip(".")
        else:
            body = f"{self.key}: {self.value}"
        if self.unit:
            body += f" {self.unit}"
        if self.source:
            body += f"  [{self.source}]"
        if self.note:
            body += f"  ({self.note})"
        return body


@dataclass(frozen=True)
class FactSheet:
    """Everything retrieved for one anomaly, and nothing else."""

    subject: str
    start_utc: datetime
    end_utc: datetime
    market_day: date
    facts: list[Fact] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def numbers(self) -> set[float]:
        """Every numeric value that appeared in a retrieved document.

        This is the allowlist the verifier checks generated text against. It
        deliberately excludes anything derived, because a derived number is a
        computation, and computation is not the model's job.
        """
        return {float(fact.value) for fact in self.facts if fact.is_numeric}

    def get(self, key: str) -> Fact | None:
        for fact in self.facts:
            if fact.key == key:
                return fact
        return None

    def sources(self) -> list[str]:
        seen: list[str] = []
        for fact in self.facts:
            if fact.source and fact.source not in seen:
                seen.append(fact.source)
        return seen

    def render(self) -> str:
        """The block handed to the model. Plain, flat, and fully attributed."""
        lines = [
            f"Subject: {self.subject}",
            f"Window: {self.start_utc:%Y-%m-%d %H:%M}Z to {self.end_utc:%Y-%m-%d %H:%M}Z",
            f"Market day: {self.market_day}",
            "",
            "Retrieved facts:",
        ]
        lines += [f"  - {fact.render()}" for fact in self.facts]
        if self.caveats:
            lines += ["", "Caveats that must not be omitted:"]
            lines += [f"  - {caveat}" for caveat in self.caveats]
        return "\n".join(lines)


def _number(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def episode_facts(
    episode: pd.Series,
    intervals: pd.DataFrame,
    assets: list[dict] | None = None,
) -> FactSheet:
    """Assemble the sheet for one market splitting episode.

    `intervals` is the gold interval table restricted to this episode, and
    `assets` is the output of `binding_assets` with the point in time filter
    already applied. Nothing here reaches the network: retrieval happens in the
    caller, so the assembly stays testable in milliseconds.
    """
    facts: list[Fact] = []
    caveats: list[str] = []

    def add(key, value, unit="", source="", note=""):
        number = _number(value) if not isinstance(value, str) else value
        facts.append(Fact(key=key, value=number, unit=unit, source=source, note=note))

    add("duration_hours", episode.get("duration_hours"), "hours", "derived from A44 prices")
    add("intervals", episode.get("intervals"), "settlement intervals", "ENTSO-E A44")
    add("peak_premium", episode.get("peak_spread"), "EUR/MWh", "ENTSO-E A44 day-ahead")
    add("premium_side", episode.get("premium_side"), source="ENTSO-E A44 day-ahead")
    add("severity", episode.get("max_severity"), source="derived from the spread")

    if pd.notna(episode.get("extra_cost_eur")):
        add(
            "extra_import_cost",
            episode.get("extra_cost_eur"),
            "EUR",
            "A44 prices with A09 scheduled flows",
            "premium applied to energy actually imported, not to national demand",
        )

    # Everything below comes from one interval, the worst one, rather than
    # taking a maximum here and a minimum there. Mixing intervals produces
    # figures that cannot be reconciled: the highest Portuguese price minus the
    # lowest Spanish price is not the peak spread, and the largest flow can
    # exceed the smallest capacity, which reads as impossible. A model handed
    # that would write something false, and the fault would be in the evidence
    # rather than in the model.
    if not intervals.empty and "abs_premium_eur_mwh" in intervals:
        worst = intervals.loc[intervals["abs_premium_eur_mwh"].idxmax()]
        moment = f"at {pd.Timestamp(worst['ts_utc']):%H:%M}Z, the worst interval"

        if "price_pt_eur_mwh" in worst:
            add("price_pt_at_peak", worst["price_pt_eur_mwh"], "EUR/MWh",
                "ENTSO-E A44 day-ahead", moment)
        if "price_es_eur_mwh" in worst:
            add("price_es_at_peak", worst["price_es_eur_mwh"], "EUR/MWh",
                "ENTSO-E A44 day-ahead", moment)
        if pd.notna(worst.get("capacity_mw")):
            add("border_capacity_at_peak", worst["capacity_mw"], "MW",
                "ENTSO-E A61 day-ahead capacity", moment)
        if pd.notna(worst.get("net_flow_mw")):
            add("net_flow_es_to_pt_at_peak", worst["net_flow_mw"], "MW",
                "ENTSO-E A09 scheduled exchanges", moment)

        capacity = intervals["capacity_mw"].dropna() if "capacity_mw" in intervals else None
        if capacity is not None and not capacity.empty:
            add("lowest_border_capacity", capacity.min(), "MW",
                "ENTSO-E A61 day-ahead capacity", "lowest across the episode")

    if pd.notna(episode.get("share_saturated")):
        share = float(episode["share_saturated"])
        add("share_of_intervals_saturated", share, "",
            "A09 flow against A61 capacity")
        if share >= 0.5:
            caveats.append(
                "The border being full is consistent with the price separation. "
                "It is not proof of cause: both follow from the same market "
                "coupling, and this must not be stated as causation."
            )

    if assets:
        tightest = assets[0]
        add("constrained_asset", tightest.get("asset"),
            source="ENTSO-E A78 transmission unavailability")
        add("constrained_asset_available", tightest.get("available_mw"), "MW",
            "ENTSO-E A78 transmission unavailability")
        add("constrained_asset_status", tightest.get("status"),
            source="ENTSO-E A78 transmission unavailability")
        add("notices_in_force", len(assets), "notices",
            "ENTSO-E A78, published before the episode began")
        published = tightest.get("published_at")
        if published is not None:
            add("notice_published", f"{published:%Y-%m-%d}",
                source="ENTSO-E A78 publication timestamp")

        capacity_fact = next(
            (f for f in facts if f.key == "lowest_border_capacity"), None
        )
        available = _number(tightest.get("available_mw"))
        if capacity_fact and available is not None and capacity_fact.is_numeric:
            if available > float(capacity_fact.value):
                caveats.append(
                    "The notice permits more capacity than the border actually "
                    "had, so it does not account for the reduction. Say so "
                    "rather than presenting it as the explanation."
                )
    else:
        add("notices_in_force", 0, "notices",
            "ENTSO-E A78, published before the episode began")
        caveats.append(
            "No transmission notice covers this window in this direction. The "
            "reduction is unexplained by published unavailability."
        )

    caveats.append(
        "A78 notices are asset level while A61 is the net border figure after "
        "the operator's security assessment. They are related but not the same "
        "quantity, and the difference is not an error to explain away."
    )

    return FactSheet(
        subject=f"Market splitting episode starting {episode['start_utc']:%Y-%m-%d %H:%M}Z",
        start_utc=pd.Timestamp(episode["start_utc"]).to_pydatetime(),
        end_utc=pd.Timestamp(episode["end_utc"]).to_pydatetime(),
        market_day=episode["market_day"],
        facts=facts,
        caveats=caveats,
    )