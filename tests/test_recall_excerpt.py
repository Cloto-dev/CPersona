"""The excerpt a preview-cut recall row carries: cpersona/excerpts.py.

The preview shows a record's start. The excerpt shows the part that matched the
query, under a cap, next to it: the record's governing ranges taken in the
server's block ranking order while they fit, shown in text order. The FTS arm stays on
in these tests so the long record is returned at all (reaching it is block
reach's business, not the excerpt's). What is under test here is that the excerpt is the matching part, that it stays within its cap
and inside the record, that it says how it was chosen, and that every response
that did not ask for it is exactly what it was.
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
    server,
    session,
    tasks,
)

AGENT = "agent.excerpt"
TAIL = "zzarquon nebulite flimsy was decided against for the pilot."
QUERY = "zzarquon nebulite flimsy"

#: Well past the preview, with the matching sentence at the end.
LONG = "\n\n".join(
    f"Paragraph {i} is about budget headcount procurement and warehouse staffing." for i in range(20)
) + "\n\n" + TAIL

#: Past the preview but one block: no sentence end, no line break, under the
#: forced-boundary length.
ONE_BLOCK = " ".join(["plain words without any break"] * 20)


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "excerpt.db")
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


#: Unrelated short records. With a pool of one, the pool-size gate a small
#: corpus runs drops everything, and these tests are about the rows it returns.
FILLER = [f"note {i} on topic {i} with words alpha{i} beta{i}" for i in range(30)]


async def _store(tmp, *texts):
    for text in (*texts, *FILLER):
        await memory_handlers.do_store(AGENT, {"content": text})
    await tmp.drain()


async def _boundary(query=QUERY, **kw):
    return await server.do_recall_boundary(AGENT, query, 5, False, "", [], None, "", **kw)


def _row(result, startswith):
    return next(m for m in result["messages"] if m["content"].startswith(startswith[:40]))


# --------------------------------------------------------------------------
# fill
# --------------------------------------------------------------------------


def test_fill_takes_ranges_in_ranking_order_until_the_cap_and_shows_them_in_text_order():
    text = "Alpha one. Beta two. Gamma three. Delta four."
    spans = [(0, 11), (11, 21), (21, 34), (34, 45)]
    ranked = [(2, 21, 34, None), (0, 0, 11, None), (3, 34, 45, None), (1, 11, 21, None)]
    assert excerpts.fill(text, spans, ranked, 30) == text[0:11] + excerpts.SEPARATOR + text[21:34]


def test_fill_cuts_a_best_range_longer_than_the_cap_rather_than_returning_nothing():
    text = "Alpha one. Beta two. Gamma three. Delta four."
    spans = [(0, 11), (11, 21), (21, 34), (34, 45)]
    assert excerpts.fill(text, spans, [(2, 21, 34, None)], 5) == text[21:26]


def test_fill_never_repeats_text_a_taken_range_already_covers():
    text = "one two three four"
    spans = [(0, 4), (4, 8), (8, 14), (14, 18)]
    # Block 1 does not end a sentence, so its governing range reaches the others.
    out = excerpts.fill(text, spans, [(1, 4, 8, None), (0, 0, 4, None)], 100)
    assert out == text


# --------------------------------------------------------------------------
# the recall row
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cut_row_carries_the_part_that_matched(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        row = _row(await _boundary(), LONG)
        assert row["content_truncated"] is True and TAIL not in row["content"]
        assert TAIL in row["excerpt"], "the excerpt is the part the preview cut away"
        assert len(row["excerpt"]) <= config.RECALL_EXCERPT_CHARS
        assert row["excerpt_basis"] == "lexical"


@pytest.mark.asyncio
async def test_every_passage_of_an_excerpt_is_text_the_record_holds(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        row = _row(await _boundary(), LONG)
        for passage in row["excerpt"].split(excerpts.SEPARATOR):
            assert passage in LONG


@pytest.mark.asyncio
async def test_a_record_with_a_block_set_is_ranked_by_it(fake_embedding_client, monkeypatch):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        row = _row(await _boundary(), LONG)
        assert row["excerpt_basis"] == "blocks"
        assert TAIL in row["excerpt"]


@pytest.mark.asyncio
async def test_a_block_set_is_ranked_with_the_query_vector_the_recall_embedded(
    fake_embedding_client, monkeypatch
):
    """The bits are what make a block set's ranking more than a word match."""
    from cpersona import reconstruct

    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    seen = []
    real = reconstruct.rank_blocks

    def spy(text, rows, query_bits, grams):
        seen.append(query_bits)
        return real(text, rows, query_bits, grams)

    monkeypatch.setattr(reconstruct, "rank_blocks", spy)
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        await _boundary()
    assert seen and all(bits is not None for bits in seen)


@pytest.mark.asyncio
async def test_a_record_of_one_block_shows_its_start(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store(tmp, ONE_BLOCK)
        row = _row(await _boundary("plain words"), ONE_BLOCK)
        assert row["excerpt_basis"] == "start"
        assert row["excerpt"] == ONE_BLOCK[: config.RECALL_EXCERPT_CHARS]


@pytest.mark.asyncio
async def test_an_episode_is_excerpted_from_its_summary(fake_embedding_client):
    async with _TempDB() as tmp:
        await memory_handlers.do_archive_episode(AGENT, [], summary=LONG, keywords="kw")
        await _store(tmp)
        row = next(m for m in (await _boundary())["messages"] if m.get("ref", "").startswith("ep:"))
        assert TAIL in row["excerpt"]
        assert "[Episode]" not in row["excerpt"], "the excerpt quotes the stored text"


@pytest.mark.asyncio
async def test_the_same_query_gives_the_same_excerpt(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        first = _row(await _boundary(), LONG)["excerpt"]
        assert _row(await _boundary(), LONG)["excerpt"] == first


# --------------------------------------------------------------------------
# where there is none
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_the_preview_shows_whole_carries_no_excerpt(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store(tmp, TAIL)
        row = _row(await _boundary(), TAIL)
        assert "excerpt" not in row and "excerpt_basis" not in row


@pytest.mark.asyncio
async def test_full_content_carries_no_excerpt(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        row = _row(await _boundary(full_content=True), LONG)
        assert row["content"] == LONG and "excerpt" not in row


@pytest.mark.asyncio
async def test_a_zero_cap_turns_the_excerpt_off(fake_embedding_client, monkeypatch):
    monkeypatch.setattr(config, "RECALL_EXCERPT_CHARS", 0)
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        assert "excerpt" not in _row(await _boundary(), LONG)


@pytest.mark.asyncio
async def test_no_preview_means_no_excerpt(fake_embedding_client, monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 0)
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        row = _row(await _boundary(), LONG)
        assert row["content"] == LONG and "excerpt" not in row


@pytest.mark.asyncio
async def test_a_library_caller_gets_exactly_what_it_got_before(fake_embedding_client):
    """do_recall asks for no excerpt unless told to: a bench or a reranker sees no new key."""
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        result = await memory_handlers.do_recall(AGENT, QUERY, 5)
        assert all("excerpt" not in m and "excerpt_basis" not in m for m in result["messages"])


@pytest.mark.asyncio
async def test_recall_with_context_carries_the_excerpt_too(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        result = await server.do_recall_with_context_boundary(AGENT, QUERY, [], 5, "", False, None, "")
        row = _row(result, LONG)
        assert TAIL in row["excerpt"]
