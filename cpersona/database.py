"""Database connection, schema, and migrations for CPersona."""

import logging
import os

import aiosqlite

from cpersona.config import DB_PATH, FTS_ENABLED

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 12

# v2.4.17: project_id is a second isolation axis layered on top of agent_id,
# giving agent_id × project_id two-tier γ semantics.
#   - write: omitted → stored as '' (= global pool)
#   - read:  project_id='X' matches the union of 'X' and '' (global pool)
# Existing rows get project_id='' via the v9 migration, so the change is
# backward compatible (legacy data behaves as the shared global pool).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    msg_id     TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '{}',
    timestamp  TEXT NOT NULL,
    metadata   TEXT NOT NULL DEFAULT '{}',
    embedding  BLOB,
    channel    TEXT NOT NULL DEFAULT '',
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT,
    locked     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_agent
    ON memories(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_msg_id
    ON memories(agent_id, msg_id);

CREATE TABLE IF NOT EXISTS profiles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    user_id    TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, user_id)
);

CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL,
    keywords   TEXT NOT NULL DEFAULT '',
    embedding  BLOB,
    start_time TEXT,
    end_time   TEXT,
    resolved   INTEGER NOT NULL DEFAULT 0,
    channel    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_agent
    ON episodes(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pending_memory_tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type  TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL,
    retries    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# v2.4.17: the project_id isolation index depends on a column that CREATE
# TABLE IF NOT EXISTS will not add to an existing table. It is kept out of
# SCHEMA_SQL so a v8 DB does not fail on "no such column" at boot, and is
# run after the v9 ALTER TABLE migration instead. CREATE INDEX IF NOT
# EXISTS makes it idempotent for fresh DBs and v9+ boots alike.
ISOLATION_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_memories_isolation
    ON memories(agent_id, project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_isolation
    ON episodes(agent_id, project_id, created_at DESC);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary,
    keywords,
    content=episodes,
    content_rowid=id,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary, keywords)
    VALUES (new.id, new.summary, new.keywords);
END;

CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, keywords)
    VALUES ('delete', old.id, old.summary, old.keywords);
END;

CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, keywords)
    VALUES ('delete', old.id, old.summary, old.keywords);
    INSERT INTO episodes_fts(rowid, summary, keywords)
    VALUES (new.id, new.summary, new.keywords);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content=memories,
    content_rowid=id,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_db: aiosqlite.Connection | None = None


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, coldef: str) -> None:
    """Idempotently add a column via an existence check (not a swallowed error).

    The previous migrations wrapped each ALTER in a bare ``except Exception: pass``
    to tolerate the "duplicate column" case on re-run, but that also swallowed
    genuine failures (database is locked / disk I/O), leaving the column missing
    while the migration was still stamped complete (bug-004). Checking
    PRAGMA table_info first means the normal "already applied" path never raises,
    so any exception that does propagate is a real failure the caller must act on.
    """
    cols = await db.execute_fetchall(f"PRAGMA table_info({table})")
    if column not in {c[1] for c in cols}:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


async def get_db() -> aiosqlite.Connection:
    """Get or create the database connection."""
    global _db
    if _db is not None:
        return _db

    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    _db = await aiosqlite.connect(DB_PATH)
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")

    await _db.executescript(SCHEMA_SQL)

    if FTS_ENABLED:
        await _db.executescript(FTS_SQL)

    row = await _db.execute_fetchall("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    current = row[0][0] if row else 0

    # Run the migration ladder. Any unexpected failure (locked DB, disk I/O)
    # withholds the version stamp below so the ladder is retried on the next
    # boot rather than being marked complete with a column missing (bug-004).
    # The idempotent existence checks make every step safe to re-run.
    migration_error: Exception | None = None
    try:
        if current < 3:
            await _ensure_column(_db, "episodes", "resolved", "INTEGER NOT NULL DEFAULT 0")

        if current < 4 and FTS_ENABLED:
            await _db.execute("INSERT OR IGNORE INTO memories_fts(rowid, content) SELECT id, content FROM memories")

        if current < 5 and FTS_ENABLED:
            await _db.executescript(
                """
                DROP TRIGGER IF EXISTS episodes_ai;
                DROP TRIGGER IF EXISTS episodes_ad;
                DROP TRIGGER IF EXISTS episodes_au;
                DROP TRIGGER IF EXISTS memories_fts_ai;
                DROP TRIGGER IF EXISTS memories_fts_ad;
                DROP TABLE IF EXISTS episodes_fts;
                DROP TABLE IF EXISTS memories_fts;
                """
            )
            await _db.executescript(FTS_SQL)
            await _db.execute(
                "INSERT OR IGNORE INTO episodes_fts(rowid, summary, keywords) "
                "SELECT id, summary, keywords FROM episodes"
            )
            await _db.execute("INSERT OR IGNORE INTO memories_fts(rowid, content) SELECT id, content FROM memories")

        if current < 6:
            await _ensure_column(_db, "memories", "channel", "TEXT NOT NULL DEFAULT ''")
            await _db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_agent_channel ON memories(agent_id, channel, created_at DESC)"
            )

        if current < 7:
            await _ensure_column(_db, "memories", "recall_count", "INTEGER NOT NULL DEFAULT 0")
            await _ensure_column(_db, "memories", "last_recalled_at", "TEXT")

        if current < 8:
            await _ensure_column(_db, "memories", "locked", "INTEGER NOT NULL DEFAULT 0")

        # v2.4.17: add the project_id γ-semantics axis to all four data tables.
        if current < 9:
            for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
                await _ensure_column(_db, table, "project_id", "TEXT NOT NULL DEFAULT ''")

        # v2.4.22: per-channel episodic loop. Episodes gain a `channel` column so
        # archived sessions can be scoped to one conversation channel. Existing
        # episodes default to '' (= unscoped / shared).
        if current < 10:
            await _ensure_column(_db, "episodes", "channel", "TEXT NOT NULL DEFAULT ''")
            await _db.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_agent_channel "
                "ON episodes(agent_id, channel, created_at DESC)"
            )

        # v2.4.35 (bug-008): memories_fts lacked an AFTER UPDATE trigger, so
        # in-place content edits (do_update_memory, check_health content fixes)
        # left stale trigrams in the FTS index — old wording kept matching new
        # content (recall contamination). Add the trigger (idempotent via FTS_SQL)
        # and rebuild the index once to clear any contamination already present.
        if current < 11 and FTS_ENABLED:
            await _db.executescript(FTS_SQL)
            await _db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")

        # v2.4.36 (bug-010): do_store's SELECT-probe dedup and post-commit
        # `SELECT id ... ORDER BY id DESC` id lookup both race concurrent
        # stores on the shared connection. The store path now uses
        # INSERT OR IGNORE + cursor.lastrowid; these UNIQUE indexes are the
        # constraint OR IGNORE resolves against. Unlocked exact duplicates are
        # collapsed first with the same keep-MIN(id), never-touch-locked
        # policy as check_health's duplicate_content repair. If a locked
        # duplicate still blocks index creation, the index is skipped
        # non-fatally below (SELECT-based dedup stays as the fallback), same
        # doctrine as the isolation index.
        if current < 12:
            await _db.execute(
                """DELETE FROM memories
                   WHERE locked = 0
                     AND id NOT IN (
                         SELECT MIN(id) FROM memories
                         GROUP BY agent_id, project_id, channel, content
                     )"""
            )
            for index_sql in (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedup_content "
                "ON memories(agent_id, project_id, channel, content)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedup_msg_id "
                "ON memories(agent_id, project_id, msg_id) WHERE msg_id != ''",
            ):
                try:
                    await _db.execute(index_sql)
                except Exception as e:
                    logger.warning(
                        "dedup unique index creation failed (non-fatal, SELECT dedup remains): %s",
                        e,
                    )
    except Exception as e:
        migration_error = e
        logger.error(
            "schema migration failed at current=%d (version stamp withheld, will retry next boot): %s",
            current,
            e,
        )

    # The isolation index depends on the v2.4.17 project_id column. Run it
    # after the migration so v8 boots get the index once the columns exist;
    # CREATE INDEX IF NOT EXISTS keeps it idempotent. Non-fatal on its own.
    try:
        await _db.executescript(ISOLATION_INDEX_SQL)
    except Exception as e:
        logger.warning("isolation index creation failed (non-fatal): %s", e)

    # Only advance the recorded version if the ladder completed cleanly. A
    # withheld stamp leaves `current` unchanged so the next boot re-runs the
    # idempotent steps rather than skipping a step that never applied.
    if migration_error is None and current < SCHEMA_VERSION:
        await _db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    await _db.commit()

    return _db


async def close_db():
    """Close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
