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

from iberian.agent.facts import FactSheet

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
# into numbers would produce spurious failures on 2026, 09 and 18.
_DATELIKE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?|\d{1,2}:\d{2}")


@dataclass(frozen=True)
class Claim:
    """A number as it appeared in the text, with how it was written."""

    text: str
    value: float
    is_percent: bool
    decimals: int


@dataclass(frozen=True)
class Verdict:
    """The outcome, and enough detail to show a reader what failed."""

    ok: bool
    claims: list[Claim] = field(default_factory=list)
    unsupported: list[Claim] = field(default_factory=list)
    missing_sources: bool = False

    def describe(self) -> str:
        if self.ok:
            return f"{len(self.claims)} numeric claim(s), all retrieved."
        bad = ", ".join(claim.text for claim in self.unsupported)
        return f"Unsupported: {bad}"


def extract_claims(text: str) -> list[Claim]:
    """Every number a reader would take as a factual assertion."""
    masked = _DATELIKE.sub(lambda m: " " * len(m.group(0)), text)

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


def verify(text: str, sheet: FactSheet, require_sources: bool = True) -> Verdict:
    """Check an explanation against the facts it was given.

    `require_sources` also demands that at least one retrieved document is
    named in the text, because an explanation a reader cannot trace is not
    grounded even when every figure in it happens to be right.
    """
    claims = extract_claims(text)
    allowed = sheet.numbers()

    unsupported = [
        claim
        for claim in claims
        if claim.value not in FREE_NUMBERS
        and not any(_supports(value, claim) for value in allowed)
    ]

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
        ok=not unsupported and not missing_sources,
        claims=claims,
        unsupported=unsupported,
        missing_sources=missing_sources,
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
