"""bug-247, second design: rsf's gate reads a fixed-scale score, its order is unchanged.

rsf orders by the per-channel min-max fused score, which places a row among the rows
retrieved beside it. The quality gate compares a score with an absolute threshold, so
reading that order score pinned the weakest row of a strong set to 0.0 (always dropped)
and stretched a lone weak hit to 1.0 (always passed). The first fix put every channel on
a fixed scale for both order and gate and was withdrawn when LMEB Track B scored it lower
on 19 of 22 tasks. This design keeps the order bit for bit and gives the gate its own
score (benchmarks/measurements/prereg-rsf-gate-scale.md).

The three properties the pre-registration requires before measuring:

1. Order identity: with the gate open, the full rsf ranking is 2.6.0a8's.
2. The two defects are gone, judged on the gate's score.
3. The gate reads the gate score, not the order score.

The retrievers are replaced so every channel's raw scores are chosen exactly.
"""
from __future__ import annotations

import pytest

from cpersona import memory_handlers as mh

GATE = 0.5


def _near(pairs):
    return [{"id": i, "content": f"memory {i}", "_cosine": c, "_rid": ("mem", i)} for i, c in pairs]


def _keyword(pairs):
    # FTS5 reports bm25 as a negative number, better when more negative.
    return [{"id": i, "content": f"memory {i}", "_bm25": (-s if s is not None else None)} for i, s in pairs]


def _episodes(pairs):
    return [{"id": i, "summary": f"episode {i}", "_bm25": (-s if s is not None else None)} for i, s in pairs]


def _patch_retrievers(monkeypatch, near=(), far=(), keyword=(), episodes=()):
    async def vector(db, agent_id, query, depth, min_similarity=None, far_out=None, **kw):
        if far_out is not None:
            far_out.extend(dict(r) for r in far)
        return [dict(r) for r in near]

    async def memories(db, agent_id, query, depth, **kw):
        return [dict(r) for r in keyword]

    async def eps(db, agent_id, query, depth, **kw):
        return [dict(r) for r in episodes]

    monkeypatch.setattr(mh, "_search_vector", vector)
    monkeypatch.setattr(mh, "_search_memories_keyword", memories)
    monkeypatch.setattr(mh, "_search_episodes_fts", eps)
    monkeypatch.setattr(mh, "FTS_ENABLED", True)
    monkeypatch.setattr(mh.vector, "_embedding_client", object())


async def _fuse(monkeypatch, **channels):
    _patch_retrievers(monkeypatch, **channels)
    from cpersona.database import get_db

    rows = await mh._recall_rsf(await get_db(), "agent.bug247", "a query", 10, False)
    return [r for r in rows if r.get("id") != -1]


def _key(row):
    return ("ep" if "summary" in row else "mem", row["id"])


def _order(rows):
    return {r["id"]: r["_rsf_score"] for r in rows}


def _gate(rows):
    return {r["id"]: r["_rsf_gate_score"] for r in rows}


def _gated(rows, threshold=GATE):
    return {r["id"] for r in mh._apply_quality_gate(rows, threshold, 1000)}


# --- 1. Order identity -------------------------------------------------------------

# (kind, id, _rsf_score) in the order 2.6.0a8's _recall_rsf returned them, recorded on
# master at 7cfdb59 (whose fusion and gate are a8's: the only change since is bug-442,
# which is inert at the default far weight of 1) before this change was written.
A8_ORDER = {
    "near_only": (
        dict(near=_near([(1, .9), (2, .85), (3, .8)])),
        [("mem", 1, 1.0), ("mem", 2, 0.49999999999999944), ("mem", 3, 0.0)],
    ),
    "lone_weak": (
        dict(near=_near([(1, .2)])),
        [("mem", 1, 1.0)],
    ),
    "near_and_keyword": (
        dict(near=_near([(1, .9), (2, .6), (4, .55)]), keyword=_keyword([(2, 1.0), (3, 4.0), (5, .3)])),
        [("mem", 1, 0.5), ("mem", 3, 0.5), ("mem", 2, 0.1660231660231659), ("mem", 4, 0.0), ("mem", 5, 0.0)],
    ),
    "like_fallback": (
        dict(near=_near([(1, .7), (2, .65)]), keyword=_keyword([(2, None), (6, None)])),
        [("mem", 1, 0.5), ("mem", 2, 0.5), ("mem", 6, 0.5)],
    ),
    "far_channel": (
        dict(near=_near([(1, .8), (2, .7)]), far=_near([(7, .75), (8, .5)]), keyword=_keyword([(8, 2.0)])),
        [("mem", 1, 1 / 3), ("mem", 7, 1 / 3), ("mem", 8, 1 / 3), ("mem", 2, 0.0)],
    ),
    "three_channels": (
        dict(
            near=_near([(1, .8), (2, .78), (3, .5)]),
            keyword=_keyword([(3, 6.0), (4, 1.0)]),
            episodes=_episodes([(10, 3.0), (11, 1.0)]),
        ),
        [("mem", 1, 1 / 3), ("mem", 3, 1 / 3), ("ep", 10, 1 / 3), ("mem", 2, 0.31111111111111106),
         ("ep", 11, 0.0), ("mem", 4, 0.0)],
    ),
    "ties": (
        dict(near=_near([(1, .8), (2, .8)]), keyword=_keyword([(3, 2.0)])),
        [("mem", 1, 0.5), ("mem", 2, 0.5), ("mem", 3, 0.5)],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", sorted(A8_ORDER))
async def test_with_the_gate_open_the_ranking_is_a8s(monkeypatch, fixture):
    channels, expected = A8_ORDER[fixture]
    rows = await _fuse(monkeypatch, **channels)
    # The gate open: threshold 0 admits every row (the gate score is never negative),
    # and it must not reorder what it admits.
    admitted = mh._apply_quality_gate(rows, 0.0, 1000)
    got = [(*_key(r), r["_rsf_score"]) for r in admitted]
    assert [g[:2] for g in got] == [e[:2] for e in expected]
    assert [g[2] for g in got] == pytest.approx([e[2] for e in expected], abs=1e-12)


@pytest.mark.asyncio
async def test_the_gate_score_can_disagree_with_the_order_and_the_order_wins(monkeypatch):
    # Min-max: vector 1 -> 1.0, 3 -> 0.9, 4 -> 0.0; keyword 5 -> 1.0, 3 -> 0.0. Over two
    # channels the order is 1 (0.5), 5 (0.5), 3 (0.45), 4 (0.0). On the gate's scale row 3
    # (0.85 + 6/14) is the strongest and 5 (12/20) is below 1 (0.9): read as an order, the
    # gate score would give 3, 1, 5, 4.
    rows = await _fuse(monkeypatch, near=_near([(1, .9), (3, .85), (4, .4)]), keyword=_keyword([(3, 6.0), (5, 12.0)]))
    g = _gate(rows)
    assert g[3] > g[1] > g[5] > g[4]
    assert [r["id"] for r in rows] == [1, 5, 3, 4]


# --- 2. The two defects, judged on the gate score ----------------------------------

@pytest.mark.asyncio
async def test_a_rows_gate_score_does_not_depend_on_the_rows_retrieved_beside_it(monkeypatch):
    alone = _gate(await _fuse(monkeypatch, near=_near([(1, 0.8)])))
    beside = _gate(await _fuse(monkeypatch, near=_near([(1, 0.8), (2, 0.95), (3, 0.5)])))
    assert alone[1] == pytest.approx(beside[1])


@pytest.mark.asyncio
async def test_the_weakest_row_of_a_strong_set_passes_a_gate_it_clears(monkeypatch):
    rows = await _fuse(monkeypatch, near=_near([(1, 0.9), (2, 0.85), (3, 0.8)]))
    assert _order(rows)[3] == 0.0  # the order score still pins it; the gate does not read it
    assert _gate(rows)[3] == pytest.approx(0.8)
    assert _gated(rows) == {1, 2, 3}


@pytest.mark.asyncio
async def test_a_lone_weak_hit_is_gated_like_any_other(monkeypatch):
    rows = await _fuse(monkeypatch, near=_near([(1, 0.2)]))
    assert _order(rows)[1] == 1.0
    assert _gate(rows)[1] == pytest.approx(0.2)
    assert _gated(rows) == set()


@pytest.mark.asyncio
async def test_the_keyword_channel_is_monotone_and_bounded_on_the_gate_scale(monkeypatch):
    rows = await _fuse(monkeypatch, keyword=_keyword([(1, 0.5), (2, 2.0), (3, 10.0)]))
    g = _gate(rows)
    assert 0.0 < g[1] < g[2] < g[3] < 1.0
    again = _gate(await _fuse(monkeypatch, keyword=_keyword([(2, 2.0)])))
    assert again[2] == pytest.approx(g[2])


@pytest.mark.asyncio
async def test_a_like_fallback_match_still_casts_a_full_keyword_vote(monkeypatch):
    # The LIKE fallback has no bm25: an exact substring match counts in full.
    rows = await _fuse(monkeypatch, keyword=_keyword([(1, None), (2, None)]))
    assert _gate(rows) == {1: 1.0, 2: 1.0}


@pytest.mark.asyncio
async def test_the_constants_are_the_first_fixs_not_chosen_again(monkeypatch):
    # prereg-rsf-fixed-scale.md chose H = 8 and the undivided sum on its dev half; this
    # design reuses both. A keyword score of 8 is half a vote, and a row both channels
    # found keeps the plain sum of its two votes.
    g = _gate(await _fuse(monkeypatch, near=_near([(1, 0.6)]), keyword=_keyword([(1, 8.0), (2, 8.0)])))
    assert g[2] == pytest.approx(0.5)
    assert g[1] == pytest.approx(1.1)


@pytest.mark.asyncio
async def test_a_strong_vector_only_row_clears_the_gate_while_the_keyword_channel_answers(monkeypatch):
    # Undivided, a keyword channel that answers for other rows does not halve a strong
    # vector-only row below the uncalibrated cosine-scale gate.
    rows = await _fuse(monkeypatch, near=_near([(1, 0.8)]), keyword=_keyword([(2, 4.0)]))
    assert _gate(rows)[1] == pytest.approx(0.8)
    assert 1 in _gated(rows)


@pytest.mark.asyncio
async def test_the_far_channel_is_weighted_on_the_gate_scale_too(monkeypatch):
    monkeypatch.setattr(mh, "PRIOR_FAR_WEIGHT", 0.5)
    rows = await _fuse(monkeypatch, near=_near([(1, 0.8)]), far=_near([(7, 0.6)]))
    assert _gate(rows)[7] == pytest.approx(0.3)
    assert _gate(rows)[1] == pytest.approx(0.8)


# --- 3. The gate reads the gate score ----------------------------------------------

def _split_rows():
    # Two rows whose scores fall on opposite sides of 0.5.
    return [
        {"id": 1, "_rsf_score": 0.9, "_rsf_gate_score": 0.1},
        {"id": 2, "_rsf_score": 0.1, "_rsf_gate_score": 0.9},
    ]


def test_the_heuristic_gate_admits_by_the_gate_score():
    assert {r["id"] for r in mh._apply_quality_gate(_split_rows(), 0.5, 1000)} == {2}


def test_the_calibrated_gate_admits_by_the_gate_score():
    out = mh._apply_quality_gate(_split_rows(), 0.0, 1000, gate=0.5, gate_signal="rsf")
    assert {r["id"] for r in out} == {2}


def test_calibration_measures_the_gate_score():
    # Calibration builds its curve from _gate_score, so it follows the gate.
    assert mh._gate_score({"_rsf_score": 0.9, "_rsf_gate_score": 0.1}) == (0.1, "rsf")


def test_a_row_without_a_gate_score_is_gated_on_its_rsf_score():
    # A caller that sets only _rsf_score (older fixtures, other providers) is compared as before.
    out = mh._apply_quality_gate([{"id": 1, "_rsf_score": 0.6}, {"id": 2, "_rsf_score": 0.4}], 0.5, 1000)
    assert {r["id"] for r in out} == {1}
    assert mh._gate_score({"_rsf_score": 0.4}) == (0.4, "rsf")


@pytest.mark.asyncio
async def test_the_episode_penalty_scales_both_scores(monkeypatch):
    from cpersona import config
    from cpersona.database import get_db

    db = await get_db()
    agent = "agent.bug247.penalty"
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) VALUES (?, 's', 'k', datetime('now'))",
        (agent,),
    )
    await db.commit()
    monkeypatch.setattr(mh, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(mh, "EPISODE_PENALTY_ENABLED", True)
    row = {"id": 8, "content": "old memory", "timestamp": "2020-01-01T00:00:00+00:00",
           "_cosine": 0.62, "_rsf_score": 0.9, "_rsf_gate_score": 0.7}
    results, _, _, _ = await mh._apply_recall_scoring(db, agent, [row], deep=False)
    floor = config.EPISODE_DECAY_FLOOR
    assert floor < 1.0
    assert results[0]["_rsf_score"] == pytest.approx(0.9 * floor)
    assert results[0]["_rsf_gate_score"] == pytest.approx(0.7 * floor)


# --- End to end: what a caller reads -----------------------------------------------

@pytest.mark.asyncio
async def test_a_recall_reports_the_gate_score_it_passed_and_the_order_score_beside_it(monkeypatch):
    _patch_retrievers(monkeypatch, near=_near([(1, 0.9), (2, 0.85), (3, 0.8)]))
    monkeypatch.setattr(mh, "RECALL_MODE", "rsf")
    monkeypatch.setattr(mh, "CONFIDENCE_ENABLED", False)
    resp = await mh.do_recall("agent.bug247.e2e", "a query", 10)
    reasons = {m["ref"]: m["match_reason"] for m in resp["messages"]}
    # The weakest of the three (order 0.0) is no longer cut by the gate.
    assert set(reasons) == {"mem:1", "mem:2", "mem:3"}
    assert reasons["mem:3"]["signal"] == "rsf"
    assert reasons["mem:3"]["score"] == pytest.approx(0.8)
    assert reasons["mem:3"]["rsf"] == pytest.approx(0.0)
    # The response lists the best row last (do_recall reverses the ranked list).
    assert [m["ref"] for m in resp["messages"]] == ["mem:3", "mem:2", "mem:1"]
