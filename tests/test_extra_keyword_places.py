"""Extra keyword places: more places for rows only the keyword arms found that the gate refused on rrf.

The keyword seat holds one place for such a row, or for one the gate admitted and the
count cut. Under a mode reconstruct also asks its candidate recall for extra keyword
places -- eight under "pro", none under "lite" -- which hold only the rows the gate
refused on their rrf score, the scale no keyword-only row can pass. They come after
every other held place, the extra block places included, in the keyword seat's order
(more of the question's parts first), and their seat numbers follow the keyword
seat's. Where no row is keyed on rrf they hold nothing, and a row the vector arm
returned stays out even when it carries no score. The recall tool never asks.

Fixture: the keyword-seat fixture (test_keyword_seats) -- memories inserted directly so
a keyword-only row can carry a blob disjoint from its content -- with more keyword-only
rows, each holding one word of the question.
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


async def _seed_refused(n: int) -> dict[str, int]:
    """The keyword-seat fixture plus n more keyword-only rows, every one refused on rrf.

    Each carries the keyword-only row's own vector, which the vector arm does not return
    for this question (a vector of its own could land near it by chance)."""
    await kw._seed()
    texts = [f"orchard note {i} kkk" for i in range(n)]
    ids = {}
    for i, text in enumerate(texts):
        ids[text] = await kw._insert(text, kw._pack_of(FAR), f"2026-01-03T00:00:{i:02d}Z")
    kw._gate("rrf", 0.05)
    return ids


def _keyword_rows(out):
    return [m for m in out["messages"] if m["match_reason"].get("signal") == "keyword"]


@pytest.mark.asyncio
async def test_extra_places_follow_the_keyword_seat_with_refused_rows():
    await _seed_refused(3)
    base = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1)
    assert len(_keyword_rows(base)) == 1, "fixture regression: the keyword seat holds no row"
    out = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1, extra_keyword_seats=2)
    ranked = out["messages"][::-1]  # do_recall emits its ranked list most relevant last
    seated = [m for m in ranked if m["match_reason"].get("signal") == "keyword"]
    assert [m["match_reason"]["seat"] for m in seated] == [1, 2, 3], [m["content"] for m in seated]
    assert seated[0]["content"] == _keyword_rows(base)[0]["content"]
    for m in seated:
        assert m["match_reason"]["admission"] == "reservation" and 0 < m["match_reason"]["rrf"] < 0.05
    assert ranked[-2:] == seated[1:], "the extra places are not the last rows"
    assert kw.STRONG in [m["content"] for m in out["messages"]]


@pytest.mark.asyncio
async def test_extra_places_hold_at_most_their_number(monkeypatch):
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 20)  # every keyword-only row reaches the fusion
    ids = await _seed_refused(4)
    out = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1, extra_keyword_seats=2)
    assert len(_keyword_rows(out)) == 3
    every = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1, extra_keyword_seats=9)
    assert len(_keyword_rows(every)) == len(ids) + 1, "the refused rows are all eligible"


@pytest.mark.asyncio
async def test_a_row_the_gate_admitted_takes_no_extra_place(monkeypatch):
    # Under rrf with a zero gate the keyword-only row passes on its rrf score and the
    # vector hit on its cosine; a depth above the count lets the keyword arm hand over
    # both, and the count of 1 keeps the vector hit, which holds both words.
    await kw._seed()
    kw._gate("rrf", 0.0)
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 5)
    seat = await M.do_recall(AGENT, QUERY, limit=1, trace=True, keyword_seats=1)
    assert len(seat["trace"]["order"]["cut_by_count"]) == 1, "fixture is vacuous: the count cut nothing"
    assert [m["content"] for m in _keyword_rows(seat)] == [kw.KEYWORD_ONLY], "fixture regression"
    out = await M.do_recall(AGENT, QUERY, limit=1, extra_keyword_seats=2)
    assert _keyword_rows(out) == [], "a row the gate admitted came back through an extra place"


@pytest.mark.asyncio
async def test_a_row_refused_on_the_rsf_scale_takes_no_extra_place(monkeypatch):
    await _seed_refused(1)
    on_rrf = await M.do_recall(AGENT, QUERY, limit=5, extra_keyword_seats=2)
    assert len(_keyword_rows(on_rrf)) == 2, "fixture regression: rrf seats nothing"
    kw._gate("rsf", 0.5)
    monkeypatch.setattr(M, "RECALL_MODE", "rsf")
    out = await M.do_recall(AGENT, QUERY, limit=5, extra_keyword_seats=2)
    contents = [m["content"] for m in out["messages"]]
    assert kw.STRONG in contents, "fixture regression: the vector hit did not survive the rsf gate"
    assert _keyword_rows(out) == [] and kw.KEYWORD_ONLY not in contents, contents


@pytest.mark.asyncio
async def test_a_vector_row_without_a_score_takes_no_place(monkeypatch):
    # A vector backend that returns a hit with no score leaves the row without a cosine,
    # so the gate keys it on rrf like a keyword-only row; it is still the vector arm's.
    await kw._seed()
    unscored = "pears basket shelf"
    await kw._insert(unscored, kw._pack_of(QUERY), "2026-01-04T00:00:00Z")
    kw._gate("rrf", 0.05)
    original = M._search_vector

    async def without_scores(*args, **kwargs):
        rows = await original(*args, **kwargs)
        for row in rows:
            if row.get("content") == unscored:
                row["_cosine"] = None
        return rows

    monkeypatch.setattr(M, "_search_vector", without_scores)
    out = await M.do_recall(AGENT, QUERY, limit=5, extra_keyword_seats=3, trace=True)
    db = await get_db()
    (row_id,) = (await db.execute_fetchall("SELECT id FROM memories WHERE content = ?", (unscored,)))[0]
    decision = next(d for d in out["trace"]["gate"]["decisions"] if d["ref"] == f"mem:{row_id}")
    assert decision["signal"] == "rrf" and not decision["passed"], f"fixture is vacuous: {decision}"
    seated = [m["content"] for m in _keyword_rows(out)]
    assert unscored not in seated and seated == [kw.KEYWORD_ONLY], seated


@pytest.mark.asyncio
async def test_the_extra_places_go_to_the_rows_holding_more_of_the_question():
    # FEW holds one of the question's parts and ranks first on the keyword arm; MORE holds
    # both and ranks after it. One extra place goes to MORE.
    await kw._seed()
    few = "orchard orchard orchard"
    more = "apples " + " ".join(f"filler{i}" for i in range(60)) + " orchard"
    await kw._insert(few, kw._pack_of("unrelated vector text one kkk"), "2026-01-01T00:00:05Z")
    await kw._insert(more, kw._pack_of("unrelated vector text two jjj"), "2026-01-01T00:00:06Z")
    kw._gate("rrf", 0.05)
    out = await M.do_recall(AGENT, QUERY, limit=5, extra_keyword_seats=1, trace=True)
    order = [d["ref"] for d in out["trace"]["gate"]["decisions"] if not d["passed"]]
    db = await get_db()
    ids = dict(await db.execute_fetchall("SELECT content, id FROM memories WHERE content IN (?, ?)", (few, more)))
    assert order.index(f"mem:{ids[few]}") < order.index(f"mem:{ids[more]}"), f"fixture is vacuous: gate order {order}"
    assert [m["content"] for m in _keyword_rows(out)] == [more]


@pytest.mark.asyncio
async def test_the_extra_places_come_after_the_extra_block_places(monkeypatch):
    blocks_, _ = await pb._seed_blocks(monkeypatch, pb.BLOCK_TEXTS)
    second_id = await kw._insert("orchard second keyword row kkk", kw._pack_of("unrelated vector text qq"),
                                 "2026-01-03T00:00:00Z")
    db = await get_db()
    (first_id,) = (await db.execute_fetchall("SELECT id FROM memories WHERE content = ?", (kw.KEYWORD_ONLY,)))[0]
    keyword_only = {f"mem:{first_id}", f"mem:{second_id}"}
    base = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1, extra_block_seats=2, trace=True)
    assert [k for k, _ in pb._held(base)] == ["block", "block", "keyword", "block", "block"], "fixture regression"
    keyword_ref = pb._held(base)[2][1]
    assert keyword_ref in keyword_only
    (other,) = keyword_only - {keyword_ref}
    out = await M.do_recall(AGENT, QUERY, limit=5, keyword_seats=1, extra_block_seats=2, extra_keyword_seats=1,
                            trace=True)
    assert pb._held(out) == [
        ("block", blocks_[0]), ("block", blocks_[1]), ("keyword", keyword_ref),
        ("block", blocks_[2]), ("block", blocks_[3]), ("keyword", other),
    ]


@pytest.mark.asyncio
async def test_a_row_in_an_extra_place_is_not_credited(monkeypatch):
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    ids = await _seed_refused(1)
    out = await M.do_recall(AGENT, QUERY, limit=5, extra_keyword_seats=2)
    assert {m["content"] for m in _keyword_rows(out)} == {kw.KEYWORD_ONLY, *ids}
    db = await get_db()
    counts = dict(await db.execute_fetchall("SELECT content, recall_count FROM memories"))
    assert counts[kw.STRONG] >= 1 and counts[kw.KEYWORD_ONLY] == 0 and all(counts[t] == 0 for t in ids), counts


@pytest.mark.asyncio
async def test_the_recall_count_bound_counts_the_extra_places(monkeypatch):
    """Past the slack the cue's places and the other held places leave in recall's
    bound: a bound that did not count the extra places would refuse this answer."""
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 20)  # limit 1 is otherwise also the depth
    ids = await _seed_refused(7)
    out = await M.do_recall(AGENT, QUERY, limit=1, keyword_seats=1, extra_keyword_seats=7)
    assert len(_keyword_rows(out)) == len(ids) + 1


@pytest.mark.asyncio
async def test_the_recall_tool_asks_for_no_extra_place():
    await _seed_refused(2)
    out = await server.registry._handlers["recall"]({"agent_id": AGENT, "query": QUERY, "limit": 5})
    assert _keyword_rows(out) == [], [m["content"] for m in out["messages"]]
    for name, schema in {t.name: t.inputSchema for t in server.registry._tools}.items():
        assert "extra_keyword_seats" not in (schema or {}).get("properties", {}), name


def _held_items(out):
    return [it["content"] for it in out["items"] if it.get("admission") == "reservation"]


@pytest.mark.asyncio
async def test_pro_asks_for_eight_and_lite_and_no_mode_for_none(monkeypatch):
    ids = await _seed_refused(9)
    pro, seen = await pb._reconstruct_kwargs(monkeypatch, mode="pro")
    assert seen.get("extra_keyword_seats") == 8
    assert len(_held_items(pro)) == 9 and set(_held_items(pro)) <= {kw.KEYWORD_ONLY, *ids}, pro["items"]
    lite, seen = await pb._reconstruct_kwargs(monkeypatch, mode="lite")
    assert "extra_keyword_seats" not in seen
    assert len(_held_items(lite)) == 1, lite["items"]  # the keyword seat alone
    plain, seen = await pb._reconstruct_kwargs(monkeypatch)
    assert "extra_keyword_seats" not in seen
    assert len(_held_items(plain)) == 1, plain["items"]  # the keyword seat alone, since 2.6.12


@pytest.mark.asyncio
async def test_the_reconstruct_bound_counts_the_extra_places(monkeypatch):
    """Nine held items under count 1 is past reconstruct's other places when the block
    places are taken away (two block, one keyword, three cue): a bound that did not
    count the extra keyword places would refuse it."""
    await _seed_refused(9)
    monkeypatch.setattr(R, "MODE_EXTRA_BLOCK_SEATS", {})
    monkeypatch.setattr(R, "MODE_EXTRA_KEYWORD_SEATS", {R.MODE_PRO: 8})
    monkeypatch.setattr(R, "MODE_DEPTH_SEATS", {})  # their slack would admit it anyway
    out = await R.do_reconstruct(AGENT, QUERY, count=1, mode="pro")
    assert len(_held_items(out)) == 9, out["items"]


def test_lite_budget_is_the_documented_one():
    assert R.LITE_BUDGET == 2800
    schemas = {t.name: t.inputSchema for t in server.registry._tools}
    mode = schemas["reconstruct"]["properties"]["mode"]["description"]
    assert f"up to {R.LITE_BUDGET:,} characters" in mode, mode
