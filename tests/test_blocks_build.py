"""Building a record's blocks on the queue: docs/BLOCK_REACH_DESIGN.md §6, §7.

The division itself is pinned by test_blocks_segment and the table by
test_record_blocks_schema. What is under test here is the path between them:
that nothing is built unless the deployment asked for it, that a set written
after the record moved underneath it is refused rather than stored, and that a
failure leaves the previous set intact rather than half of a new one.

The queue is a real ``MemoryTaskQueue`` that is never started; each test drains
it by hand, so the moment blocks appear is the moment the test chose.
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
    session,
    tasks,
)

AGENT = "agent.blocks"

#: Divides into several blocks under any policy, and short enough that no
#: boundary is forced.
MANY = "一文目です。二文目です。\n\n三文目です。四文目です。"
#: One sentence, so the division yields a single block.
ONE = "ひとつの文だけ"


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "blocks_build.db")
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

    async def rows(self, kind="mem", parent_id=None):
        db = await database.get_db()
        return await db.execute_fetchall(
            "SELECT block_index, start_char, end_char, forced_boundary, "
            "embedding_bits IS NOT NULL, embedding_model, agent_id, project_id, channel "
            "FROM record_blocks WHERE parent_kind = ? AND parent_id = ? ORDER BY block_index",
            (kind, parent_id),
        )


@pytest.fixture
def enabled(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


# --------------------------------------------------------------------------
# opt-in (§7): off means no work, not "built but unread"
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_store_queues_nothing_when_the_feature_is_off(fake_embedding_client):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        assert "blocks" not in result
        pending = await (await database.get_db()).execute_fetchall(
            "SELECT task_type FROM pending_memory_tasks"
        )
        assert "build_blocks" not in {r[0] for r in pending}
        await tmp.drain()
        assert await tmp.rows(parent_id=result["id"]) == []


@pytest.mark.asyncio
async def test_a_task_queued_before_the_gate_closed_builds_nothing(monkeypatch, enabled):
    """A queue outlives a setting change. Off must mean no rows even then."""
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        assert result["blocks"] == {"status": "queued"}

        monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", False)
        await tmp.drain()

        assert await tmp.rows(parent_id=result["id"]) == []


@pytest.mark.asyncio
async def test_building_is_disabled_without_a_client(monkeypatch):
    from cpersona import vector

    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(vector, "_embedding_client", None)
    assert blocks.building_enabled() is False


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_store_queues_and_the_drain_builds_the_blocks(enabled):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        assert result["blocks"] == {"status": "queued"}

        await tmp.drain()

        rows = await tmp.rows(parent_id=result["id"])
        assert len(rows) > 1
        assert [r[0] for r in rows] == list(range(len(rows)))
        assert rows[0][1] == 0 and rows[-1][2] == len(MANY)
        assert all(a[2] == b[1] for a, b in zip(rows, rows[1:]))
        assert all(r[4] for r in rows), "a block was written without its bits"
        assert not any(r[3] for r in rows), "nothing here should need a forced boundary"


@pytest.mark.asyncio
async def test_the_axes_are_copied_from_the_parent(enabled):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(
            AGENT, {"content": MANY}, project_id="proj", channel="chan"
        )
        await tmp.drain()
        rows = await tmp.rows(parent_id=result["id"])
        assert rows
        assert {(r[6], r[7], r[8]) for r in rows} == {(AGENT, "proj", "chan")}


@pytest.mark.asyncio
async def test_a_record_that_yields_one_block_is_not_stored(enabled):
    """Its block would be the record, and the search already has that vector."""
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": ONE})
        await tmp.drain()
        assert await tmp.rows(parent_id=result["id"]) == []


@pytest.mark.asyncio
async def test_a_second_build_finds_the_blocks_current_and_writes_nothing(enabled):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        await tmp.drain()
        before = await tmp.rows(parent_id=result["id"])

        outcome = await blocks.build_blocks({"kind": "mem", "id": result["id"]})

        assert outcome == "blocks already current"
        assert await tmp.rows(parent_id=result["id"]) == before


@pytest.mark.asyncio
async def test_a_malformed_payload_is_discarded(enabled):
    async with _TempDB():
        assert await blocks.build_blocks({"kind": "nope", "id": 1}) == "malformed payload, discarded"
        assert await blocks.build_blocks({"kind": "mem", "id": True}) == (
            "malformed payload, discarded"
        )


@pytest.mark.asyncio
async def test_a_deleted_parent_builds_nothing(enabled):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        db = await database.get_db()
        await db.execute("DELETE FROM memories WHERE id = ?", (result["id"],))
        await db.commit()

        await tmp.drain()

        assert await tmp.rows(parent_id=result["id"]) == []


# --------------------------------------------------------------------------
# the unlocked window: a set built from text that has since changed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_set_prepared_from_stale_text_is_refused(enabled):
    """The division runs without a lock. If the record is rewritten in between,
    writing the set anyway would leave blocks quoting text that is gone — and
    the triggers cannot help, because they only remove blocks that existed when
    the rewrite happened."""
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        mem_id = result["id"]
        prepared = await blocks.prepare_blocks("mem", mem_id, MANY, ())
        assert prepared is not None

        db = await database.get_db()
        await db.execute("UPDATE memories SET content = ? WHERE id = ?", ("別の本文。", mem_id))
        await db.commit()

        from cpersona.database import transaction

        async with transaction() as tx:
            assert await blocks.write_blocks(tx, prepared) is False

        assert await tmp.rows(parent_id=mem_id) == []


@pytest.mark.asyncio
async def test_a_rewrite_during_the_drain_leaves_no_blocks_behind(enabled):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        await tmp.drain()
        assert await tmp.rows(parent_id=result["id"])

        db = await database.get_db()
        await db.execute(
            "UPDATE memories SET content = ? WHERE id = ?", ("まったく別の本文。", result["id"])
        )
        await db.commit()

        assert await tmp.rows(parent_id=result["id"]) == [], "the trigger did not fire"


@pytest.mark.asyncio
async def test_a_failed_embedding_leaves_the_previous_set_intact(enabled, monkeypatch):
    """A partial set is never published: the request raises, the queue retries,
    and what was already stored is still what the search sees."""
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        await tmp.drain()
        before = await tmp.rows(parent_id=result["id"])
        assert before

        async def broken(texts):
            raise RuntimeError("the embedding server is down")

        monkeypatch.setattr(enabled, "embed", broken)
        db = await database.get_db()
        await db.execute(
            "UPDATE memories SET content = ? WHERE id = ?", (MANY + "五文目です。", result["id"])
        )
        await db.commit()
        # The rewrite dropped the old set; a build that fails must not replace it
        # with a partial one.
        with pytest.raises(RuntimeError, match="embedding server"):
            await blocks.build_blocks({"kind": "mem", "id": result["id"]})

        assert await tmp.rows(parent_id=result["id"]) == []


@pytest.mark.asyncio
async def test_a_vector_refused_for_storage_stops_the_build(enabled, monkeypatch):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})

        async def nan_vectors(texts):
            return [[float("nan")] * 8 for _ in texts]

        monkeypatch.setattr(enabled, "embed", nan_vectors)
        with pytest.raises(RuntimeError, match="refused for storage"):
            await blocks.build_blocks({"kind": "mem", "id": result["id"]})

        assert await tmp.rows(parent_id=result["id"]) == []


# --------------------------------------------------------------------------
# model identity (§6): unknown is unknown
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreported_model_is_stored_as_unknown_not_as_the_default(
    enabled, monkeypatch
):
    """On the http transport this process cannot learn the backend's model name.
    Writing the resolved default there would let a model swap read as a match,
    because every row would carry the same constant either way."""
    monkeypatch.setattr(config, "reported_embedding_model", lambda: "")
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        await tmp.drain()

        rows = await tmp.rows(parent_id=result["id"])
        assert rows
        assert {r[5] for r in rows} == {""}
        assert config.EMBEDDING_MODEL not in {r[5] for r in rows}


@pytest.mark.asyncio
async def test_blocks_built_under_another_model_are_not_current(enabled, monkeypatch):
    async with _TempDB() as tmp:
        monkeypatch.setattr(config, "reported_embedding_model", lambda: "model-a")
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        await tmp.drain()
        assert {r[5] for r in await tmp.rows(parent_id=result["id"])} == {"model-a"}

        monkeypatch.setattr(config, "reported_embedding_model", lambda: "model-b")
        outcome = await blocks.build_blocks({"kind": "mem", "id": result["id"]})

        assert outcome.startswith("built ")
        assert {r[5] for r in await tmp.rows(parent_id=result["id"])} == {"model-b"}


@pytest.mark.asyncio
async def test_an_unknown_model_is_current_against_itself(enabled, monkeypatch):
    """Empty equals empty: an unreported deployment stays consistent with itself
    rather than rebuilding every record on every pass."""
    monkeypatch.setattr(config, "reported_embedding_model", lambda: "")
    async with _TempDB() as tmp:
        result = await memory_handlers.do_store(AGENT, {"content": MANY})
        await tmp.drain()

        outcome = await blocks.build_blocks({"kind": "mem", "id": result["id"]})

        assert outcome == "blocks already current"


# --------------------------------------------------------------------------
# quantisation
# --------------------------------------------------------------------------


def test_pack_bits_is_one_bit_per_dimension_most_significant_first():
    assert blocks.pack_bits([1.0, -1.0, 1.0, -1.0, -1.0, -1.0, -1.0, -1.0]) == b"\xa0"
    assert blocks.pack_bits([-1.0] * 8) == b"\x00"
    assert blocks.pack_bits([1.0] * 8) == b"\xff"


def test_pack_bits_treats_zero_as_not_positive():
    assert blocks.pack_bits([0.0] * 8) == b"\x00"


def test_pack_bits_refuses_what_storage_refuses():
    assert blocks.pack_bits([float("nan")] * 8) is None
    assert blocks.pack_bits([]) is None
