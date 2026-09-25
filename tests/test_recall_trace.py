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
    assert tr["trace_version"] == 1
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
        assert row["score"] == pytest.approx(sum(row["votes"].values()))
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
    assert out["trace"]["recall"]["trace_version"] == 1
    assert "recall" not in (await reconstruct.do_reconstruct(AGENT, QUERY, count=2)).get("trace", {})


@pytest.mark.asyncio
async def test_the_recall_tool_passes_trace_through_the_boundary(fake_embedding_client):
    await _seed()
    out = await server.do_recall_boundary(AGENT, QUERY, 10, False, "", [], None, "", trace=True)
    assert out["trace"]["trace_version"] == 1
    plain = await server.do_recall_boundary(AGENT, QUERY, 10, False, "", [], None, "")
    assert "trace" not in plain


# --- deliberate defects name their stage (§1.4 check 1) -----------------------------


@pytest.mark.asyncio
async def test_a_disabled_arm_is_confirmed_as_candidate_miss(fake_embedding_client, monkeypatch):
    refs = await _seed()

    async def no_vector(*args, **kwargs):
        return []

    monkeypatch.setattr(memory_handlers, "_search_vector", no_vector)
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)
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
