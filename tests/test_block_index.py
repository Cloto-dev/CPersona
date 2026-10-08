"""The block index file returns what the SQLite read returns (docs/BLOCK_CANDIDATES_CONTRACT.md §3).

The file is only worth having if the answer does not move, so most of this file
compares the two reads row for row: the golden's cases through the file, then
random writes after a build, under caps that bind and caps that do not, with the
file read in chunks small enough that records run across their boundaries. The
rest pins each state in which the file must be refused rather than read: the
changes it needs pruned, the log off or restarted, a schema change, a file newer
than the snapshot, too many changed rows, a value it cannot hold, and a build
that would begin without its triggers.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest

from cpersona import block_index, blocks, config, database
from cpersona.isolation import isolation_where

GOLDEN = Path(__file__).parent / "golden" / "block_candidates.json"
INSERT = (
    "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id,"
    " channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model)"
    " VALUES (?, ?, ?, ?, ?, ?, 0, 1, 0, ?, ?)"
)


@contextlib.asynccontextmanager
async def _database():
    saved = (database._db, database.DB_PATH, config.DB_PATH)
    directory = tempfile.mkdtemp()
    database._db = None
    database.DB_PATH = config.DB_PATH = os.path.join(directory, "block_index.db")
    block_index._cache.clear()
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        database._db, database.DB_PATH, config.DB_PATH = saved
        block_index._cache.clear()
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)


def _caps(monkeypatch, examined: int, per_parent: int, depth: int = blocks.BLOCK_RERANK_DEPTH):
    monkeypatch.setattr(blocks, "BLOCK_EXAMINED_CAP", examined)
    monkeypatch.setattr(blocks, "BLOCK_PER_PARENT_CAP", per_parent)
    monkeypatch.setattr(blocks, "BLOCK_RERANK_DEPTH", depth)


async def _both(db, keys, axes, query_bits):
    """(file read, SQLite read) for one query; the file read must not fall back."""
    agent_id, project_id, channel = axes
    from_file = await block_index.examined(
        db, keys, agent_id=agent_id, project_id=project_id, channel=channel, width=len(query_bits)
    )
    assert from_file is not None, "the file fell back where it should have answered"
    rows = await blocks._examined(
        db, isolation_where(agent_id=agent_id, project_id=project_id, channel=channel), keys
    )
    return from_file, rows


def _assert_same(from_file, rows, query_bits):
    assert from_file.keys() == [(r[0], r[1], r[2]) for r in rows]
    usable_f, dist_f = from_file.measured(query_bits)
    usable_s, dist_s = blocks._measured(rows, query_bits)
    assert list(usable_f) == usable_s
    if dist_s is None:
        assert dist_f is None
    else:
        assert [int(d) for d in dist_f] == [int(d) for d in dist_s]
    assert blocks._order(usable_f, dist_f) == blocks._order(usable_s, dist_s)


# --------------------------------------------------------------------------------------
# the golden, through the file
# --------------------------------------------------------------------------------------


def _golden_cases():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _golden_cases(), ids=lambda c: c["name"])
@pytest.mark.parametrize("chunk", [3, block_index._CHUNK])
async def test_the_golden_through_the_file(case, chunk, on, monkeypatch):
    monkeypatch.setattr(block_index, "_CHUNK", chunk)
    _caps(monkeypatch, case["caps"]["examined"], case["caps"]["per_parent"], case["caps"]["depth"])
    q = case["query"]
    query_bits = bytes.fromhex(q["bits"])
    async with _database() as db:
        await db.executemany(
            INSERT,
            [(r[0], r[1], r[2], r[3], r[4], r[5], None if r[7] is None else bytes.fromhex(r[7]), r[6])
             for r in case["rows"]],
        )
        await db.commit()
        built = await block_index.build_block_index(width=len(query_bits))
        if not built["built"]:
            # Only a case with no rows to index declines; it answers nothing either way.
            assert built["reason"] == "there are no block rows to index" and not case["expected"]
            return
        from_file, rows = await _both(db, tuple(q["models"]), (q["agent_id"], q["project_id"], q["channel"]), query_bits)
        _assert_same(from_file, rows, query_bits)
        near = blocks._order(*from_file.measured(query_bits))
    assert [[r[0], r[1], r[2], d] for r, d in near] == case["expected"]


# --------------------------------------------------------------------------------------
# random writes after a build
# --------------------------------------------------------------------------------------

AGENTS = ("a", "b")
PROJECTS = ("", "p1")
CHANNELS = ("", "c1")
MODELS = ("m", "", "old")
WIDTH = 4
QUERIES = (
    (("m", ""), ("a", None, "")),
    (("m", ""), ("b", "p1", "c1")),
    (("m", "m"), ("a", "", "c1")),
    (("m", ""), ("nobody", None, "")),
)


def _bits(rng: random.Random):
    roll = rng.random()
    if roll < 0.08:
        return None
    width = WIDTH if roll < 0.85 else rng.choice((WIDTH - 1, WIDTH + 1))
    return bytes(rng.getrandbits(8) for _ in range(width))


def _record_rows(rng, kind, parent_id, count=None, **axes):
    count = rng.randint(1, 8) if count is None else count
    model = rng.choice(MODELS)
    agent = axes.get("agent", rng.choice(AGENTS))
    project = axes.get("project", rng.choice(PROJECTS))
    channel = axes.get("channel", rng.choice(CHANNELS))
    return [(kind, parent_id, i, agent, project, channel, _bits(rng), model) for i in range(count)]


async def _parents(db):
    return [tuple(r) for r in await db.execute_fetchall(
        "SELECT DISTINCT parent_kind, parent_id FROM record_blocks"
    )]


async def _write(db, rng):
    """One write a deployment makes, chosen at random, committed."""
    parents = await _parents(db)
    op = rng.choice(("new", "new_old_id", "replace", "delete", "axes", "key", "bits", "model"))
    if op in ("new", "new_old_id") or not parents:
        kind = rng.choice(("ep", "mem"))
        taken = {p for k, p in parents if k == kind}
        high = max(taken, default=0)
        # An id below records already held: a backfill reaches old records late.
        parent_id = rng.choice([i for i in range(1, high + 2) if i not in taken]) if op == "new_old_id" else high + 1
        await db.executemany(INSERT, _record_rows(rng, kind, parent_id))
    else:
        kind, parent_id = rng.choice(parents)
        if op == "replace":
            await db.execute("DELETE FROM record_blocks WHERE parent_kind = ? AND parent_id = ?", (kind, parent_id))
            await db.executemany(INSERT, _record_rows(rng, kind, parent_id))
        elif op == "delete":
            await db.execute("DELETE FROM record_blocks WHERE parent_kind = ? AND parent_id = ?", (kind, parent_id))
        elif op == "axes":
            await db.execute(
                "UPDATE record_blocks SET agent_id = ?, project_id = ?, channel = ?"
                " WHERE parent_kind = ? AND parent_id = ?",
                (rng.choice(AGENTS), rng.choice(PROJECTS), rng.choice(CHANNELS), kind, parent_id),
            )
        elif op == "key":
            taken = {p for k, p in parents if k == kind}
            target = rng.choice([i for i in range(1, max(taken) + 3) if i not in taken])
            await db.execute(
                "UPDATE record_blocks SET parent_id = ? WHERE parent_kind = ? AND parent_id = ?",
                (target, kind, parent_id),
            )
        elif op == "bits":
            await db.execute(
                "UPDATE record_blocks SET embedding_bits = ? WHERE parent_kind = ? AND parent_id = ?"
                " AND block_index = (SELECT MIN(block_index) FROM record_blocks WHERE parent_kind = ? AND parent_id = ?)",
                (_bits(rng), kind, parent_id, kind, parent_id),
            )
        else:
            await db.execute(
                "UPDATE record_blocks SET embedding_model = ? WHERE parent_kind = ? AND parent_id = ?",
                (rng.choice(MODELS), kind, parent_id),
            )
    await db.commit()
    return op


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(12))
async def test_random_writes_after_a_build_read_the_same_rows(seed, on, monkeypatch):
    rng = random.Random(seed)
    monkeypatch.setattr(block_index, "_CHUNK", rng.choice((2, 5, 11, 64)))
    async with _database() as db:
        rows = []
        for kind in ("ep", "mem"):
            for parent_id in range(1, rng.randint(6, 14)):
                rows += _record_rows(rng, kind, parent_id * 2)
        await db.executemany(INSERT, rows)
        await db.commit()
        assert (await block_index.build_block_index(width=WIDTH))["built"]
        ops = set()
        for step in range(14):
            ops.add(await _write(db, rng))
            if step % 2:
                continue
            for keys, axes in QUERIES:
                query_bits = bytes(rng.getrandbits(8) for _ in range(WIDTH))
                for examined, per_parent in ((10_000, 64), (17, 3), (5, 1)):
                    _caps(monkeypatch, examined, per_parent, depth=7)
                    _assert_same(*await _both(db, keys, axes, query_bits), query_bits)
        # A rebuild in the middle of the writes reads the same rows too.
        assert (await block_index.build_block_index(width=WIDTH))["built"]
        for _ in range(4):
            ops.add(await _write(db, rng))
        for keys, axes in QUERIES:
            query_bits = bytes(rng.getrandbits(8) for _ in range(WIDTH))
            _caps(monkeypatch, 17, 3, depth=7)
            _assert_same(*await _both(db, keys, axes, query_bits), query_bits)
    assert len(ops) >= 4, f"seed {seed} exercised too few kinds of write: {ops}"


@pytest.mark.asyncio
async def test_a_record_moved_to_another_key_leaves_its_old_rows_behind(on):
    """Scenario B of the premise check: the UPDATE logs both records."""
    async with _database() as db:
        await db.executemany(INSERT, [("mem", 1, 0, "a", "", "", b"\x00" * WIDTH, "m"),
                                      ("mem", 3, 0, "a", "", "", b"\xff" * WIDTH, "m")])
        await db.commit()
        assert (await block_index.build_block_index())["built"]
        await db.execute("UPDATE record_blocks SET parent_id = 2 WHERE parent_id = 1")
        await db.commit()
        logged = await db.execute_fetchall("SELECT parent_kind, parent_id FROM record_block_changes ORDER BY seq")
        assert sorted(map(tuple, logged)) == [("mem", 1), ("mem", 2)]
        from_file, rows = await _both(db, ("m", ""), ("a", None, ""), b"\x00" * WIDTH)
        assert from_file.keys() == [("mem", 2, 0), ("mem", 3, 0)]
        _assert_same(from_file, rows, b"\x00" * WIDTH)


# --------------------------------------------------------------------------------------
# states the file must refuse
# --------------------------------------------------------------------------------------


async def _built(db, rows=None):
    await db.executemany(INSERT, rows or [("mem", i, 0, "a", "", "", bytes([i]) * WIDTH, "m") for i in range(1, 6)])
    await db.commit()
    built = await block_index.build_block_index()
    assert built["built"], built
    return built


async def _reads_the_file(db) -> bool:
    return await block_index.examined(db, ("m", ""), agent_id="a", project_id=None, channel="", width=WIDTH) is not None


@pytest.mark.asyncio
async def test_a_file_whose_changes_were_pruned_is_refused(on):
    """P1 / P2: a process holding an older file sees the mark past it."""
    async with _database() as db:
        await _built(db)
        older = Path(block_index.index_path()).read_bytes()
        await db.execute("UPDATE record_blocks SET embedding_bits = ? WHERE parent_id = 1", (b"\xee" * WIDTH,))
        await db.commit()
        newer = await block_index.build_block_index()
        assert newer["built"] and newer["pruned"]
        assert await _reads_the_file(db)
        Path(block_index.index_path()).write_bytes(older)
        block_index._cache.clear()
        assert not await _reads_the_file(db)


@pytest.mark.asyncio
async def test_pruning_never_lowers_the_mark(on):
    """P2: an older build finishing second must not move the mark back."""
    async with _database() as db:
        built = await _built(db)
        for value in (b"\x01", b"\x02"):
            await db.execute("UPDATE record_blocks SET embedding_bits = ? WHERE parent_id = 1", (value * WIDTH,))
            await db.commit()
        newer = await block_index.build_block_index()
        assert await block_index._prune(built["generation"], built["built_seq"])
        clock = await block_index._clock(db)
        assert clock[3] == newer["built_seq"] > built["built_seq"]


@pytest.mark.asyncio
async def test_a_build_prunes_only_the_generation_it_read(on):
    async with _database() as db:
        built = await _built(db)
        await db.execute("UPDATE record_blocks SET embedding_bits = ? WHERE parent_id = 1", (b"\x01" * WIDTH,))
        await db.commit()
        assert await block_index.stop_logging(db)
        await db.commit()
        assert await block_index.start_logging() == built["generation"] + 1
        await db.execute("UPDATE record_blocks SET embedding_bits = ? WHERE parent_id = 2", (b"\x02" * WIDTH,))
        await db.commit()
        assert not await block_index._prune(built["generation"], 10**9)
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_block_changes") == [(1,)]


@pytest.mark.asyncio
async def test_a_file_newer_than_the_snapshot_is_refused(on):
    """The order hole of the premise check: an old snapshot with a new file passes
    every other guard, and only the clock's head tells them apart."""
    async with _database() as db:
        await _built(db)
        await db.execute("UPDATE record_blocks SET embedding_bits = ? WHERE parent_id = 1", (b"\x01" * WIDTH,))
        await db.commit()
        assert (await block_index.build_block_index())["built"]
        assert await _reads_the_file(db)
        # The snapshot as it was before that write: the clock one change back.
        await db.execute("UPDATE block_log_clock SET head = head - 1 WHERE id = 0")
        await db.commit()
        assert not await _reads_the_file(db)


@pytest.mark.asyncio
async def test_a_log_turned_off_or_restarted_refuses_the_file(on):
    async with _database() as db:
        await _built(db)
        assert await _reads_the_file(db)
        assert await block_index.stop_logging(db)
        await db.commit()
        assert not await _reads_the_file(db)
        await block_index.start_logging()
        assert not await _reads_the_file(db), "a file from before the gap was read after logging restarted"


@pytest.mark.asyncio
async def test_a_schema_change_refuses_the_file(on):
    """A trigger dropped and recreated could have missed writes in between."""
    async with _database() as db:
        await _built(db)
        sql = (await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE name = 'record_block_log_au'"))[0][0]
        await db.execute("DROP TRIGGER record_block_log_au")
        await db.execute(sql)
        await db.commit()
        assert not await _reads_the_file(db)


@pytest.mark.asyncio
async def test_too_many_changed_rows_reads_sqlite(on, monkeypatch):
    async with _database() as db:
        await _built(db)
        monkeypatch.setattr(block_index, "LIVE_ROW_BOUND", 2)
        await db.execute("UPDATE record_blocks SET embedding_model = 'm' WHERE parent_id IN (1, 2)")
        await db.commit()
        assert await _reads_the_file(db)
        await db.execute("UPDATE record_blocks SET embedding_model = 'm' WHERE parent_id = 3")
        await db.commit()
        assert not await _reads_the_file(db)


@pytest.mark.asyncio
async def test_another_width_reads_sqlite(on):
    async with _database() as db:
        await _built(db)
        assert await block_index.examined(db, ("m", ""), agent_id="a", project_id=None, channel="", width=WIDTH + 1) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row, reason",
    [
        (("zz", 9, 0, "a", "", "", b"\x00" * WIDTH, "m"), "kind"),
        (("mem", 9, 2**31, "a", "", "", b"\x00" * WIDTH, "m"), "int32"),
        (("mem", 9, 0, "a", "", "", "text bits", "m"), "not a blob"),
        (("mem", 9, 0, b"agent", "", "", b"\x00" * WIDTH, "m"), "not text"),
    ],
)
async def test_a_value_the_file_cannot_hold_declines_the_build(row, reason, on):
    async with _database() as db:
        await db.executemany(INSERT, [("mem", 1, 0, "a", "", "", b"\x00" * WIDTH, "m"), row])
        await db.commit()
        built = await block_index.build_block_index()
        assert not built["built"] and reason in built["reason"]
        assert not os.path.exists(block_index.index_path())


@pytest.mark.asyncio
async def test_a_live_row_the_file_cannot_hold_reads_sqlite(on):
    async with _database() as db:
        await _built(db)
        await db.execute(INSERT, ("zz", 9, 0, "a", "", "", b"\x00" * WIDTH, "m"))
        await db.commit()
        assert not await _reads_the_file(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", block_index.LOG_TRIGGERS)
async def test_a_build_without_every_logging_trigger_declines(trigger, on):
    """A cookie compared later sees a change after the build, never a build that
    began without complete logging."""
    async with _database() as db:
        await db.execute(INSERT, ("mem", 1, 0, "a", "", "", b"\x00" * WIDTH, "m"))
        await db.execute(f"DROP TRIGGER {trigger}")
        await db.commit()
        built = await block_index.build_block_index()
        assert not built["built"] and trigger in built["reason"]


@pytest.mark.asyncio
async def test_a_damaged_file_reads_sqlite(on):
    async with _database() as db:
        await _built(db)
        path = block_index.index_path()
        with open(path, "ab") as fh:
            fh.write(b"\x00")
        block_index._cache.clear()
        assert not await _reads_the_file(db)


# --------------------------------------------------------------------------------------
# the log
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_logged_while_the_log_is_off():
    async with _database() as db:
        await db.execute(INSERT, ("mem", 1, 0, "a", "", "", b"\x00" * WIDTH, "m"))
        await db.execute("UPDATE record_blocks SET agent_id = 'b'")
        await db.execute("DELETE FROM record_blocks")
        await db.commit()
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_block_changes") == [(0,)]
        assert await block_index._clock(db) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_the_log_numbers_from_its_own_clock(on):
    """Scenario A of the premise check: clearing sqlite_sequence reuses nothing,
    because the log never asks it for a number."""
    async with _database() as db:
        built = await _built(db)
        await db.execute("UPDATE record_blocks SET agent_id = 'b' WHERE parent_id = 1")
        await db.commit()
        with contextlib.suppress(Exception):
            await db.execute("DELETE FROM sqlite_sequence")
        await db.execute("UPDATE record_blocks SET embedding_bits = ? WHERE parent_id = 2", (b"\xff" * WIDTH,))
        await db.commit()
        seqs = [r[0] for r in await db.execute_fetchall("SELECT seq FROM record_block_changes ORDER BY seq")]
        assert seqs == [built["built_seq"] + 1, built["built_seq"] + 2]
        from_file, rows = await _both(db, ("m", ""), ("a", None, ""), b"\x00" * WIDTH)
        _assert_same(from_file, rows, b"\x00" * WIDTH)


@pytest.mark.asyncio
async def test_startup_stops_the_log_when_the_file_is_off():
    async with _database() as db:
        await db.execute("UPDATE block_log_clock SET logging = 1 WHERE id = 0")
        await db.execute(INSERT, ("mem", 1, 0, "a", "", "", b"\x00" * WIDTH, "m"))
        await db.commit()
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_block_changes") == [(1,)]
        await block_index.on_startup()
        assert (await block_index._clock(db))[0] == 0
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_block_changes") == [(0,)]


@pytest.mark.asyncio
async def test_a_build_is_due_when_absent_or_behind(on, monkeypatch):
    async with _database() as db:
        assert not await block_index._build_due(), "no rows: nothing to build"
        await db.execute(INSERT, ("mem", 1, 0, "a", "", "", b"\x00" * WIDTH, "m"))
        await db.commit()
        assert await block_index._build_due()
        assert (await block_index.build_block_index())["built"]
        assert not await block_index._build_due()
        monkeypatch.setattr(block_index, "LIVE_ROW_BOUND", 1)
        await db.execute("UPDATE record_blocks SET agent_id = 'b'")
        await db.execute("UPDATE record_blocks SET agent_id = 'a'")
        await db.commit()
        assert await block_index._build_due()


# --------------------------------------------------------------------------------------
# the recall path, the distance, the command line
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_reads_the_file_and_answers_as_sqlite_does(on, monkeypatch):
    async with _database() as db:
        rng = random.Random(7)
        rows = [r for parent_id in range(1, 30) for r in _record_rows(rng, "mem", parent_id, agent="a", project="", channel="")]
        await db.executemany(INSERT, rows)
        await db.commit()
        assert (await block_index.build_block_index(width=WIDTH))["built"]
        monkeypatch.setattr(blocks.generation, "block_keys", lambda: ("m", ""))
        embedding = [rng.uniform(-1, 1) for _ in range(WIDTH * 8)]
        iso = isolation_where(agent_id="a", project_id=None, channel="")
        calls = []
        original = block_index.examined

        async def spy(*args, **kwargs):
            found = await original(*args, **kwargs)
            calls.append(found is not None)
            return found

        monkeypatch.setattr(block_index, "examined", spy)
        through_file = await blocks.search(db, embedding, iso, axes=("a", None, ""))
        through_sqlite = await blocks.search(db, embedding, iso)
    assert calls == [True], "search did not read the file"
    assert through_file == through_sqlite and through_file


def test_the_two_popcounts_agree():
    rng = np.random.default_rng(3)
    packed = rng.integers(0, 256, size=(500, 17), dtype=np.uint8)
    query = rng.integers(0, 256, size=17, dtype=np.uint8)
    table = blocks._popcount_table()[np.bitwise_xor(packed, query)].sum(axis=1)
    assert hasattr(np, "bitwise_count"), "numpy 2 is what CI runs; the other branch is the table above"
    assert [int(d) for d in blocks.hamming_matrix(packed, query)] == [int(d) for d in table]


@pytest.mark.asyncio
async def test_the_command_line_builds_and_reports(on, capsys, tmp_path):
    import asyncio

    saved = (database._db, database.DB_PATH, config.DB_PATH)
    path = str(tmp_path / "cli.db")
    database._db = None
    database.DB_PATH = config.DB_PATH = path
    try:
        db = await database.get_db()
        await db.execute(INSERT, ("mem", 1, 0, "a", "", "", b"\x00" * WIDTH, "m"))
        await db.commit()
        await database.close_db()
        database._db = None
        block_index._cache.clear()
        # main() runs its own event loop, so it runs in a thread beside this one.
        assert await asyncio.to_thread(block_index.main, ["--db", path, "build"]) == 0
        built = json.loads(capsys.readouterr().out)
        assert built["built"] and built["count"] == 1
        assert await asyncio.to_thread(block_index.main, ["--db", path, "--json", "status"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["usable"] and status["rows"] == 1 and status["current"]
    finally:
        await database.close_db()
        database._db, database.DB_PATH, config.DB_PATH = saved
        block_index._cache.clear()


# --------------------------------------------------------------------------------------
# a purge removes it
# --------------------------------------------------------------------------------------


async def _memory_with_blocks(db, agent: str) -> int:
    cursor = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, 'held', '2026-10-08T00:00:00+00:00')",
        (agent,),
    )
    await db.execute(INSERT, ("mem", cursor.lastrowid, 0, agent, "", "", b"\x00" * WIDTH, "m"))
    await db.commit()
    return cursor.lastrowid


@pytest.mark.asyncio
async def test_a_purge_removes_the_block_index_with_the_rows_it_held(on):
    from cpersona import admin_handlers

    async with _database() as db:
        await _memory_with_blocks(db, "agent.purged")
        await _memory_with_blocks(db, "agent.kept")
        assert (await block_index.build_block_index())["built"]
        path = block_index.index_path()
        # The control: the file named the agent before the purge.
        assert b"agent.purged" in Path(path).read_bytes()

        result = await admin_handlers.do_delete_agent_data(agent_id="agent.purged")
        assert result["ok"] and result["deleted_memories"] == 1, result
        residue = Path(path).read_bytes() if os.path.exists(path) else b""
        assert b"agent.purged" not in residue


@pytest.mark.asyncio
async def test_a_purge_that_removed_nothing_keeps_the_block_index(on):
    from cpersona import admin_handlers

    async with _database() as db:
        await _memory_with_blocks(db, "agent.kept")
        assert (await block_index.build_block_index())["built"]
        before = Path(block_index.index_path()).read_bytes()
        result = await admin_handlers.do_delete_agent_data(agent_id="agent.nobody")
        assert result["ok"] and result["deleted_memories"] == 0, result
        assert Path(block_index.index_path()).read_bytes() == before


# --------------------------------------------------------------------------------------
# a traced recall says which read served the block arm
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_traced_recall_says_which_read_served_the_block_arm(monkeypatch, fake_embedding_client):
    from cpersona import admin_handlers, memory_handlers, nodes, session, tasks

    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", False)
    session.reset_pauses_for_tests()
    queue = tasks.MemoryTaskQueue()
    queue._running = True
    monkeypatch.setattr(tasks, "_task_queue", queue)
    agent, query = "agent.traced", "zzarquon nebulite flimsy"
    async with _database() as db:
        for text in ("filler " * 300 + "\n\n" + query + " was decided against", query, "an unrelated note"):
            await memory_handlers.do_store(agent, {"content": text})
        await queue._drain(admin_handlers, memory_handlers, nodes)
        assert (await db.execute_fetchall("SELECT COUNT(*) FROM record_blocks"))[0][0] > 0

        async def traced():
            out = await memory_handlers.do_recall(agent, query, limit=3, trace=True)
            return [m["ref"] for m in out["messages"]], out["trace"]["block_source"]

        rows_off, source = await traced()
        assert source == {"source": "sqlite", "reason": "off"} and rows_off

        monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
        rows, source = await traced()
        assert source == {"source": "sqlite", "reason": "no_file"} and rows == rows_off

        assert (await block_index.build_block_index())["built"]
        rows, source = await traced()
        assert source == {"source": "file", "changed_records": 0, "live_rows": 0} and rows == rows_off

        assert await block_index.stop_logging(db)
        await db.commit()
        rows, source = await traced()
        assert source == {"source": "sqlite", "reason": "logging_off"} and rows == rows_off
    session.reset_pauses_for_tests()
