"""The propagation seat (cpersona/propagation.py).

Pinned here: the selector's arithmetic -- the weights, the offset, the direction
of the cosine rank, ties and candidates without a vector; that the seat adds at
most one row after the window and changes no row of the window, byte for byte;
that the row it adds is the best candidate of the deeper order by that
arithmetic; that with the seat off no deeper ranking runs and neither the ledger
nor the trace grows; that the Core refuses a selector which seats an ineligible
row or too many; where it does not apply (cascade, a blank query, a gate rescue,
reconstruct, recall_with_context); and that the recall tool follows the setting.
"""
import random

import numpy as np
import pytest

from cpersona import budget, builtin_providers, config, memory_handlers, propagation, providers, reconstruct
from cpersona.database import get_db

from test_providers import AGENT, CORPUS, QUERY, _seed, _with


@pytest.fixture
def installed():
    """Install a provider set for one test and put the previous one back."""
    previous = providers.active()
    yield providers.install
    providers.install(previous)


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def _reference(n, sims):
    """The formula in propagation's docstring, written out once more with plain floats."""
    by_cos = sorted(range(n), key=lambda i: (-sims[i], i))
    cos_rank = {i: r + 1 for r, i in enumerate(by_cos)}
    score = {
        i: (1 - 0.35) / (60 + i + 1) + 0.35 / (60 + cos_rank[i])
        for i in range(n)
    }
    return sorted(range(n), key=lambda i: (-score[i], i))


# --- the selector's arithmetic ----------------------------------------------------------


def test_the_policy_is_the_measured_one():
    """The values measured on two-record questions; changing one is a new policy version."""
    assert (propagation.POLICY, propagation.DEPTH, propagation.COSINE_WEIGHT,
            propagation.RANK_OFFSET, propagation.SEATS) == ("propagation-v1", 100, 0.35, 60, 1)


def test_the_fused_order_wins_when_closeness_barely_differs():
    # Fused order A, B, C; cosines 0.1 / 0.5 / 0.9 give cosine ranks 3 / 2 / 1.
    #   A = .65/61 + .35/63 = .016211   B = .65/62 + .35/62 = .016129   C = .65/63 + .35/61 = .016055
    # With the weights swapped C would win (.016211 against A's .016055).
    anchor = _unit([1, 0])
    cands = [{"id": 1}, {"id": 2}, {"id": 3}]
    vecs = {i: _unit([c, (1 - c * c) ** 0.5]) for i, c in enumerate([0.1, 0.5, 0.9])}
    assert propagation.order(cands, anchor, vecs) == [0, 1, 2]


def test_a_close_second_overtakes_a_distant_first():
    # 20 candidates. The first in fused order is the farthest (cosine rank 20),
    # the second the closest (rank 1):
    #   first  = .65/61 + .35/80 = .015031      second = .65/62 + .35/61 = .016222
    # With the cosine ranked in the wrong direction the first would win instead.
    anchor = _unit([1, 0])
    cosines = [0.0, 0.99] + [0.5 - 0.02 * i for i in range(18)]
    vecs = {i: _unit([c, (1 - c * c) ** 0.5]) for i, c in enumerate(cosines)}
    cands = [{"id": i + 1} for i in range(20)]
    assert propagation.order(cands, anchor, vecs)[0] == 1


def test_the_order_is_the_documented_formula():
    rng = random.Random(260926)
    for _ in range(200):
        n = rng.randint(1, 40)
        anchor = _unit([rng.gauss(0, 1) for _ in range(8)])
        missing = {i for i in range(n) if rng.random() < 0.15}
        vecs = {i: _unit([rng.gauss(0, 1) for _ in range(8)]) for i in range(n) if i not in missing}
        sims = [float(vecs[i] @ anchor) if i in vecs else float("-inf") for i in range(n)]
        cands = [{"id": i + 1} for i in range(n)]
        assert propagation.order(cands, anchor, vecs) == _reference(n, sims)


def test_ties_keep_the_deeper_order():
    anchor = _unit([1, 0])
    same = _unit([0.7, 0.3])
    vecs = {0: same, 1: same, 2: same}
    cands = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert propagation.order(cands, anchor, vecs) == [0, 1, 2]
    assert propagation.ranks(cands, anchor, vecs) == {0: 1, 1: 2, 2: 3}


def test_a_candidate_without_a_vector_ranks_last_by_closeness():
    anchor = _unit([1, 0])
    vecs = {1: _unit([0.9, 0.1]), 2: _unit([0.2, 0.8])}
    cands = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert propagation.ranks(cands, anchor, vecs) == {1: 1, 2: 2, 0: 3}


@pytest.mark.parametrize("blob", [None, b"", b"\x00\x00\x00", np.zeros(4, np.float32).tobytes(),
                                  np.array([np.nan, 1, 2, 3], np.float32).tobytes()])
def test_a_vector_that_cannot_be_a_direction_is_none(blob):
    assert propagation.unit(blob) is None


def test_a_vector_of_another_width_is_none():
    assert propagation.unit(np.ones(4, np.float32).tobytes(), 8) is None
    assert propagation.unit(np.ones(8, np.float32).tobytes(), 8) is not None


# --- the recall ------------------------------------------------------------------------


def _rows(out):
    return [m for m in out["messages"] if m.get("match_reason", {}).get("signal") == "propagation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rrf", "rsf"])
async def test_the_seat_adds_one_row_and_changes_none_of_the_window(fake_embedding_client, monkeypatch, mode):
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", mode)
    await _seed(CORPUS)
    off = await memory_handlers.do_recall(AGENT, QUERY, limit=3)
    on = await memory_handlers.do_recall(AGENT, QUERY, limit=3, propagation_seat=True)
    seat = _rows(on)
    assert len(seat) == 1
    # recall returns least relevant first, so the added place comes first; every
    # other row is the recall without the seat, byte for byte.
    assert on["messages"][0] is seat[0]
    assert on["messages"][1:] == off["messages"]
    assert seat[0]["ref"] not in {m["ref"] for m in off["messages"]}
    assert seat[0]["match_reason"]["admission"] == "reservation"


@pytest.mark.asyncio
async def test_the_seat_is_the_best_candidate_of_the_deeper_order(fake_embedding_client, monkeypatch):
    await _seed(CORPUS)
    on = await memory_handlers.do_recall(AGENT, QUERY, limit=3, propagation_seat=True, trace=True)
    note = on["trace"]["propagation"]
    window = [e["ref"] for e in on["trace"]["order"]["before_cut"][:3]]
    # The deeper order, ranked the way the seat ranks it but outside the seat, at the
    # measured depth written out rather than read back from the module under test.
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 100)
    deeper = await memory_handlers.do_recall(AGENT, QUERY, limit=3, trace=True)
    order = [e["ref"] for e in deeper["trace"]["order"]["before_cut"]]
    cands = [r for r in order if r not in window]
    assert note["candidates"] == len(cands) > 1
    db = await get_db()

    async def vec(ref):
        kind, i = ref.split(":")
        table = "memories" if kind == "mem" else "episodes"
        async with db.execute(f"SELECT embedding FROM {table} WHERE id = ?", (int(i),)) as cur:
            (blob,) = await cur.fetchone()
        return propagation.unit(blob)

    anchor = await vec(window[0])
    sims = [float(await vec(r) @ anchor) for r in cands]
    best = cands[_reference(len(cands), sims)[0]]
    assert [s["ref"] for s in note["seated"]] == [best] == [r["ref"] for r in _rows(on)]
    assert note["anchor"] == window[0]


@pytest.mark.asyncio
async def test_no_candidate_below_the_cut_leaves_the_place_empty(fake_embedding_client):
    await _seed(CORPUS)
    on = await memory_handlers.do_recall(AGENT, QUERY, limit=50, propagation_seat=True, trace=True)
    assert _rows(on) == []
    assert on["trace"]["propagation"]["candidates"] == 0
    assert on["trace"]["budget"]["used"][budget.PROPAGATION_FETCH] == 1


@pytest.mark.asyncio
async def test_off_the_ledger_and_the_trace_do_not_grow(fake_embedding_client):
    assert config.RECALL_PROPAGATION_SEAT is False
    await _seed(CORPUS)
    off = await memory_handlers.do_recall(AGENT, QUERY, limit=3, trace=True)
    trace = off["trace"]
    assert budget.PROPAGATION_FETCH not in trace["budget"]["limits"]
    assert "propagation" not in trace and "propagation" not in trace["policy"]
    assert "propagation_seat" not in trace["request"]
    assert [r for r in trace["reservation"] if r["kind"] == "propagation"] == []
    on = await memory_handlers.do_recall(AGENT, QUERY, limit=3, propagation_seat=True, trace=True)
    assert on["trace"]["budget"]["limits"][budget.PROPAGATION_FETCH] == 1
    assert on["trace"]["policy"]["propagation"] == propagation.POLICY
    assert on["trace"]["request"]["propagation_seat"] is True
    assert [r["kind"] for r in on["trace"]["reservation"]] == ["propagation"]


def test_the_ledger_declares_one_deeper_fetch_and_refuses_a_second():
    ledger = budget.Ledger.for_recall(propagation=True)
    ledger.spend(budget.PROPAGATION_FETCH)
    with pytest.raises(budget.BudgetExceeded):
        ledger.spend(budget.PROPAGATION_FETCH)
    assert list(budget.Ledger.for_recall().limits) == [
        budget.ORDINARY_FETCH, budget.BLOCK_FETCH, budget.CUE_STAGE, budget.ITERATION,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("choose, message", [
    (lambda self, cands, anchor, vecs, places: [dict(cands[0])], "not eligible"),
    (lambda self, cands, anchor, vecs, places: cands[:2], "2 seats filled; 1 are held"),
])
async def test_the_core_refuses_a_selector_that_breaks_the_seat(
    fake_embedding_client, installed, choose, message
):
    await _seed(CORPUS)
    installed(_with("propagation_selector", seat=choose))
    with pytest.raises(providers.ProviderContractError, match=message):
        await memory_handlers.do_recall(AGENT, QUERY, limit=3, propagation_seat=True)


@pytest.mark.asyncio
async def test_not_in_the_cascade_nor_for_a_blank_query(fake_embedding_client, monkeypatch):
    await _seed(CORPUS)
    blank = await memory_handlers.do_recall(AGENT, "   ", limit=3, propagation_seat=True, trace=True)
    assert _rows(blank) == [] and budget.PROPAGATION_FETCH not in blank["trace"]["budget"]["limits"]
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "cascade")
    cascade = await memory_handlers.do_recall(AGENT, QUERY, limit=3, propagation_seat=True, trace=True)
    assert _rows(cascade) == [] and budget.PROPAGATION_FETCH not in cascade["trace"]["budget"]["limits"]


@pytest.mark.asyncio
async def test_not_after_a_gate_rescue(fake_embedding_client, monkeypatch, installed):
    await _seed(CORPUS)
    base = builtin_providers.Scoring.score

    async def backfilled(self, db, agent_id, results, deep, **kw):
        out = await base(self, db, agent_id, results, deep, **kw)
        for r in out[0]:
            r["_cosine_backfilled"] = True
        return out

    installed(_with("scoring", score=backfilled))
    monkeypatch.setattr(memory_handlers, "_apply_quality_gate", lambda results, *a, **k: [])
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=3, propagation_seat=True, trace=True)
    assert out.get("gate_fallback") is True
    assert _rows(out) == []
    assert out["trace"]["budget"]["used"][budget.PROPAGATION_FETCH] == 0


@pytest.mark.asyncio
async def test_reconstruct_and_recall_with_context_do_not_take_the_seat(fake_embedding_client, monkeypatch):
    await _seed(CORPUS)
    before_r = await reconstruct.do_reconstruct(AGENT, QUERY, count=3)
    before_c = await memory_handlers.do_recall_with_context(AGENT, QUERY, [], limit=3)
    monkeypatch.setattr(config, "RECALL_PROPAGATION_SEAT", True)
    after_r = await reconstruct.do_reconstruct(AGENT, QUERY, count=3)
    after_c = await memory_handlers.do_recall_with_context(AGENT, QUERY, [], limit=3)
    assert after_r == before_r
    assert after_c == before_c and _rows(after_c) == []


@pytest.mark.asyncio
async def test_the_recall_tool_follows_the_setting(fake_embedding_client, monkeypatch):
    from cpersona import server

    await _seed(CORPUS)
    tool = server.registry._handlers["recall"]
    off = await tool({"agent_id": AGENT, "query": QUERY, "limit": 3})
    assert _rows(off) == []
    monkeypatch.setattr(config, "RECALL_PROPAGATION_SEAT", True)
    on = await tool({"agent_id": AGENT, "query": QUERY, "limit": 3})
    assert len(_rows(on)) == 1
    assert on["messages"][1:] == off["messages"]


@pytest.mark.asyncio
async def test_the_deeper_ranking_leaves_the_trace_to_the_answer(fake_embedding_client):
    await _seed(CORPUS)
    off = await memory_handlers.do_recall(AGENT, QUERY, limit=3, trace=True)
    on = await memory_handlers.do_recall(AGENT, QUERY, limit=3, propagation_seat=True, trace=True)
    for key in ("arms", "fusion", "gate", "autocut", "order"):
        assert on["trace"][key] == off["trace"][key], key


@pytest.mark.asyncio
async def test_the_count_allows_both_held_seats_at_once(fake_embedding_client, monkeypatch):
    from test_providers import SEAT_CORPUS, _period

    from cpersona import blocks

    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    # No block places, so the bound is exactly limit + the two held seats and a
    # count that forgot one of them refuses this answer.
    monkeypatch.setattr(blocks, "BLOCK_RESERVATION", 0)
    await _seed(SEAT_CORPUS)
    out = await memory_handlers.do_recall(
        AGENT, QUERY, limit=2, time_cue=_period(115, 95), propagation_seat=True
    )
    signals = [m["match_reason"]["signal"] for m in out["messages"]]
    assert signals.count("cue") == 1 and signals.count("propagation") == 1
    assert len(out["messages"]) == 2 + 2
