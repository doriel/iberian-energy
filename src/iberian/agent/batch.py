"""Explaining a set of episodes, and only the ones that need it.

The evaluation script explained everything on every run, which was right when
the set was fixed and a human was deciding when to run it. As a daily task it is
wrong twice: it pays for 126 model calls to produce 124 answers that already
exist, and it rewrites explanations that were already reviewed.

So the unit of work here is "episodes with no explanation yet". Two or three a
day, which costs seconds, and an episode's explanation does not change once its
evidence is published: the evidence is fixed the moment the market day settles.

Nothing in this module calls a model or an API. It is handed a retriever and a
completer, which is what lets the test suite drive the whole loop with neither.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable, Iterator

import pandas as pd

from iberian.agent.explain import explain
from iberian.agent.facts import FactSheet, episode_facts
from iberian.agent.tracing import SpanType, trace


def episode_key(episode) -> str:
    """The identifier used everywhere: market day and the start time.

    Defined once here because it is computed in four places and a mismatch
    would silently re-explain every episode on every run.
    """
    start = pd.Timestamp(episode["start_utc"])
    return f"{episode['market_day']}T{start:%H%M}"


def load_records(path: Path) -> list[dict]:
    """Existing explanations, or an empty list. A missing file is not an error."""
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).open():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def write_records(path: Path, records: Iterable[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str) + "\n")


def merge(existing: Iterable[dict], fresh: Iterable[dict]) -> list[dict]:
    """Existing records, with the fresh ones added or replacing by key.

    Newest last on purpose: re-explaining an episode deliberately, by deleting
    its record or passing --all, should win over what was there.
    """
    by_key: dict[str, dict] = {r["episode_key"]: r for r in existing}
    for record in fresh:
        by_key[record["episode_key"]] = record
    return list(by_key.values())


def pending(episodes: pd.DataFrame, existing: Iterable[dict]) -> pd.DataFrame:
    """The episodes with no explanation on file.

    A record that exists but is not grounded still counts as done. Re-running a
    rejection produces another rejection at temperature zero, and turning a
    failed episode into a daily retry loop would spend a model call a day to
    learn the same thing.
    """
    known = {record["episode_key"] for record in existing}
    if episodes.empty:
        return episodes
    keys = episodes.apply(episode_key, axis=1)
    return episodes.loc[~keys.isin(known)]


def to_record(key: str, result, sheet: FactSheet, truth: str = "") -> dict:
    return {
        "episode_key": key,
        "model": result.model,
        "grounded": result.ok,
        "attempts": result.attempts,
        "unsupported": [claim.text for claim in result.verdict.unsupported],
        "missing_sources": result.verdict.missing_sources,
        "numeric_claims": len(result.verdict.claims),
        "text": result.text if result.ok else "",
        # The draft that failed is the most useful row in the file: without it a
        # failure can only be guessed at from the offending figures.
        "final_draft": "" if result.ok else result.text,
        "rejected_drafts": result.rejected,
        "true_cause": truth,
        "sources": sheet.sources(),
    }


@trace(span_type=SpanType.CHAIN)
def explain_episodes(
    episodes: pd.DataFrame,
    build_sheet: Callable[[pd.Series], FactSheet],
    complete,
    model: str,
    max_attempts: int = 2,
    truth_for: Callable[[str], str] | None = None,
    on_each: Callable[[str, object], None] | None = None,
) -> Iterator[dict]:
    """One record per episode, yielded as they are produced.

    Yielded rather than returned so a caller can write each one as it arrives.
    A batch that dies on episode forty should not lose the first thirty-nine,
    and at one model call each that is minutes of work.
    """
    for _, episode in episodes.iterrows():
        key = episode_key(episode)
        sheet = build_sheet(episode)
        result = explain(sheet, complete, max_attempts=max_attempts, model=model)
        record = to_record(key, result, sheet, truth_for(key) if truth_for else "")
        if on_each:
            on_each(key, result)
        yield record


def sheet_builder(client, intervals: pd.DataFrame, direction, parse, binding):
    """A `build_sheet` that retrieves the A78 notices for an episode's day.

    Factored out of the two callers rather than written twice. The script and
    the daily task must assemble identical evidence: a difference here would
    show up as the published explanation disagreeing with the evaluated one,
    which is the sort of thing nobody notices until somebody asks.

    The day's notices are fetched once and reused, because several episodes
    share a market day and the transparency platform is rate limited.

    `parse` and `binding` are passed in rather than imported so this module
    keeps no dependency on the parsing package, and so a test can drive the
    whole loop without an HTTP client.
    """
    from iberian.market_time import market_day_window

    curves_by_day: dict[object, list] = {}

    def build(episode) -> FactSheet:
        window = intervals[
            (intervals["ts_utc"] >= episode["start_utc"])
            & (intervals["ts_utc"] < episode["end_utc"])
        ]

        day = episode["market_day"]
        if day not in curves_by_day:
            day_start, day_end = market_day_window(day)
            response = client.transmission_unavailability(*direction, day_start, day_end)
            curves_by_day[day] = [] if response.is_empty else parse(response)

        start = pd.Timestamp(episode["start_utc"]).to_pydatetime()
        assets = binding(
            curves_by_day[day],
            start,
            pd.Timestamp(episode["end_utc"]).to_pydatetime(),
            # Only notices published before the episode began. Without this the
            # accuracy numbers leak information from the future.
            published_before=start,
            direction=direction,
        )
        return episode_facts(episode, window, assets or None)

    return build