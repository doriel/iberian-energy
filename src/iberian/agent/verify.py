"""Reject any explanation containing a number that was not retrieved.

The design commitment is that the model phrases facts and never produces a
figure of its own. That commitment is worth nothing as a promise in a prompt,
because a prompt is a request and this is a guarantee. So it is enforced after
generation: every number in the text is extracted and matched against the set
that came out of a document. Anything unmatched fails, and a failed
explanation is not shown.

What counts as a match needs care in both directions.

Too strict and every answer fails: a model writing "utilisation was 100%" when
the retrieved value is 1.0 has not invented anything, and "3,195 MW" is the
same figure as 3195.0. Rounding to the precision the writer used is legitimate
too, because "roughly 3,200 MW" is a faithful reading of 3195.

Too lax and the check is theatre. Matching on "close enough" within some
percentage would accept 3,500 for 3,195, which is a different number about a
different border. So the rules here are exact, or a rounding of the retrieved
value to the precision actually written, or the ratio to percentage conversion.
Nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from iberian.agent.facts import FactSheet
from iberian.agent.tracing import SpanType, trace

#: Numbers that carry no factual claim. A model writing "the first of two
#: assets" is not asserting a measurement, and failing it would teach nobody
#: anything. Kept deliberately short: every entry here is a hole in the check.
FREE_NUMBERS = {0.0, 1.0, 2.0, 100.0}

_NUMBER = re.compile(
    r"""
    (?<![\w.])            # not mid-identifier
    (-?\d{1,3}(?:,\d{3})+(?:\.\d+)?   # 1,234,567.8
     |-?\d+(?:\.\d+)?)                 # or 1234.8
    \s*(%)?                            # trailing percent sign
    """,
    re.VERBOSE,
)

# Dates and clock times are quoted from the facts as text, and splitting them
# into numbers would produce spurious failures on 2026, 09 and 18. Prose dates
# have to be covered as well as ISO ones: a model writing "published on 25 June
# 2026" is quoting a publication timestamp, not asserting that 25 and 2026 are
# measurements, and treating them as claims rejected explanations that were
# entirely faithful. Month names are matched case sensitively, so an ordinary
# "may" followed by a figure is not swallowed.
_MONTHS = (
    r"(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December)"
)

_DATELIKE = re.compile(
    rf"""
      \d{{4}}-\d{{2}}-\d{{2}}(?:[T ]\d{{2}}:\d{{2}}(?::\d{{2}})?Z?)?  # 2026-08-18T07:45Z
    | \d{{1,2}}:\d{{2}}                                               # 07:45
    | \b\d{{1,2}}\s+{_MONTHS}(?:\s+\d{{4}})?                          # 25 June 2026
    | \b{_MONTHS}\s+\d{{4}}\b                                         # June 2026
    | \b{_MONTHS}\s+\d{{1,2}}(?!\d)(?:,\s*\d{{4}})?                   # June 25, 2026
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Claim:
    """A number as it appeared in the text, with how it was written."""

    text: str
    value: float
    is_percent: bool
    decimals: int


@dataclass(frozen=True)
class DateClaim:
    """A date as it appeared in the text, resolved enough to compare."""

    text: str
    year: int
    month: int
    day: int | None = None


@dataclass(frozen=True)
class Verdict:
    """The outcome, and enough detail to show a reader what failed."""

    ok: bool
    claims: list[Claim] = field(default_factory=list)
    unsupported: list[Claim] = field(default_factory=list)
    missing_sources: bool = False
    wrong_dates: list["DateClaim"] = field(default_factory=list)

    def describe(self) -> str:
        if self.ok:
            return f"{len(self.claims)} numeric claim(s), all retrieved."
        parts = []
        if self.unsupported:
            parts.append(
                "Unsupported: " + ", ".join(claim.text for claim in self.unsupported)
            )
        if self.wrong_dates:
            parts.append(
                "Dates not in the evidence: "
                + ", ".join(claim.text for claim in self.wrong_dates)
            )
        if self.missing_sources:
            parts.append("No source named.")
        return "  ".join(parts) or "Rejected."


#: The same shapes as _DATELIKE, but with the parts named so a date can be
#: resolved rather than only skipped. Masking dates stopped the verifier
#: reading "25 June 2026" as the numbers 25 and 2026, which was right, and left
#: this class of error completely unguarded: three explanations in a run of
#: forty-five stated a day that was not the episode's. A reader checking one
#: figure would check that one, because it is the only claim in the sentence
#: they can verify without the data.
_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_DAY_MONTH = re.compile(
    rf"\b(?P<day>\d{{1,2}})\s+(?P<month>{_MONTHS})(?:\s+(?P<year>\d{{4}}))?\b"
)
_MONTH_DAY = re.compile(
    rf"\b(?P<month>{_MONTHS})\s+(?P<day>\d{{1,2}})(?!\d)(?:,\s*(?P<year>\d{{4}}))?\b"
)
_MONTH_YEAR = re.compile(rf"\b(?P<month>{_MONTHS})\s+(?P<year>\d{{4}})\b")

_MONTH_NUMBERS = {
    name: number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December",
        ),
        start=1,
    )
}


#: A model writing a negative price often reaches for the typographic minus
#: sign rather than the hyphen. "−0.07 EUR/MWh" is the retrieved -0.07, and
#: reading it as +0.07 rejected a faithful explanation of a negative Spanish
#: price, which is an ordinary event in this market rather than an oddity.
_MINUS_SIGNS = str.maketrans({"−": "-"})


def extract_claims(text: str) -> list[Claim]:
    """Every number a reader would take as a factual assertion."""
    masked = _DATELIKE.sub(lambda m: " " * len(m.group(0)), text.translate(_MINUS_SIGNS))

    claims: list[Claim] = []
    for match in _NUMBER.finditer(masked):
        raw = match.group(1)
        cleaned = raw.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        claims.append(
            Claim(
                text=match.group(0).strip(),
                value=value,
                is_percent=match.group(2) is not None,
                decimals=decimals,
            )
        )
    return claims


def extract_dates(text: str, year_hint: int | None = None) -> list["DateClaim"]:
    """Every date a reader would take as the date of something.

    A month and year without a day resolves to the month, because "June 2026"
    asserts less than "25 June 2026" and failing it for being vague would be
    wrong. A day and month without a year takes the year from the episode,
    which is what a reader does.
    """
    found: list[DateClaim] = []
    seen: set[tuple[int, int, int]] = set()

    def record(raw: str, year, month, day) -> None:
        if year is None:
            if year_hint is None:
                return
            year = year_hint
        key = (int(year), int(month), int(day or 0))
        if key in seen:
            return
        seen.add(key)
        found.append(
            DateClaim(text=raw.strip(), year=int(year), month=int(month),
                      day=int(day) if day else None)
        )

    # Specific shapes first, and each match is blanked before the next pattern
    # runs. "2 September 2026" contains "September 2026", and counting it twice
    # would report one mistake as two and make the retry message wrong.
    remaining = text
    for pattern, has_day in (
        (_ISO_DATE, True),
        (_DAY_MONTH, True),
        (_MONTH_DAY, True),
        (_MONTH_YEAR, False),
    ):
        for match in list(pattern.finditer(remaining)):
            month = match["month"]
            record(
                match.group(0),
                match["year"],
                int(month) if month.isdigit() else _MONTH_NUMBERS[month],
                match["day"] if has_day else None,
            )
        remaining = pattern.sub(lambda m: " " * len(m.group(0)), remaining)
    return found


def allowed_dates(sheet: FactSheet) -> set[tuple[int, int, int]]:
    """Dates the sheet actually contains, as (year, month, day) triples.

    Three sources: the market day, the window the episode spans, and any date
    written into a retrieved fact. The third is what lets a model quote a
    notice's publication date, which it is required to do.

    The window is expanded day by day rather than taking only its ends. An
    episode running past midnight legitimately touches both, and the Iberian
    market day begins at local midnight, so the market day and the UTC date of
    the start are often different and both are honest to write.
    """
    out: set[tuple[int, int, int]] = set()

    def add(value) -> None:
        out.add((value.year, value.month, value.day))

    add(sheet.market_day)
    current = sheet.start_utc.date()
    last = sheet.end_utc.date()
    while current <= last:
        add(current)
        current += timedelta(days=1)

    for fact in sheet.facts:
        for candidate in (fact.value, fact.note):
            if isinstance(candidate, str):
                for match in _ISO_DATE.finditer(candidate):
                    out.add((int(match["year"]), int(match["month"]), int(match["day"])))
    return out


def unsupported_dates(text: str, sheet: FactSheet) -> list["DateClaim"]:
    """Dates in the text that are in no retrieved fact and no part of the window."""
    allowed = allowed_dates(sheet)
    months = {(year, month) for year, month, _ in allowed}

    bad = []
    for claim in extract_dates(text, year_hint=sheet.market_day.year):
        if claim.day is None:
            if (claim.year, claim.month) not in months:
                bad.append(claim)
        elif (claim.year, claim.month, claim.day) not in allowed:
            bad.append(claim)
    return bad


def mask_retrieved_strings(text: str, sheet: FactSheet) -> str:
    """Blank out retrieved names before looking for numbers.

    Asset identifiers carry digits: `AT 2 400/220 SRM` is a transformer, and
    400 and 220 are part of its name rather than quantities anyone measured.
    Quoting it is precisely what the agent was asked to do, so the digits
    inside a string that was itself retrieved are not claims.

    The hole this opens is narrow and worth naming: a model could take a figure
    out of an asset name and reuse it elsewhere as a measurement. Only values
    that appear in the sheet as text are masked, and only where the text
    reproduces them in full.
    """
    for fact in sheet.facts:
        value = fact.value
        if not isinstance(value, str) or len(value) < 3:
            continue
        if not any(character.isdigit() for character in value):
            continue
        text = re.sub(
            re.escape(value),
            lambda match: " " * len(match.group(0)),
            text,
            flags=re.IGNORECASE,
        )
    return text


def _supports(allowed: float, claim: Claim) -> bool:
    """Is this retrieved value the one the writer meant?"""
    candidates = [allowed]
    if claim.is_percent:
        # A share of 1.0 written as 100%, or 0.37 written as 37%.
        candidates.append(allowed * 100.0)
    for candidate in candidates:
        if candidate == claim.value:
            return True
        # Rounding to the precision actually written is a faithful reading.
        if round(candidate, claim.decimals) == claim.value:
            return True
        # "roughly 3,200 MW" for 3195: rounding up the scale the writer chose.
        if claim.decimals == 0 and claim.value != 0:
            for scale in (10, 100, 1000):
                if round(candidate / scale) * scale == claim.value:
                    return True
    return False


@trace(span_type=SpanType.PARSER)
def verify(text: str, sheet: FactSheet, require_sources: bool = True) -> Verdict:
    """Check an explanation against the facts it was given.

    `require_sources` also demands that at least one retrieved document is
    named in the text, because an explanation a reader cannot trace is not
    grounded even when every figure in it happens to be right.
    """
    claims = extract_claims(mask_retrieved_strings(text, sheet))
    allowed = sheet.numbers()

    unsupported = [
        claim
        for claim in claims
        if claim.value not in FREE_NUMBERS
        and not any(_supports(value, claim) for value in allowed)
    ]

    # Dates are checked rather than skipped. The numeric extractor masks them
    # so that 25 and 2026 are not read as measurements, which is right, and
    # leaves the date itself unchecked, which is not: a sentence opening "the
    # market split on 2 September" about an episode on the third is wrong in
    # the one way a reader can catch unaided.
    wrong_dates = unsupported_dates(text, sheet)

    missing_sources = False
    if require_sources and claims:
        # Only sources that carry an identifier can be cited at all. "derived
        # from the spread" names no document, so demanding it be quoted would
        # fail explanations that are perfectly well grounded.
        named = [source for source in sheet.sources() if citable_tokens(source)]
        missing_sources = bool(named) and not any(
            _mentions(text, source) for source in named
        )

    return Verdict(
        ok=not unsupported and not missing_sources and not wrong_dates,
        claims=claims,
        unsupported=unsupported,
        missing_sources=missing_sources,
        wrong_dates=wrong_dates,
    )


#: What counts as naming a source: a publisher or a document code, not an
#: ordinary word. Matching on any token of the source string would let
#: "capacity fell to 3,195 MW" count as citing "A61 day-ahead capacity", which
#: would make the requirement meaningless. Identifiers have to be used on
#: purpose; "capacity" can be typed by accident.
_CITABLE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*\b")


def citable_tokens(source: str) -> set[str]:
    """The identifiers in a source string: ENTSO-E, A44, A61, OMIE, ESIOS."""
    return {
        token
        for token in _CITABLE.findall(source)
        if any(character.isdigit() for character in token) or len(token) >= 3
    }


def _mentions(text: str, source: str) -> bool:
    """Was this source named, rather than merely echoed by coincidence?"""
    lowered = text.lower()
    if source.lower() in lowered:
        return True
    return any(token.lower() in lowered for token in citable_tokens(source))