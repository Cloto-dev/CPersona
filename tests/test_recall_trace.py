"""The recall trace (docs/RECALL_PROCESS_DESIGN.md §1) and the tool that confirms a
failure's stage from it (benchmarks/recall_trace_confirm.py).

The trace is an instrument, so the checks here are the ones §1.4 names: it changes
nothing when not requested, and when a defect is injected on purpose the confirmation
names the stage that the defect lives in.
"""
import copy

import pytest

from benchmarks.recall_trace_confirm import confirm
from cpersona import config, memory_handlers, reconstruct, server
from cpersona.database import get_db

AGENT = "agent.recall-trace"
QUERY = "harbor lighthouse keeper logbook"
CORPUS = [
    "harbor lighthouse keeper logbook entry about the storm",
    "harbor lighthouse keeper notes",
    "lighthouse logbook",
    "keeper of the harbor",
    "logbook of the lighthouse keeper at the harbor",
    "harbor",
]


async def _seed():
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT,))
    await db.commit()
    refs = []
    for i, content in enumerate(CORPUS):
        out = await memory_handlers.do_store(
            AGENT,
            {"content": content, "source": {"System": "test"}, "timestamp": f"2026-09-{10 + i:02d}T00:00:00+00:00"},
        )
        refs.append(f"mem:{out['id']}")
    return refs


# --- the recall is unchanged -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rrf", "rsf", "cascade"])
async def test_a_requested_trace_changes_nothing_in_the_messages(fake_embedding_client, monkeypatch, mode):
    await _seed()
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", mode)
    # Under the shipped gate and autocut.
    shipped = await memory_handlers.do_recall(AGENT, QUERY, limit=10)
    assert (await memory_handlers.do_recall(AGENT, QUERY, limit=10, trace=True))["messages"] == shipped["messages"]
    assert shipped["messages"]
    # Nothing admits fewer rows than the count, so a trace that changed the count would show.
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "_adaptive_min_score", lambda count: 0.0)
    monkeypatch.setattr(memory_handlers, "AUTOCUT_ENABLED", False)
    plain = await memory_handlers.do_recall(AGENT, QUERY, limit=3)
    traced = await memory_handlers.do_recall(AGENT, QUERY, limit=3, trace=True)
    assert "trace" not in plain
    assert len(plain["messages"]) == 3
    assert traced["messages"] == plain["messages"]
    assert {k for k in traced if k != "trace"} == set(plain)


# --- the shape -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rrf", "rsf"])
async def test_the_trace_accounts_for_every_returned_row(fake_embedding_client, monkeypatch, mode):
    await _seed()
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", mode)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=3, trace=True)
    tr = out["trace"]
    assert tr["trace_version"] == 2
    assert tr["policy"]["process"] == "single-pass-v0" and tr["policy"]["scoring"]
    assert tr["stages"] == [{"stage": 0, "searched": "all arms", "next": None}] and tr["suspected"] == []
    assert tr["scope"]["agent_id"] == AGENT and tr["request"]["limit"] == 3
    assert tr["request"]["depth"] >= 3 and "depth" not in tr
    assert tr["gate"]["origin"] in {"calibrated", "heuristic"}
    returned = [m["ref"] for m in out["messages"]]
    # Returned rows are the first `limit` of the recorded order (recall reverses it).
    assert [row["ref"] for row in tr["order"]["before_cut"][:3]] == list(reversed(returned))
    # Every fused candidate has a gate decision, and every returned row passed it.
    decided = {d["ref"]: d for d in tr["gate"]["decisions"]}
    assert {row["ref"] for row in tr["fusion"]} <= set(decided)
    assert all(decided[ref]["passed"] for ref in returned)
    # Every fused candidate came from at least one arm, with the votes that made its score.
    arm_refs = {row["ref"] for rows in tr["arms"].values() for row in rows}
    for row in tr["fusion"]:
        assert row["ref"] in arm_refs
        # Score and votes are each cut to 4 significant digits, so the sum holds to that precision.
        assert row["score"] == pytest.approx(sum(row["votes"].values()), rel=1e-3)
    # Every decision's threshold is readable: its own where it differs, else its signal's.
    held = tr["gate"]["thresholds"]
    for d in tr["gate"]["decisions"]:
        if d["signal"] in {"rrf", "rsf", "cosine", "confidence"}:
            assert d.get("threshold", held.get(d["signal"])) is not None
    assert set(tr["timing_ms"]) >= {"retrieve_and_score", "gate", "order", "total"}


@pytest.mark.asyncio
async def test_the_trace_carries_no_stored_text(fake_embedding_client):
    await _seed()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=10, trace=True)
    flat = repr(out["trace"])
    assert not any(content in flat for content in CORPUS)


@pytest.mark.asyncio
async def test_reconstruct_returns_the_trace_of_its_recall(fake_embedding_client):
    await _seed()
    out = await reconstruct.do_reconstruct(AGENT, QUERY, count=2, trace=True)
    assert out["trace"]["recall"]["trace_version"] == 2
    assert "recall" not in (await reconstruct.do_reconstruct(AGENT, QUERY, count=2)).get("trace", {})


@pytest.mark.asyncio
async def test_the_recall_tool_passes_trace_through_the_boundary(fake_embedding_client):
    await _seed()
    out = await server.do_recall_boundary(AGENT, QUERY, 10, False, "", [], None, "", trace=True)
    assert out["trace"]["trace_version"] == 2
    plain = await server.do_recall_boundary(AGENT, QUERY, 10, False, "", [], None, "")
    assert "trace" not in plain


# --- deliberate defects name their stage (§1.4 check 1) -----------------------------


@pytest.mark.asyncio
async def test_a_disabled_arm_is_confirmed_as_candidate_miss(fake_embedding_client, monkeypatch):
    refs = await _seed()

    async def no_vector(*args, **kwargs):
        return []

    monkeypatch.setattr(memory_handlers, "_search_vector", no_vector)
    # Every arm disabled: FTS off, and the keyword search too, since with FTS off
    # it still answers through its LIKE fallback.
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "_search_memories_keyword", no_vector)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=10, trace=True)
    assert confirm(out["trace"], {refs[0]}) == "CANDIDATE_MISS"


@pytest.mark.asyncio
async def test_a_gate_forced_high_is_confirmed_as_filter_drop(fake_embedding_client, monkeypatch):
    refs = await _seed()
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "_adaptive_min_score", lambda count: 10.0)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=10, trace=True)
    decisions = [d for d in out["trace"]["gate"]["decisions"] if d["ref"] != "profile"]
    assert decisions and not any(d["passed"] for d in decisions)
    assert {d["reason"] for d in decisions} == {"below_gate"}
    assert out["trace"]["gate"]["origin"] == "heuristic"
    assert confirm(out["trace"], {refs[0]}) == "FILTER_DROP"


@pytest.mark.asyncio
async def test_a_row_below_the_count_is_confirmed_as_ranking_miss(fake_embedding_client, monkeypatch):
    await _seed()
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    # A fusion deeper than the count, so rows exist below the cut (2.6 "Depth is not count").
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 10)
    monkeypatch.setattr(memory_handlers, "AUTOCUT_ENABLED", False)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=1, trace=True)
    cut = out["trace"]["order"]["cut_by_count"]
    assert cut, "the corpus must leave rows below a count of one"
    assert confirm(out["trace"], {cut[-1]}) == "RANKING_MISS"
    kept = out["trace"]["order"]["before_cut"][0]["ref"]
    assert confirm(out["trace"], {kept}) is None


@pytest.mark.asyncio
async def test_a_calibrated_gate_is_traced_as_calibrated(fake_embedding_client, monkeypatch):
    await _seed()
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    monkeypatch.setattr(memory_handlers.vector, "_get_fused_gate", lambda agent_id: 0.0001)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=10, trace=True)
    assert out["trace"]["gate"]["origin"] == "calibrated"
    assert out["trace"]["gate"]["calibrated"] == pytest.approx(0.0001)


@pytest.mark.asyncio
async def test_the_prior_weights_are_traced_under_scoring(fake_embedding_client, monkeypatch):
    refs = await _seed()
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_RATE", 0.01)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=10, trace=True)
    tr = out["trace"]
    ordered = [row["ref"] for row in tr["order"]["before_cut"] if row["ref"] != "profile"]
    prior = tr["scoring"]["prior"]
    assert set(prior) == set(ordered) and refs[-1] in prior
    # Age is measured from the newest row, which weighs 1; every older row weighs less.
    assert prior[refs[-1]] == 1.0
    assert all(w < 1.0 for ref, w in prior.items() if ref != refs[-1])
    by_ref = {m["ref"]: m for m in out["messages"]}
    assert set(by_ref) & set(prior)
    for ref in set(by_ref) & set(prior):
        assert by_ref[ref]["match_reason"]["prior"] == pytest.approx(prior[ref], abs=1e-4)


# --- the confirmation rules on their own ---------------------------------------------


def _trace(arms=(), decisions=(), autocut_dropped=(), before_cut=(), limit=10, reserved=()):
    return {
        "arms": {"vector_near": [{"ref": r, "rank": i, "raw": None} for i, r in enumerate(arms)]},
        "gate": {"decisions": [{"ref": r, "passed": p} for r, p in decisions]},
        "autocut": {"dropped": list(autocut_dropped)},
        "order": {"before_cut": [{"ref": r} for r in before_cut], "limit": limit,
                  "cut_by_count": list(before_cut[limit:])},
        "reservation": [{"ref": r, "kind": "block"} for r in reserved],
    }


def test_confirm_orders_the_stages():
    base = dict(arms=["mem:a", "mem:b"], decisions=[("mem:a", True), ("mem:b", True)],
                before_cut=["mem:b", "mem:a"], limit=1)
    assert confirm(_trace(**base), {"mem:z"}) == "CANDIDATE_MISS"
    assert confirm(_trace(**{**base, "decisions": [("mem:a", False), ("mem:b", True)]}), {"mem:a"}) == "FILTER_DROP"
    assert confirm(_trace(**{**base, "autocut_dropped": ["mem:a"]}), {"mem:a"}) == "FILTER_DROP"
    assert confirm(_trace(**base), {"mem:a"}) == "RANKING_MISS"
    assert confirm(_trace(**{**base, "reserved": ["mem:a"]}), {"mem:a"}) is None
    # A held seat admits a row the gate refused; the row was returned, not dropped.
    held = {**base, "decisions": [("mem:a", False), ("mem:b", True)], "reserved": ["mem:a"]}
    assert confirm(_trace(**held), {"mem:a"}) is None
    # The block arm reached a row that no held seat was left for.
    seats_full = {**_trace(**base), "arms": {"block": [{"ref": r, "rank": i, "raw": None}
                                                       for i, r in enumerate(["mem:b", "mem:c", "mem:d"])]},
                  "reservation": [{"ref": "mem:c", "kind": "block"}]}
    assert confirm(seats_full, {"mem:d"}) == "RANKING_MISS"
    assert confirm(seats_full, {"mem:c"}) is None
    # The time cue's arm holds a seat the same way, at either stage of the loop.
    for arm in ("cue", "cue_stage_1"):
        cue_full = {**seats_full, "arms": {arm: seats_full["arms"]["block"]}}
        assert confirm(cue_full, {"mem:d"}) == "RANKING_MISS"
    assert confirm({**seats_full, "arms": {"vector_near": seats_full["arms"]["block"]}}, {"mem:d"}) == "UNATTRIBUTED"
    assert confirm(_trace(**base), {"mem:b"}) is None


def test_confirm_needs_every_copy_of_the_answer_to_fail():
    tr = _trace(arms=["mem:a", "ep:a"], decisions=[("mem:a", False), ("ep:a", True)],
                before_cut=["ep:a"], limit=10)
    assert confirm(tr, {"mem:a", "ep:a"}) is None


def test_confirm_separates_what_was_shown_from_what_the_reader_did():
    tr = _trace(arms=["mem:a"], decisions=[("mem:a", True)], before_cut=["mem:a"], limit=10)
    quote = ["the vault combination is 4-1-7"]
    assert confirm(tr, {"mem:a"}, shown_text="a preview that stops early", quotes=quote) == "RECONSTRUCTION_LOSS"
    shown = "note: the vault   combination is 4-1-7 as of today"
    assert confirm(tr, {"mem:a"}, shown_text=shown, quotes=quote, correct=False) == "AGENT_MISUSE"
    assert confirm(tr, {"mem:a"}, shown_text=shown, quotes=quote, correct=True) is None
    assert confirm(copy.deepcopy(tr), set()) == "UNATTRIBUTED"


def test_autocut_is_the_cause_only_when_it_removed_every_copy_that_passed():
    # Both copies passed the gate and autocut removed one; where the other went is not
    # in the record, so the failure is not autocut's.
    tr = _trace(arms=["mem:a", "ep:a"], decisions=[("mem:a", True), ("ep:a", True)],
                autocut_dropped=["mem:a"], before_cut=["mem:c"], limit=10)
    assert confirm(tr, {"mem:a", "ep:a"}) == "UNATTRIBUTED"


def test_one_quote_of_the_answer_in_view_is_enough():
    tr = _trace(arms=["mem:a"], decisions=[("mem:a", True)], before_cut=["mem:a"], limit=10)
    quotes = ["the vault combination is 4-1-7", "the vault is behind the clock"]
    shown = "the vault combination is 4-1-7, and nothing else"
    assert confirm(tr, {"mem:a"}, shown_text=shown, quotes=quotes, correct=True) is None


def test_a_wrong_answer_without_the_shown_text_is_unattributed_not_a_success():
    tr = _trace(arms=["mem:a"], decisions=[("mem:a", True)], before_cut=["mem:a"], limit=10)
    assert confirm(tr, {"mem:a"}, correct=False) == "UNATTRIBUTED"


_QUOTE = ["the vault combination is 4-1-7"]
_TEXT = "notes from tuesday. the vault combination is 4-1-7, said the manager."


def test_an_answer_the_store_does_not_hold_is_a_capture_miss():
    tr = _trace(arms=["mem:b"], decisions=[("mem:b", True)], before_cut=["mem:b"], limit=10)
    # The record was never stored.
    assert confirm(tr, {"mem:a"}, stored_texts={}) == "CAPTURE_MISS"
    # It was stored, but the answer did not survive the write (a write bound cut it).
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, stored_texts={"mem:a": "notes from tuesday."}) == "CAPTURE_MISS"
    # One stored copy that holds the answer is enough; the stage is then found as before.
    held = {"mem:a": "notes from tuesday.", "ep:a": _TEXT}
    assert confirm(tr, {"mem:a", "ep:a"}, quotes=_QUOTE, stored_texts=held) == "CANDIDATE_MISS"
    # Without `stored_texts` the code is never assigned.
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE) == "CANDIDATE_MISS"
    # A copy that was never stored drops out; the stored one decides.
    assert confirm(tr, {"mem:a", "ep:a"}, quotes=_QUOTE, stored_texts={"ep:a": _TEXT}) == "CANDIDATE_MISS"
    assert confirm(tr, {"mem:a", "ep:a"}, quotes=_QUOTE, stored_texts={"ep:a": "notes from tuesday."}) == "CAPTURE_MISS"


def test_an_answer_past_the_searchable_part_is_a_representation_miss():
    tr = _trace(arms=["mem:b"], decisions=[("mem:b", True)], before_cut=["mem:b"], limit=10)
    start = _TEXT.index("the vault")
    stored = {"mem:a": _TEXT}
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, stored_texts=stored,
                   searchable_chars={"mem:a": start}) == "REPRESENTATION_MISS"
    # One character more and the answer begins inside the searchable part.
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, stored_texts=stored,
                   searchable_chars={"mem:a": start + 1}) == "CANDIDATE_MISS"
    # Every place the answer appears must lie past it, not only the last one.
    twice = {"mem:a": "the vault combination is 4-1-7. " + _TEXT}
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, stored_texts=twice,
                   searchable_chars={"mem:a": 5}) == "CANDIDATE_MISS"
    # A copy whose searchable part is unknown cannot be blamed on its representation,
    # even beside a copy whose answer lies past its window.
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, stored_texts=stored, searchable_chars={}) == "CANDIDATE_MISS"
    assert confirm(tr, {"mem:a", "ep:a"}, quotes=_QUOTE, stored_texts={"mem:a": _TEXT, "ep:a": _TEXT},
                   searchable_chars={"mem:a": start}) == "CANDIDATE_MISS"
    # A copy that does not hold the answer has nothing to measure and does not decide.
    assert confirm(tr, {"mem:a", "ep:a"}, quotes=_QUOTE,
                   stored_texts={"mem:a": "notes from tuesday.", "ep:a": _TEXT},
                   searchable_chars={"mem:a": 100, "ep:a": start}) == "REPRESENTATION_MISS"
    # A record an arm reached is not a representation miss, however short its window.
    reached = _trace(arms=["mem:a"], decisions=[("mem:a", False)], before_cut=[], limit=10)
    assert confirm(reached, {"mem:a"}, quotes=_QUOTE, stored_texts=stored,
                   searchable_chars={"mem:a": 0}) == "FILTER_DROP"


def test_the_searchable_part_is_measured_in_the_stored_text_not_the_normalised_one():
    tr = _trace(arms=["mem:b"], decisions=[("mem:b", True)], before_cut=["mem:b"], limit=10)
    spaced = "a\n\n\n\n\n\n\n\nb the vault   combination is 4-1-7"
    start = spaced.index("the vault")
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, stored_texts={"mem:a": spaced},
                   searchable_chars={"mem:a": start}) == "REPRESENTATION_MISS"
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, stored_texts={"mem:a": spaced},
                   searchable_chars={"mem:a": start + 1}) == "CANDIDATE_MISS"


def test_a_degraded_recall_that_lost_the_answer_is_system_degraded():
    missed = _trace(arms=["mem:b"], decisions=[("mem:b", True)], before_cut=["mem:b"], limit=10)
    assert confirm(missed, {"mem:a"}, degraded=True) == "SYSTEM_DEGRADED"
    assert confirm(missed, {"mem:a"}, degraded=False) == "CANDIDATE_MISS"
    # The answer came back anyway: what went wrong after that is not the fault's.
    got = _trace(arms=["mem:a"], decisions=[("mem:a", True)], before_cut=["mem:a"], limit=10)
    assert confirm(got, {"mem:a"}, degraded=True, shown_text=_TEXT, quotes=_QUOTE, correct=False) == "AGENT_MISUSE"
    # A record that was never stored is a capture miss whatever the system's state.
    assert confirm(missed, {"mem:a"}, degraded=True, stored_texts={}) == "CAPTURE_MISS"


def test_a_returned_record_with_nothing_of_it_in_view_is_evidence_noise():
    tr = _trace(arms=["mem:a", "mem:b"], decisions=[("mem:a", True), ("mem:b", True)],
                before_cut=["mem:b", "mem:a"], limit=10)
    # The payload went to the other record; the answer's record is a bare reference.
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, shown_by_ref={"mem:b": "something else", "mem:a": ""}) == "EVIDENCE_NOISE"
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, shown_by_ref={"mem:b": "something else"}) == "EVIDENCE_NOISE"
    # Some of it was shown, but not the passage that holds the answer.
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, shown_by_ref={"mem:a": "notes from tuesday."}) == "RECONSTRUCTION_LOSS"
    # Another copy of the answer in view is enough.
    both = _trace(arms=["mem:a", "ep:a"], decisions=[("mem:a", True), ("ep:a", True)],
                  before_cut=["mem:a", "ep:a"], limit=10)
    assert confirm(both, {"mem:a", "ep:a"}, quotes=_QUOTE, shown_by_ref={"mem:a": "", "ep:a": _TEXT}, correct=True) is None
    assert confirm(both, {"mem:a", "ep:a"}, quotes=_QUOTE, shown_by_ref={"mem:a": "", "ep:a": _TEXT}, correct=False) == "AGENT_MISUSE"
    # One copy shown without the answer and the other not shown at all: the record was
    # in view, so the passage was lost, not crowded out.
    assert confirm(both, {"mem:a", "ep:a"}, quotes=_QUOTE,
                   shown_by_ref={"mem:a": "", "ep:a": "notes from tuesday."}) == "RECONSTRUCTION_LOSS"
    # The text shown for a record that is not the answer's does not count.
    assert confirm(tr, {"mem:a"}, quotes=_QUOTE, shown_by_ref={"mem:b": _TEXT, "mem:a": "x"}) == "RECONSTRUCTION_LOSS"


def test_every_gate_branch_records_its_signal_and_reason():
    """One row per branch of the quality gate, run with a recorder active: each decision
    carries the branch's signal and, when it fails, the reason the gate had."""
    from cpersona import recall_trace

    rows = [
        {"id": 1, "_rid": ("mem", 1), "_confidence_score": 0.1},
        {"id": 2, "_rid": ("mem", 2), "_rsf_score": 0.1},
        {"id": 3, "_rid": ("mem", 3), "_cosine": 0.1},
        {"id": 4, "_rid": ("mem", 4), "_rrf_score": 0.0001},
        {"id": 5, "_rid": ("mem", 5)},
        {"id": -1},
    ]
    rec = recall_trace.TraceRecorder()
    token = rec.activate()
    try:
        memory_handlers._apply_quality_gate(rows, min_score=0.5, memory_count=10)
    finally:
        rec.deactivate(token)
    got = [(d["ref"], d["signal"], d["passed"], d.get("reason")) for d in rec.data["gate"]["decisions"]]
    signals = ["confidence", "rsf", "cosine", "rrf", None]
    reasons = ["below_gate"] * 4 + ["unscored_volume"]
    expected = [(f"mem:{i + 1}", s, False, r) for i, (s, r) in enumerate(zip(signals, reasons))]
    assert got == expected + [("profile", "profile", False, "profile_small_pool")]


# --- the size of a requested trace (trace_version 2) ---------------------------------


def _floats(value):
    if isinstance(value, float):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _floats(v)
    elif isinstance(value, list):
        for v in value:
            yield from _floats(v)


def test_numbers_keep_four_significant_digits_not_four_places():
    from cpersona.recall_trace import _round

    assert _round(0.03278688524590164) == 0.03279
    assert _round(0.7880957126617432) == 0.7881
    # Deep RRF ranks differ past the fourth decimal; four places would print them equal.
    assert _round(1 / 159) != _round(1 / 160)
    assert _round({"a": [1 / 3, {"b": 2.0}], "n": 7, "ok": True, "s": "x"}) == {
        "a": [0.3333, {"b": 2.0}], "n": 7, "ok": True, "s": "x"}
    assert _round(_round(1 / 7)) == _round(1 / 7)


def _gate(decisions):
    return {"decisions": [dict(d) for d in decisions]}


def _read(gate, d):
    return d.get("threshold", gate["thresholds"].get(d["signal"]))


def test_each_signal_holds_its_threshold_once_and_a_differing_decision_keeps_its_own():
    from cpersona.recall_trace import _hoist_thresholds

    original = [
        {"ref": "mem:1", "signal": "rrf", "score": 0.03, "threshold": 0.0098, "passed": True},
        {"ref": "mem:2", "signal": "rrf", "score": 0.01, "threshold": 0.0164, "passed": False},  # capped row
        {"ref": "mem:3", "signal": "rrf", "score": 0.02, "threshold": 0.0098, "passed": True},
        {"ref": "mem:4", "signal": "cosine", "score": 0.7, "threshold": 0.2, "passed": True},
        {"ref": "mem:5", "signal": None, "score": None, "threshold": None, "passed": True},
        {"ref": "profile", "signal": "profile", "score": None, "threshold": 50, "passed": False},
    ]
    gate = _gate(original)
    _hoist_thresholds(gate)
    # The threshold most of a signal's decisions used is held, not the first one seen.
    assert gate["thresholds"] == {"rrf": 0.0098, "cosine": 0.2, "profile": 50}
    assert [("threshold" in d) for d in gate["decisions"]] == [False, True, False, False, False, False]
    # Nothing is lost: every decision reads back the threshold it was decided against.
    assert [_read(gate, d) for d in gate["decisions"]] == [d["threshold"] for d in original]
    # A second finish() leaves it as it was.
    before = copy.deepcopy(gate)
    _hoist_thresholds(gate)
    assert gate == before


def test_the_most_used_threshold_is_held_even_when_it_is_not_first():
    from cpersona.recall_trace import _hoist_thresholds

    original = [{"ref": f"mem:{i}", "signal": "rrf", "threshold": t, "passed": True}
                for i, t in enumerate([0.0164, 0.0098, 0.0098])]
    gate = _gate(original)
    _hoist_thresholds(gate)
    assert gate["thresholds"] == {"rrf": 0.0098}
    assert [_read(gate, d) for d in gate["decisions"]] == [0.0164, 0.0098, 0.0098]


@pytest.mark.asyncio
async def test_a_finished_trace_carries_no_float_past_four_significant_digits(fake_embedding_client):
    await _seed()
    tr = (await memory_handlers.do_recall(AGENT, QUERY, limit=10, trace=True))["trace"]
    floats = list(_floats(tr))
    assert floats  # scores, votes, timings
    assert all(f == float(f"{f:.4g}") for f in floats)
    assert "thresholds" in tr["gate"] and all("threshold" not in d or d["threshold"] != tr["gate"]["thresholds"].get(d["signal"])
                                             for d in tr["gate"]["decisions"])
