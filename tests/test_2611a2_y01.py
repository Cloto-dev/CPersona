"""Recall's held places do not seat a row twice, and keyword seats never follow a gate rescue.

Three guards in do_recall decide which rows a held place may take after the answer
is cut to its count:

- the keyword seats skip a gate rescue: a rescued answer is made of below-gate rows,
  and the seats would add more rows on top of an answer that already says it is
  below the gate;
- a keyword seat goes only to a row the answer does not already hold;
- the extra block places re-check presence, because the keyword seats run after the
  reservation computed its spare rows and can seat one of them first.

Each test reads refs, not a content-keyed dict, so a duplicated row is visible.

Fixture: the keyword-seat fixture (test_keyword_seats) -- memories inserted directly
so a keyword-only row can carry a blob disjoint from its own content -- with the
block arm closed unless a test stubs it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import test_keyword_seats as kw
from cpersona import builtin_providers, config
from cpersona import memory_handlers as M
from cpersona.database import get_db

AGENT = kw.AGENT


@pytest_asyncio.fixture(autouse=True)
async def _fresh(monkeypatch, blocks_off, fake_embedding_client):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    kw._reset_gate()
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    yield
    kw._reset_gate()


def _refs(out) -> list[str]:
    return [m.get("ref") for m in out["messages"]]


def _keyword_seated(out) -> list[str]:
    return [
        m.get("ref") for m in out["messages"]
        if m.get("match_reason", {}).get("signal") == "keyword"
        or m.get("match_reason", {}).get("seat") is not None
    ]


@pytest.mark.asyncio
async def test_no_keyword_seat_follows_a_gate_rescue(monkeypatch):
    """A gate rescue returns below-gate rows and says so; no keyword seat is added to it.

    Every row here is keyword-only with a blob unrelated to the query, so under
    confidence the backfill gives each a near-zero cosine, the heuristic gate blocks
    all of them, and the rescue restores them with gate_fallback=true. The rescue
    replaces the result before ``admitted`` is taken, so the rescued rows the count
    then cuts are in the gate's passed set but not in the answer: exactly the rows a
    keyword seat would otherwise take.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(M, "CONFIDENCE_ORDERING", "legacy")
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    # The fusion depth follows the count; a floor lets more rows than the count reach
    # the gate, so the rescue holds rows beyond the limit.
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 10)
    for i in range(3):
        await kw._insert(
            f"apples keyword only row {i} zzz",
            kw._pack_of(f"completely different unrelated content {i} xxxx"),
            f"2026-01-01T00:00:0{i}Z",
        )

    out = await M.do_recall(AGENT, "apples", limit=1, keyword_seats=2, trace=True)

    assert out.get("gate_fallback") is True, f"fixture is vacuous: the rescue did not fire: {out!r}"
    cut = out["trace"]["order"]["cut_by_count"]
    assert len(cut) >= 2, f"fixture is vacuous: no rescued row was cut by the count: {cut!r}"
    assert _keyword_seated(out) == [], (
        f"a keyword seat was added to a gate_fallback answer: {out['messages']!r}"
    )
    assert len(out["messages"]) == 1, _refs(out)


@pytest.mark.asyncio
async def test_an_admitted_keyword_only_row_in_the_window_is_not_seated_again(monkeypatch):
    """A keyword-only row the gate admitted and the count kept is in the answer once.

    A zero rsf gate admits both rows and the window (limit 5) holds both, so the
    keyword-only row is lexical-only, in the gate's passed set and already present.
    The keyword seats must skip it rather than append a second copy labelled as a
    reservation.
    """
    strong, weak = await kw._seed()
    kw._gate("rsf", 0.0)
    monkeypatch.setattr(M, "RECALL_MODE", "rsf")

    out = await M.do_recall(AGENT, "apples orchard", limit=5, keyword_seats=2)
    refs = _refs(out)

    assert f"mem:{weak}" in refs, f"fixture is vacuous: the keyword-only row is not in the answer: {refs}"
    assert len(refs) == len(set(refs)), f"the same row was returned twice: {refs}"
    assert _keyword_seated(out) == [], (
        f"a row already in the answer took a keyword seat: {out['messages']!r}"
    )
    assert refs.count(f"mem:{strong}") == 1, refs


@pytest.mark.asyncio
async def test_a_spare_block_row_seated_by_keyword_is_not_held_again(monkeypatch):
    """The extra block places skip a spare block row that a keyword seat already took.

    The stubbed block arm reaches two records and then the keyword-only row, so the
    reservation seats the first two and leaves the keyword-only row in its spare
    list. The keyword seat runs next and takes it (the rrf gate refused it on a
    scale it cannot pass). The extra block places then read the spare list, and
    must not append that row again.
    """
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    strong, weak = await kw._seed()
    kw._gate("rrf", 0.05)
    ids = [
        await kw._insert(f"tail record {n} beyond the window", kw._pack_of(f"unrelated vector {i} qqq"),
                         f"2026-01-02T00:00:0{i}Z")
        for i, n in enumerate(("one", "two", "three"))
    ]
    order = [(ids[0], "tail record one beyond the window"), (ids[1], "tail record two beyond the window"),
             (weak, kw.KEYWORD_ONLY), (ids[2], "tail record three beyond the window")]

    async def reserved_rows(self, db, query_vec, *, agent_id, project_id, channel, source_id, exclude_set, limit):
        return [
            {"id": i, "msg_id": None, "content": text, "source": "{}", "timestamp": "2026-01-02T00:00:00Z",
             "_rid": ("mem", i), "_block_distance": n, "_block_index": 0, "_block_order": "hamming"}
            for n, (i, text) in enumerate(order)
        ]

    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(builtin_providers.BlockCandidates, "reserved_rows", reserved_rows)

    out = await M.do_recall(AGENT, "apples orchard", limit=5, keyword_seats=1, extra_block_seats=2, trace=True)
    refs = _refs(out)
    held = [(x["kind"], x["ref"]) for x in out["trace"]["reservation"]]

    assert ("keyword", f"mem:{weak}") in held, f"fixture is vacuous: the keyword seat did not take the spare row: {held}"
    assert len(refs) == len(set(refs)), f"the same row was returned twice: {refs}"
    assert held == [
        ("block", f"mem:{ids[0]}"), ("block", f"mem:{ids[1]}"),
        ("keyword", f"mem:{weak}"), ("block", f"mem:{ids[2]}"),
    ], held
    assert sorted(refs) == sorted([f"mem:{strong}", f"mem:{weak}", *(f"mem:{i}" for i in ids)]), refs
