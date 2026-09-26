"""Actual generation per generation unit [16.1.A], XML to rows.

One row per unit per production type per direction per interval. Pure functions
over a string, no Spark and no network, so the awkward parts are tested against
documents rather than discovered during an ingestion of seven hundred files.

## What the document looks like

A `GL_MarketDocument` holding many `TimeSeries`, one per unit per production
type per direction per period. Each carries the unit's EIC code and name inside
`MktPSRType/PowerSystemResources`, the production type as a `psrType` code, and
a `Period` with a `timeInterval`, a `resolution`, and a `Point` per step.

## Four things that bite

**The two zones publish at different resolutions.** Spain reports quarter
hourly, Portugal hourly. Nothing here resamples either of them. Upsampling the
Portuguese hour into four quarters would invent three readings that nobody
measured, and this project's whole claim is that its figures are traceable to a
published document. `resolution_minutes` is a column, and whoever joins to the
market intervals decides what to do about it, in the open.

**A unit publishes generation and consumption as two separate series.** Both
positive, both `businessType` `A01`, distinguished only by which bidding zone
element is present: `inBiddingZone_Domain.mRID` means the energy went into the
zone and the unit was generating, `outBiddingZone_Domain.mRID` means it came out
of the zone and the unit was consuming. This is not only pumped storage. A
combined cycle plant and a run-of-river station both publish their own station
consumption the same way. Measured on one Portuguese day: Lares, Aguieira and
Carrapatelo all do it.

Missing this is what produced 39,061 apparently duplicated keys the first time
this table was built, and deduplicating them would have erased half of what
pumped storage does, which is exactly the behaviour that explains prices. So
`flow_direction` is part of the key, and **`quantity_mw` is always positive as
published**: a consumption row is not a negative generation row. Any aggregate
over output has to filter on `flow_direction` or it counts consumption as
production.

**A unit appears in more than one TimeSeries for other reasons too.** Different
production types, or a period split across a day boundary. The key is the unit,
the production type, the direction and the timestamp.

**Positions are one based and can be sparse.** The documents carry `curveType`
`A03`, a variable sized block, where a published point holds until the next
published position rather than the series being dense. Nothing here expands
those blocks: a repeated value and a measured one are different things, and the
difference matters to somebody counting how much a plant ran. Gaps stay gaps and
`curve_type` is a column, so whoever aggregates can decide to carry a value
forward with a window function, visibly, rather than inherit a decision made
here.
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

#: Which way the energy went. Not a boolean, because a third value showing up
#: later should be a new label rather than a silent reclassification.
GENERATION = "generation"
CONSUMPTION = "consumption"

#: The elements that say so. Only one of the two is present on a TimeSeries.
INTO_ZONE = "inBiddingZone_Domain.mRID"
OUT_OF_ZONE = "outBiddingZone_Domain.mRID"

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
    """One unit, one production type, one direction, one interval.

    `quantity_mw` is what the document said and nothing else. No filling, no
    interpolation, no sign flipping. A consumption reading arrives positive in
    its own series and is left positive, because turning it negative would be
    this module inventing an encoding the publisher did not use.
    """

    zone: str
    unit_eic: str
    unit_name: str
    psr_type: str
    psr_label: str
    flow_direction: str
    ts_utc: datetime
    resolution_minutes: int | None
    quantity_mw: float
    position: int
    curve_type: str

    def as_row(self) -> dict:
        return {
            "zone": self.zone,
            "unit_eic": self.unit_eic,
            "unit_name": self.unit_name,
            "psr_type": self.psr_type,
            "psr_label": self.psr_label,
            "flow_direction": self.flow_direction,
            "ts_utc": self.ts_utc,
            "resolution_minutes": self.resolution_minutes,
            "quantity_mw": self.quantity_mw,
            "position": self.position,
            "curve_type": self.curve_type,
        }


def _local(tag: str) -> str:
    """The element name without its namespace.

    Used instead of a namespaced `find` for the bidding zone elements, whose
    names contain a dot. ElementTree's path syntax gives a dot its own meaning,
    so `find('{ns}inBiddingZone_Domain.mRID')` is a trap rather than a lookup.
    """
    return tag.split("}")[-1]


def _child_names(element: ET.Element) -> set[str]:
    return {_local(child.tag) for child in element}


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _flow_direction(series: ET.Element) -> str:
    """Generation or consumption, from the bidding zone element.

    `businessType` is `A01` on both series and cannot be used: that was checked
    against a real Portuguese document rather than assumed.

    A series carrying neither element is read as generation. That is a default,
    not a reading, and it is the safe one: the overwhelming majority of series
    are production, and a null in a key column is worse than a documented
    assumption. If the default were ever wrong at scale, the duplicate key check
    in the silver build says so immediately, which is how this was found.
    """
    names = _child_names(series)
    if OUT_OF_ZONE in names and INTO_ZONE not in names:
        return CONSUMPTION
    return GENERATION


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
    head = xml.lstrip("﻿ \t\r\n")[:4000]
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

    Note that the bidding zone elements read here are the *direction* of the
    flow, not the zone. Both say `10YPT-REN------W` on a Portuguese document.
    Which of the two is present is the information; its value is not.
    """
    if not xml or not xml.strip():
        return []

    declined, _ = is_acknowledgement(xml)
    if declined:
        return []

    # A byte order mark or leading whitespace before the declaration makes
    # ElementTree refuse the whole document, and ENTSO-E sends both.
    text = xml.lstrip("﻿ \t\r\n")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        # The message this replaces is "no element found: line 1, column 0",
        # which is true and useless. The overwhelmingly likely cause is a
        # fragment rather than a document, and the reader is where that starts.
        raise ValueError(
            f"Not a parseable XML document: {exc}. It starts {text[:60]!r}. "
            "A fragment rather than a whole document usually means the file was "
            "read a line at a time: pass wholetext=True to spark.read.text, as "
            "a parameter rather than as an option."
        ) from exc
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

        flow_direction = _flow_direction(series)
        curve_type = _child_text(series, "curveType")

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
                        flow_direction=flow_direction,
                        ts_utc=ts,
                        resolution_minutes=minutes,
                        quantity_mw=quantity,
                        position=position,
                        curve_type=curve_type,
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