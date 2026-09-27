"""bug-247: an rsf fused score must mean the same thing whatever else a query retrieved.

The quality gate compares the fused score with an absolute threshold. When each channel
was min-max normalised against the rows retrieved beside it, the weakest row of a strong
set was pinned to 0.0 and dropped however similar it was, and a lone weak hit was
stretched to 1.0 and passed however weak. These tests drive `_recall_rsf` with the
retrievers replaced, so every channel's raw scores are chosen exactly.
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


async def _fuse(monkeypatch, near=(), keyword=()):
    async def vector(db, agent_id, query, depth, min_similarity=None, **kw):
        return [dict(r) for r in near]

    async def memories(db, agent_id, query, depth, **kw):
        return [dict(r) for r in keyword]

    async def episodes(db, agent_id, query, depth, **kw):
        return []

    monkeypatch.setattr(mh, "_search_vector", vector)
    monkeypatch.setattr(mh, "_search_memories_keyword", memories)
    monkeypatch.setattr(mh, "_search_episodes_fts", episodes)
    monkeypatch.setattr(mh, "FTS_ENABLED", True)
    monkeypatch.setattr(mh.vector, "_embedding_client", object())
    from cpersona.database import get_db

    return await mh._recall_rsf(await get_db(), "agent.bug247", "a query", 10, False)


def _scores(rows):
    return {r["id"]: r["_rsf_score"] for r in rows}


def _gated(rows):
    return {r["id"] for r in mh._apply_quality_gate(rows, GATE, 1000)}


@pytest.mark.asyncio
async def test_a_rows_score_does_not_depend_on_the_rows_retrieved_beside_it(monkeypatch):
    alone = _scores(await _fuse(monkeypatch, near=_near([(1, 0.8)])))
    beside = _scores(await _fuse(monkeypatch, near=_near([(1, 0.8), (2, 0.95), (3, 0.5)])))
    assert alone[1] == pytest.approx(beside[1])


@pytest.mark.asyncio
async def test_the_weakest_row_of_a_strong_set_passes_a_gate_it_clears(monkeypatch):
    rows = await _fuse(monkeypatch, near=_near([(1, 0.9), (2, 0.85), (3, 0.8)]))
    assert _scores(rows)[3] > 0.0
    assert _gated(rows) == {1, 2, 3}


@pytest.mark.asyncio
async def test_a_lone_weak_hit_is_gated_like_any_other(monkeypatch):
    rows = await _fuse(monkeypatch, near=_near([(1, 0.2)]))
    assert _scores(rows)[1] < 1.0
    assert _gated(rows) == set()


@pytest.mark.asyncio
async def test_the_keyword_channel_is_monotone_and_bounded(monkeypatch):
    rows = await _fuse(monkeypatch, keyword=_keyword([(1, 0.5), (2, 2.0), (3, 10.0)]))
    s = _scores(rows)
    assert 0.0 < s[1] < s[2] < s[3] < 1.0
    again = _scores(await _fuse(monkeypatch, keyword=_keyword([(2, 2.0)])))
    assert again[2] == pytest.approx(s[2])


@pytest.mark.asyncio
async def test_a_like_fallback_match_still_casts_a_full_keyword_vote(monkeypatch):
    # The LIKE fallback has no bm25: an exact substring match counts in full.
    rows = await _fuse(monkeypatch, keyword=_keyword([(1, None), (2, None)]))
    assert _scores(rows) == {1: 1.0, 2: 1.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("divisor", ["active", "present", "none"])
async def test_rows_are_ordered_by_the_score_they_carry(monkeypatch, divisor):
    # Under "present" a row in one channel is divided by 1 and a row in two by 2, so an
    # order taken from the undivided sum would disagree with the scores it reports.
    monkeypatch.setattr(mh, "RSF_DIVISOR", divisor)
    rows = await _fuse(monkeypatch, near=_near([(1, 0.9), (2, 0.6)]), keyword=_keyword([(2, 1.0)]))
    got = [r["_rsf_score"] for r in rows]
    assert got == sorted(got, reverse=True)


@pytest.mark.asyncio
async def test_each_divisor_divides_by_what_it_names(monkeypatch):
    near, keyword = _near([(1, 0.9), (2, 0.6)]), _keyword([(2, 2.0)])
    expect_2 = 0.6 + 2.0 / (2.0 + mh.RSF_LEXICAL_HALF)
    for divisor, d1, d2 in (("none", 1, 1), ("present", 1, 2), ("active", 2, 2)):
        monkeypatch.setattr(mh, "RSF_DIVISOR", divisor)
        s = _scores(await _fuse(monkeypatch, near=near, keyword=keyword))
        assert s[1] == pytest.approx(0.9 / d1) and s[2] == pytest.approx(expect_2 / d2), divisor


@pytest.mark.asyncio
@pytest.mark.parametrize("divisor", ["present", "none"])
async def test_a_strong_single_channel_row_still_clears_a_cosine_scale_gate(monkeypatch, divisor):
    # The reason "active" is not a candidate: a keyword channel that answers for other
    # rows must not halve a strong vector-only row below the uncalibrated cosine gate.
    monkeypatch.setattr(mh, "RSF_DIVISOR", divisor)
    rows = await _fuse(monkeypatch, near=_near([(1, 0.8)]), keyword=_keyword([(2, 4.0)]))
    assert 1 in _gated(rows)
    monkeypatch.setattr(mh, "RSF_DIVISOR", "active")
    assert 1 not in _gated(await _fuse(monkeypatch, near=_near([(1, 0.8)]), keyword=_keyword([(2, 4.0)])))


@pytest.mark.asyncio
async def test_the_shipped_constants_are_the_ones_the_dev_measurement_chose(monkeypatch):
    # benchmarks/measurements/prereg-rsf-fixed-scale.md chose divisor "none" with H = 8 on
    # the dev half and judged that choice on the test half. A keyword score of 8 is half a
    # vote, and a row both channels found keeps the plain sum of its two votes.
    s = _scores(await _fuse(monkeypatch, near=_near([(1, 0.6)]), keyword=_keyword([(1, 8.0), (2, 8.0)])))
    assert s[2] == pytest.approx(0.5)
    assert s[1] == pytest.approx(1.1)
