"""Depth places: places for rows a deeper ranking of the same recall admits, asked for by mode "pro".

A recall ranks at its candidate depth, and an admitted row ranked past it never reaches
the window. Under mode "pro" reconstruct asks its candidate recall for eight depth places:
the recall is ranked again at the propagation seat's depth -- the arms, scoring, gate,
autocut and prior as the window was -- and the admitted rows the answer does not hold
are eligible, the rows holding more of the question's parts first, ties in the deeper
order. They come after every other held place, are one more fetch on the ledger, and
hold nothing in the cascade or for an empty query. Lite, a call that names no mode and
the recall tool ask for none.

Fixture: memories inserted directly (as in test_keyword_seats), so a row's vector can be
chosen apart from its text. A recall limit of 1 or 2 is also its candidate depth, so
admitted rows past it are what the depth places can hold.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import test_keyword_seats as kw
import test_pro_block_seats as pb
from cpersona import config, server
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

AGENT = kw.AGENT
QUERY = "apples orchard"
FAR = "completely different unrelated content xxxx"  # the vector of kw._seed's keyword-only row


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


async def _seed_near(n: int) -> list[str]:
    """n rows near the question by vector and admitted on their cosine, whose text holds none
    of its words, so the keyword arms find none of them (no keyword seat or place takes one);
    distinct times, so none cluster."""
    texts = [f"pears note {i} entry" for i in range(n)]
    for i, text in enumerate(texts):
        await kw._insert(text, kw._pack_of(f"apples orchard note {i} entry"), f"2026-02-{1 + i // 24:02d}T{i % 24:02d}:00:00Z")
    return texts


def _depth_rows(out):
    return [m for m in out["messages"] if m.get("match_reason", {}).get("signal") == "depth"]


@pytest.mark.asyncio
async def test_depth_places_hold_admitted_rows_past_the_candidate_depth_last():
    texts = await _seed_near(6)
    plain = await M.do_recall(AGENT, QUERY, limit=2)
    assert len(plain["messages"]) == 2, "fixture regression: the window does not stop at the limit"
    out = await M.do_recall(AGENT, QUERY, limit=2, depth_seats=3, trace=True)
    ranked = out["messages"][::-1]  # do_recall emits its ranked list most relevant last
    window = [m for m in ranked if m.get("match_reason", {}).get("admission") != "reservation"]
    held = _depth_rows(out)[::-1]
    assert [m["content"] for m in window] == [m["content"] for m in plain["messages"][::-1]]
    assert ranked[-3:] == held and [m["match_reason"]["seat"] for m in held] == [1, 2, 3]
    contents = [m["content"] for m in ranked]
    assert len(set(contents)) == len(contents) and set(contents) <= set(texts), contents
    assert out["trace"]["depth_places"]["candidates"] == 4
    assert out["trace"]["budget"]["limits"]["depth_fetch"] == 1 and out["trace"]["budget"]["used"]["depth_fetch"] == 1


@pytest.mark.asyncio
async def test_depth_places_hold_at_most_their_number():
    await _seed_near(6)
    two = await M.do_recall(AGENT, QUERY, limit=2, depth_seats=2)
    assert len(_depth_rows(two)) == 2
    every = await M.do_recall(AGENT, QUERY, limit=2, depth_seats=9)
    assert len(_depth_rows(every)) == 4, "the four admitted rows past the window are all eligible"


@pytest.mark.asyncio
async def test_the_row_holding_more_of_the_question_sits_first():
    # Six short rows each hold one of the question's two parts, three times over, and carry
    # the question's own vector, so they lead both arms; MORE holds both parts once, buried
    # in filler, with a vector further off, and is the oldest (the prior favours recent
    # rows), so it ranks below them on both arms and in the deeper order.
    for i in range(3):
        await kw._insert(f"apples apples apples {i}", kw._pack_of(QUERY), f"2026-02-01T{i:02d}:00:00Z")
        await kw._insert(f"orchard orchard orchard {i}", kw._pack_of(QUERY), f"2026-02-01T{10 + i:02d}:00:00Z")
    more = "apples " + " ".join(f"filler{i}" for i in range(60)) + " orchard"
    await kw._insert(more, kw._pack_of("apples orchard harvest"), "2026-01-15T00:00:00Z")
    out = await M.do_recall(AGENT, QUERY, limit=1, depth_seats=1)
    (seated,) = _depth_rows(out)
    assert seated["content"] == more
    assert seated["match_reason"]["depth_rank"] > 1, "fixture is vacuous: MORE already led the deeper order"


@pytest.mark.asyncio
async def test_a_row_the_deeper_gate_refuses_takes_no_place():
    await kw._seed()  # the vector hit and a keyword-only row the rrf gate refuses
    await _seed_near(3)
    kw._gate("rrf", 0.05)
    wide = await M.do_recall(AGENT, QUERY, limit=5, trace=True)
    refused = [d for d in wide["trace"]["gate"]["decisions"] if d["signal"] == "rrf" and not d["passed"]]
    assert refused, "fixture is vacuous: the gate refused nothing on rrf"
    out = await M.do_recall(AGENT, QUERY, limit=1, depth_seats=8)
    held = [m["content"] for m in _depth_rows(out)]
    assert held and kw.KEYWORD_ONLY not in held, held


@pytest.mark.asyncio
async def test_rsf_holds_depth_places_too(monkeypatch):
    await _seed_near(5)
    monkeypatch.setattr(M, "RECALL_MODE", "rsf")
    out = await M.do_recall(AGENT, QUERY, limit=2, depth_seats=2)
    assert len(_depth_rows(out)) == 2


@pytest.mark.asyncio
async def test_the_cascade_and_an_empty_query_hold_none(monkeypatch):
    await _seed_near(5)
    blank = await M.do_recall(AGENT, "", limit=2, depth_seats=3, trace=True)
    assert _depth_rows(blank) == [] and "depth_fetch" not in blank["trace"]["budget"]["limits"]
    monkeypatch.setattr(M, "RECALL_MODE", "cascade")
    cascade = await M.do_recall(AGENT, QUERY, limit=2, depth_seats=3, trace=True)
    assert _depth_rows(cascade) == [] and "depth_fetch" not in cascade["trace"]["budget"]["limits"]


@pytest.mark.asyncio
async def test_a_recall_without_depth_places_declares_no_depth_fetch():
    await _seed_near(4)
    out = await M.do_recall(AGENT, QUERY, limit=2, trace=True)
    assert "depth_fetch" not in out["trace"]["budget"]["limits"]


@pytest.mark.asyncio
async def test_a_row_in_a_depth_place_is_not_credited(monkeypatch):
    # The recall count is bumped only where confidence reads it (do_recall).
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    texts = await _seed_near(5)
    out = await M.do_recall(AGENT, QUERY, limit=2, depth_seats=3)
    held = {m["content"] for m in _depth_rows(out)}
    assert len(held) == 3
    db = await get_db()
    counts = dict(await db.execute_fetchall("SELECT content, recall_count FROM memories"))
    shown = {m["content"] for m in out["messages"]} - held
    assert all(counts[t] >= 1 for t in shown) and all(counts[t] == 0 for t in held), counts
    assert set(texts) >= held


@pytest.mark.asyncio
async def test_the_recall_count_bound_counts_the_depth_places():
    """Past the slack the cue's and the reservation's places leave in recall's bound: a
    bound that did not count the depth places would refuse this answer."""
    await _seed_near(10)
    out = await M.do_recall(AGENT, QUERY, limit=1, depth_seats=8)
    assert len(_depth_rows(out)) == 8


@pytest.mark.asyncio
async def test_the_recall_tool_holds_no_depth_place():
    await _seed_near(5)
    out = await server.registry._handlers["recall"]({"agent_id": AGENT, "query": QUERY, "limit": 2})
    assert _depth_rows(out) == []
    for name, schema in {t.name: t.inputSchema for t in server.registry._tools}.items():
        assert "depth_seats" not in (schema or {}).get("properties", {}), name


def _held_items(out):
    return [it["content"] for it in out["items"] if it.get("admission") == "reservation"]


@pytest.mark.asyncio
async def test_pro_asks_for_eight_and_lite_and_no_mode_for_none(monkeypatch):
    texts = await _seed_near(30)
    pro, seen = await pb._reconstruct_kwargs(monkeypatch, mode="pro")
    assert seen.get("depth_seats") == 8
    held = _held_items(pro)
    assert len(held) == 8 and set(held) <= set(texts), pro["items"]
    lite, seen = await pb._reconstruct_kwargs(monkeypatch, mode="lite")
    assert "depth_seats" not in seen and _held_items(lite) == [], lite["items"]
    plain, seen = await pb._reconstruct_kwargs(monkeypatch)
    assert "depth_seats" not in seen and _held_items(plain) == [], plain["items"]


@pytest.mark.asyncio
async def test_the_reconstruct_bound_counts_the_depth_places(monkeypatch):
    """Eight held items under count 1 is past reconstruct's other places once the extra
    block and keyword places are taken away (two block, one keyword, three cue): a bound
    that did not count the depth places would refuse it."""
    await _seed_near(30)
    monkeypatch.setattr(R, "MODE_EXTRA_BLOCK_SEATS", {})
    monkeypatch.setattr(R, "MODE_EXTRA_KEYWORD_SEATS", {})
    out = await R.do_reconstruct(AGENT, QUERY, count=1, mode="pro")
    assert len(_held_items(out)) == 8, out["items"]


async def _seed_keywords():
    """Rows the keyword arms find: the keyword-seat fixture, rows on the topic holding the
    question's words, and the vector-only rows of _seed_near."""
    await kw._seed()
    for i in range(6):
        text = f"apples orchard ledger {i} kk"
        await kw._insert(text, kw._pack_of(text), f"2026-03-01T{i:02d}:00:00Z")
    # A strong keyword-only row: both words twice, a vector no arm returns, so it can sit
    # in the window on its keyword score alone and is read by both rankings.
    await kw._insert("apples orchard apples orchard zq", kw._pack_of(FAR), "2026-03-02T00:00:00Z")
    await kw._insert("apples orchard apples zr", kw._pack_of(FAR), "2026-03-02T01:00:00Z")
    await _seed_near(4)
    kw._gate("rrf", 0.05)


@pytest.mark.asyncio
async def test_a_recall_with_depth_places_reads_the_keyword_arms_once(monkeypatch):
    await _seed_keywords()
    reads = []
    original = M._lexical_arms_read

    async def counted(*args, **kwargs):
        reads.append(args[3])  # the depth it read at
        return await original(*args, **kwargs)

    monkeypatch.setattr(M, "_lexical_arms_read", counted)
    await M.do_recall(AGENT, QUERY, limit=2)
    assert reads == [2], reads
    reads.clear()
    out = await M.do_recall(AGENT, QUERY, limit=2, depth_seats=3)
    assert _depth_rows(out), "fixture is vacuous: no depth place was held"
    assert reads == [100], reads


@pytest.mark.parametrize("mode,confidence", [("rrf", False), ("rsf", False), ("rsf", True)])
@pytest.mark.asyncio
async def test_reading_the_keyword_arms_once_changes_no_answer(monkeypatch, mode, confidence):
    # rsf with confidence is the production setting: there the deeper ranking scores its
    # own rows, so rows shared with the window would carry the deeper ranking's scores.
    await _seed_keywords()
    monkeypatch.setattr(M, "RECALL_MODE", mode)
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", confidence)
    if mode == "rsf":
        kw._gate("rsf", 0.3)
    once = await M.do_recall(AGENT, QUERY, limit=5, depth_seats=3, keyword_seats=1, extra_keyword_seats=2)
    # A recall credits what it returned, and confidence reads the count: start the second
    # call from the same counts.
    db = await get_db()
    await db.execute("UPDATE memories SET recall_count = 0, last_recalled_at = NULL")
    await db.commit()
    # Each ranking reads the arms itself, as the separate rankings did.
    monkeypatch.setattr(M, "_lexical_arms", M._lexical_arms_read)
    twice = await M.do_recall(AGENT, QUERY, limit=5, depth_seats=3, keyword_seats=1, extra_keyword_seats=2)
    assert _depth_rows(once), "fixture is vacuous: no depth place was held"
    assert once["messages"] == twice["messages"]
