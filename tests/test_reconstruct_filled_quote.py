"""A reconstruct item's head quote, filled from the parts of its record that matched (2.6).

The head quote used to be one governing passage cut at the preview tier. It is now
the recall excerpt's filling (cpersona/excerpts.py): the record's governing ranges
in ranking order while they fit CPERSONA_RECONSTRUCT_QUOTE_CHARS, shown in text
order. Measured with an answer reader on LongMemEval this answered 154 of 500
questions at count 1 against 116, and 321 against 223 at count 5.

What is held here: the quote is the part that matched even past the cap, it says
how it was chosen and where it came from, a short record is quoted whole, the
quote is the recall excerpt of the same record for the same query, the budget
default makes room for a filled head per item, a severed best passage says so, and
setting the knob to 0 brings back the single passage exactly.
"""

import os
import tempfile

import pytest

from cpersona import (
    admin_handlers,
    config,
    database,
    excerpts,
    memory_handlers,
    nodes,
    reconstruct,
    session,
    tasks,
)

AGENT = "agent.filled"
TAIL = "zzarquon nebulite flimsy was decided against for the pilot."
QUERY = "zzarquon nebulite flimsy"
#: Well past the quote cap, with the matching sentence at the end.
LONG = "\n\n".join(
    f"Paragraph {i} is about budget headcount procurement and warehouse staffing." for i in range(20)
) + "\n\n" + TAIL
SHORT = "zzarquon nebulite flimsy is a short note that fits the quote whole."
FILLER = [f"note {i} on topic {i} with words alpha{i} beta{i}" for i in range(30)]


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "filled.db")
        self.queue = tasks.MemoryTaskQueue()
        self.queue._running = True
        tasks._task_queue = self.queue
        await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()

    async def drain(self):
        await self.queue._drain(admin_handlers, memory_handlers, nodes)


@pytest.fixture
def filling(fake_embedding_client, monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 800)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    return fake_embedding_client


async def _store(tmp, *texts):
    ids = {}
    for text in (*texts, *FILLER):
        stored = await memory_handlers.do_store(AGENT, {"content": text})
        ids[text] = f"mem:{stored['id']}"
    await tmp.drain()
    return ids


async def _item(ref, **kw):
    out = await reconstruct.do_reconstruct(AGENT, QUERY, count=3, deep=True, **kw)
    return next(i for i in out["items"] if i["head_ref"] == ref), out


@pytest.mark.asyncio
async def test_a_long_record_is_quoted_from_the_part_that_matched_past_the_cap(filling):
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        item, _ = await _item(ids[LONG])
        assert LONG.index(TAIL) > 800, "the fixture's answer is inside the cap, so this proves nothing"
        assert TAIL in item["content"], "the head quote did not reach the part that matched"
        assert len(item["content"]) <= 800
        assert item["quote_basis"] == "lexical"
        assert item["content_truncated"] is True and item["content_len"] == len(LONG)


@pytest.mark.asyncio
async def test_the_ranges_are_where_the_quote_came_from(filling):
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        item, _ = await _item(ids[LONG])
        ranges = item["ranges"]
        assert ranges == sorted(ranges) and all(0 <= s < e <= len(LONG) for s, e in ranges)
        assert item["content"] == excerpts.SEPARATOR.join(LONG[s:e] for s, e in ranges)


@pytest.mark.asyncio
async def test_a_record_with_a_block_set_is_ranked_by_it(filling, monkeypatch):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        item, _ = await _item(ids[LONG])
        assert item["quote_basis"] == "blocks"
        assert TAIL in item["content"]


@pytest.mark.asyncio
async def test_a_record_no_longer_than_the_cap_is_quoted_whole(filling):
    async with _TempDB() as tmp:
        ids = await _store(tmp, SHORT)
        item, _ = await _item(ids[SHORT])
        assert item["content"] == SHORT and item["quote_basis"] == "whole"
        assert item["ranges"] == [[0, len(SHORT)]]
        assert "content_truncated" not in item


@pytest.mark.asyncio
async def test_the_head_quote_is_the_recall_excerpt_of_the_same_record(filling, monkeypatch):
    monkeypatch.setattr(config, "RECALL_EXCERPT_CHARS", 800)
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        item, _ = await _item(ids[LONG])
        found = await excerpts.for_refs(AGENT, [ids[LONG]], QUERY, None, 800)
        assert item["content"] == found[ids[LONG]]["excerpt"]
        assert item["quote_basis"] == found[ids[LONG]]["basis"]


def test_the_default_budget_holds_a_filled_head_per_item(monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 800)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_BUDGET", 4000)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 20000)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", None)
    assert reconstruct.resolve_budget(None, 1)[0] == 4000
    assert reconstruct.resolve_budget(None, 5)[0] == 4000
    assert reconstruct.resolve_budget(None, 10)[0] == 8000, "ten filled heads do not fit the old 10 x 500"
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    assert reconstruct.resolve_budget(None, 10)[0] == 5000, "with filling off the head is preview-sized again"


@pytest.mark.asyncio
async def test_a_best_passage_longer_than_the_cap_says_it_was_cut(filling, monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 20)
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        item, _ = await _item(ids[LONG])
        assert len(item["content"]) == 20
        assert item["context_incomplete"] is True
        back = await memory_handlers.do_get_contents(AGENT, [item["expand"]])
        passage = back["items"][0]["content"]
        assert passage.startswith(item["content"]) and len(passage) > 20, "the handover does not read the rest"


@pytest.mark.asyncio
async def test_zero_brings_back_the_single_passage(filling, monkeypatch):
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        filled, _ = await _item(ids[LONG])
        monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
        single, _ = await _item(ids[LONG])
        assert "quote_basis" not in single and "ranges" not in single
        assert single["content"] == LONG[:500], "with no nodes or blocks the old quote is the record's start"
        assert filled["content"] != single["content"]


@pytest.mark.asyncio
async def test_raising_the_budget_does_not_replace_a_filled_quote(filling):
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG, SHORT)
        small = await reconstruct.do_reconstruct(AGENT, QUERY, count=3, budget=900, deep=True)
        large = await reconstruct.do_reconstruct(AGENT, QUERY, count=3, budget=20000, deep=True)
        quoted = {i["head_ref"]: i["content"] for i in small["items"]}
        assert quoted, "the small budget returned nothing, so monotonicity is vacuous"
        for item in large["items"]:
            if item["head_ref"] in quoted:
                assert item["content"] == quoted[item["head_ref"]]
        assert ids[LONG] in {i["head_ref"] for i in large["items"]}


@pytest.mark.asyncio
async def test_the_mcp_boundary_delivers_the_filled_quote_uncut(filling):
    """The library quote is not what a client receives unless the boundary lets it through:
    the boundary used to cut every head to the preview tier, which would drop exactly the
    part that matched. Pinned at the call site a client reaches, not at the helper."""
    from cpersona import server

    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        out = await server.do_reconstruct_boundary(AGENT, QUERY, 3, None, None, None, True, "", None, "")
        item = next(i for i in out["items"] if i["head_ref"] == ids[LONG])
        assert TAIL in item["content"], "the boundary cut the filled quote back to the record's start"
        assert item["quote_basis"] == "lexical" and item["content_len"] == len(LONG)


@pytest.mark.asyncio
async def test_the_mcp_boundary_still_cuts_the_single_passage(filling, monkeypatch):
    from cpersona import server

    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        out = await server.do_reconstruct_boundary(AGENT, QUERY, 3, None, None, None, True, "", None, "")
        item = next(i for i in out["items"] if i["head_ref"] == ids[LONG])
        assert item["content"] == LONG[:500] and item["content_truncated"] is True
