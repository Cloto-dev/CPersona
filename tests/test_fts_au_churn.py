"""Regression tests for bug-012 — FTS AU trigger churn on the recall hot path.

The v11 AFTER UPDATE triggers fired on every column update, so do_recall's
recall_count/last_recalled_at bump rewrote each hit's full trigram posting on
every recall (~20x slower UPDATE, unbounded FTS index bloat). The v13 triggers
are column-scoped and content-guarded; these tests pin both sides of the fix:

- metadata-only UPDATEs (the hot path) must not touch the FTS index;
- real content edits must still reindex (the bug-008 guarantee is preserved);
- the v12 -> v13 migration must replace the old trigger definitions in place.
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Hermetic DB + embeddings-off before importing any cpersona module.
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_fts_au_churn.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import database  # noqa: E402
from cpersona.database import get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


async def _fts_index_writes(db) -> int:
    """Total FTS b-tree payload bytes — grows iff the index was written."""
    rows = await db.execute_fetchall(
        "SELECT COALESCE(sum(length(block)), 0) FROM memories_fts_data"
    )
    return rows[0][0]


async def _insert_memory(db, content: str) -> int:
    cur = await db.execute(
        "INSERT INTO memories(agent_id, content, timestamp) "
        "VALUES ('t', ?, datetime('now'))",
        (content,),
    )
    await db.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_recall_count_bump_does_not_touch_fts():
    """The do_recall hot-path UPDATE (metadata columns only) must be a no-op
    for the FTS index."""
    db = await get_db()
    mid = await _insert_memory(db, "alpha beta gamma delta content")
    before = await _fts_index_writes(db)
    for _ in range(5):
        await db.execute(
            "UPDATE memories SET recall_count = recall_count + 1, "
            "last_recalled_at = datetime('now') WHERE id = ?",
            (mid,),
        )
        await db.commit()
    assert await _fts_index_writes(db) == before


@pytest.mark.asyncio
async def test_content_update_still_reindexes():
    """bug-008 guarantee: an in-place content edit must replace the old
    trigrams with the new ones."""
    db = await get_db()
    mid = await _insert_memory(db, "original wording here")
    await db.execute(
        "UPDATE memories SET content = ? WHERE id = ?", ("rewritten text now", mid)
    )
    await db.commit()
    old = await db.execute_fetchall(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH '\"original\"'"
    )
    new = await db.execute_fetchall(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH '\"rewritten\"'"
    )
    assert old == []
    assert [r[0] for r in new] == [mid]
    # external-content self-check: index and content table agree
    await db.execute("INSERT INTO memories_fts(memories_fts, rank) VALUES ('integrity-check', 1)")


@pytest.mark.asyncio
async def test_episode_embedding_update_does_not_touch_fts():
    """Maintenance embedding-only episode UPDATEs must not churn episodes_fts."""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO episodes(agent_id, summary, keywords) "
        "VALUES ('t', 'session summary', 'kw1 kw2')"
    )
    await db.commit()
    eid = cur.lastrowid
    rows = await db.execute_fetchall(
        "SELECT COALESCE(sum(length(block)), 0) FROM episodes_fts_data"
    )
    before = rows[0][0]
    await db.execute(
        "UPDATE episodes SET embedding = ? WHERE id = ?", (b"\x00" * 8, eid)
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT COALESCE(sum(length(block)), 0) FROM episodes_fts_data"
    )
    assert rows[0][0] == before


@pytest.mark.asyncio
async def test_trigger_definitions_are_column_scoped():
    """The live schema must carry the v13 trigger shape (UPDATE OF + WHEN)."""
    db = await get_db()
    for trig, needle in (
        ("memories_fts_au", "AFTER UPDATE OF content ON memories"),
        ("episodes_au", "AFTER UPDATE OF summary, keywords ON episodes"),
    ):
        rows = await db.execute_fetchall(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trig,)
        )
        assert rows, f"{trig} missing"
        assert needle in rows[0][0]
        assert "WHEN" in rows[0][0]


@pytest.mark.asyncio
async def test_v12_to_v13_migration_replaces_triggers():
    """A DB stamped at v12 with the old broad triggers gets them swapped for
    the column-scoped definitions on the next boot."""
    import aiosqlite

    path = os.path.join(_tmpdir, "migrate_v12.db")
    old_au = (
        "CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, content) "
        "VALUES ('delete', old.id, old.content); "
        "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END"
    )
    seed = await aiosqlite.connect(path)
    await seed.executescript(database.SCHEMA_SQL)
    await seed.executescript(database.FTS_SQL)
    # Recreate the v11-era broad trigger over the v13 definition, stamp v12.
    await seed.execute("DROP TRIGGER memories_fts_au")
    await seed.execute(old_au)
    await seed.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (12)")
    await seed.commit()
    await seed.close()

    saved_db, saved_path = database._db, database.DB_PATH
    database._db, database.DB_PATH = None, path
    try:
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memories_fts_au'"
        )
        assert "AFTER UPDATE OF content ON memories" in rows[0][0]
        ver = await db.execute_fetchall(
            "SELECT MAX(version) FROM schema_version"
        )
        assert ver[0][0] == 13
        await database.close_db()
    finally:
        database._db, database.DB_PATH = saved_db, saved_path
