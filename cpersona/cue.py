"""The time cue and the loop's basic form (2.6, docs/RECALL_PROCESS_DESIGN.md §2).

A caller that half-remembers *when* something happened passes `time_cue`. The
server turns it into a period, searches that period with one more retrieval arm
(the cue arm), and then, after the quality gate and autocut have decided which
rows remain, moves a row the cue arm also found up by a bounded number of
places. Up to `L` seats are held for the best rows the cue arm found that the
answer does not hold and the gate did not refuse. If the period holds nothing,
the loop widens it once and runs the cue arm again.

The cue never touches a fused score, the quality gate or autocut, so the set of
rows that pass the gate is the same with and without it. What it can change is
the order (no row moves up more than `L` places) and at most `L` held seats.
The cue arm searches to its own depth, `DEPTH`, whatever the count.

Everything here that a measurement could move -- the margins, `L`, the widening
steps -- is part of the policy version, `POLICY`, which every traced recall
records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

POLICY = "cued-v0.3"

CONFIDENCES = ("sure", "likely", "vague")
# How far a period is widened on each side, as a fraction of its own length.
MARGIN = {"sure": 0.0, "likely": 0.5, "vague": 1.0}
# The most places a row the cue arm found can move up.
LIFT = {"sure": 3, "likely": 2, "vague": 1}
# One confidence step wider, for the one revision. `None` means no period.
WIDER = {"sure": "likely", "likely": "vague", "vague": None}
# The rank constant of the bound: key = p - L * K / (K + c), with c counted from 0,
# the same 61 the fusion's reciprocal rank uses (k = 60, rank + 1).
RANK_CONSTANT = 61
# Seats held for rows the cue arm found, per confidence (§2.11): as many as the
# places a found row may move up. A seat takes a row the answer does not hold and
# the quality gate and autocut did not refuse -- a row no ordinary arm reached, or
# one they admitted that the count cut.
SEATS = {"sure": 3, "likely": 2, "vague": 1}
MAX_SEATS = max(SEATS.values())
# How deep the cue arm searches the period, independent of the count (§2.11): at
# the count's own depth it ranked the rows the ordinary arms already returned.
DEPTH = 50
# A cue whose own period (before any margin) starts no earlier than this long before
# now points only at today or the future. Such a cue is not used: an agent that fills
# the cue with today's date for a request that named no time is the observed way a
# cue goes wrong, and today's records are the newest in the store in any case.
RECENT_ONLY = timedelta(hours=24)

_UNITS = {"days": timedelta(days=1), "weeks": timedelta(weeks=1), "months": timedelta(days=30)}
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The ends of the representable range. A period that reaches past one is clipped
# to it (bug-443): the reach is not always the caller's -- a record stored with a
# year-1 stamp closes an open end at year 1, and a vague margin then reaches past
# it -- so refusing the cue could not prevent it, and raising would turn a recall
# into an error the tool does not document.
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)
_LATEST = datetime.max.replace(tzinfo=timezone.utc)


def _later(t: datetime, delta: timedelta) -> datetime:
    try:
        return t + delta
    except OverflowError:
        return _LATEST


def _earlier(t: datetime, delta: timedelta) -> datetime:
    try:
        return t - delta
    except OverflowError:
        return _EARLIEST


class TimeCueError(ValueError):
    """The caller's `time_cue` could not be read. The message says which part."""


@dataclass(frozen=True)
class TimeCue:
    confidence: str
    after: datetime | None = None
    before: datetime | None = None
    ago: tuple[str, int] | None = None
    long_ago: bool = False

    def echo(self) -> dict:
        """The cue as the trace records it (normalised, never the caller's raw text)."""
        out: dict = {"confidence": self.confidence}
        if self.after is not None:
            out["after"] = self.after.isoformat()
        if self.before is not None:
            out["before"] = self.before.isoformat()
        if self.ago is not None:
            out["ago"] = {"unit": self.ago[0], "value": self.ago[1]}
        if self.long_ago:
            out["ago"] = "long_ago"
        return out


def _parse_instant(value, field: str, end_of_day: bool) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TimeCueError(f"time_cue.{field} must be a date (YYYY-MM-DD) or an ISO-8601 timestamp")
    text = value.strip()
    try:
        if _DATE_ONLY.match(text):
            day = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            # A date names the whole day: `before` includes it.
            return _later(day, timedelta(days=1)) if end_of_day else day
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeCueError(f"time_cue.{field} is not a date or timestamp: {text!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse(raw) -> TimeCue | None:
    """Read a `time_cue` argument. None (or an empty object) means no cue."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise TimeCueError("time_cue must be an object")
    unknown = set(raw) - {"after", "before", "ago", "confidence"}
    if unknown:
        raise TimeCueError(f"time_cue has unknown fields: {sorted(unknown)}")
    confidence = raw.get("confidence")
    if confidence not in CONFIDENCES:
        raise TimeCueError("time_cue.confidence is required: one of sure, likely, vague")
    has_abs = "after" in raw or "before" in raw
    has_rel = "ago" in raw
    if has_abs == has_rel:
        raise TimeCueError("time_cue needs either after/before or ago, not both and not neither")
    if has_abs:
        after = _parse_instant(raw["after"], "after", end_of_day=False) if "after" in raw else None
        before = _parse_instant(raw["before"], "before", end_of_day=True) if "before" in raw else None
        if after is not None and before is not None and before <= after:
            raise TimeCueError("time_cue.before must be later than time_cue.after")
        return TimeCue(confidence, after=after, before=before)
    ago = raw["ago"]
    if ago == "long_ago":
        return TimeCue(confidence, long_ago=True)
    if not isinstance(ago, dict) or set(ago) != {"unit", "value"}:
        raise TimeCueError('time_cue.ago must be {"unit": ..., "value": ...} or "long_ago"')
    unit, value = ago["unit"], ago["value"]
    # A list is not hashable, so the membership test alone raised TypeError (bug-443).
    if not isinstance(unit, str) or unit not in _UNITS:
        raise TimeCueError("time_cue.ago.unit must be one of days, weeks, months")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TimeCueError("time_cue.ago.value must be a whole number, 0 or more")
    return TimeCue(confidence, ago=(unit, value))


def period(
    cue: TimeCue,
    confidence: str,
    now: datetime,
    span: tuple[datetime | None, datetime | None],
) -> tuple[datetime, datetime] | None:
    """The period `[start, end)` a cue points at, widened by `confidence`'s margin.

    `confidence` is passed separately so the one revision can ask for the next
    step's width. `span` is the scope's `(oldest, newest)` timestamp: it closes an
    open end and defines `long_ago` (the oldest third of what the scope holds).
    Returns None when there is no period to search (an empty scope for a cue
    that needs the span).
    """
    oldest, newest = span
    if cue.long_ago:
        if oldest is None or newest is None:
            return None
        start, end = oldest, oldest + (newest - oldest) / 3
    elif cue.ago is not None:
        unit, value = cue.ago
        width = _UNITS[unit]
        try:
            centre = _earlier(now, width * value)
        except OverflowError:  # the product itself is past the largest timedelta
            centre = _EARLIEST
        start, end = _earlier(centre, width / 2), min(_later(centre, width / 2), now)
    else:
        start = cue.after if cue.after is not None else oldest
        end = cue.before if cue.before is not None else now
        if start is None:
            return None
    if end <= start:
        # A one-instant scope (a single record) still has a period around it.
        end = _later(start, timedelta(seconds=1))
    margin = (end - start) * MARGIN[confidence]
    return _earlier(start, margin), _later(end, margin)


def recent_only(cue: TimeCue, now: datetime, span: tuple[datetime | None, datetime | None]) -> bool:
    """Whether the cue points only at the last `RECENT_ONLY` or later, and so is not used.

    Judged on the cue's own period, before any confidence margin: a `sure` cue for
    today and a `vague` one for today point at the same thing.
    """
    window = period(cue, "sure", now, span)
    return window is not None and window[0] >= now - RECENT_ONLY


def lift(admitted: list[dict], cue_rank: dict, bound: int, rid_of) -> tuple[list[dict], list[dict]]:
    """Move rows the cue arm found up, by at most `bound` places.

    `admitted` is the order the gate, autocut and prior produced (best first);
    `cue_rank` maps a row id to its 0-based rank on the cue arm. A row's sort key
    is its position minus `bound * 61 / (61 + c)`, sorted ascending. A tie between
    a row the cue found and one it did not goes to the found row; any other tie
    keeps the original order. So a found row can pass a row at most `bound`
    places ahead of it, and only another found row fewer than `bound` places
    ahead: no row moves up more than `bound` places, however many move at once,
    and the row the cue arm ranks first moves exactly `bound` places when it
    stands that far down.
    Returns the new order and one entry per row that moved.
    """
    if bound <= 0 or not cue_rank:
        return admitted, []

    def key(item):
        p, row = item
        c = cue_rank.get(rid_of(row))
        if c is None:
            return (p, 1, p)
        return (p - bound * RANK_CONSTANT / (RANK_CONSTANT + c), 0, p)

    ordered = [row for _, row in sorted(enumerate(admitted), key=key)]
    before = {id(row): p for p, row in enumerate(admitted)}
    moves = [
        {"row": row, "from": before[id(row)], "to": q}
        for q, row in enumerate(ordered)
        if before[id(row)] != q
    ]
    return ordered, moves


def utc(value: str | None) -> datetime | None:
    """A stored timestamp as an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def sql_instant(value: datetime) -> str:
    """A period bound as SQLite's `datetime()` reads it (UTC, second resolution).

    isoformat rather than strftime: strftime's %Y does not pad a year below 1000
    on every platform, and a clipped bound at year 1 would then compare as text
    against four-digit years in the wrong order.
    """
    return value.astimezone(timezone.utc).isoformat(sep=" ", timespec="seconds")[:19]
