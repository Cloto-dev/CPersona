"""bug-356: read_snapshot() is a snapshot on an in-memory database too.

The ``:memory:`` branch used to yield the shared write connection with no
transaction, so a commit landing mid-scope was visible to the rest of the scope.
Every test here runs against a genuine in-memory database (the connection is
re-pointed, not just the path), because the defect lived in that branch.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import aiosqlite
import pytest
import pytest_asyncio

from cpersona import admin_handlers, database
from cpersona.database import read_snapshot, transaction


@pytest_asyncio.fixture
async def in_memory_db(monkeypatch):
    # A test here contends the write lock, and an asyncio.Lock binds to the event
    # loop it is first contended in. Each test runs in its own loop, so contending
    # the process-wide lock would leave it bound to a loop that is gone, and the
    # next test to contend it would fail with "bound to a different event loop".
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    await database.close_db()
    monkeypatch.setattr(database, "DB_PATH", ":memory:")
    db = await database.get_db()
    yield db
    await database.close_db()


async def _store(agent_id: str, content: str) -> None:
    async with transaction() as db:
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp)"
            " VALUES (?, ?, '{}', 't')",
            (agent_id, content),
        )


async def _count(db, agent_id: str) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (agent_id,)
    )
    return rows[0][0]


@pytest.mark.asyncio
async def test_a_commit_inside_the_scope_is_not_seen_by_it(in_memory_db):
    await _store("snap", "before the scope")

    async with read_snapshot() as snap:
        assert await _count(snap, "snap") == 1
        await _store("snap", "committed mid-scope")
        # The measured defect: this read returned 2.
        assert await _count(snap, "snap") == 1

    # The commit is real; only the scope was kept from seeing it.
    assert await _count(in_memory_db, "snap") == 2


@pytest.mark.asyncio
async def test_the_snapshot_reads_the_live_corpus_not_an_empty_database(in_memory_db):
    # bug-235's reason for sharing the connection: a second connect(":memory:")
    # is a different, empty database with no schema at all.
    await _store("snap", "a row the export must see")
    async with read_snapshot() as snap:
        assert snap is not in_memory_db
        assert await _count(snap, "snap") == 1


class _CountingLock(asyncio.Lock):
    """The write lock, counting how many times it has been asked for."""

    def __init__(self):
        super().__init__()
        self.requests = 0

    async def acquire(self):
        self.requests += 1
        return await super().acquire()


@pytest.mark.asyncio
async def test_a_writers_uncommitted_work_is_not_copied(in_memory_db, monkeypatch):
    """The copy is taken under the write seam. The shared connection sees its own
    uncommitted rows, so a copy taken between a writer's first write and its
    rollback would carry a row that never existed.

    The writer is released only once the reader has either asked for the write
    lock (and so waits for the rollback) or finished without asking for it. A
    fixed number of loop turns is not enough: opening the copy's connection can
    take longer, and a reader that copies after the rollback passes whether or
    not it took the lock.
    """
    lock = _CountingLock()
    monkeypatch.setattr(database, "_write_lock", lock)
    inserted = asyncio.Event()
    release = asyncio.Event()

    class _Abort(Exception):
        pass

    async def writer():
        with pytest.raises(_Abort):
            async with transaction() as db:
                await db.execute(
                    "INSERT INTO memories (agent_id, content, source, timestamp)"
                    " VALUES ('snap', 'rolled back', '{}', 't')"
                )
                inserted.set()
                await release.wait()
                raise _Abort

    async def reader():
        async with read_snapshot() as snap:
            return await _count(snap, "snap")

    writer_task = asyncio.create_task(writer())
    await inserted.wait()
    assert lock.requests == 1  # the writer's
    reader_task = asyncio.create_task(reader())
    async with asyncio.timeout(10):
        while lock.requests < 2 and not reader_task.done():
            await asyncio.sleep(0.005)
    release.set()
    await writer_task
    assert await reader_task == 0
    assert await _count(in_memory_db, "snap") == 0


@pytest.mark.asyncio
async def test_a_copy_refuses_to_wait_on_an_uncommitted_write():
    """Measured: a backup whose source holds an uncommitted write retries forever.

    Built so that it fails rather than hangs if the refusal is removed: the
    source is a private connection, and if the copy is still waiting after a few
    seconds the write is rolled back from this thread, which releases the
    driver's retry loop so the worker thread can finish and the test can fail.
    """
    source = await aiosqlite.connect(":memory:", check_same_thread=False)
    target = await aiosqlite.connect(":memory:", check_same_thread=False)
    try:
        await source.execute("CREATE TABLE t (x)")
        await source.execute("INSERT INTO t VALUES (1)")
        await source.commit()
        await source.execute("INSERT INTO t VALUES (2)")  # open, never committed

        copy = asyncio.create_task(database._copy_database(source, target))
        done, _ = await asyncio.wait({copy}, timeout=5)
        if not done:
            source._conn.rollback()  # release the retry loop, then fail
            await copy
            pytest.fail("the copy waited on an uncommitted write instead of refusing")
        with pytest.raises(database.SnapshotUnavailable):
            copy.result()

        # The source is untouched and still usable: its write is still open.
        assert source.in_transaction
        await source.rollback()
        assert (await source.execute_fetchall("SELECT COUNT(*) FROM t"))[0][0] == 1
    finally:
        await target.close()
        await source.close()


@pytest.mark.asyncio
async def test_a_copy_does_not_refuse_an_open_read():
    """Only a write is refused: an open read transaction copies normally."""
    source = await aiosqlite.connect(":memory:", check_same_thread=False)
    target = await aiosqlite.connect(":memory:", check_same_thread=False)
    try:
        await source.execute("CREATE TABLE t (x)")
        await source.execute("INSERT INTO t VALUES (1)")
        await source.commit()
        await source.execute("BEGIN")
        await source.execute_fetchall("SELECT COUNT(*) FROM t")
        await asyncio.wait_for(database._copy_database(source, target), 5)
        assert (await target.execute_fetchall("SELECT COUNT(*) FROM t"))[0][0] == 1
        await source.rollback()
    finally:
        await target.close()
        await source.close()


@pytest.mark.asyncio
async def test_the_write_lock_is_not_held_for_the_scope(in_memory_db):
    """The lock guards the copy, not the read: a writer must not wait on an
    export that is still streaming."""
    async with read_snapshot() as snap:
        assert not database.write_lock().locked()
        await asyncio.wait_for(_store("snap", "written while the scope is open"), 5)
        assert await _count(snap, "snap") == 0


@pytest.mark.asyncio
async def test_the_copy_is_closed_and_the_shared_connection_survives(in_memory_db):
    await _store("snap", "row")
    async with read_snapshot() as snap:
        pass
    with pytest.raises(ValueError):
        await snap.execute("SELECT 1")
    assert await _count(in_memory_db, "snap") == 1


class _InjectingConnection:
    """Commits one memory through the write seam right after the export has read
    its three header counts -- the interleaving the registry entry measured."""

    def __init__(self, db):
        self._inner = db
        self.counts_read = 0
        self.injected = False

    async def execute(self, sql, params=None):
        cursor = await (self._inner.execute(sql) if params is None else self._inner.execute(sql, params))
        if sql.startswith("SELECT COUNT(*)"):
            self.counts_read += 1
            if self.counts_read == 3 and not self.injected:
                self.injected = True
                await _store("export-agent", "committed after the header counts")
        return cursor


@pytest.mark.asyncio
async def test_the_export_header_matches_its_body(in_memory_db, monkeypatch, tmp_path):
    await _store("export-agent", "the only memory at the start")

    original = admin_handlers.read_snapshot
    proxies = []

    @asynccontextmanager
    async def injecting_snapshot():
        async with original() as db:
            proxy = _InjectingConnection(db)
            proxies.append(proxy)
            yield proxy

    monkeypatch.setattr(admin_handlers, "read_snapshot", injecting_snapshot)
    path = tmp_path / "in-memory.jsonl"
    exported = await admin_handlers.do_export_memories("export-agent", str(path))
    assert exported["ok"] is True, exported
    assert proxies and proxies[0].injected

    records = [json.loads(line) for line in path.read_text().splitlines()]
    header = records[0]
    body_memories = sum(r.get("_type") == "memory" for r in records[1:])
    # Measured before the fix: header 1, body 2.
    assert header["memory_count"] == body_memories == 1
    assert await _count(in_memory_db, "export-agent") == 2
