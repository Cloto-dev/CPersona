"""Overflow tree: dividing a long record and building its nodes on the queue.

docs/OVERFLOW_TREE_DESIGN.md §2 (division), §3 (when nodes are built) and §5
(invariants). The table and its triggers are pinned by test_record_nodes_schema.

The token report comes from the conftest double (``token_window``), whose tokens are
runs of up to three word characters or one other non-space character. The queue is a
real ``MemoryTaskQueue`` that is never started: each test drains it by hand, so the
moment nodes appear is the moment the test chose.
"""

import os
import re
import tempfile

import pytest

from cpersona import admin_handlers, config, database, memory_handlers, nodes, session, tasks
from cpersona._vendored_mcp_common.embedding_client import TokenInfo
from cpersona.nodes import Span, choose_cut, divide

AGENT = "agent.nodes"
WINDOW = 24  # tokens, specials included: 22 content tokens per node


# --------------------------------------------------------------------------------------
# choose_cut -- the boundary rule (§2)
# --------------------------------------------------------------------------------------


def test_a_paragraph_break_is_taken_before_a_line_break():
    text = "aaaa bbbb\n\ncccc\ndddd eeee ffff"
    # The last paragraph break ends at 11, exactly half of 22; the line break at 16 is later.
    assert choose_cut(text, 22) == 11


def test_a_boundary_class_short_of_half_the_window_gives_way_to_the_next():
    text = "aa\n\nbbbbbbbb cccccccc\ndddddddd eeee"
    # The paragraph break ends at 4, under half of 30; the line break at 22 is taken.
    assert choose_cut(text, 30) == 22


def test_a_sentence_end_is_taken_before_whitespace():
    text = "one two three. four five six seven"
    # The sentence ends at 14, past half of 26; the last space before 26 is at 24.
    assert choose_cut(text, 26) == 14


def test_a_decimal_point_is_not_a_sentence_end():
    text = "value 3.14159 and more words here"
    # "3." is followed by a digit: the whitespace class decides, at the last space <= 20.
    assert choose_cut(text, 20) == 18


def test_a_half_width_mark_at_the_window_is_not_a_sentence_end_when_text_continues():
    text = "aaaaaaaaaaaa bbbb.cccc"
    # The window closes right after ".": bounding the search would read it as an end.
    assert choose_cut(text, 18) == 13


def test_a_full_width_sentence_end_needs_no_following_space():
    text = "今日は晴れです。明日は雨が降るでしょう。週末は"
    assert choose_cut(text, 22) == 20


def test_no_boundary_cuts_at_the_window():
    assert choose_cut("x" * 50, 17) == 17


def test_the_cut_is_never_empty():
    with pytest.raises(ValueError):
        choose_cut("abc", 0)
    assert choose_cut("abc def", 1) == 1


# --------------------------------------------------------------------------------------
# divide -- invariants 3, 4 and 5
# --------------------------------------------------------------------------------------


def _fake_measure(window: int):
    from tests.conftest import FakeEmbeddingClient

    client = FakeEmbeddingClient()
    client.token_window = window

    async def measure(text):
        return (await client.count_tokens([text]))[0]

    return measure


_TEXTS = {
    "prose": " ".join(
        f"Sentence number {i} talks about topic {i % 7} and goes on for a while." for i in range(40)
    ),
    "paragraphs": "\n\n".join(f"Paragraph {i}\nline one of it\nline two of it" for i in range(30)),
    "japanese": "".join(f"これは{i}番目の文で、話題は{i % 5}です。" for i in range(60)),
    "one_word": "x" * 400,
    "fits": "short text",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_TEXTS))
async def test_spans_partition_the_text_and_each_fits_the_window(name):
    text = _TEXTS[name]
    measure = _fake_measure(WINDOW)
    spans = await divide(text, measure)

    # invariant 4: contiguous, complete, in order
    assert spans[0].start == 0
    assert spans[-1].end == len(text)
    assert all(a.end == b.start for a, b in zip(spans, spans[1:]))
    assert all(s.end > s.start for s in spans)
    # invariant 3: counted on the span's own text, and within the window
    for s in spans:
        info = await measure(text[s.start : s.end])
        assert s.token_count == info.count <= s.window == WINDOW
    if name == "fits":
        assert spans == [Span(0, len(text), 6, WINDOW)]  # "sho" "rt" "tex" "t" + 2 specials
    else:
        assert len(spans) > 1


@pytest.mark.asyncio
async def test_division_is_deterministic():
    text = _TEXTS["prose"]
    first = await divide(text, _fake_measure(WINDOW))
    assert await divide(text, _fake_measure(WINDOW)) == first
    # a different window is a different division, not the same one relabelled
    assert await divide(text, _fake_measure(WINDOW * 2)) != first


@pytest.mark.asyncio
async def test_a_span_that_still_overflows_is_cut_again():
    """A cut can change how the last characters tokenize; the span's own report wins."""
    text = "aaaa bbbb cccc dddd eeee ffff gggg"
    calls = []

    async def measure(t):
        calls.append(t)
        n = len(t)
        # Pretends every text over 12 characters overflows, and reports the window as
        # closing at character 20 -- more than the span's own measurement allows.
        if n > 12:
            return TokenInfo(count=99, window=10, truncated=True, window_end_char=min(20, n))
        return TokenInfo(count=n // 2, window=10, truncated=False, window_end_char=n)

    spans = await divide(text, measure)

    assert all(s.end - s.start <= 12 for s in spans)
    assert spans[0].start == 0 and spans[-1].end == len(text)
    assert all(a.end == b.start for a, b in zip(spans, spans[1:]))


@pytest.mark.asyncio
async def test_an_unknown_report_stops_the_division():
    async def measure(t):
        return None

    with pytest.raises(nodes.TokensUnknown):
        await divide("anything", measure)


# --------------------------------------------------------------------------------------
# the queue: store / archive / update, and the build task
# --------------------------------------------------------------------------------------


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "nodes_build.db")
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

    async def pending(self):
        db = await database.get_db()
        return await db.execute_fetchall("SELECT task_type, payload FROM pending_memory_tasks ORDER BY id")

    async def nodes_of(self, kind, parent_id):
        db = await database.get_db()
        return await db.execute_fetchall(
            "SELECT node_index, start_char, end_char, token_count, window, embedding IS NOT NULL, "
            "embedding_model FROM record_nodes WHERE parent_kind = ? AND parent_id = ? ORDER BY node_index",
            (kind, parent_id),
        )


LONG = _TEXTS["prose"]
SHORT = "a short memory that fits"


@pytest.fixture
def windowed(fake_embedding_client):
    fake_embedding_client.token_window = WINDOW
    return fake_embedding_client


def _assert_partition(rows, text):
    assert rows, "no nodes were built"
    assert [r[0] for r in rows] == list(range(len(rows)))
    assert rows[0][1] == 0 and rows[-1][2] == len(text)
    assert all(a[2] == b[1] for a, b in zip(rows, rows[1:]))
    assert all(r[3] <= r[4] == WINDOW for r in rows)
    assert all(r[5] for r in rows), "a node was written without its embedding"
    assert {r[6] for r in rows} == {config.EMBEDDING_MODEL}


@pytest.mark.asyncio
async def test_a_long_store_reports_queued_and_the_drain_builds_its_nodes(windowed):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": LONG})
        assert result["result"] == "stored"
        assert result["nodes"] == {"status": "queued"}
        db = await database.get_db()
        before = await db.execute_fetchall("SELECT * FROM memories WHERE id = ?", (result["id"],))

        await tmp.drain()

        _assert_partition(await tmp.nodes_of("mem", result["id"]), LONG)
        assert await tmp.pending() == []
        # invariant 2: building nodes did not touch the record
        assert await db.execute_fetchall("SELECT * FROM memories WHERE id = ?", (result["id"],)) == before


@pytest.mark.asyncio
async def test_a_store_that_fits_reports_nothing_and_queues_nothing(windowed):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": SHORT})
        assert result["result"] == "stored"
        assert "nodes" not in result
        assert await tmp.pending() == []


@pytest.mark.asyncio
async def test_an_unknown_token_report_queues_nothing(fake_embedding_client):
    assert fake_embedding_client.token_window is None
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": LONG})
        assert "nodes" not in result
        assert await tmp.pending() == []


@pytest.mark.asyncio
async def test_with_the_queue_disabled_nothing_is_reported(windowed):
    async with _TempDB():
        tasks._task_queue = None
        result = await memory_handlers.do_store(AGENT, {"content": LONG})
        assert result["result"] == "stored"
        assert "nodes" not in result


@pytest.mark.asyncio
async def test_a_duplicate_store_queues_nothing(windowed):
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": LONG})
        await tmp.drain()
        again = await memory_handlers.do_store(AGENT, {"content": LONG})
        assert again["result"] == "skipped"
        assert "nodes" not in again
        assert await tmp.pending() == []


@pytest.mark.asyncio
async def test_a_long_episode_is_queued_and_built(windowed):
    summary = _TEXTS["japanese"]
    async with _TempDB() as tmp:
        result = await memory_handlers.do_archive_episode(AGENT, [], summary=summary)
        assert result["nodes"] == {"status": "queued"}
        await tmp.drain()
        _assert_partition(await tmp.nodes_of("ep", result["episode_id"]), summary)


@pytest.mark.asyncio
async def test_update_memory_replaces_the_nodes_of_the_old_text(windowed):
    new_text = _TEXTS["paragraphs"]
    async with _TempDB() as tmp:
        mem = (await memory_handlers.do_store(AGENT, {"content": LONG}))["id"]
        await tmp.drain()
        old_nodes = await tmp.nodes_of("mem", mem)

        result = await admin_handlers.do_update_memory(mem, new_text, agent_id=AGENT)
        assert result["nodes"] == {"status": "queued"}
        assert await tmp.nodes_of("mem", mem) == []  # the trigger, before the rebuild
        await tmp.drain()

        rebuilt = await tmp.nodes_of("mem", mem)
        _assert_partition(rebuilt, new_text)
        assert rebuilt != old_nodes


@pytest.mark.asyncio
async def test_update_memory_to_a_short_text_leaves_no_nodes(windowed):
    async with _TempDB() as tmp:
        mem = (await memory_handlers.do_store(AGENT, {"content": LONG}))["id"]
        await tmp.drain()
        result = await admin_handlers.do_update_memory(mem, SHORT, agent_id=AGENT)
        assert "nodes" not in result
        await tmp.drain()
        assert await tmp.nodes_of("mem", mem) == []


@pytest.mark.asyncio
async def test_a_record_deleted_before_the_drain_gets_no_nodes(windowed):
    async with _TempDB() as tmp:
        mem = (await memory_handlers.do_store(AGENT, {"content": LONG}))["id"]
        await admin_handlers.do_delete_memory(mem, agent_id=AGENT)
        await tmp.drain()
        assert await tmp.nodes_of("mem", mem) == []
        assert await tmp.pending() == []


@pytest.mark.asyncio
async def test_a_text_changed_during_the_build_discards_the_stale_nodes(windowed, monkeypatch):
    """Invariant 6 across the unlocked window: the trigger cannot remove nodes not yet written."""
    async with _TempDB() as tmp:
        mem = (await memory_handlers.do_store(AGENT, {"content": LONG}))["id"]
        real_embed = windowed.embed
        changed = []

        async def embed_and_rewrite(texts):
            if len(texts) > 1 and not changed:
                changed.append(True)
                db = await database.get_db()
                await db.execute("UPDATE memories SET content = ? WHERE id = ?", (SHORT + " rewritten", mem))
                await db.commit()
            return await real_embed(texts)

        monkeypatch.setattr(windowed, "embed", embed_and_rewrite)
        await tmp.drain()

        assert changed, "the build never reached its embedding step"
        assert await tmp.nodes_of("mem", mem) == []


@pytest.mark.asyncio
async def test_a_second_build_finds_the_nodes_current(windowed):
    async with _TempDB() as tmp:
        mem = (await memory_handlers.do_store(AGENT, {"content": LONG}))["id"]
        await tmp.drain()
        first = await tmp.nodes_of("mem", mem)
        assert await nodes.build_nodes({"kind": "mem", "id": mem}) == "nodes already current"
        assert await tmp.nodes_of("mem", mem) == first


@pytest.mark.asyncio
async def test_nodes_from_another_model_are_rebuilt(windowed, monkeypatch):
    async with _TempDB() as tmp:
        mem = (await memory_handlers.do_store(AGENT, {"content": LONG}))["id"]
        await tmp.drain()
        monkeypatch.setattr(config, "EMBEDDING_MODEL", "another-model")
        assert (await nodes.build_nodes({"kind": "mem", "id": mem})).startswith("built")
        assert {r[6] for r in await tmp.nodes_of("mem", mem)} == {"another-model"}


@pytest.mark.asyncio
async def test_a_failed_token_report_during_the_build_is_retried_not_written(windowed, monkeypatch):
    async with _TempDB() as tmp:
        mem = (await memory_handlers.do_store(AGENT, {"content": LONG}))["id"]
        windowed.token_window = None
        monkeypatch.setattr(tasks, "TASK_RETRY_DELAY", 0)
        await tmp.drain()
        assert await tmp.nodes_of("mem", mem) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"kind": "row", "id": 1}, {"kind": "mem", "id": "1"}, ["mem", 1]])
async def test_a_malformed_payload_is_discarded(windowed, payload):
    async with _TempDB():
        assert await nodes.build_nodes(payload) == "malformed payload, discarded"


# --------------------------------------------------------------------------------------
# invariant 1: retrieval is unchanged by nodes
# --------------------------------------------------------------------------------------


def _ranking(response):
    return [(m.get("ref") or m.get("id"), m.get("content")) for m in response["messages"]]


@pytest.mark.asyncio
async def test_recall_returns_the_same_records_in_the_same_order_with_nodes(windowed):
    corpus = [
        LONG,
        _TEXTS["paragraphs"],
        _TEXTS["japanese"],
        "sourdough bread proofing recipe for the weekend",
        "raspberry pi gpio sensor wiring tutorial",
        "topic 3 is the one about the garden",
    ]
    queries = ["topic 3", "sensor wiring", "Paragraph 12 line two", "話題は2です", "weekend bread"]
    async with _TempDB() as tmp:
        for text in corpus:
            await memory_handlers.do_store(AGENT, {"content": text})
        before = [_ranking(await memory_handlers.do_recall(AGENT, q, 10)) for q in queries]

        await tmp.drain()
        db = await database.get_db()
        assert (await db.execute_fetchall("SELECT COUNT(DISTINCT parent_id) FROM record_nodes"))[0][0] == 3

        after = [_ranking(await memory_handlers.do_recall(AGENT, q, 10)) for q in queries]
        assert after == before
        assert any(before), "the queries retrieved nothing, so the comparison proves nothing"
        assert re.search("topic", str(before))
