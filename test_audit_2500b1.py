"""Regression tests for the 2.5.0b1 audit fixes (bug-086+).

Covers the architectural fixes landed for the pre-b1 comprehensive audit:
read/write connection separation (bug-086), init-failure memoisation (bug-087),
and the read-seam snapshot-unpinning guarantee that backs both.
"""
import asyncio

import pytest
import pytest_asyncio

from cpersona import database
from cpersona.database import connection, get_db, transaction


@pytest_asyncio.fixture
async def clean_db():
    """A freshly-truncated DB for the b1 audit tests."""
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-086: the read seam must not observe another coroutine's uncommitted
# transaction. On the old shared connection, a connection() SELECT executed
# inside the writer's open transaction — a dedup probe could match an import
# row that was later rolled back (the acknowledged store then existed nowhere).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_seam_does_not_see_uncommitted_writes(clean_db):
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with transaction() as db:
            await db.execute(
                "INSERT INTO memories (agent_id, content, source, timestamp) "
                "VALUES ('holder', 'uncommitted sentinel', '{}', 't')"
            )
            started.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await started.wait()
    try:
        async with connection() as r:
            rows = await r.execute_fetchall(
                "SELECT COUNT(*) FROM memories WHERE agent_id = 'holder'"
            )
        assert rows[0][0] == 0, "read seam observed an uncommitted write (dirty read)"
    finally:
        release.set()
        await task

    # After the holder commits, the same read seam sees the row.
    async with connection() as r:
        rows = await r.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = 'holder'"
        )
    assert rows[0][0] == 1


# ---------------------------------------------------------------------------
# bug-086 companion: a statement that opens an implicit transaction on the read
# connection (FTS5's integrity-check is spelled as an INSERT) must not pin the
# WAL snapshot past the seam scope — connection() rolls it back on exit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_seam_unpins_snapshot_on_scope_exit(clean_db):
    async with connection() as r:
        await r.execute("BEGIN")
        assert r._conn.in_transaction
    rd = database._read_db
    if rd is not None and rd is not database._db and rd._conn is not None:
        assert not rd._conn.in_transaction, "read seam leaked an open transaction"

    # The proof that matters: a commit AFTER the (rolled-back) pin is visible.
    async with transaction() as db:
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            "VALUES ('post-pin', 'fresh row', '{}', 't')"
        )
    async with connection() as r:
        rows = await r.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = 'post-pin'"
        )
    assert rows[0][0] == 1, "read connection served a stale pinned snapshot"


# ---------------------------------------------------------------------------
# bug-086 companion: when the write connection is swapped out (close_db +
# re-init, the harnesses' boot simulations), the read seam re-keys itself
# instead of serving the old database file.
# ---------------------------------------------------------------------------


async def _reset_read_seam():
    """Close and drop the current read connection; the seam lazily reopens it.

    Restoring a stashed read-connection object is never safe — the seam may
    have closed it during a re-key — so tests always reset to None instead."""
    if database._read_db is not None and database._read_db is not database._db:
        await database._read_db.close()
    database._read_db = None
    database._read_db_owner = None


@pytest.mark.asyncio
async def test_read_seam_rekeys_after_reboot(clean_db, tmp_path, monkeypatch):
    async with connection() as r:  # materialise the read conn on the main DB
        await r.execute_fetchall("SELECT 1")

    saved_db = database._db
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "boot.db"))
    database._db = None
    try:
        async with transaction() as db:
            await db.execute(
                "INSERT INTO memories (agent_id, content, source, timestamp) "
                "VALUES ('boot-agent', 'boot row', '{}', 't')"
            )
        async with connection() as r:
            rows = await r.execute_fetchall(
                "SELECT COUNT(*) FROM memories WHERE agent_id = 'boot-agent'"
            )
        assert rows[0][0] == 1, "read seam still pointed at the pre-reboot database"
    finally:
        if database._db is not None:
            await database._db.close()
        await _reset_read_seam()
        database._db = saved_db


# ---------------------------------------------------------------------------
# bug-087: get_db() must not memoise a half-initialised connection. A failed
# boot leaves _db unset (and the connection closed) so the next call re-runs
# the idempotent ladder instead of serving missing tables from the fast path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_db_is_not_memoized_on_failed_init(tmp_path, monkeypatch):
    saved_db = database._db
    database._db = None
    await _reset_read_seam()
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "flaky-boot.db"))

    real_init = database._init_schema
    calls = {"n": 0}

    async def flaky_init(db):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected boot fault")
        return await real_init(db)

    monkeypatch.setattr(database, "_init_schema", flaky_init)
    try:
        with pytest.raises(RuntimeError, match="injected boot fault"):
            await database.get_db()
        assert database._db is None, "failed init published a half-initialised connection"

        db = await database.get_db()  # the retry re-runs the ladder and succeeds
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        )
        assert rows, "retried init did not complete the schema"
    finally:
        if database._db is not None:
            await database._db.close()
        await _reset_read_seam()
        database._db = saved_db
