"""Behavioural test harnesses for the v2.5.0 stabilization line (deep-audit [17]-[20]).

The 2.4.39 deep audit identified four durability/concurrency contracts that were
enforced in code but had no behavioural test pinning them — only static gates or
unit-level flag roundtrips. These harnesses fix the baseline BEFORE the C-seam
(transaction()/connection() context-manager) refactor, so the refactor can prove
behaviour preservation by keeping them green:

[17] write_lock serialization — a committer blocked on the shared write lock must
     not have its commit flush another coroutine's half-written transaction.
[18] bug-060 durable FTS backfill — a failed/crashed rebuild leaves the durable
     user_version pending bit armed, and the NEXT boot re-indexes; table presence
     alone must never suppress the retry.
[19] migration withhold — a failed migration ladder withholds the schema_version
     stamp so the next boot re-runs the idempotent steps instead of skipping them.
[20] import/merge rollback — a mid-transaction fault rolls the whole restore/merge
     back (no partial corpus) and always releases the shared write lock.
"""

import asyncio
import json

import pytest
import pytest_asyncio

from cpersona import admin_handlers, database, memory_handlers
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db, write_lock


@pytest_asyncio.fixture
async def clean_db():
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _boot_fresh(monkeypatch, dbfile):
    """Point the module singleton at ``dbfile`` and boot it."""
    monkeypatch.setattr(database, "DB_PATH", dbfile)
    # On entry the singleton is already closed/None (every test in this suite closes its
    # own boots — see _shutdown) and awaiting close_db() here would cross event loops:
    # the leftover object is bound to the PREVIOUS test's loop, and a cross-loop close
    # raises. The bug-124 hazard is mid-test re-points, not this entry-point reset.
    database._db = None  # orphan-waiver: entry-point reset; cross-loop close is unsafe
    return await database.get_db()


async def _shutdown():
    if database._db is not None:
        await database._db.close()
        database._db = None


# ---------------------------------------------------------------------------
# [17] write_lock serialization (behavioural — the static gate only proves the
# lock is TAKEN; this proves it actually serialises the commit boundary).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_lock_serialises_commit_boundaries(clean_db):
    """While one coroutine holds write_lock() with an uncommitted row, a real
    handler (do_store) must block before its commit; when the holder rolls back,
    only the handler's row may exist — the holder's partial row must NOT have
    been flushed by the handler's commit (the bug-042/043 failure mode)."""
    db = clean_db
    order: list[str] = []
    release = asyncio.Event()

    async def holder():
        async with write_lock():
            await db.execute(
                "INSERT INTO memories (agent_id, content, timestamp) VALUES ('lock-a', 'partial work', '')"
            )
            order.append("A-in")
            await release.wait()
            await db.rollback()  # abort: the partial row must vanish
        order.append("A-out")

    async def contender():
        await memory_handlers.do_store(
            "lock-b", {"content": "b row", "source": {}, "timestamp": "t"}
        )
        order.append("B-done")

    t_holder = asyncio.create_task(holder())
    while "A-in" not in order:
        await asyncio.sleep(0)
    t_contender = asyncio.create_task(contender())

    # Give the contender ample scheduling opportunity: it must stay blocked on
    # the lock (its commit cannot run while the holder's transaction is open).
    await asyncio.sleep(0.05)
    assert "B-done" not in order, "do_store committed while another writer held write_lock"

    release.set()
    await t_holder
    await t_contender
    assert order.index("A-out") < order.index("B-done")

    a_rows = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = 'lock-a'")
    b_rows = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = 'lock-b'")
    assert a_rows == [], "holder's rolled-back partial row was flushed by the contender's commit"
    assert b_rows, "contender's own committed row is missing"


# ---------------------------------------------------------------------------
# [18] bug-060: durable FTS backfill retry across boots.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fts_pending_bit_drives_next_boot_rebuild(tmp_path, monkeypatch):
    """A de-indexed corpus with the durable pending bit armed (= a prior boot's
    rebuild failed or crashed after arming) must be re-indexed on the next boot,
    and the bit cleared. Negative control first: with the bit CLEAR, a normal
    boot must NOT rebuild (tables exist), proving the bit — not table presence —
    is what drives the retry."""
    dbfile = str(tmp_path / "durable_retry.db")
    monkeypatch.setattr(database, "FTS_ENABLED", True)
    saved = database._db
    try:
        # Boot 1: normal. Row is indexed.
        db = await _boot_fresh(monkeypatch, dbfile)
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES ('a', 'kumquat sentinel', '')"
        )
        await db.commit()
        hit = await db.execute_fetchall(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'kumquat'"
        )
        assert hit, "sanity: row should be indexed on a healthy boot"
        uv = (await db.execute_fetchall("PRAGMA user_version"))[0][0]
        assert not (uv & 1), "pending bit must be clear after a successful boot"

        # Simulate the bug-060 damage: the index loses the row out-of-band.
        await db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('delete-all')")
        await db.commit()
        await _shutdown()

        # Negative control: bit clear + tables present → boot does NOT re-index.
        db = await _boot_fresh(monkeypatch, dbfile)
        hit = await db.execute_fetchall(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'kumquat'"
        )
        assert hit == [], "a normal boot must not rebuild without the pending bit"

        # Arm the durable bit (what a failed/crashed rebuild leaves behind).
        await database._set_fts_backfill_pending(db, True)
        await db.commit()
        await _shutdown()

        # Boot with the bit armed: rebuild runs, row is searchable, bit cleared.
        db = await _boot_fresh(monkeypatch, dbfile)
        hit = await db.execute_fetchall(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'kumquat'"
        )
        assert hit, "armed pending bit did not trigger the next-boot rebuild (bug-060)"
        uv = (await db.execute_fetchall("PRAGMA user_version"))[0][0]
        assert not (uv & 1), "pending bit must be cleared after a successful rebuild"
        await _shutdown()
    finally:
        await _shutdown()
        database._db = saved


# ---------------------------------------------------------------------------
# [19] migration withhold: a failed ladder must not stamp schema_version.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_migration_withholds_stamp_and_retries_next_boot(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "withhold.db")
    saved = database._db
    try:
        # Boot 1: healthy fresh DB stamped at the current version.
        db = await _boot_fresh(monkeypatch, dbfile)
        row = await db.execute_fetchall("SELECT MAX(version) FROM schema_version")
        assert row[0][0] == database.SCHEMA_VERSION

        # Rewind the stamp so the ladder has real steps to run next boot.
        await db.execute("DELETE FROM schema_version")
        await db.execute("INSERT INTO schema_version (version) VALUES (6)")
        await db.commit()
        await _shutdown()

        # Boot 2: first ladder step blows up (simulated locked-DB / disk fault).
        async def _boom(db_, table, column, coldef):
            raise RuntimeError("simulated migration fault")

        monkeypatch.setattr(database, "_ensure_column", _boom)
        db = await _boot_fresh(monkeypatch, dbfile)
        row = await db.execute_fetchall("SELECT MAX(version) FROM schema_version")
        assert row[0][0] == 6, "failed migration must withhold the version stamp (bug-004 contract)"
        # Boot stays non-fatal: the connection is usable.
        assert await db.execute_fetchall("SELECT 1") == [(1,)]
        await _shutdown()

        # Boot 3: fault gone → the idempotent ladder re-runs and stamps current.
        monkeypatch.undo()
        monkeypatch.setattr(database, "DB_PATH", dbfile)
        database._db = None
        db = await database.get_db()
        row = await db.execute_fetchall("SELECT MAX(version) FROM schema_version")
        assert row[0][0] == database.SCHEMA_VERSION, "retry boot did not complete the ladder"
        await _shutdown()
    finally:
        await _shutdown()
        database._db = saved


# ---------------------------------------------------------------------------
# [20] import/merge rollback atomicity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_mid_file_fault_rolls_back_everything(clean_db, tmp_path):
    """A poison record mid-file (bind-incompatible content) aborts the restore:
    rows imported before the fault must be rolled back, the handler reports the
    abort, and the shared write lock is released."""
    db = clean_db
    dump = tmp_path / "restore.jsonl"
    lines = [
        {"_type": "header", "version": 1},
        {"_type": "memory", "agent_id": "imp", "content": "good row one", "timestamp": "t"},
        {"_type": "episode", "agent_id": "imp", "summary": "good episode"},
        # Poison: content is truthy but not bindable as a SQLite parameter.
        {"_type": "memory", "agent_id": "imp", "content": {"nested": "dict"}, "timestamp": "t"},
    ]
    dump.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n", encoding="utf-8")

    result = await admin_handlers.do_import_memories(str(dump))

    assert result["ok"] is False
    assert "rolled back" in result["error"]
    assert not write_lock().locked(), "import abort leaked the shared write lock"
    mems = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = 'imp'")
    eps = await db.execute_fetchall("SELECT id FROM episodes WHERE agent_id = 'imp'")
    assert mems == [] and eps == [], "aborted import left a partial corpus behind"


@pytest.mark.asyncio
async def test_merge_commit_failure_rolls_back_target(clean_db, monkeypatch):
    """If the merge's final commit fails, the target agent must be left exactly
    as it was (no partial copy) and the shared write lock released."""
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('src-m', 'row one', 't')"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('src-m', 'row two', 't')"
    )
    await db.execute("INSERT INTO episodes (agent_id, summary) VALUES ('src-m', 'ep one')")
    await db.commit()

    async def _boom():
        raise RuntimeError("simulated commit fault")

    monkeypatch.setattr(db, "commit", _boom)
    result = await admin_handlers.do_merge_memories("src-m", "tgt-m")
    monkeypatch.undo()

    assert result["ok"] is False
    assert "rolled back" in result["error"]
    assert not write_lock().locked(), "merge abort leaked the shared write lock"
    mems = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = 'tgt-m'")
    eps = await db.execute_fetchall("SELECT id FROM episodes WHERE agent_id = 'tgt-m'")
    assert mems == [] and eps == [], "aborted merge left partial rows on the target"
