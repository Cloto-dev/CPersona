"""The re-rank of the block arm's best Hamming rows: docs/BLOCK_REACH_DESIGN.md §4b.

The Hamming pass itself is pinned by test_blocks_retrieval and the reservation
that admits its hits by the same file. What is under test here is the step
between them: that each block keeps a vector the re-rank can read, that the
vector lives and dies with its block, that the re-rank orders by that vector
rather than by the distance, and that a set part-way through being rebuilt
falls back to the previous release's order instead of mixing two scales.
"""

import os
import tempfile

import numpy as np
import pytest

from cpersona import (
    admin_handlers,
    blocks,
    checks,
    config,
    database,
    memory_handlers,
    nodes,
    session,
    tasks,
)
from cpersona.database import SCHEMA_VERSION
from cpersona.isolation import isolation_where

AGENT = "agent.rerank"

#: Divides into several blocks, short enough that no boundary is forced.
MANY = "一文目です。二文目です。\n\n三文目です。四文目です。"


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "blocks_rerank.db")
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


async def _store(tmp, text, agent=AGENT) -> int:
    mem_id = (await memory_handlers.do_store(agent, {"content": text}))["id"]
    await tmp.drain()
    return mem_id


async def _count(db, table: str, parent_id: int) -> int:
    rows = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM {table} WHERE parent_kind = 'mem' AND parent_id = ?",
        (parent_id,),
    )
    return rows[0][0]


# --------------------------------------------------------------------------
# the stored vector
# --------------------------------------------------------------------------


def test_pack_int8_puts_the_largest_component_at_full_scale():
    packed = np.frombuffer(blocks.pack_int8([0.5, -2.0, 1.0, 0.0]), dtype=np.int8)
    assert packed.tolist() == [32, -127, 64, 0]


def test_pack_int8_refuses_a_vector_with_no_direction():
    assert blocks.pack_int8([0.0, 0.0, 0.0, 0.0]) is None


def test_pack_int8_refuses_what_storage_refuses():
    assert blocks.pack_int8([0.1, float("nan"), 0.2, 0.3]) is None
    assert blocks.pack_int8([]) is None


def test_int8_keeps_the_cosine_the_rerank_reads():
    """The scale is dropped because a cosine cannot see it. What is left is the
    rounding, which has to stay far below the gaps the re-rank separates."""
    rng = np.random.default_rng(1709)
    worst = 0.0
    for _ in range(200):
        a, b = rng.normal(size=1024), rng.normal(size=1024)
        exact = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
        qa = np.frombuffer(blocks.pack_int8(a.tolist()), dtype=np.int8).astype(np.float64)
        approx = float(qa @ b / (np.linalg.norm(qa) * np.linalg.norm(b)))
        worst = max(worst, abs(exact - approx))
    assert worst < 0.01


@pytest.mark.asyncio
async def test_a_build_stores_one_vector_per_block(building):
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        db = await database.get_db()
        n_blocks = await _count(db, "record_blocks", mem)
        assert n_blocks > 1
        rows = await db.execute_fetchall(
            "SELECT block_index, embedding_i8 FROM record_block_vectors "
            "WHERE parent_kind = 'mem' AND parent_id = ? ORDER BY block_index",
            (mem,),
        )
        assert [r[0] for r in rows] == list(range(n_blocks))
        width = len((await db.execute_fetchall(
            "SELECT embedding_bits FROM record_blocks WHERE parent_id = ? LIMIT 1", (mem,)
        ))[0][0]) * 8
        assert all(len(r[1]) == width for r in rows)


@pytest.mark.asyncio
async def test_a_deleted_record_takes_its_vectors_with_it(building):
    async with _TempDB() as tmp:
        doomed = await _store(tmp, MANY)
        spared = await _store(tmp, MANY + "五文目です。")
        await admin_handlers.do_delete_memory(doomed, agent_id=AGENT)
        db = await database.get_db()
        assert await _count(db, "record_block_vectors", doomed) == 0
        assert await _count(db, "record_block_vectors", spared) > 0


@pytest.mark.asyncio
async def test_a_rewritten_record_takes_its_vectors_with_it(building):
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        db = await database.get_db()
        await db.execute("UPDATE memories SET content = ? WHERE id = ?", ("別の本文。", mem))
        await db.commit()
        assert await _count(db, "record_block_vectors", mem) == 0


@pytest.mark.asyncio
async def test_a_retag_keeps_the_vectors(building):
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        db = await database.get_db()
        before = await _count(db, "record_block_vectors", mem)
        await db.execute("UPDATE memories SET project_id = 'moved' WHERE id = ?", (mem,))
        await db.commit()
        assert await _count(db, "record_block_vectors", mem) == before > 0


@pytest.mark.asyncio
async def test_a_set_missing_a_vector_is_not_current_and_is_rebuilt(building):
    """A set built before the vectors existed must be rebuilt, not half-used —
    otherwise every query touching it falls back to Hamming order for good."""
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        db = await database.get_db()
        await db.execute(
            "DELETE FROM record_block_vectors WHERE parent_id = ? AND block_index = 1", (mem,)
        )
        await db.commit()
        n_blocks = await _count(db, "record_blocks", mem)

        outcome = await blocks.build_blocks({"kind": "mem", "id": mem})

        assert outcome == f"built {n_blocks} blocks"
        assert await _count(db, "record_block_vectors", mem) == n_blocks


@pytest.mark.asyncio
async def test_a_complete_set_is_still_current(building):
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        assert await blocks.build_blocks({"kind": "mem", "id": mem}) == "blocks already current"


@pytest.mark.asyncio
async def test_the_sweep_counts_a_set_missing_a_vector_as_stale(building):
    """The sweep and the builder read currentness through one aggregate; this
    pins the sweep's half of it."""
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        db = await database.get_db()
        keys = blocks.generation.block_keys()
        text_len = len(MANY)
        sets = await blocks._sets_for(db, "mem", [mem], keys)
        assert blocks._set_is_current(*sets[mem], text_len)
        await db.execute("DELETE FROM record_block_vectors WHERE parent_id = ? AND block_index = 0", (mem,))
        await db.commit()
        sets = await blocks._sets_for(db, "mem", [mem], keys)
        assert not blocks._set_is_current(*sets[mem], text_len)


# --------------------------------------------------------------------------
# the depth: a fixed number of rows, cut in the written-down order
# --------------------------------------------------------------------------


def _bits(ones: int, width_bytes: int = 2) -> bytes:
    """A bit string whose first ``ones`` bits are set."""
    value = [1] * ones + [0] * (width_bytes * 8 - ones)
    return np.packbits(np.array(value, dtype=np.uint8)).tobytes()


def test_the_depth_is_cut_on_rows_in_the_written_down_order(monkeypatch):
    monkeypatch.setattr(blocks, "BLOCK_RERANK_DEPTH", 3)
    query = _bits(0)
    rows = [
        ("mem", 9, 0, _bits(1)),  # distance 1
        ("mem", 2, 0, _bits(2)),  # distance 2, tied with the next two
        ("ep", 7, 1, _bits(2)),
        ("mem", 2, 1, _bits(2)),
        ("mem", 1, 0, _bits(5)),  # farther than the cut
    ]
    near = blocks._hamming_order(rows, query)
    # Ties at the cut go by kind, parent id, block index: "ep" sorts before "mem".
    assert [(r[0], r[1], r[2], d) for r, d in near] == [
        ("mem", 9, 0, 1),
        ("ep", 7, 1, 2),
        ("mem", 2, 0, 2),
    ]


def test_the_depth_skips_rows_of_another_width():
    query = _bits(0)
    rows = [("mem", 1, 0, _bits(1)), ("mem", 2, 0, b"\x00")]
    assert [r[1] for r, _ in blocks._hamming_order(rows, query)] == [1]


# --------------------------------------------------------------------------
# the order: by the stored vector, collapsed to the parent
# --------------------------------------------------------------------------


def _i8(values) -> bytes:
    return np.asarray(values, dtype=np.int8).tobytes()


_QUERY = [1.0] + [0.0] * 15


def test_the_rerank_orders_by_the_stored_vector_not_the_distance():
    """Parent 1 is nearer in Hamming and farther in angle; the re-rank must put
    parent 2 first. Removing the re-rank leaves the Hamming order, which fails."""
    near = [(("mem", 1, 0, _bits(0)), 0), (("mem", 2, 0, _bits(3)), 3)]
    stored = {
        ("mem", 1, 0): _i8([10, 100] + [0] * 14),  # cosine ~0.10
        ("mem", 2, 0): _i8([100, 10] + [0] * 14),  # cosine ~0.99
    }
    hits = blocks._rerank(near, stored, _QUERY)
    assert [(h.parent_id, h.distance) for h in hits] == [(2, 3), (1, 0)]
    assert hits[0].cosine > 0.99 > hits[1].cosine


def test_a_parent_takes_its_best_block_and_nothing_is_summed():
    """Parent 1 has two middling blocks whose cosines sum past parent 2's one
    good block; summing would put 1 first."""
    near = [
        (("mem", 1, 0, _bits(0)), 0),
        (("mem", 1, 1, _bits(0)), 0),
        (("mem", 2, 0, _bits(0)), 0),
    ]
    stored = {
        ("mem", 1, 0): _i8([60, 80] + [0] * 14),  # 0.6
        ("mem", 1, 1): _i8([80, 60] + [0] * 14),  # 0.8
        ("mem", 2, 0): _i8([90, 40] + [0] * 14),  # ~0.91
    }
    hits = blocks._rerank(near, stored, _QUERY)
    assert [(h.parent_id, h.block_index) for h in hits] == [(2, 0), (1, 1)]


def test_equal_cosines_go_by_kind_parent_and_block():
    same = _i8([100] + [0] * 15)
    near = [
        (("mem", 3, 2, _bits(0)), 0),
        (("mem", 3, 1, _bits(0)), 0),
        (("ep", 8, 0, _bits(0)), 0),
        (("mem", 1, 0, _bits(0)), 0),
    ]
    stored = {(r[0], r[1], r[2]): same for r, _ in near}
    hits = blocks._rerank(near, stored, _QUERY)
    assert [(h.kind, h.parent_id, h.block_index) for h in hits] == [
        ("ep", 8, 0),
        ("mem", 1, 0),
        ("mem", 3, 1),
    ]


def test_one_missing_vector_declines_the_whole_rerank():
    near = [(("mem", 1, 0, _bits(0)), 0), (("mem", 2, 0, _bits(1)), 1)]
    assert blocks._rerank(near, {("mem", 1, 0): _i8([100] + [0] * 15)}, _QUERY) is None


def test_a_vector_of_another_width_declines_the_whole_rerank():
    near = [(("mem", 1, 0, _bits(0)), 0)]
    assert blocks._rerank(near, {("mem", 1, 0): _i8([100] * 8)}, _QUERY) is None


def test_a_stored_vector_with_no_direction_declines_the_whole_rerank():
    """The builder never writes one, but a row is data, not a promise: a zero
    vector has no cosine, and letting it through would put a NaN in the order."""
    near = [(("mem", 1, 0, _bits(0)), 0), (("mem", 2, 0, _bits(1)), 1)]
    stored = {("mem", 1, 0): _i8([100] + [0] * 15), ("mem", 2, 0): _i8([0] * 16)}
    assert blocks._rerank(near, stored, _QUERY) is None


def test_a_query_of_another_width_declines_the_whole_rerank():
    near = [(("mem", 1, 0, _bits(0)), 0)]
    assert blocks._rerank(near, {("mem", 1, 0): _i8([100] + [0] * 15)}, [1.0] * 8) is None


# --------------------------------------------------------------------------
# search: the re-rank when every row has its vector, Hamming otherwise
# --------------------------------------------------------------------------


async def _search(query: str):
    db = await database.get_db()
    vec = (await blocks.vector._embedding_client.embed([query]))[0]
    return await blocks.search(db, vec, isolation_where(agent_id=AGENT))


@pytest.mark.asyncio
async def test_search_reranks_when_every_vector_is_there(building):
    async with _TempDB() as tmp:
        await _store(tmp, MANY)
        await _store(tmp, "五文目です。六文目です。\n\n七文目です。")
        hits = await _search("三文目です。")
        assert hits and all(h.cosine is not None for h in hits)
        cosines = [h.cosine for h in hits]
        assert cosines == sorted(cosines, reverse=True)


@pytest.mark.asyncio
async def test_search_falls_back_to_the_previous_order_when_a_vector_is_missing(building):
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        await _store(tmp, "五文目です。六文目です。\n\n七文目です。")
        db = await database.get_db()
        await db.execute("DELETE FROM record_block_vectors WHERE parent_id = ? AND block_index = 0", (mem,))
        await db.commit()
        query = "三文目です。"
        vec = (await blocks.vector._embedding_client.embed([query]))[0]
        rows = await blocks._examined(db, isolation_where(agent_id=AGENT), blocks.generation.block_keys())

        hits = await _search(query)

        assert hits == blocks._hamming(rows, blocks.pack_bits(vec))
        assert all(h.cosine is None for h in hits)


@pytest.mark.asyncio
async def test_the_response_says_which_order_filled_the_places(building, monkeypatch):
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)
    async with _TempDB() as tmp:
        filler = "\n\n".join(
            f"paragraph {i} about budget headcount procurement logistics" for i in range(40)
        )
        await _store(tmp, f"{filler}\n\nzzarquon nebulite flimsy was decided against")
        out = await memory_handlers.do_recall(AGENT, "zzarquon nebulite flimsy", limit=3)
        reserved = [m for m in out["messages"] if (m.get("match_reason") or {}).get("signal") == "block"]
        assert reserved and reserved[0]["match_reason"]["order"] == "vector"
        assert "cosine" not in reserved[0]["match_reason"], "a score was offered where a distance is"

        db = await database.get_db()
        await db.execute("DELETE FROM record_block_vectors WHERE block_index = 0")
        await db.commit()
        out = await memory_handlers.do_recall(AGENT, "zzarquon nebulite flimsy", limit=3)
        reserved = [m for m in out["messages"] if (m.get("match_reason") or {}).get("signal") == "block"]
        assert reserved and reserved[0]["match_reason"]["order"] == "hamming"


# --------------------------------------------------------------------------
# schema (v17)
# --------------------------------------------------------------------------


def test_schema_version_is_at_least_17():
    assert SCHEMA_VERSION >= 17


@pytest.mark.asyncio
async def test_a_v16_database_gains_the_table_and_keeps_its_blocks(building):
    async with _TempDB() as tmp:
        mem = await _store(tmp, MANY)
        db = await database.get_db()
        n_blocks = await _count(db, "record_blocks", mem)
        # A v16 database: everything v17 added removed, stamped 16.
        await db.execute("DROP TABLE record_block_vectors")
        await db.execute("DROP TRIGGER record_block_vectors_ad")
        await db.execute("DELETE FROM schema_version")
        await db.execute("INSERT INTO schema_version (version) VALUES (16)")
        await db.commit()

        await database.close_db()
        database._db = None
        db = await database.get_db()

        assert (await db.execute_fetchall("SELECT MAX(version) FROM schema_version"))[0][0] == SCHEMA_VERSION
        assert await _count(db, "record_blocks", mem) == n_blocks
        assert await _count(db, "record_block_vectors", mem) == 0
        # The blocks it kept are not current, so the sweep rebuilds them.
        assert await blocks.build_blocks({"kind": "mem", "id": mem}) == f"built {n_blocks} blocks"
        # And the migrated trigger works, not merely exists.
        await db.execute("DELETE FROM record_blocks WHERE parent_id = ?", (mem,))
        await db.commit()
        assert await _count(db, "record_block_vectors", mem) == 0


@pytest.mark.asyncio
async def test_check_schema_objects_reports_and_repairs_the_vector_trigger():
    async with _TempDB():
        db = await database.get_db()
        await db.execute("DROP TRIGGER record_block_vectors_ad")
        await db.commit()

        issues = await checks.check_schema_objects(db, "", fix=False)
        missing = [i for i in issues if i["object"] == "record_block_vectors_ad"]
        assert missing and missing[0]["state"] == "missing" and missing[0]["severity"] == "critical"

        issues = await checks.check_schema_objects(db, "", fix=True)
        repaired = [i for i in issues if i["object"] == "record_block_vectors_ad"]
        assert repaired and repaired[0]["fixed"] is True
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'record_block_vectors_ad'"
        )
        assert rows


@pytest.mark.asyncio
async def test_a_fresh_database_reports_nothing_about_the_vector_trigger():
    """The expected DDL in checks.py must match what the boot actually creates,
    or every healthy deployment reports a drifted trigger."""
    async with _TempDB():
        db = await database.get_db()
        issues = await checks.check_schema_objects(db, "", fix=False)
        assert not [i for i in issues if i["object"] == "record_block_vectors_ad"]
