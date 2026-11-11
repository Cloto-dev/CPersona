"""Confirm a recall failure's stage from its trace (docs/RECALL_PROCESS_DESIGN.md §1.3).

The server records what it did; only a caller that knows the answer can say that a
recall failed and where. This module does that from a recall trace
(`recall(..., trace=True)["trace"]`) and the reference of the record that holds the
answer, and assigns one confirmed code from the failure taxonomy:

    CAPTURE_MISS         the answer's record is not in the store, or the stored text
                         does not hold the answer (needs `stored_texts`)
    SYSTEM_DEGRADED      the recall ran degraded and the answer's record was not
                         returned (needs `degraded`)
    REPRESENTATION_MISS  the record is in no retrieval arm, and every place the answer
                         appears in it lies past the part that is searchable (needs
                         `stored_texts`, `quotes` and `searchable_chars`)
    CANDIDATE_MISS       the answer's record is in no retrieval arm
    FILTER_DROP          it was a candidate, and the quality gate or autocut removed it
    RANKING_MISS         it passed, but the count cut dropped it; or an arm with held seats
                         (the block arm, the time cue's arm) reached it and it ranked below
                         the seats held for that arm
    EVIDENCE_NOISE       it was returned, and none of its text was shown: the payload went
                         to other evidence (needs `shown_by_ref`)
    RECONSTRUCTION_LOSS  it was returned, and the answer's text was not in what was shown
    AGENT_MISUSE         the answer's text was shown, and the reader still answered wrong
    UNATTRIBUTED         none of the above can be established from the record

`None` means no failure: the record was returned and the reader answered correctly.

The codes that need an argument are assigned only when the caller passes it, so a
caller that passes only the trace gets the same code as before they existed.

Usage as a library: `confirm(trace, gold_refs, shown_text=..., quotes=..., correct=...)`.
`attribution_record(case, trace, gold_refs, ...)` writes the same verdict as a record of
the auditor profile (docs/schemas/recall-failure-attribution-v1.schema.json).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

# The auditor profile's record format this module writes; it reads trace_version 2.
PROFILE_VERSION = 1
PROFILE_TRACE_VERSION = 2

_WS = re.compile(r"\s+")


def _norm(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def _arm_refs(trace: dict) -> set[str]:
    return {row["ref"] for rows in trace.get("arms", {}).values() for row in rows}


def _seat_arm_refs(trace: dict) -> set[str]:
    """Rows reached by an arm whose rows are admitted only through held seats."""
    return {
        row["ref"]
        for name, rows in trace.get("arms", {}).items()
        if name == "block" or name == "cue" or name.startswith("cue_stage_")
        for row in rows
    }


def _holds_answer(text: str | None, quotes: list[str]) -> bool:
    shown = _norm(text)
    return any(_norm(q) and _norm(q) in shown for q in quotes)


def _starts(text: str, quote: str) -> list[int]:
    """Where `quote` begins in `text`, as offsets into `text` itself (whitespace-tolerant)."""
    words = _norm(quote).split(" ")
    if not words or words == [""]:
        return []
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words))
    return [m.start() for m in pattern.finditer(text)]


def _past_the_searchable_part(
    gold: set[str], quotes: list[str], stored_texts: Mapping[str, str], searchable_chars: Mapping[str, int]
) -> bool:
    """Every place the answer appears in every copy begins past that copy's searchable part.

    The capture check has already found the answer in at least one copy, so there is
    always a place to measure.
    """
    for ref in gold:
        if ref not in searchable_chars:
            return False
        for q in quotes:
            starts = _starts(stored_texts[ref], q)  # in order, so the first decides
            if starts and starts[0] < searchable_chars[ref]:
                return False
    return True


def confirm(
    trace: dict,
    gold_refs: set[str] | list[str],
    *,
    shown_text: str | None = None,
    quotes: list[str] | None = None,
    correct: bool | None = None,
    stored_texts: Mapping[str, str] | None = None,
    searchable_chars: Mapping[str, int] | None = None,
    shown_by_ref: Mapping[str, str] | None = None,
    degraded: bool | None = None,
) -> str | None:
    """The confirmed failure code for one recall, or None when it did not fail.

    `gold_refs`: references of the records holding the answer (any one suffices).
    `shown_text`: the text the reader was shown; `quotes`: the answer's text in the
    record. Both are needed to tell RECONSTRUCTION_LOSS from AGENT_MISUSE.
    `correct`: whether the reader answered correctly, when a reader was run.
    `stored_texts`: the stored text of each gold reference the store holds; a gold
    reference missing from it was not stored.
    `searchable_chars`: per gold reference, how many leading characters of its stored
    text semantic search reaches: where the record's embedding window closes, or the
    end of its last block that carries bits, whichever is later. The lexical arm reads
    the whole text and is not counted.
    `shown_by_ref`: the text the reader was shown for each returned record; it tells
    EVIDENCE_NOISE from RECONSTRUCTION_LOSS and stands in for `shown_text`.
    `degraded`: whether the recall ran degraded (a fault, timeout or misconfiguration),
    as the caller recorded it. The response's `advisory` is given once per episode, so
    it alone is not a per-call record.
    """
    gold = set(gold_refs)
    if not gold:
        return "UNATTRIBUTED"
    quotes = quotes or []

    if stored_texts is not None:
        gold &= set(stored_texts)
        if not gold:
            return "CAPTURE_MISS"
        if quotes and not any(_holds_answer(stored_texts[ref], quotes) for ref in gold):
            return "CAPTURE_MISS"  # stored, but the answer did not survive the write

    order = trace.get("order", {})
    before_cut = [row["ref"] for row in order.get("before_cut", [])]
    limit = order.get("limit")
    kept = set(before_cut[:limit]) if limit is not None else set(before_cut)
    reserved = {row["ref"] for row in trace.get("reservation", [])}
    returned = gold & (kept | reserved)

    if degraded and not returned:
        return "SYSTEM_DEGRADED"

    if not gold & _arm_refs(trace):
        if (
            stored_texts is not None
            and searchable_chars is not None
            and quotes
            and _past_the_searchable_part(gold, quotes, stored_texts, searchable_chars)
        ):
            return "REPRESENTATION_MISS"
        return "CANDIDATE_MISS"

    # Returned first: a held seat admits a row the gate never passed (the block
    # arm's reservation), so a gate verdict alone does not mean the row was lost.
    if not returned:
        decisions = {d["ref"]: d for d in trace.get("gate", {}).get("decisions", [])}
        passed = gold & {ref for ref, d in decisions.items() if d.get("passed")}
        if gold & set(decisions) and not passed:
            return "FILTER_DROP"  # the gate removed every copy of the answer that reached it
        if passed and passed <= set(trace.get("autocut", {}).get("dropped", [])):
            return "FILTER_DROP"  # it passed the gate, and autocut removed it
        if gold & set(order.get("cut_by_count", [])):
            return "RANKING_MISS"
        if gold & _seat_arm_refs(trace):
            return "RANKING_MISS"  # an arm with held seats reached it, and it ranked below them
        return "UNATTRIBUTED"

    if shown_by_ref is not None and quotes:
        shown = [shown_by_ref.get(ref) for ref in returned]
        if not any(_holds_answer(text, quotes) for text in shown):
            if not any(_norm(text) for text in shown):
                return "EVIDENCE_NOISE"  # returned with nothing of it in view
            return "RECONSTRUCTION_LOSS"
        if correct is False:
            return "AGENT_MISUSE"
        return None
    if shown_text is not None and quotes:
        if not _holds_answer(shown_text, quotes):
            return "RECONSTRUCTION_LOSS"
        if correct is False:
            return "AGENT_MISUSE"
        return None
    if correct is False:
        return "UNATTRIBUTED"
    return None


def trace_digest(trace: dict) -> str:
    """The first 16 hex digits of the SHA-256 of `trace` as JSON with sorted keys and no
    whitespace: names the exact trace a record was read from without carrying it."""
    body = json.dumps(trace, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def attribution_record(
    case: str,
    trace: dict,
    gold_refs: set[str] | list[str],
    *,
    shown_text: str | None = None,
    quotes: list[str] | None = None,
    correct: bool | None = None,
    stored_texts: Mapping[str, str] | None = None,
    searchable_chars: Mapping[str, int] | None = None,
    shown_by_ref: Mapping[str, str] | None = None,
    degraded: bool | None = None,
    unattributed_reason: str | None = None,
    replay: dict | None = None,
) -> dict | None:
    """One failure as a record of the auditor profile, or None when the recall did not fail.

    The code is `confirm`'s. An UNATTRIBUTED failure needs `unattributed_reason`: the tool
    knows that nothing it was given establishes a stage, not why (for example, that the
    benchmark marks no answer turn), so the auditor states it. `replay` is passed through
    as the profile's replay outcome.
    """
    gold = sorted(set(gold_refs))
    if not gold:
        raise ValueError("a failure needs the reference of at least one record that holds the answer")
    if trace.get("trace_version") != PROFILE_TRACE_VERSION:
        raise ValueError(f"the profile reads trace_version {PROFILE_TRACE_VERSION}, not {trace.get('trace_version')!r}")
    code = confirm(
        trace, gold, shown_text=shown_text, quotes=quotes, correct=correct, stored_texts=stored_texts,
        searchable_chars=searchable_chars, shown_by_ref=shown_by_ref, degraded=degraded,
    )
    if code is None:
        return None
    if code == "UNATTRIBUTED" and not unattributed_reason:
        raise ValueError("an unattributed failure needs the reason it cannot be attributed")
    record = {
        "profile_version": PROFILE_VERSION,
        "case": case,
        "code": code,
        "gold_refs": gold,
        "inputs": {
            "shown_text": shown_text is not None,
            "quotes": bool(quotes),
            "correct": correct,
            "stored_texts": stored_texts is not None,
            "searchable_chars": searchable_chars is not None,
            "shown_by_ref": shown_by_ref is not None,
            "degraded": degraded,
        },
        "trace_version": PROFILE_TRACE_VERSION,
        "trace_digest": trace_digest(trace),
    }
    if code == "UNATTRIBUTED":
        record["unattributed_reason"] = unattributed_reason
    if replay is not None:
        record["replay"] = replay
    return record
