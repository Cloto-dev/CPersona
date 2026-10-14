"""The write seam's boundary against other connections and against the read seam.

bug-508: ``transaction()`` serialised writers with an in-process asyncio.Lock
only. The write connection BEGINs implicitly at the body's first DML, so a read
at the top of a body ran outside any SQLite transaction, and another server
process sharing the file could commit between that read and the body's first
write. Bodies that read rows and then write them back by id -- merge(move),
deep_check's short_content repair -- then destroyed that process's committed
work while both sides answered ok. The other process is modelled here as a
second ``sqlite3`` connection on the same file in its own thread: to SQLite's
locking that is exactly what a second process is.

bug-544: on an in-memory database the read seam is the write connection itself,
and the report-only FTS integrity probe's release rolled back whatever
transaction that connection had open -- including another coroutine's open
``transaction()``, whose earlier statements were lost while it reported success.
"""

import asyncio
import sqlite3
import threading
import time

import pytest
import pytest_asyncio

from cpersona import database
from cpersona.admin_handlers import do_merge_memories
from cpersona.database import connection, read_snapshot, transaction
from cpersona.maintenance_handlers import do_check_health, do_deep_check
from cpersona.memory_handlers import do_store

EDITED = "EDITED by the other process: the user's corrected, much longer note"
HOLD_S = 1.0


@pytest_asyncio.fixture
async def file_db(monkeypatch, tmp_path):
    # Fresh lock per test: an asyncio.Lock binds to the first loop that contends it.
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    await database.close_db()
    path = str(tmp_path / "u03.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.get_db()
    yield path
    await database.close_db()


@pytest_asyncio.fixture
async def in_memory_db(monkeypatch):
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    await database.close_db()
    monkeypatch.setattr(database, "DB_PATH", ":memory:")
    db = await database.get_db()
    yield db
    await database.close_db()


class _OtherProcess(threading.Thread):
    """Another server process's write: BEGIN IMMEDIATE, one statement, a hold, COMMIT.

    The hold keeps that transaction open while the handler under test starts, so
    the handler's reads land while the other side's change is not yet committed
    and its writes have to wait for the commit -- the window the seam must close.
    """

    def __init__(self, path: str, sql: str, params: tuple, hold_s: float = HOLD_S):
        super().__init__(daemon=True)
        self.path, self.sql, self.params, self.hold_s = path, sql, params, hold_s
        self.ready = threading.Event()
        self.rowcount = None
        self.committed_at = None
        self.error: BaseException | None = None

    def run(self):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            self.rowcount = con.execute(self.sql, self.params).rowcount
            self.ready.set()
            time.sleep(self.hold_s)
            con.execute("COMMIT")
            self.committed_at = time.time()
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            self.error = exc
            self.ready.set()
        finally:
            con.close()


async def _race(path: str, sql: str, params: tuple, handler):
    """Run ``handler`` while another connection holds an uncommitted change."""
    other = _OtherProcess(path, sql, params)
    other.start()
    assert await asyncio.to_thread(other.ready.wait, 10), "the other connection never wrote"
    assert other.error is None, other.error
    started_at = time.time()
    result = await handler()
    await asyncio.to_thread(other.join, 20)
    assert other.error is None, other.error
    assert other.rowcount == 1, "the other connection's change matched no row"
    # Precondition: the handler started inside the other side's open transaction.
    assert started_at < other.committed_at
    return result


def _rows(path: str, sql: str, params: tuple = ()) -> list:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


async def _store(agent_id: str, content: str) -> int:
    r = await do_store(agent_id, {"content": content, "source": {"type": "User", "id": "u"}})
    assert r.get("result") == "stored", r
    return r["id"]


# ---------------------------------------------------------------------------
# bug-508
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug508_transaction_holds_the_sqlite_write_lock_before_its_first_statement(file_db):
    """The seam itself: no other connection can start a write once a body has begun."""
    async with transaction():
        other = sqlite3.connect(file_db, timeout=0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            if other.in_transaction:
                other.execute("ROLLBACK")
            other.close()


@pytest.mark.asyncio
async def test_bug508_deep_check_fix_keeps_a_note_another_process_expanded(file_db):
    async with transaction() as db:
        cur = await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp)"
            " VALUES ('S', 'tbd', '{}', datetime('now'))"
        )
        mid = cur.lastrowid

    result = await _race(
        file_db,
        # do_update_memory's UPDATE.
        "UPDATE memories SET content = ?, embedding = ?, embedding_model = ? WHERE id = ? AND locked = 0",
        (EDITED, None, "", mid),
        lambda: do_deep_check("S", fix=True, checks=["short_content"]),
    )

    row = _rows(file_db, "SELECT content FROM memories WHERE id = ?", (mid,))
    # Unfixed: the repair read 'tbd' before the edit committed, then deleted the
    # row by id after it had -- the committed note existed nowhere.
    assert row == [(EDITED,)], (result["results"], row)


@pytest.mark.asyncio
async def test_bug508_merge_move_keeps_an_edit_another_process_committed(file_db):
    ids = [await _store("A", f"A original text {i}") for i in range(3)]

    merged = await _race(
        file_db,
        "UPDATE memories SET content = ?, embedding = ?, embedding_model = ? WHERE id = ? AND locked = 0",
        (EDITED, None, "", ids[0]),
        lambda: do_merge_memories("A", "C", mode="move"),
    )

    assert merged.get("ok") is True, merged
    holders = _rows(file_db, "SELECT agent_id FROM memories WHERE content = ?", (EDITED,))
    # Unfixed: merge copied the stale page it had read, then deleted the source
    # rows by id, the edited one included.
    assert holders == [("C",)], holders


@pytest.mark.asyncio
async def test_bug508_merge_move_keeps_a_lock_another_process_committed(file_db):
    ids = [await _store("B", f"B original text {i}") for i in range(3)]

    merged = await _race(
        file_db,
        # do_lock_memory's UPDATE.
        "UPDATE memories SET locked = 1 WHERE id = ?",
        (ids[0],),
        lambda: do_merge_memories("B", "D", mode="move"),
    )

    assert merged.get("ok") is True, merged
    locked = _rows(
        file_db, "SELECT COUNT(*) FROM memories WHERE content = 'B original text 0' AND locked = 1"
    )
    # Unfixed: the locked source row was deleted and its copy written unlocked.
    assert locked == [(1,)]


# ---------------------------------------------------------------------------
# bug-544
# ---------------------------------------------------------------------------


async def _count(agent_id: str) -> int:
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (agent_id,))
    return rows[0][0]


async def _paused_writer(agent_id: str, first_written: asyncio.Event, resume: asyncio.Event) -> str:
    """Two statements in one transaction() with an await point between them."""
    async with transaction() as db:
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, 'first row', '{}', 't')",
            (agent_id,),
        )
        first_written.set()
        await resume.wait()
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, 'second row', '{}', 't')",
            (agent_id,),
        )
    return "committed"


@pytest.mark.asyncio
async def test_bug544_report_only_fts_probe_keeps_a_concurrent_writers_statements(in_memory_db):
    assert await database._get_read_db() is in_memory_db, "precondition: the read seam is the writer"
    first_written, resume = asyncio.Event(), asyncio.Event()

    async def health():
        await first_written.wait()
        try:
            return await do_check_health(agent_id="", fix=False, checks=["fts_integrity"])
        finally:
            resume.set()

    writer, health_result = await asyncio.gather(_paused_writer("w", first_written, resume), health())

    assert writer == "committed"
    assert health_result.get("ok", True) is not False, health_result
    # Unfixed: the probe's release rolled back 'first row'; 1 of 2 survived.
    assert await _count("w") == 2


@pytest.mark.asyncio
async def test_declare_reuses_an_entity_another_process_registers_in_the_gap(file_db):
    """bug-631: declare looks an entity up before it inserts one. With the lookup
    outside SQLite's write lock, another process could register the same name in
    between, and the insert then failed on the unique index -- the whole
    declaration answered an error for an entity that did exist."""
    from cpersona import associations

    agent = "u03.declare"

    async def handler():
        return await associations.declare(agent, {"entities": [{"name": "Kenji"}]})

    result = await _race(
        file_db,
        "INSERT INTO entities (agent_id, project_id, channel, name, normalized, declared_by, created_at) "
        "VALUES (?, '', '', 'Kenji', ?, 'agent', '2026-01-01T00:00:00+00:00')",
        (agent, associations.normalize("Kenji")),
        handler,
    )
    assert result["dropped"] == [], result
    assert _rows(file_db, "SELECT name FROM entities WHERE agent_id = ?", (agent,)) == [("Kenji",)]


@pytest.mark.asyncio
async def test_declare_does_not_mention_a_record_another_process_deletes_in_the_gap(file_db):
    """bug-631 (b): the anchor check ran before the write lock, so a record deleted
    by another process in between still got a mention, after its delete trigger had
    already fired -- an orphan no health check sweeps."""
    from cpersona import associations

    agent = "u03.anchor"
    mem_id = await _store(agent, "Kenji leads the payments rewrite")

    async def handler():
        return await associations.declare(
            agent, {"entities": [{"name": "Kenji"}]}, anchor_ref=f"mem:{mem_id}"
        )

    result = await _race(file_db, "DELETE FROM memories WHERE id = ?", (mem_id,), handler)
    assert result["mentions"] == 0, result
    assert _rows(file_db, "SELECT COUNT(*) FROM entity_mentions WHERE ref = ?", (f"mem:{mem_id}",)) == [(0,)]


@pytest.mark.asyncio
async def test_bug544_probe_with_no_writer_still_releases_its_own_transaction(in_memory_db):
    """The guard must not keep the probe's own transaction open on the shared
    connection: an in-memory export copies the database with SQLite's backup,
    which refuses a source that holds an open write transaction."""
    async with transaction() as db:
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) VALUES ('e', 'row', '{}', 't')"
        )
    await do_check_health(agent_id="", fix=False, checks=["fts_integrity"])

    assert not in_memory_db.in_transaction
    async with read_snapshot() as snap:
        rows = await snap.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'e'")
    assert rows == [(1,)]
