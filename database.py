"""Database connection, schema, and migrations for CPersona."""

import logging
import os

import aiosqlite

from config import DB_PATH, FTS_ENABLED

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 8

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
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
    user_id    TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, user_id)
);

CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    summary    TEXT NOT NULL,
    keywords   TEXT NOT NULL DEFAULT '',
    embedding  BLOB,
    start_time TEXT,
    end_time   TEXT,
    resolved   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_agent
    ON episodes(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pending_memory_tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type  TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    retries    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
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
"""

_db: aiosqlite.Connection | None = None


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

    if current < 3:
        try:
            await _db.execute("ALTER TABLE episodes ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

    if current < 4 and FTS_ENABLED:
        try:
            await _db.execute("INSERT OR IGNORE INTO memories_fts(rowid, content) SELECT id, content FROM memories")
        except Exception:
            pass

    if current < 5 and FTS_ENABLED:
        try:
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
        except Exception as e:
            logger.warning("FTS trigram migration failed (non-fatal): %s", e)

    if current < 6:
        try:
            await _db.execute("ALTER TABLE memories ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
            await _db.execute("UPDATE memories SET channel = 'chat' WHERE channel = ''")
        except Exception:
            pass
        await _db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_agent_channel ON memories(agent_id, channel, created_at DESC)"
        )

    if current < 7:
        try:
            await _db.execute("ALTER TABLE memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            await _db.execute("ALTER TABLE memories ADD COLUMN last_recalled_at TEXT")
        except Exception:
            pass

    if current < 8:
        try:
            await _db.execute("ALTER TABLE memories ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

    if current < SCHEMA_VERSION:
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
