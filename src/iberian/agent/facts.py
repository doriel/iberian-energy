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

from iberian.agent.tracing import SpanType, trace

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

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Fact":
        return cls(
            key=str(raw["key"]),
            value=raw.get("value"),
            unit=str(raw.get("unit") or ""),
            source=str(raw.get("source") or ""),
            note=str(raw.get("note") or ""),
        )

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

    def to_dict(self) -> dict:
        """A JSON safe form, for crossing a request boundary.

        Timestamps go out as ISO strings rather than epoch numbers. The sheet
        is the allowlist the verifier checks generated text against, and a
        number in it is a number the model is permitted to write, so putting
        1755500700 in there would authorise it to appear in the prose.
        """
        return {
            "subject": self.subject,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "market_day": self.market_day.isoformat(),
            "facts": [fact.to_dict() for fact in self.facts],
            "caveats": list(self.caveats),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "FactSheet":
        """Rebuild a sheet that arrived over the wire.

        Strict about the four scalars, because a sheet missing its window is a
        sheet the verifier cannot reason about, and a confusing failure here is
        better than a confident explanation of the wrong episode.
        """
        missing = {"subject", "start_utc", "end_utc", "market_day"} - set(raw)
        if missing:
            raise ValueError(f"fact sheet is missing {sorted(missing)}")
        return cls(
            subject=str(raw["subject"]),
            start_utc=datetime.fromisoformat(str(raw["start_utc"])),
            end_utc=datetime.fromisoformat(str(raw["end_utc"])),
            market_day=date.fromisoformat(str(raw["market_day"])[:10]),
            facts=[Fact.from_dict(item) for item in raw.get("facts", [])],
            caveats=[str(item) for item in raw.get("caveats", [])],
        )

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
            f"Market day: {self.market_day}, written {written_date(self.market_day)}",
            "",
            "Retrieved facts:",
        ]
        lines += [f"  - {fact.render()}" for fact in self.facts]
        if self.caveats:
            lines += ["", "Caveats that must not be omitted:"]
            lines += [f"  - {caveat}" for caveat in self.caveats]
        return "\n".join(lines)


def written_date(day: date) -> str:
    """"1 August 2026", the form an explanation actually uses.

    Given only 2026-08-01, the small model converted it to prose itself and got
    the day wrong, "2 August 2026", on three different episodes and in both
    runs. Handing it the written form removes the conversion rather than
    checking it afterwards. English month names on purpose: the explanations
    are written in English and the verifier matches English month names.
    """
    months = (
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December",
    )
    return f"{day.day} {months[day.month - 1]} {day.year}"


def _number(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


@trace(span_type=SpanType.RETRIEVER)
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

    # The length of a settlement interval is a property of the market, not a
    # figure the model should be inventing, and every explanation wants to say
    # it: "the single 15 minute interval". Leaving it out of the sheet made a
    # true and necessary sentence fail verification. It is read off the episode
    # rather than assumed, because the resolution is not ours to decide.
    count, hours = episode.get("intervals"), episode.get("duration_hours")
    if pd.notna(count) and pd.notna(hours) and float(count) > 0:
        add("settlement_interval_minutes", round(float(hours) * 60.0 / float(count)),
            "minutes", "ENTSO-E A44 resolution")
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
        # The direction is in the key and the note, as it already was for the
        # flow. Without it a model wrote "fully saturated at 5,445 MW in both
        # directions": the figure was retrieved, so the verifier passed it, and
        # the qualifier was invented. A61 is published per direction and this
        # is only the Spain to Portugal one.
        if pd.notna(worst.get("capacity_mw")):
            add("border_capacity_es_to_pt_at_peak", worst["capacity_mw"], "MW",
                "ENTSO-E A61 day-ahead capacity",
                f"{moment}, Spain to Portugal direction only")
        if pd.notna(worst.get("net_flow_mw")):
            add("net_flow_es_to_pt_at_peak", worst["net_flow_mw"], "MW",
                "ENTSO-E A09 scheduled exchanges", moment)

        capacity = intervals["capacity_mw"].dropna() if "capacity_mw" in intervals else None
        if capacity is not None and not capacity.empty:
            add("lowest_border_capacity", capacity.min(), "MW",
                "ENTSO-E A61 day-ahead capacity",
                "lowest across the episode, Spain to Portugal direction only")

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

        # Some A78 notices arrive with no asset at all: no registered resource,
        # no name, no mRID. The parser falls back to a placeholder label, and a
        # model handed that placeholder writes "an unnamed asset", which reads
        # as though this project lost the name. The truth is that the operator
        # published a capacity restriction without saying where, and for the
        # journalist persona that is a finding rather than a gap to paper over.
        named = tightest.get("asset_named", True)
        add("constrained_asset", tightest.get("asset") if named else None,
            source="ENTSO-E A78 transmission unavailability")
        if not named:
            caveats.append(
                "The most restrictive notice does not identify the asset: the "
                "operator published it with no asset name or identifier. Say "
                "that the publisher did not name the asset. Do not call it "
                "unnamed as if the name were missing here, and do not guess one."
            )
        add("constrained_asset_available", tightest.get("available_mw"), "MW",
            "ENTSO-E A78 transmission unavailability")
        add("constrained_asset_status", tightest.get("status"),
            source="ENTSO-E A78 transmission unavailability")
        add("notices_in_force", len(assets), "notices",
            "ENTSO-E A78, published before the episode began",
            "a count only: these notices were published on different dates")

        # Named for the one notice it belongs to. As `notice_published`, beside
        # a count of every notice in force, models wrote "nine notices were in
        # force, published on 13 August", attaching one notice's date to all
        # nine. The date is in the sheet, so the verifier passed it: it checks
        # values, not what a value is attached to. The fix has to be here.
        published = tightest.get("published_at")
        if published is not None:
            add("constrained_asset_notice_published", f"{published:%Y-%m-%d}",
                source="ENTSO-E A78 publication timestamp",
                note="the publication date of the most restrictive notice only, "
                "not of every notice in force")

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