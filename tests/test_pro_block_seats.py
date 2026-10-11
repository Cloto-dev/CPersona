"""Extra block places: two more places for records the block arm reached, asked for by mode "pro".

The block arm's reservation holds two places. Under mode "pro" reconstruct asks its
candidate recall for two more, and the recall puts them after every other held place
(the keyword, far and cue seats), so a budget that cuts held rows from the end cuts
these first. Mode "lite" and a call that names no mode ask for none, and the recall
tool never does.

Fixture: the keyword-seat fixture (test_keyword_seats) gives a row only the keyword arm
found, which the rrf gate refuses, and the block arm is replaced by a stub that reaches
four other records in a fixed order, so both kinds of held place fill in one recall.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import test_keyword_seats as kw
from cpersona import builtin_providers, config
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

AGENT = kw.AGENT
QUERY = "apples orchard"
BLOCK_TEXTS = [f"tail record {n} beyond the window" for n in ("one", "two", "three", "four")]


@pytest_asyncio.fixture(autouse=True)
async def _fresh(monkeypatch, blocks_off, fake_embedding_client):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    kw._reset_gate()
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    yield
    kw._reset_gate()


async def _seed_blocks(monkeypatch, texts):
    """The keyword-only row, and records only the stubbed block arm reaches, in this order."""
    await kw._seed()
    kw._gate("rrf", 0.05)
    ids = []
    for i, text in enumerate(texts):
        ids.append(await kw._insert(text, kw._pack_of(f"unrelated vector {i} qqq"), f"2026-01-02T00:00:0{i}Z"))
    asked = {}

    async def reserved_rows(self, db, query_vec, *, agent_id, project_id, channel, source_id, exclude_set, limit):
        asked["limit"] = limit
        return [
            {"id": i, "msg_id": None, "content": text, "source": "{}", "timestamp": "2026-01-02T00:00:00Z",
             "_rid": ("mem", i), "_block_distance": n, "_block_index": 0, "_block_order": "hamming"}
            for n, (i, text) in enumerate(zip(ids, texts))
        ]

    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(builtin_providers.BlockCandidates, "reserved_rows", reserved_rows)
    return [f"mem:{i}" for i in ids], asked


@pytest_asyncio.fixture
async def seeded(monkeypatch):
    return await _seed_blocks(monkeypatch, BLOCK_TEXTS)


def _held(out):
    return [(x["kind"], x["ref"]) for x in out["trace"]["reservation"]]


@pytest.mark.asyncio
async def test_the_extra_places_come_after_the_keyword_seat(seeded):
    blocks_, asked = seeded
    base = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1, trace=True)
    kinds = [k for k, _ in _held(base)]
    assert kinds == ["block", "block", "keyword"], "the fixture does not fill both kinds of place"
    keyword_ref = _held(base)[2][1]

    out = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1, extra_block_seats=2, trace=True)
    assert _held(out) == [
        ("block", blocks_[0]), ("block", blocks_[1]), ("keyword", keyword_ref),
        ("block", blocks_[2]), ("block", blocks_[3]),
    ]


@pytest.mark.asyncio
async def test_the_block_arm_reads_enough_hits_for_the_extra_places(seeded):
    _, asked = seeded
    await M.do_recall(AGENT, QUERY, limit=5)
    assert asked["limit"] == 5
    await M.do_recall(AGENT, QUERY, limit=5, extra_block_seats=2)
    assert asked["limit"] == 7


@pytest.mark.asyncio
async def test_extra_places_hold_at_most_the_rows_the_reservation_left(seeded):
    blocks_, _ = seeded
    out = await M.do_recall(AGENT, QUERY, limit=5, extra_block_seats=9, trace=True)
    assert [ref for kind, ref in _held(out) if kind == "block"] == blocks_


async def _reconstruct_kwargs(monkeypatch, **call):
    seen = {}
    original = builtin_providers.ReconstructCandidates.candidates

    async def candidates(self, agent_id, query, top_k, **kwargs):
        seen.update(kwargs)
        return await original(self, agent_id, query, top_k, **kwargs)

    monkeypatch.setattr(builtin_providers.ReconstructCandidates, "candidates", candidates)
    out = await R.do_reconstruct(AGENT, QUERY, count=5, **call)
    return out, seen


def _held_items(out):
    return [it["head_ref"] for it in out["items"] if it.get("admission") == "reservation"]


@pytest.mark.asyncio
async def test_pro_holds_the_extra_places_last(monkeypatch, seeded):
    blocks_, _ = seeded
    out, seen = await _reconstruct_kwargs(monkeypatch, mode="pro")
    assert seen.get("extra_block_seats") == 2
    held = _held_items(out)
    assert held[:2] == blocks_[:2] and held[3:] == blocks_[2:] and len(held) == 5, out["items"]
    assert out["reserved_count"] == 5


@pytest.mark.asyncio
async def test_lite_and_no_mode_ask_for_no_extra_place(monkeypatch, seeded):
    blocks_, _ = seeded
    lite, seen = await _reconstruct_kwargs(monkeypatch, mode="lite")
    assert "extra_block_seats" not in seen
    assert [ref for ref in _held_items(lite) if ref in blocks_] == blocks_[:2]
    plain, seen = await _reconstruct_kwargs(monkeypatch)
    assert "extra_block_seats" not in seen
    assert _held_items(plain) == blocks_[:2]


@pytest.mark.asyncio
async def test_the_recall_tool_asks_for_no_extra_place(seeded):
    from cpersona import server

    blocks_, _ = seeded
    out = await server.registry._handlers["recall"]({"agent_id": AGENT, "query": QUERY, "limit": 5})
    refs = [m["ref"] for m in out["messages"]]
    assert [ref for ref in blocks_ if ref in refs] == blocks_[:2], refs
    for name, schema in {t.name: t.inputSchema for t in server.registry._tools}.items():
        assert "extra_block_seats" not in (schema or {}).get("properties", {}), name


MANY = [f"tail record number {n} beyond the window" for n in range(8)]


@pytest.mark.asyncio
async def test_the_recall_count_bound_counts_the_extra_places(monkeypatch):
    """Past the slack the cue's places leave in recall's bound: a bound that did not
    count the extra places would refuse this answer."""
    blocks_, _ = await _seed_blocks(monkeypatch, MANY)
    out = await M.do_recall(AGENT, QUERY, limit=1, keyword_seats=1, extra_block_seats=6, trace=True)
    assert [ref for kind, ref in _held(out) if kind == "block"] == blocks_


@pytest.mark.asyncio
async def test_the_reconstruct_bound_counts_the_extra_places(monkeypatch):
    """Six extra places put nine held items past reconstruct's other places (two
    block, one keyword, three cue): a bound that did not count them would refuse it."""
    blocks_, _ = await _seed_blocks(monkeypatch, MANY)
    monkeypatch.setattr(R, "MODE_EXTRA_BLOCK_SEATS", {R.MODE_PRO: 6})
    out = await R.do_reconstruct(AGENT, QUERY, count=1, mode="pro")
    assert len(_held_items(out)) == 9 and [r for r in _held_items(out) if r in blocks_] == blocks_, out["items"]
