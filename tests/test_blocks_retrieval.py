"""The block arm and its reservation: docs/BLOCK_REACH_DESIGN.md §4, §5, §7.

The division is pinned by test_blocks_segment, the table by
test_record_blocks_schema, and the build by test_blocks_build and
test_blocks_backfill. What is under test here is what recall does with the
index: that a record its own vector cannot reach arrives anyway, that it
arrives in a place nothing else was using, and that no number from this arm
reaches a gate calibrated on another population.
"""

import os
import tempfile

import pytest

from cpersona import (
    admin_handlers,
    blocks,
    config,
    database,
    generation,
    memory_handlers,
    nodes,
    session,
    tasks,
)
from cpersona.isolation import isolation_where

AGENT = "agent.reach"

#: The subject of the record's last paragraph, and of the query. It appears
#: nowhere else in the corpus.
TAIL_SUBJECT = "zzarquon nebulite flimsy"

#: Forty paragraphs about something else, so that the whole-record vector is a
#: poor match for a query about the tail — which is the defect this arm exists
#: for, reproduced rather than asserted. Space-separated because the test
#: double embeds a bag of tokens; blank lines because that is where the divider
#: cuts.
_FILLER = "\n\n".join(
    f"paragraph {i} about budget headcount procurement logistics warehouse staffing rota"
    for i in range(40)
)
LONG_RECORD = f"{_FILLER}\n\n{TAIL_SUBJECT} was decided against for the pilot"

#: A short record that says only the tail subject. Its own vector matches the
#: query exactly, so the quality gate admits it — it is the row the reservation
#: must not displace.
ECHO_RECORD = TAIL_SUBJECT

#: Two sentences, so it has blocks, and short enough that its own vector clears
#: the gate. Both arms reach it, which is the only shape in which "a record the
#: gate admitted is not reserved for as well" can be false.
BOTH_WAYS_RECORD = f"{TAIL_SUBJECT} was decided against. the pilot ran in march."


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "blocks_retrieval.db")
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
def building(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


@pytest.fixture
def reading(monkeypatch, building):
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    return building


@pytest.fixture
def lexical_off(monkeypatch):
    """Switch the FTS arm off for the reach tests.

    The lexical arm reaches this record: a query in the tail's own vocabulary
    finds it by keyword whether or not a block index exists, which this project
    measured before building one. Leaving it on would make these tests pass
    with the arm under test removed — so it is off, and what is left is the
    semantic path the tail was unreachable on.
    """
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)


async def _store(tmp, texts, agent=AGENT):
    ids = []
    for text in texts:
        ids.append((await memory_handlers.do_store(agent, {"content": text}))["id"])
    await tmp.drain()
    return ids


def _refs(result):
    return [m.get("ref") for m in result["messages"]]


# --------------------------------------------------------------------------
# ranking: one record, one candidate (§4)
# --------------------------------------------------------------------------


def _row(kind, parent_id, block_index, bits):
    return (kind, parent_id, block_index, bits)


def test_a_records_blocks_collapse_to_one_hit_at_its_best():
    query = blocks.pack_bits([1.0] * 16)
    near = blocks.pack_bits([1.0] * 15 + [-1.0])
    far = blocks.pack_bits([-1.0] * 16)
    hits = blocks._hamming(
        [_row("mem", 1, 0, far), _row("mem", 1, 1, near), _row("mem", 2, 0, far)], query
    )
    assert [(h.parent_id, h.block_index, h.distance) for h in hits] == [(1, 1, 1), (2, 0, 16)]


def test_the_distances_of_a_records_other_blocks_are_not_added():
    """Blocks of one record are correlated observations of one source. Summing
    them would make the length of a record into evidence: the record with ten
    middling blocks would outrank the one with a single exact match."""
    query = blocks.pack_bits([1.0] * 16)
    middling = blocks.pack_bits([1.0] * 8 + [-1.0] * 8)
    exact = blocks.pack_bits([1.0] * 16)
    rows = [_row("mem", 1, i, middling) for i in range(10)] + [_row("mem", 2, 0, exact)]
    hits = blocks._hamming(rows, query)
    assert [h.parent_id for h in hits] == [2, 1], "a record was scored by how many blocks it has"
    assert [h.distance for h in hits] == [0, 8]


def test_a_block_of_another_width_is_skipped_rather_than_compared():
    """A different width is a different dimension. A distance between the two
    would be a number with no meaning rather than a large one."""
    query = blocks.pack_bits([1.0] * 16)
    hits = blocks._hamming(
        [_row("mem", 1, 0, b"\x00"), _row("mem", 2, 0, blocks.pack_bits([1.0] * 16))], query
    )
    assert [h.parent_id for h in hits] == [2]


def test_ties_are_broken_by_a_written_down_total_order():
    query = blocks.pack_bits([1.0] * 16)
    same = blocks.pack_bits([1.0] * 16)
    hits = blocks._hamming(
        [_row("mem", 2, 0, same), _row("ep", 5, 0, same), _row("mem", 1, 0, same)], query
    )
    assert [(h.kind, h.parent_id) for h in hits] == [("ep", 5), ("mem", 1), ("mem", 2)]


@pytest.mark.asyncio
async def test_the_per_parent_cap_bounds_what_one_record_takes(monkeypatch, reading):
    """One long record must not be able to spend the examined set on itself."""
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD])
        monkeypatch.setattr(blocks, "BLOCK_PER_PARENT_CAP", 1)
        db = await database.get_db()
        rows = await blocks._examined(
            db, isolation_where(agent_id=AGENT), generation.block_keys()
        )
        assert len(rows) == 1, "the cap did not bound one parent's share"


# --------------------------------------------------------------------------
# reach: the record its own vector cannot bring back
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tail_is_out_of_reach_without_the_arm(building, lexical_off):
    """The defect, reproduced: the record holding the answer is not merely
    ranked low, it never reaches the gate at all. A fix measured against a
    baseline that was already returning the row would prove nothing."""
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        pool: list[list] = []
        original = memory_handlers._apply_quality_gate

        def spy(results, *args, **kwargs):
            pool.append([r.get("id") for r in results])
            return original(results, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(memory_handlers, "_apply_quality_gate", spy)
            out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3)

        assert pool and 1 not in pool[0], "the record reached the gate, so nothing was out of reach"
        assert _refs(out) == ["mem:2"]


@pytest.mark.asyncio
async def test_the_arm_brings_the_tail_back(reading, lexical_off):
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3)
        assert "mem:1" in _refs(out)


@pytest.mark.asyncio
async def test_a_reserved_row_says_why_it_is_here(reading, lexical_off):
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3)
        reserved = [m for m in out["messages"] if m.get("ref") == "mem:1"][0]
        reason = reserved["match_reason"]
        assert reason["signal"] == "block" and reason["admission"] == "reservation"
        assert isinstance(reason["hamming"], int) and reason["block"] >= 0
        assert "score" not in reason, "a distance was offered where the gate's scores are read"


# --------------------------------------------------------------------------
# the reservation displaces nothing (§5)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turning_the_arm_on_adds_rows_and_removes_none(monkeypatch, building, lexical_off):
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        before = _refs(await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3))
        assert before, "the baseline returned nothing, so 'removes none' is vacuous"

        monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
        after = _refs(await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3))

        assert set(before) < set(after), "a row the gate admitted was displaced"


@pytest.mark.asyncio
async def test_the_reservation_is_a_fixed_bound(monkeypatch, reading, lexical_off):
    async with _TempDB() as tmp:
        await _store(
            tmp,
            [
                LONG_RECORD,
                LONG_RECORD.replace("pilot", "trial"),
                LONG_RECORD.replace("pilot", "rollout"),
                ECHO_RECORD,
            ],
        )
        monkeypatch.setattr(blocks, "BLOCK_RESERVATION", 1)
        out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=1)
        reserved = [m for m in out["messages"] if m.get("match_reason", {}).get("signal") == "block"]
        assert len(reserved) == 1


@pytest.mark.asyncio
async def test_an_unfilled_reservation_leaves_the_result_shorter(reading, lexical_off):
    """Not padded: a reservation nothing reached is simply not filled."""
    async with _TempDB() as tmp:
        await _store(tmp, [ECHO_RECORD])
        out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=5)
        assert _refs(out) == ["mem:1"]


@pytest.mark.asyncio
async def test_a_record_the_gate_already_admitted_is_not_reserved_for(reading, lexical_off):
    """The reservation exists to bring a record in, not to list it twice.

    The record here is reached by both arms — its own vector clears the gate and
    its blocks rank well — which is the only shape in which this can go wrong.
    """
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, BOTH_WAYS_RECORD])
        db = await database.get_db()
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM record_blocks WHERE parent_id = 2"
        )
        assert rows[0][0] > 1, "the fixture's second record has no blocks to be reached by"

        refs = _refs(await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=5))

        assert refs.count("mem:2") == 1, "a record the gate admitted was also reserved for"
        assert refs.count("mem:1") == 1


# --------------------------------------------------------------------------
# no new score reaches the gate (invariant 7), and the count decides nothing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_block_row_is_shown_to_the_quality_gate(monkeypatch, reading, lexical_off):
    """The gate scores the parent, and the parent is exactly what scores badly.
    A block distance pushed into it would govern a different population at the
    same threshold -- comparing a relative quantity against an absolute one."""
    # The values, not the rows: the response builder strips these keys off the
    # very dicts a spy would have kept, so a recorded row read after the call
    # answers for the state it was left in rather than the state it was seen in.
    seen: list[list] = []
    original = memory_handlers._apply_quality_gate

    def spy(results, *args, **kwargs):
        seen.append([r.get("_block_distance") for r in results])
        return original(results, *args, **kwargs)

    monkeypatch.setattr(memory_handlers, "_apply_quality_gate", spy)
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3)

    assert "mem:1" in _refs(out), "the arm found nothing, so the gate saw nothing to leak"
    assert seen, "the gate never ran"
    for batch in seen:
        assert all(distance is None for distance in batch)


@pytest.mark.asyncio
async def test_changing_the_count_alone_leaves_the_examined_set_unchanged(
    monkeypatch, reading, lexical_off
):
    """Invariant 5. A cap or a reservation read off the response count would
    make the search depend on how many rows the caller wanted."""
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        examined: list[list] = []
        original = blocks._examined

        async def spy(db, iso, model):
            rows = await original(db, iso, model)
            examined.append([(r[0], r[1], r[2]) for r in rows])
            return rows

        monkeypatch.setattr(blocks, "_examined", spy)
        for limit in (1, 3, 10):
            await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=limit)

        assert len(examined) == 3, "the arm did not run on every call"
        assert examined[0] == examined[1] == examined[2]
        assert examined[0], "the examined set was empty, so equality proves nothing"


# --------------------------------------------------------------------------
# the arm is bound by everything that binds the others
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_examined_set_holds_no_other_agents_rows(reading, lexical_off):
    """Two guards sit on this, and the outer one hides the inner.

    The hydrate re-applies isolation fail-closed, so another agent's record
    cannot reach a caller even if the index offers it — which means the test
    that watches the answer cannot tell whether the index filtered at all. The
    filter's own job is to keep the Hamming cut off rows the authority will
    drop, and that is visible only here, in what was examined.
    """
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD], agent="agent.other")
        await _store(tmp, [LONG_RECORD.replace("pilot", "trial")])
        db = await database.get_db()
        rows = await blocks._examined(
            db, isolation_where(agent_id=AGENT), generation.block_keys()
        )

        assert rows, "nothing was examined, so the filter proves nothing"
        assert {r[1] for r in rows} == {2}, "the cut was spent on another agent's rows"


@pytest.mark.asyncio
async def test_another_agents_record_is_never_reserved(reading, lexical_off):
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD], agent="agent.other")
        await _store(tmp, [ECHO_RECORD])
        assert _refs(await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=5)) == ["mem:2"]


@pytest.mark.asyncio
async def test_an_episode_is_reserved_for_like_a_memory(reading, lexical_off):
    """Episodes are half of what the index holds, and they hydrate from another
    table through another branch. Without this the branch could be broken in
    any way and every other test here would still pass."""
    async with _TempDB() as tmp:
        result = await memory_handlers.do_archive_episode(AGENT, [], summary=LONG_RECORD)
        await tmp.drain()
        db = await database.get_db()
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM record_blocks WHERE parent_kind = 'ep'"
        )
        assert rows[0][0] > 1, "the fixture built no episode blocks"

        out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3)

        episode = [m for m in out["messages"] if m.get("ref") == f"ep:{result['episode_id']}"]
        assert episode, "an episode the block arm reached was not reserved for"
        assert episode[0]["match_reason"]["signal"] == "block"
        assert episode[0]["content"].startswith("[Episode] ")


@pytest.mark.asyncio
async def test_a_source_scoped_recall_reserves_no_episode(reading, lexical_off):
    """The rule the fused arms already apply: episodes carry no per-user source
    tag, so a source-scoped recall sees them only when a channel scopes them
    too. A reservation that ignored it would hand one user another's session."""
    async with _TempDB() as tmp:
        await memory_handlers.do_archive_episode(AGENT, [], summary=LONG_RECORD)
        await tmp.drain()

        out = await memory_handlers.do_recall(
            AGENT, TAIL_SUBJECT, limit=3, source_id="discord:12345"
        )

        assert not [m for m in out["messages"] if str(m.get("ref", "")).startswith("ep:")]


@pytest.mark.asyncio
async def test_the_arm_ranks_against_the_vector_the_search_embedded(reading, lexical_off):
    """The query is embedded once, and the arm reads it through the search's
    out-parameter. If that parameter stopped being filled the arm would rank
    against nothing and quietly find nothing, which is the failure this catches
    at the seam rather than through its consequences."""
    seen: list = []
    original = blocks.search

    async def spy(db, embedding, iso):
        seen.append(embedding)
        return await original(db, embedding, iso)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(blocks, "search", spy)
        async with _TempDB() as tmp:
            await _store(tmp, [LONG_RECORD, ECHO_RECORD])
            await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3)

    assert seen, "the arm was never handed a query vector"
    assert blocks.pack_bits(seen[0]) is not None, "the arm was handed something it cannot quantise"


@pytest.mark.asyncio
async def test_a_stale_axis_copy_does_not_let_a_record_through(reading, lexical_off):
    """The copies on a block row are a filter, not an authority.

    They exist so the Hamming cut is not spent on rows the authority will drop.
    A copy that is wrong — a retag whose trigger did not reach these rows, an
    index written by an older version — must cost a recall nothing but the work,
    so the hydrate re-applies the real predicate against the record itself.
    """
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD], agent="agent.other")
        await _store(tmp, [ECHO_RECORD])
        db = await database.get_db()
        # Exactly the state a missed retag leaves: blocks offering another
        # agent's record to this one.
        await db.execute(
            "UPDATE record_blocks SET agent_id = ? WHERE parent_id = 1", (AGENT,)
        )
        await db.commit()

        refs = _refs(await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=5))

        assert "mem:1" not in refs, "the index's own copy admitted a record the authority does not"
        assert refs == ["mem:2"]


@pytest.mark.asyncio
async def test_an_excluded_content_is_not_reserved(reading, lexical_off):
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        out = await memory_handlers.do_recall(
            AGENT, TAIL_SUBJECT, limit=5, exclude_contents=[LONG_RECORD]
        )
        assert "mem:1" not in _refs(out)


@pytest.mark.asyncio
async def test_a_reserved_row_earns_no_recall_count_credit(reading, lexical_off):
    """A reserved row is a disclosure, not a confirmed hit. Crediting it would
    raise its own decay floor until it began passing gates on other queries."""
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        assert "mem:1" in _refs(await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3))
        db = await database.get_db()
        rows = await db.execute_fetchall("SELECT id, recall_count FROM memories ORDER BY id")
        assert dict(rows)[1] == 0, "a reserved row was credited with a recall"


@pytest.mark.asyncio
async def test_with_the_arm_off_the_index_changes_no_answer(building, lexical_off):
    """Invariant 2: the rows exist and nothing reads them."""
    async with _TempDB() as tmp:
        await _store(tmp, [LONG_RECORD, ECHO_RECORD])
        db = await database.get_db()
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM record_blocks")
        assert rows[0][0] > 1, "the fixture built no blocks, so it proves nothing"

        out = await memory_handlers.do_recall(AGENT, TAIL_SUBJECT, limit=3)
        assert _refs(out) == ["mem:2"]


def test_reading_without_building_is_a_startup_error(monkeypatch):
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", False)
    with pytest.raises(ValueError, match="no block index to read"):
        config.validate_block_gates()

    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    config.validate_block_gates()
