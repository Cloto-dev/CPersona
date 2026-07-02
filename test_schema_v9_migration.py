"""Tests for the SCHEMA_VERSION 8 → 9 project_id migration (Phase 3-β-3a).

Covers upstream cloto-mcp-servers/servers/cpersona patch v2.4.17 (5959957) —
the irreversible schema half: project_id column on the four data tables,
the PRAGMA-guarded ALTER TABLE migration, and ISOLATION_INDEX_SQL.

These tests build their own DB files and drive `get_db()` directly, so they
save / restore the module-global connection and `config.DB_PATH` rather than
using the shared autouse fixture other test files rely on.
"""

import os
import tempfile

import aiosqlite
import pytest

from cpersona import database
from cpersona.database import SCHEMA_VERSION

# v8 schema as it stood before the project_id axis was added (v2.4.10 era).
_V8_SCHEMA = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    msg_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '{}',
    timestamp TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    embedding BLOB,
    channel TEXT NOT NULL DEFAULT '',
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, user_id)
);
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    start_time TEXT,
    end_time TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE pending_memory_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    retries INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_ISOLATED_TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks")


async def _columns(db, table: str) -> set[str]:
    return {c[1] for c in await db.execute_fetchall(f"PRAGMA table_info({table})")}


async def _schema_version(db) -> int:
    rows = await db.execute_fetchall("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    return rows[0][0] if rows else 0


async def _index_names(db) -> set[str]:
    return {r[0] for r in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='index'")}


class _TempDB:
    """Point get_db() at a throwaway file, restoring global state on exit.

    get_db() resolves the path via the module-level ``database.DB_PATH`` name
    (bound from ``config`` at import time), so that — not ``config.DB_PATH`` —
    is what must be swapped.
    """

    def __init__(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "schema_test.db")
        self._saved_db = None
        self._saved_path = None

    async def __aenter__(self):
        self._saved_db = database._db
        self._saved_path = database.DB_PATH
        database._db = None
        database.DB_PATH = self.path
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db = self._saved_db
        database.DB_PATH = self._saved_path


@pytest.mark.asyncio
async def test_schema_version_constant_is_11():
    """The migration target constant must be 11 (v11 = memories_fts AU trigger, bug-008)."""
    assert SCHEMA_VERSION == 11


@pytest.mark.asyncio
async def test_fresh_db_creates_current_schema():
    """A brand-new DB is stamped at the current version with project_id on every
    data table and a channel column on episodes."""
    async with _TempDB() as tmp:
        db = await database.get_db()
        for table in _ISOLATED_TABLES:
            assert "project_id" in await _columns(db, table), f"{table} missing project_id"
        assert "channel" in await _columns(db, "episodes"), "episodes missing channel"
        assert await _schema_version(db) == 11
        indexes = await _index_names(db)
        assert "idx_memories_isolation" in indexes
        assert "idx_episodes_isolation" in indexes
        assert "idx_episodes_agent_channel" in indexes
        _ = tmp  # silence unused


@pytest.mark.asyncio
async def test_v8_to_v9_migration_preserves_legacy_rows():
    """A real v8 DB migrates in place: columns added, rows kept, version stamped."""
    async with _TempDB() as tmp:
        # Hand-roll a v8 DB with a legacy row in each table.
        conn = await aiosqlite.connect(tmp.path)
        await conn.executescript(_V8_SCHEMA)
        await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
            ("legacy-agent", "legacy memory", "2026-01-01T00:00:00Z"),
        )
        await conn.execute(
            "INSERT INTO episodes (agent_id, summary) VALUES (?, ?)",
            ("legacy-agent", "legacy episode"),
        )
        await conn.execute("INSERT INTO schema_version (version) VALUES (8)")
        await conn.commit()
        await conn.close()

        # Boot through get_db() — this runs the real v9 + v10 migration path.
        db = await database.get_db()

        for table in _ISOLATED_TABLES:
            assert "project_id" in await _columns(db, table), f"{table} missing project_id after migration"
        assert "channel" in await _columns(db, "episodes"), "episodes missing channel after migration"
        assert await _schema_version(db) == 11

        # Legacy rows survive and default to the global pool (project_id = '')
        # and the unscoped channel ('').
        mem = await db.execute_fetchall(
            "SELECT content, project_id FROM memories WHERE agent_id = 'legacy-agent'"
        )
        assert mem == [("legacy memory", "")]
        ep = await db.execute_fetchall(
            "SELECT summary, project_id, channel FROM episodes WHERE agent_id = 'legacy-agent'"
        )
        assert ep == [("legacy episode", "", "")]

        # Isolation + channel indexes are created once the columns exist.
        indexes = await _index_names(db)
        assert "idx_memories_isolation" in indexes
        assert "idx_episodes_isolation" in indexes
        assert "idx_episodes_agent_channel" in indexes


@pytest.mark.asyncio
async def test_v9_migration_is_idempotent():
    """Re-running get_db() on an already-migrated v9 DB is a no-op, not an error."""
    async with _TempDB() as tmp:
        # First boot: fresh → v9.
        db = await database.get_db()
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, project_id) VALUES (?, ?, ?, ?)",
            ("agent-x", "tagged memory", "2026-05-14T00:00:00Z", "proj-1"),
        )
        await db.commit()
        await database.close_db()
        database._db = None

        # Second boot on the same file: must not raise, must keep the row.
        db = await database.get_db()
        assert await _schema_version(db) == 11
        rows = await db.execute_fetchall(
            "SELECT content, project_id FROM memories WHERE agent_id = 'agent-x'"
        )
        assert rows == [("tagged memory", "proj-1")]
        _ = tmp


# v9 episodes table as it stood before the channel axis (project_id present,
# channel absent). Used to drive the v9 → v10 migration in isolation.
_V9_EPISODES = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    start_time TEXT,
    end_time TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@pytest.mark.asyncio
async def test_v9_to_v10_adds_episode_channel():
    """A v9 DB migrates to v10: episodes gains channel, legacy rows default ''."""
    async with _TempDB() as tmp:
        # Hand-roll a v9 DB with an episodes table that has no channel column.
        conn = await aiosqlite.connect(tmp.path)
        await conn.executescript(_V9_EPISODES)
        await conn.execute(
            "INSERT INTO episodes (agent_id, summary) VALUES (?, ?)",
            ("legacy-agent", "pre-channel episode"),
        )
        await conn.execute("INSERT INTO schema_version (version) VALUES (9)")
        await conn.commit()
        await conn.close()

        # Boot through get_db() — runs the real v10 migration path.
        db = await database.get_db()

        assert "channel" in await _columns(db, "episodes"), "episodes missing channel after v10"
        assert await _schema_version(db) == 11

        # Legacy episode survives and defaults to the unscoped channel ('').
        ep = await db.execute_fetchall(
            "SELECT summary, channel FROM episodes WHERE agent_id = 'legacy-agent'"
        )
        assert ep == [("pre-channel episode", "")]

        assert "idx_episodes_agent_channel" in await _index_names(db)
