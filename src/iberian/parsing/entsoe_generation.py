"""Actual generation per generation unit [16.1.A], XML to rows.

One row per unit per interval. Pure functions over a string, no Spark and no
network, so the awkward parts are tested against documents rather than
discovered during an ingestion of seven hundred files.

## What the document looks like

A `GL_MarketDocument` holding many `TimeSeries`, one per unit per production
type per period. Each carries the unit's EIC code and name inside
`MktPSRType/PowerSystemResources`, the production type as a `psrType` code, and
a `Period` with a `timeInterval`, a `resolution`, and a `Point` per step.

## Three things that bite

**The two zones publish at different resolutions.** Spain reports quarter
hourly, Portugal hourly. Nothing here resamples either of them. Upsampling the
Portuguese hour into four quarters would invent three readings that nobody
measured, and this project's whole claim is that its figures are traceable to a
published document. `resolution_minutes` is a column, and whoever joins to the
market intervals decides what to do about it, in the open.

**A unit appears in more than one TimeSeries.** Different production types, or a
period split across a day boundary. The unit is therefore not a key on its own:
the key is the unit, the production type and the timestamp.

**Positions are one based and can be sparse.** ENTSO-E's A01 curve type means a
missing position repeats the one before it. That repetition is not done here,
because a repeated value and a measured one are different things and the
difference matters to somebody counting how much a plant ran. Gaps stay gaps.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: How long one step is, by the resolution ENTSO-E states. Anything not here is
#: carried through as unknown rather than guessed: a wrong step size silently
#: puts every reading in a series at the wrong time.
RESOLUTION_MINUTES = {
    "PT15M": 15,
    "PT30M": 30,
    "PT60M": 60,
    "PT1H": 60,
    "P1D": 1440,
}

#: The production type codes that turn up in Iberia. Kept for readability in the
#: gold layer rather than for validation: an unrecognised code is passed through
#: as itself, because a new one appearing is information, not an error.
PSR_TYPES = {
    "B01": "Biomass",
    "B02": "Fossil brown coal/lignite",
    "B03": "Fossil coal-derived gas",
    "B04": "Fossil gas",
    "B05": "Fossil hard coal",
    "B06": "Fossil oil",
    "B07": "Fossil oil shale",
    "B08": "Fossil peat",
    "B09": "Geothermal",
    "B10": "Hydro pumped storage",
    "B11": "Hydro run-of-river and poundage",
    "B12": "Hydro water reservoir",
    "B13": "Marine",
    "B14": "Nuclear",
    "B15": "Other renewable",
    "B16": "Solar",
    "B17": "Waste",
    "B18": "Wind offshore",
    "B19": "Wind onshore",
    "B20": "Other",
    "B25": "Energy storage",
}


@dataclass(frozen=True)
class GenerationPoint:
    """One unit, one production type, one interval.

    `quantity_mw` is what the document said and nothing else. No filling, no
    interpolation, no negative clamping: pumped storage consuming is a real
    negative and turning it into zero would hide the behaviour that makes
    pumped storage interesting.
    """

    zone: str
    unit_eic: str
    unit_name: str
    psr_type: str
    psr_label: str
    ts_utc: datetime
    resolution_minutes: int | None
    quantity_mw: float
    position: int

    def as_row(self) -> dict:
        return {
            "zone": self.zone,
            "unit_eic": self.unit_eic,
            "unit_name": self.unit_name,
            "psr_type": self.psr_type,
            "psr_label": self.psr_label,
            "ts_utc": self.ts_utc,
            "resolution_minutes": self.resolution_minutes,
            "quantity_mw": self.quantity_mw,
            "position": self.position,
        }


def _namespace(root: ET.Element) -> str:
    """Read the namespace off the root rather than hard coding it.

    The schema version is inside the URI, so a constant here is a parser that
    stops matching anything the day ENTSO-E publishes a new version, and does
    so silently by returning no rows.
    """
    return root.tag.split("}")[0].strip("{") if "}" in root.tag else ""


def _parse_instant(text: str | None) -> datetime | None:
    """ENTSO-E writes `2026-09-23T00:00Z`, which `fromisoformat` refuses."""
    if not text:
        return None
    cleaned = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def is_acknowledgement(xml: str) -> tuple[bool, str]:
    """Whether ENTSO-E declined, and what it said.

    It answers a request it will not serve with a document rather than a status
    code, so a caller that only checks for an exception treats "no data for
    this day" as success and lands an empty file.
    """
    head = xml[:4000]
    if "Acknowledgement_MarketDocument" not in head:
        return False, ""
    reason = ""
    if "<text>" in head:
        reason = head.split("<text>", 1)[1].split("</text>", 1)[0].strip()
    return True, reason


def generation_points(xml: str, zone: str) -> list[GenerationPoint]:
    """Every reading in one document.

    `zone` is passed in rather than read from the document. The response says
    which control area was asked for in a way that varies by document version,
    and the caller already knows, so taking it from the caller removes a class
    of parsing bug for a piece of information nobody was ever unsure about.
    """
    if not xml or not xml.strip():
        return []

    declined, _ = is_acknowledgement(xml)
    if declined:
        return []

    root = ET.fromstring(xml)
    ns = _namespace(root)
    tag = (lambda name: f"{{{ns}}}{name}") if ns else (lambda name: name)

    rows: list[GenerationPoint] = []

    for series in root.iter(tag("TimeSeries")):
        psr = series.find(f".//{tag('MktPSRType')}")
        if psr is None:
            continue

        psr_type = (psr.findtext(tag("psrType")) or "").strip()
        resource = psr.find(tag("PowerSystemResources"))
        unit_eic = (resource.findtext(tag("mRID")) if resource is not None else "") or ""
        unit_name = (resource.findtext(tag("name")) if resource is not None else "") or ""
        unit_eic = unit_eic.strip()

        if not unit_eic:
            # A series with no unit is not a unit level reading, whatever else
            # it is. Dropping it is better than inventing an identifier that
            # would then be grouped on.
            continue

        for period in series.iter(tag("Period")):
            start = _parse_instant(
                period.findtext(f"{tag('timeInterval')}/{tag('start')}")
            )
            if start is None:
                continue

            resolution = (period.findtext(tag("resolution")) or "").strip()
            minutes = RESOLUTION_MINUTES.get(resolution)

            for point in period.iter(tag("Point")):
                position_text = point.findtext(tag("position"))
                quantity_text = point.findtext(tag("quantity"))
                if position_text is None or quantity_text is None:
                    continue
                try:
                    position = int(position_text)
                    quantity = float(quantity_text)
                except ValueError:
                    continue

                if minutes is None:
                    # The step is unknown, so the timestamp would be a guess.
                    # The reading is kept at the period start with the
                    # resolution left null, which is visibly wrong in a way a
                    # silently shifted series is not.
                    ts = start
                else:
                    ts = start + timedelta(minutes=minutes * (position - 1))

                rows.append(
                    GenerationPoint(
                        zone=zone,
                        unit_eic=unit_eic,
                        unit_name=unit_name.strip(),
                        psr_type=psr_type,
                        psr_label=PSR_TYPES.get(psr_type, psr_type or "unknown"),
                        ts_utc=ts,
                        resolution_minutes=minutes,
                        quantity_mw=quantity,
                        position=position,
                    )
                )

    return rows


def generation_rows(xml: str, zone: str) -> list[dict]:
    """The same, as dictionaries, for handing to Spark."""
    return [point.as_row() for point in generation_points(xml, zone)]


def units_in(xml: str, zone: str) -> dict[str, str]:
    """The distinct units a document mentions, EIC to name.

    Useful on its own: the unit dimension is discovered from the data rather
    than maintained by hand, which is the only way it stays right as plants are
    commissioned and retired.
    """
    return {point.unit_eic: point.unit_name for point in generation_points(xml, zone)}