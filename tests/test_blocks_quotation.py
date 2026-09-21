"""Quoting a claim at block granularity: docs/BLOCK_REACH_DESIGN.md invariant 9.

A block is a clause. That is small enough to read and small enough to say the
opposite of the record it came from, so what a quote carries is not the block
but the contiguous range that governs it. These tests hold the rule that decides
that range, the flag and the handover for the case where it does not fit, and
the refusal that keeps a stale offset from serving a different passage.

The control fixture matters as much as the others: a rule that always swallowed
the neighbour would pass every severance test and quote the whole record.
"""

import os
import tempfile

import pytest

from cpersona import (
    admin_handlers,
    blocks,
    config,
    database,
    memory_handlers,
    nodes,
    reconstruct,
    session,
    tasks,
)

AGENT = "agent.quote"

#: The second sentence reverses the first. Quoting either alone is a lie.
DECISION = "Aを採用する。ただし、本番ではまだ有効にしない。"
#: Two independent statements. The rule must leave them apart.
INDEPENDENT = "Aを採用する。Bは来週決める。"
#: The first block is not a sentence, so it cannot be read without the next.
CLAUSE = "第一項\n第二項です。第三項です。"
#: A correction announced by its conjunction.
CORRECTION = "移行を完了した。しかし翌日に切り戻した。"


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "quote.db")
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
def quoting(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    return fake_embedding_client


def _spans(text: str) -> list[tuple[int, int]]:
    return [(s.start, s.end) for s in blocks.segment(text, node_bounds=())]


# --------------------------------------------------------------------------
# the rule that decides what a quote carries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, index, expected",
    [
        (DECISION, 0, DECISION),  # the qualification follows: take it
        (DECISION, 1, DECISION),  # the qualification leads: take what it qualifies
        (CORRECTION, 0, CORRECTION),
        (CORRECTION, 1, CORRECTION),
        (CLAUSE, 0, "第一項\n第二項です。"),  # finish the sentence, and no further
        (CLAUSE, 2, "第三項です。"),  # a whole sentence that governs nothing else
    ],
)
def test_a_quote_carries_the_context_that_governs_it(text, index, expected):
    spans = _spans(text)
    start, end, complete = blocks.context_range(text, spans, index)
    assert text[start:end] == expected
    assert complete


@pytest.mark.parametrize("index", [0, 1])
def test_independent_sentences_are_left_apart(index):
    """The control. A rule that always took the neighbour would pass every test
    above and quote the whole record, which is the thing block granularity is
    for avoiding."""
    spans = _spans(INDEPENDENT)
    start, end, complete = blocks.context_range(INDEPENDENT, spans, index)
    assert INDEPENDENT[start:end] == INDEPENDENT[spans[index][0] : spans[index][1]]
    assert complete


@pytest.mark.parametrize(
    "index, expected",
    [
        # The rule is reaching forwards for the qualification, and cannot have it.
        (0, "Aを採用する。"),
        # ...and backwards for the thing qualified, which is the other branch and
        # needs its own case: one direction reporting honestly says nothing about
        # the other.
        (1, "ただし、本番ではまだ有効にしない。"),
    ],
)
def test_a_context_the_limit_cannot_hold_is_reported_incomplete(index, expected):
    spans = _spans(DECISION)
    start, end, complete = blocks.context_range(DECISION, spans, index, max_chars=8)
    assert not complete
    assert DECISION[start:end] == expected, "a severed quote was widened instead of reported"


def test_the_limit_is_a_server_policy_and_not_a_budget():
    """The context is decided before anything is cut, so the payload budget
    cannot change which passage a reader is shown -- only how many."""
    assert blocks.BLOCK_CONTEXT_CHARS == blocks.MAX_BLOCK_CHARS * 2


# --------------------------------------------------------------------------
# end to end: what reconstruct returns
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_is_quoted_from_its_block_with_the_qualification(quoting):
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()

        out = await reconstruct.do_reconstruct(AGENT, "採用", count=3, deep=True)

        (item,) = out["items"]
        assert item["block"]["of"] == 2
        assert item["content"] == DECISION, "the quote was severed from its qualification"
        start, end = item["block"]["span"]
        assert DECISION[start:end] == item["content"]
        assert "context_incomplete" not in item


@pytest.mark.asyncio
async def test_an_independent_sentence_is_quoted_alone(quoting):
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": INDEPENDENT})
        await tmp.drain()

        out = await reconstruct.do_reconstruct(AGENT, "来週", count=3, deep=True)

        (item,) = out["items"]
        assert item["content"] in (INDEPENDENT[:7], INDEPENDENT[7:])
        assert item["content"] != INDEPENDENT, "the quote swallowed a sentence that governs nothing"


@pytest.mark.asyncio
async def test_a_record_without_blocks_is_quoted_as_it_was(quoting):
    """One sentence divides into one block, which is the record, so no rows are
    stored and the quote comes from the record itself."""
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": "ひとつの文だけです。"})
        await tmp.drain()

        out = await reconstruct.do_reconstruct(AGENT, "ひとつ", count=3, deep=True)

        (item,) = out["items"]
        assert "block" not in item
        assert item["content"] == "ひとつの文だけです。"


@pytest.mark.asyncio
async def test_a_partial_block_set_is_not_quoted_from(quoting):
    """A set that does not cover the text is offsets into something else. The
    record falls back to the quoting it had before rather than being quoted
    from a span measured in text the set no longer describes."""
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()
        db = await database.get_db()
        await db.execute("DELETE FROM record_blocks WHERE block_index = 0")
        await db.commit()

        out = await reconstruct.do_reconstruct(AGENT, "採用", count=3, deep=True)

        assert "block" not in out["items"][0]
        assert out["items"][0]["content"] == DECISION


@pytest.mark.asyncio
async def test_with_block_retrieval_off_the_index_changes_no_quote(monkeypatch, quoting):
    """Invariant 2 applies to the quote as much as to the answer: off means the
    rows may exist and nothing reads them."""
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()
        db = await database.get_db()
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM record_blocks")
        assert rows[0][0] > 1, "the fixture built no blocks, so it proves nothing"

        monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", False)
        out = await reconstruct.do_reconstruct(AGENT, "採用", count=3, deep=True)

        assert "block" not in out["items"][0]


@pytest.mark.asyncio
async def test_raising_the_budget_does_not_replace_a_quotation(quoting):
    """Prefix monotonicity. A bigger budget buys more items and more excerpts;
    it must never show a different passage for the same claim."""
    async with _TempDB() as tmp:
        for text in (DECISION, CORRECTION, CLAUSE):
            await memory_handlers.do_store(AGENT, {"content": text})
        await tmp.drain()

        small = await reconstruct.do_reconstruct(AGENT, "採用", count=3, budget=600, deep=True)
        large = await reconstruct.do_reconstruct(AGENT, "採用", count=3, budget=20000, deep=True)

        quoted = {i["head_ref"]: i["content"] for i in small["items"]}
        assert quoted, "the small budget returned nothing, so monotonicity is vacuous"
        for item in large["items"]:
            if item["head_ref"] in quoted:
                assert item["content"] == quoted[item["head_ref"]]


@pytest.mark.asyncio
async def test_a_cut_quote_says_it_is_no_longer_whole(monkeypatch, quoting):
    """The budget can still cut a quotation short. What it must not do is let a
    prefix of a qualified statement pass as the statement: the cut says so, and
    hands over the block to read instead."""
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 8)
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()

        out = await reconstruct.do_reconstruct(AGENT, "採用", count=3, deep=True)

        (item,) = out["items"]
        assert item["content_truncated"] and item["content"] == DECISION[:8]
        assert item["context_incomplete"], "a severed quote passed as whole evidence"
        assert item["expand"]["ref"] == f"mem:{stored['id']}"
        assert item["expand"]["revision"] == blocks.text_revision(DECISION)
        # And the handover works: it reads back the block, not the whole record.
        back = await memory_handlers.do_get_contents(AGENT, [item["expand"]])
        assert back["items"][0]["content"] != DECISION
        assert back["items"][0]["content"] in DECISION


@pytest.mark.asyncio
async def test_every_head_is_allocated_before_any_excerpt(quoting):
    """Breadth before depth, with block quotes in play: a budget that cannot
    hold everything drops excerpts before it drops an item."""
    async with _TempDB() as tmp:
        for text in (DECISION, CORRECTION, CLAUSE, INDEPENDENT):
            await memory_handlers.do_store(AGENT, {"content": text})
        await tmp.drain()

        out = await reconstruct.do_reconstruct(AGENT, "採用", count=4, budget=700, deep=True)

        assert len(out["items"]) > 1, "the budget admitted one item, so ordering proves nothing"
        for item in out["items"]:
            assert item["content"], "an item was returned without its head"


# --------------------------------------------------------------------------
# handing the rest over, and refusing to serve a stale offset
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_block_range_reads_back_exactly(quoting):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()

        out = await memory_handlers.do_get_contents(
            AGENT, [{"ref": f"mem:{stored['id']}", "block": 1}]
        )

        (item,) = out["items"]
        assert item["content"] == "ただし、本番ではまだ有効にしない。"
        assert item["range"]["block"] == [1, 1] and item["range"]["of"] == 2
        assert "unresolved" not in out


@pytest.mark.asyncio
async def test_a_rewritten_record_refuses_the_offsets_it_gave_out(quoting):
    """The whole point of the revision: the same numbers in new text are a
    different passage, and serving it would quote something nothing said."""
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()
        revision = blocks.text_revision(DECISION)

        db = await database.get_db()
        await db.execute(
            "UPDATE memories SET content = ? WHERE id = ?", ("まったく別の本文です。", stored["id"])
        )
        await db.commit()

        out = await memory_handlers.do_get_contents(
            AGENT, [{"ref": f"mem:{stored['id']}", "span": [0, 7], "revision": revision}]
        )

        assert out["items"] == []
        assert out["unresolved"] == [
            {"ref": f"mem:{stored['id']}", "reason": memory_handlers.RANGE_STALE_REVISION}
        ]


@pytest.mark.asyncio
async def test_a_block_that_does_not_exist_is_unresolved_not_widened(quoting):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()

        out = await memory_handlers.do_get_contents(
            AGENT, [{"ref": f"mem:{stored['id']}", "block": 99}]
        )

        assert out["items"] == []
        assert out["unresolved"][0]["reason"] == memory_handlers.RANGE_BLOCK_OUT_OF_RANGE


@pytest.mark.asyncio
async def test_a_record_without_a_current_block_set_is_unresolved(quoting):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()
        db = await database.get_db()
        await db.execute("DELETE FROM record_blocks WHERE block_index = 1")
        await db.commit()

        out = await memory_handlers.do_get_contents(
            AGENT, [{"ref": f"mem:{stored['id']}", "block": 0}]
        )

        assert out["items"] == []
        assert out["unresolved"][0]["reason"] == memory_handlers.RANGE_NO_CURRENT_BLOCKS


def test_a_ref_naming_two_kinds_of_range_is_invalid():
    ref, request, invalid = memory_handlers._parse_ref_entry(
        {"ref": "mem:1", "block": 0, "node": 0}
    )
    assert request is None and invalid == memory_handlers.RANGE_INVALID
