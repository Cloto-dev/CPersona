"""Confirm a recall failure's stage from its trace (docs/RECALL_PROCESS_DESIGN.md §1.3).

The server records what it did; only a caller that knows the answer can say that a
recall failed and where. This module does that from a recall trace
(`recall(..., trace=True)["trace"]`) and the reference of the record that holds the
answer, and assigns one confirmed code from the failure taxonomy:

    CANDIDATE_MISS       the answer's record is in no retrieval arm
    FILTER_DROP          it was a candidate, and the quality gate or autocut removed it
    RANKING_MISS         it passed, but the count cut dropped it; or an arm with held seats
                         (the block arm, the time cue's arm) reached it and it ranked below
                         the seats held for that arm
    RECONSTRUCTION_LOSS  it was returned, and the answer's text was not in what was shown
    AGENT_MISUSE         the answer's text was shown, and the reader still answered wrong
    UNATTRIBUTED         none of the above can be established from the record

`None` means no failure: the record was returned and the reader answered correctly.

Usage as a library: `confirm(trace, gold_refs, shown_text=..., quotes=..., correct=...)`.
"""
from __future__ import annotations

import re

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


def confirm(
    trace: dict,
    gold_refs: set[str] | list[str],
    *,
    shown_text: str | None = None,
    quotes: list[str] | None = None,
    correct: bool | None = None,
) -> str | None:
    """The confirmed failure code for one recall, or None when it did not fail.

    `gold_refs`: references of the records holding the answer (any one suffices).
    `shown_text`: the text the reader was shown; `quotes`: the answer's text in the
    record. Both are needed to tell RECONSTRUCTION_LOSS from AGENT_MISUSE.
    `correct`: whether the reader answered correctly, when a reader was run.
    """
    gold = set(gold_refs)
    if not gold:
        return "UNATTRIBUTED"

    if not gold & _arm_refs(trace):
        return "CANDIDATE_MISS"

    # Returned first: a held seat admits a row the gate never passed (the block
    # arm's reservation), so a gate verdict alone does not mean the row was lost.
    order = trace.get("order", {})
    before_cut = [row["ref"] for row in order.get("before_cut", [])]
    limit = order.get("limit")
    kept = set(before_cut[:limit]) if limit is not None else set(before_cut)
    reserved = {row["ref"] for row in trace.get("reservation", [])}
    if not gold & (kept | reserved):
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

    if shown_text is not None and quotes:
        shown = _norm(shown_text)
        if not any(_norm(q) and _norm(q) in shown for q in quotes):
            return "RECONSTRUCTION_LOSS"
        if correct is False:
            return "AGENT_MISUSE"
        return None
    if correct is False:
        return "UNATTRIBUTED"
    return None
