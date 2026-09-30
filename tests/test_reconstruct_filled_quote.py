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


@pytest.mark.usefixtures("blocks_off")
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


@pytest.mark.usefixtures("blocks_off")
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


async def _episode(tmp, summary):
    for text in FILLER:
        await memory_handlers.do_store(AGENT, {"content": text})
    out = await memory_handlers.do_archive_episode(
        AGENT, [{"role": "user", "content": "notes"}], summary=summary, keywords="notes"
    )
    await tmp.drain()
    return f"ep:{out['episode_id']}"


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_an_episode_is_quoted_in_the_text_get_contents_serves(filling):
    """bug-456: without a block set, an episode was measured in recall's display
    string ('[Episode] ' + summary), so every range pointed 10 characters right of
    the passage it named. Each range, read back, must be what the quote shows."""
    async with _TempDB() as tmp:
        ref = await _episode(tmp, LONG)
        item, _ = await _item(ref)
        assert item["quote_basis"] == "lexical" and TAIL in item["content"]
        back = await memory_handlers.do_get_contents(AGENT, [{"ref": ref, "span": r} for r in item["ranges"]])
        assert excerpts.SEPARATOR.join(i["content"] for i in back["items"]) == item["content"]
        assert item["content_len"] == len(LONG)


@pytest.mark.asyncio
async def test_an_episode_s_cut_passage_expands_to_its_own_rest(filling, monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 20)
    async with _TempDB() as tmp:
        ref = await _episode(tmp, LONG)
        item, _ = await _item(ref)
        assert item["context_incomplete"] is True and item["expand"]["ref"] == ref
        passage = (await memory_handlers.do_get_contents(AGENT, [item["expand"]]))["items"][0]["content"]
        assert passage.startswith(item["content"]) and len(passage) > 20


@pytest.mark.usefixtures("blocks_off")
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


@pytest.mark.usefixtures("blocks_off")
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


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_the_mcp_boundary_still_cuts_the_single_passage(filling, monkeypatch):
    from cpersona import server

    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        out = await server.do_reconstruct_boundary(AGENT, QUERY, 3, None, None, None, True, "", None, "")
        item = next(i for i in out["items"] if i["head_ref"] == ids[LONG])
        assert item["content"] == LONG[:500] and item["content_truncated"] is True


# --- what one reconstruction reads, and how often (bug-457, 474, 479, 480, 491, 494, 495) ---


def test_a_budget_below_one_head_quote_is_raised_to_it(monkeypatch):
    """bug-457: the floor was one preview-tier excerpt, 500, while a head is cut to 800,
    so a budget of 600 returned about 800 characters and reported no overrun."""
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 800)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", None)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 20000)
    budget, policy = reconstruct.resolve_budget(600, 1)
    assert budget == 800 and policy == {"source": "caller", "clamped": True, "reason": "raised_to_one_excerpt"}
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    assert reconstruct.resolve_budget(600, 1)[0] == 600, "with filling off the floor is the preview tier again"


@pytest.mark.asyncio
async def test_what_is_returned_fits_the_budget_it_reports(filling):
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=1, deep=True, budget=600)
        # The raise is reported (requested_budget + budget_policy stay in the compact
        # response), and what came back is within what the budget was raised to.
        assert out["requested_budget"] == 600
        assert out["budget_policy"] == {"source": "caller", "clamped": True, "reason": "raised_to_one_excerpt"}
        assert sum(len(item["content"]) for item in out["items"]) <= 800


@pytest.mark.asyncio
async def test_a_reconstruction_embeds_its_query_once(filling, monkeypatch):
    """bug-494: the candidate recall embedded the query and reconstruct embedded it
    again for its quotes; it now reads the vector the recall kept."""
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        asked = []
        real = filling.embed

        async def counting(texts):
            asked.extend(t for t in texts if t == QUERY)
            return await real(texts)

        monkeypatch.setattr(filling, "embed", counting)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=3, deep=True)
        assert any(item.get("quote_basis") == "blocks" for item in out["items"]), "the quotes must use the vector"
        assert len(asked) == 1, asked


@pytest.mark.asyncio
async def test_one_provider_set_and_one_reading_of_the_cue(filling, monkeypatch):
    """bug-474: the candidate recall read the provider set again and parsed the cue
    again; a set installed between the two reads built the pool with one set and the
    items with another."""
    from cpersona import providers

    read = []
    real_active = providers.active

    def counting_active():
        read.append(1)
        return real_active()

    monkeypatch.setattr(providers, "active", counting_active)
    parsed = []
    from cpersona import cue

    real = cue.parse

    def counting_parse(raw):
        parsed.append(1)
        return real(raw)

    monkeypatch.setattr(cue, "parse", counting_parse)
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        cue_arg = {"ago": {"unit": "days", "value": 30}, "confidence": "vague"}
        await reconstruct.do_reconstruct(AGENT, QUERY, count=3, deep=True, time_cue=cue_arg)
    assert len(read) == 1 and len(parsed) == 1, (read, parsed)


@pytest.mark.asyncio
async def test_a_head_quoted_by_filling_reads_no_node_set(filling, monkeypatch):
    """bug-495: every head's node set, embeddings included, was read although a filled
    head quote never uses nodes. With one item and no other claims nothing asks for one."""
    seen = []
    real = reconstruct._current_sets

    async def spy(agent_id, node_claims, block_claims):
        seen.append([c.ref for c in node_claims])
        return await real(agent_id, node_claims, block_claims)

    monkeypatch.setattr(reconstruct, "_current_sets", spy)
    async with _TempDB() as tmp:
        await _store(tmp, LONG)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=1, deep=True, max_evidence=1)
        assert out["items"] and "quote_selection" not in out
    assert all(refs == [] for refs in seen), seen


@pytest.mark.asyncio
async def test_a_block_set_missing_a_vector_is_not_quoted_from(filling, monkeypatch):
    """bug-479: the reader held block sets to its own test, which did not ask for the
    re-rank vectors the builder and the sweep require -- so a set they rebuild was
    still quoted from. One rule now, and the quote falls back to dividing the text."""
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        item, _ = await _item(ids[LONG])
        assert item["quote_basis"] == "blocks"
        db = await database.get_db()
        mem_id = int(ids[LONG].split(":")[1])
        await db.execute("DELETE FROM record_block_vectors WHERE parent_id = ? AND block_index = 0", (mem_id,))
        await db.commit()
        item, _ = await _item(ids[LONG])
        assert item["quote_basis"] == "lexical"


@pytest.mark.asyncio
async def test_an_episode_head_quote_is_the_recall_excerpt_of_the_same_episode(filling, monkeypatch):
    """bug-480: the two copies of the excerpt rule had diverged; one rule now, and an
    episode -- where the display label is what diverged -- gives the same passages."""
    monkeypatch.setattr(config, "RECALL_EXCERPT_CHARS", 800)
    async with _TempDB() as tmp:
        await _store(tmp)
        archived = await memory_handlers.do_archive_episode(AGENT, [], summary=LONG)
        ref = f"ep:{archived['episode_id']}"
        await tmp.drain()
        item, _ = await _item(ref)
        recalled = await memory_handlers.do_recall(AGENT, QUERY, limit=10, deep=True, excerpt_chars=800)
        row = next(m for m in recalled["messages"] if m.get("ref") == ref)
        assert item["content"] == row["excerpt"]
        assert item["quote_basis"] == row["excerpt_basis"]


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_excerpts_read_no_record_the_recall_row_already_carries(filling):
    """bug-491: each record without a current block set was read again, one SELECT per
    ref, although its text was in the row being turned into the message."""
    async with _TempDB() as tmp:
        ids = await _store(tmp, LONG)
        reader = await database._get_read_db()
        statements: list[str] = []
        await reader.set_trace_callback(statements.append)
        try:
            found = await excerpts.for_refs(AGENT, [ids[LONG]], QUERY, None, 800, texts={ids[LONG]: LONG})
        finally:
            await reader.set_trace_callback(None)
        assert TAIL in found[ids[LONG]]["excerpt"]
        assert not [s for s in statements if "FROM memories" in s], statements
