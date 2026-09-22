"""The same evidence, retrieved from the vector index instead of the API.

`binding_assets` in the parsing package answers one question: which assets were
constrained across this interval, tightest first. It answers it from curves that
were just fetched from the transparency platform. This module answers the same
question from `gold_transmission_notices_index`, and returns rows of the same
shape, so the fact sheet cannot tell which path produced them.

That sameness is the whole design. The evaluation compares the two paths on the
episodes that already have explanations, and a difference in row shape, sort
order or units would show up as a finding about retrieval when it is really a
finding about this file.

**The filters are the point in time guarantee.** Three of them, and each one
exists for a reason worth stating:

- `published_epoch <= start - 1` keeps notices published after the episode out.
  Without it the index happily returns a notice written the following week, and
  a smoke test on this endpoint confirmed it: the future notice ranked first.
- the two outage epochs keep notices that do not overlap the window out, which
  the direct path does with a plain comparison.
- `out_domain` and `in_domain` keep the other side of the border out. A
  constraint on Portugal to Spain says nothing about why Spanish power could not
  reach Portugal.

**The capacity is computed here, not read from the index.** `min_available_mw`
is the lowest value across the whole notice, and what binds an interval is the
lowest value inside that interval. The step function travels with the row as
`breakpoints_json` precisely so this path can compute the same number the direct
path computes, from the same function.

**What the two paths genuinely do not share** is recall. The direct path sees
every notice the platform returned; this one sees the `num_results` nearest to a
query string. With a handful of notices in force on a border that is a
distinction without a difference, and `num_results` is set well above that. It
stops being true the day this index holds a country's worth of notices, and the
evaluation is what would show it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from iberian.agent.notices import breakpoints_from_json
from iberian.parsing.entsoe_outages import binding_order, minimum_capacity

#: What comes back from the index. `text` is not among them: the agent is given
#: facts, not prose it might copy, and the row's own fields are what the fact
#: sheet reads. It stays in the index because it is what gets embedded and what
#: a person reads when checking a retrieval by hand.
COLUMNS: tuple[str, ...] = (
    "notice_id",
    "published_epoch",
    "outage_start_epoch",
    "outage_end_epoch",
    "out_domain",
    "in_domain",
    "asset",
    "asset_named",
    "status",
    "business_type",
    "min_available_mw",
    "breakpoints_json",
)

#: Well above the number of notices in force on one border at one time. The
#: filters run before the ranking, so this is a cap on matching notices rather
#: than on the index, and anything cheaper would start deciding the answer.
DEFAULT_NUM_RESULTS = 50


def query_text(start: datetime, end: datetime) -> str:
    """What the index is asked for.

    Deliberately plain. The filters already restrict the candidates to notices
    in force on this border during this window, so the query's job is to rank
    what survives, not to select it. A cleverer query would make the ranking
    harder to explain without making it better.
    """
    return (
        "transmission unavailability reducing available capacity on the "
        f"Spain to Portugal border on {start:%Y-%m-%d}"
    )


def filters(
    start: datetime,
    end: datetime,
    published_before: datetime | None = None,
    direction: tuple[str, str] | None = None,
) -> dict:
    """The filter dictionary, in the form the endpoint documents.

    Epoch integers rather than timestamps because the filtering guide documents
    `<=`, `>=`, `>` and `=` for numeric columns and only `>` for timestamps, and
    two of the three comparisons here need a bound the timestamp form does not
    offer.

    The strict comparisons are written as inclusive ones on the neighbouring
    second. `published_epoch <= start - 1` excludes a notice published in the
    same second the episode began, which is the safe side of a tie: a notice
    published at the very instant of the anomaly is not evidence of its cause.
    """
    clauses: dict = {
        # Overlap: the outage starts before the window ends and ends after the
        # window starts. Both written inclusively, see above.
        "outage_start_epoch <=": int(end.timestamp()) - 1,
        "outage_end_epoch >": int(start.timestamp()),
    }
    if published_before is not None:
        clauses["published_epoch <="] = int(published_before.timestamp()) - 1
    if direction is not None:
        clauses["out_domain"] = direction[0]
        clauses["in_domain"] = direction[1]
    return clauses


def _column_names(response: dict, requested: tuple[str, ...]) -> list[str]:
    """The columns the response actually carries, in order.

    Read from the manifest rather than assumed, because the endpoint appends a
    similarity score to every row. Zipping against the requested columns would
    line up by luck and stop lining up the day the response gains a field.
    """
    manifest = (response.get("manifest") or {}).get("columns") or []
    names = [column.get("name") for column in manifest if column.get("name")]
    return names or list(requested)


def rows_from_response(response: dict, requested: tuple[str, ...] = COLUMNS) -> list[dict]:
    """The raw index rows as dictionaries, untouched otherwise."""
    names = _column_names(response, requested)
    data = (response.get("result") or {}).get("data_array") or []
    return [dict(zip(names, row)) for row in data]


def _moment(epoch) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def assets_from_rows(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Index rows as `binding_assets` rows, tightest first.

    A row whose curve says nothing about this window is dropped rather than
    reported at its notice-wide minimum. The direct path drops it for the same
    reason: a notice that overlaps the window by the filter but whose step
    function has no value inside it cannot say what the capacity was.
    """
    out: list[dict] = []
    for row in rows:
        outage_start = _moment(row.get("outage_start_epoch"))
        outage_end = _moment(row.get("outage_end_epoch"))
        # The index filter is supposed to have done this already. It is done
        # again because the exact boundary semantics of the endpoint's numeric
        # comparisons were not verified, and a notice that slipped through would
        # be reported at a capacity from outside the window, which is a wrong
        # number rather than a missing one.
        if outage_end is not None and outage_end <= start:
            continue
        if outage_start is not None and outage_start >= end:
            continue

        capacity = minimum_capacity(
            breakpoints_from_json(row.get("breakpoints_json")), start, end
        )
        if capacity is None:
            continue
        named = bool(row.get("asset_named"))
        out.append(
            {
                # `label` semantics, so a notice with no asset block reads the
                # same on both paths. `asset_named` is what the fact sheet
                # actually branches on.
                "asset": row.get("asset") or "unnamed asset",
                "asset_named": named,
                # Not in the index. The table would have to carry it, and
                # nothing downstream reads it: the fact sheet never did.
                "location": None,
                "status": row.get("status"),
                "business_type": row.get("business_type"),
                "available_mw": float(capacity),
                "published_at": _moment(row.get("published_epoch")),
                "outage_start": outage_start,
                "outage_end": outage_end,
                "direction": f"{row.get('out_domain')} -> {row.get('in_domain')}",
                # Only this path can name the row it came from, which is what
                # makes a disagreement with the direct path checkable by hand.
                "notice_id": row.get("notice_id"),
            }
        )
    out.sort(key=binding_order)
    return out


def vector_assets(
    index,
    start: datetime,
    end: datetime,
    published_before: datetime | None = None,
    direction: tuple[str, str] | None = None,
    num_results: int = DEFAULT_NUM_RESULTS,
) -> list[dict]:
    """`binding_assets`, from the index.

    `index` is anything with a `similarity_search`, which in production is the
    Vector Search index and in the tests is a dozen lines that return a canned
    response. This module never imports the SDK.
    """
    response = index.similarity_search(
        query_text=query_text(start, end),
        columns=list(COLUMNS),
        num_results=num_results,
        filters=filters(start, end, published_before, direction),
    )
    return assets_from_rows(rows_from_response(response), start, end)


def binding_from_index(index, num_results: int = DEFAULT_NUM_RESULTS):
    """A `binding_assets` shaped callable, for `sheet_builder` to be given.

    `sheet_builder` takes the binding function as an argument so the batch
    module keeps no dependency on parsing. That same seam is what lets the
    vector path be swapped in without touching the loop: this returns something
    with the direct function's signature, and the first argument, the curves,
    is ignored because the index has already read them.
    """

    def binding(curves, start, end, published_before=None, direction=None):
        return vector_assets(
            index,
            start,
            end,
            published_before=published_before,
            direction=direction,
            num_results=num_results,
        )

    return binding


def comparable(row: dict) -> tuple:
    """The part of a row the two paths must agree on.

    `location` and `notice_id` are excluded because only one path has them, and
    the timestamps are excluded because the direct path carries whatever the
    parser produced while this one carries a value that went through a second
    of resolution. What is left is what an explanation is built from.
    """
    return (
        row.get("asset"),
        bool(row.get("asset_named")),
        row.get("status"),
        round(float(row["available_mw"]), 3),
    )