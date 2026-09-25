"""The time cue and the loop's basic form (2.6, docs/RECALL_PROCESS_DESIGN.md §2).

A caller that half-remembers *when* something happened passes `time_cue`. The
server turns it into a period, searches that period with one more retrieval arm
(the cue arm), and then, after the quality gate and autocut have decided which
rows remain, moves a row the cue arm also found up by a bounded number of
places. One seat is held for the best row only the cue arm found. If the period
holds nothing, the loop widens it once and runs the cue arm again.

The cue never touches a fused score, the quality gate or autocut, so the set of
rows that pass the gate is the same with and without it. What it can change is
the order (no row moves up more than `L` places) and at most one held seat.

Everything here that a measurement could move -- the margins, `L`, the widening
steps -- is part of the policy version, `POLICY`, which every traced recall
records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

POLICY = "cued-v0"

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
SEATS = 1

_UNITS = {"days": timedelta(days=1), "weeks": timedelta(weeks=1), "months": timedelta(days=30)}
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
            return day + timedelta(days=1) if end_of_day else day
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
    if unit not in _UNITS:
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
        centre = now - width * value
        start, end = centre - width / 2, min(centre + width / 2, now)
    else:
        start = cue.after if cue.after is not None else oldest
        end = cue.before if cue.before is not None else now
        if start is None:
            return None
    if end <= start:
        # A one-instant scope (a single record) still has a period around it.
        end = start + timedelta(seconds=1)
    margin = (end - start) * MARGIN[confidence]
    return start - margin, end + margin


def lift(admitted: list[dict], cue_rank: dict, bound: int, rid_of) -> tuple[list[dict], list[dict]]:
    """Move rows the cue arm found up, by at most `bound` places.

    `admitted` is the order the gate, autocut and prior produced (best first);
    `cue_rank` maps a row id to its 0-based rank on the cue arm. A row's sort key
    is its position minus `bound * 61 / (61 + c)`, sorted ascending and stably, so
    a row can only pass rows fewer than `bound` places ahead of it: no row moves
    up more than `bound` places, however many rows move at once.
    Returns the new order and one entry per row that moved.
    """
    if bound <= 0 or not cue_rank:
        return admitted, []

    def key(item):
        p, row = item
        c = cue_rank.get(rid_of(row))
        return p if c is None else p - bound * RANK_CONSTANT / (RANK_CONSTANT + c)

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
    """A period bound as SQLite's `datetime()` reads it (UTC, second resolution)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
