"""The recall trace (2.6, docs/RECALL_PROCESS_DESIGN.md §1).

On request, a recall records which rows each stage kept, dropped and reordered,
and why. The record carries references, ranks, scores and reasons only, never
stored text, and is returned in the response rather than stored.

The recorder is held in a context variable for the duration of one recall, so
the stages it passes through can note what they did without a new parameter on
every function between them. Outside a traced recall the variable holds None
and every note is a no-op: a recall that did not ask for a trace does exactly
what it did before.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import time

TRACE_VERSION = 1

# The recall-process policy a call ran under. v0 without a time cue is a single
# pass; the loop that a time cue starts records its own policy name.
PROCESS_SINGLE_PASS = "single-pass-v0"

_current: contextvars.ContextVar[TraceRecorder | None] = contextvars.ContextVar(
    "cpersona_recall_trace", default=None
)


def current() -> TraceRecorder | None:
    """The recorder of the recall in progress, or None when none was requested."""
    return _current.get()


@contextlib.contextmanager
def suspended():
    """Record nothing inside the block: a second ranking a recall runs for its own
    use (the propagation seat's deeper order) must not overwrite the arms, fusion
    and gate the trace holds for the answer's own ranking."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def ref_of(row: dict) -> str:
    """A row's reference as the response spells it: `mem:<id>`, `ep:<id>`, `profile`."""
    rid = row.get("_rid")
    if isinstance(rid, tuple) and len(rid) == 2:
        return f"{rid[0]}:{rid[1]}"
    if row.get("id") == -1:
        return "profile"
    # Imported here: memory_handlers imports this module. One definition of what
    # an episode row is, so the trace cannot disagree with the response's refs.
    from cpersona.memory_handlers import _is_episode_result

    kind = "ep" if _is_episode_result(row) else "mem"
    return f"{kind}:{row.get('id')}"


def _num(value):
    return None if value is None else float(value)


def order_digest(rows: list[dict]) -> str:
    """A digest of the rows' references in order: which rows, and in what order.

    Scores are left out on purpose. They are recorded where a stage reads them
    (the fusion list, the gate decisions), compared there with a tolerance, and a
    digest over floats would differ between platforms whose last bits differ.
    """
    refs = [ref_of(r) for r in rows]
    return hashlib.sha256(json.dumps(refs, separators=(",", ":")).encode()).hexdigest()[:16]


class TraceRecorder:
    """Collects one recall's trace. Every method is safe to call in any order."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._last = self._t0
        self.data: dict = {
            "trace_version": TRACE_VERSION,
            "policy": {},
            "server_version": None,
            "scope": {},
            "request": {},
            "config": {},
            "arms": {},
            "fusion": [],
            "scoring": {},
            "gate": {"decisions": []},
            "autocut": {"fired": False},
            "order": {},
            "reservation": [],
            "stages": [],
            "suspected": [],
            "stage_inputs": {},
            "timing_ms": {},
        }

    # -- lifecycle ----------------------------------------------------------------

    def activate(self) -> contextvars.Token:
        return _current.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token) -> None:
        _current.reset(token)

    def mark(self, stage: str) -> None:
        """Close the time spent since the previous mark under `stage`."""
        now = time.perf_counter()
        self.data["timing_ms"][stage] = round((now - self._last) * 1000, 3)
        self._last = now

    # -- what the recall was asked and how it was configured -----------------------

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def stage_input(self, stage: str, rows: list[dict]) -> None:
        """What `stage` received: how many rows, and a digest of which rows in which order.

        A replay that starts from a stage's recorded input checks it against this
        before running the stage; a stage whose input digest matches and whose
        output differs is where two runs parted.
        """
        self.data["stage_inputs"][stage] = {"rows": len(rows), "order": order_digest(rows)}

    # -- retrieval ----------------------------------------------------------------

    def arm(self, name: str, rows: list[dict], raw_key: str | None) -> None:
        """One retrieval arm's ranked list, as the fusion received it."""
        self.data["arms"][name] = [
            {"ref": ref_of(r), "rank": i, "raw": _num(r.get(raw_key)) if raw_key else None}
            for i, r in enumerate(rows)
        ]

    def fusion(self, rows: list[dict], score_key: str, votes: dict | None = None) -> None:
        """The fused list, with each arm's contribution where the fusion computed one."""
        self.data["fusion"] = [
            {
                "ref": ref_of(r),
                "score": _num(r.get(score_key)),
                **({"votes": votes.get(ref_of(r), {})} if votes is not None else {}),
            }
            for r in rows
            if r.get("id") != -1
        ]

    def penalty(self, row: dict, factor: float) -> None:
        self.data["scoring"].setdefault("episode_penalty", {})[ref_of(row)] = round(factor, 6)

    # -- admission ----------------------------------------------------------------

    def gate_decision(
        self, row: dict, signal: str | None, score, threshold, passed: bool, reason: str | None
    ) -> None:
        entry = {"ref": ref_of(row), "signal": signal, "score": _num(score), "threshold": _num(threshold),
                 "passed": passed}
        if not passed:
            entry["reason"] = reason
        self.data["gate"]["decisions"].append(entry)

    def gate_summary(self, **fields) -> None:
        self.data["gate"].update(fields)

    def autocut(self, before: list[dict], after: list[dict]) -> None:
        kept = {ref_of(r) for r in after}
        dropped = [ref_of(r) for r in before if ref_of(r) not in kept]
        self.data["autocut"] = {"fired": bool(dropped), "kept": len(after), "dropped": dropped}

    # -- order --------------------------------------------------------------------

    def order(self, before_cut: list[dict], limit: int) -> None:
        priors = {ref_of(r): round(r["_prior"], 6) for r in before_cut if r.get("_prior") is not None}
        if priors:
            self.data["scoring"]["prior"] = priors
        self.data["order"] = {
            "before_cut": [{"ref": ref_of(r)} for r in before_cut],
            "limit": limit,
            "cut_by_count": [ref_of(r) for r in before_cut[limit:]],
        }

    def cue(self, note: dict, bound: int) -> None:
        """The time cue's effect: the bound it ran under, the moves and the seat."""
        self.data["cue"] = {
            "bound": bound,
            "confidence": note["confidence"],
            "lifted": [{"ref": ref_of(m["row"]), "from": m["from"], "to": m["to"]} for m in note.get("lifted", [])],
            "seated": [ref_of(r) for r in note.get("seated", [])],
        }

    def reservation(self, rows: list[dict], kind: str) -> None:
        self.data["reservation"].extend({"ref": ref_of(r), "kind": kind} for r in rows)

    # -- output -------------------------------------------------------------------

    def finish(self) -> dict:
        self.data["timing_ms"]["total"] = round((time.perf_counter() - self._t0) * 1000, 3)
        if not self.data["stages"]:
            # v0 without a time cue is one pass: the whole record above is stage 0.
            self.data["stages"] = [{"stage": 0, "searched": "all arms", "next": None}]
        return self.data
