"""Database connection, schema, and migrations for CPersona."""

import asyncio
import contextlib
import logging
import os

import aiosqlite

from cpersona.config import DB_PATH, FTS_ENABLED

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 13

# bug-042/043: all four data tables share a single aiosqlite connection, and
# aiosqlite has no per-coroutine transaction isolation — any coroutine's
# db.commit() flushes the shared connection's pending transaction, including a
# DIFFERENT coroutine's half-written multi-statement work (import/merge). This
# module-level lock serialises the commit/rollback boundary across every write
# handler + the background queue drain, so no coroutine can commit between
# another's first write and its own commit. Acquired at the LEAF committer only
# (a locked handler must never call another locked handler → asyncio.Lock is not
# reentrant); the queue drain holds it inside do_archive_episode / _delete_task,
# never across the dispatch, so there is no nesting. Uncontended in the common
# single-writer case (only import/merge hold it for a whole loop).
_write_lock = asyncio.Lock()


def write_lock() -> asyncio.Lock:
    """The shared write-serialisation lock (bug-042/043).

    2.5.0 C-seam: production code MUST NOT acquire this directly — use
    ``transaction()`` instead, which owns the [lock … commit/rollback] boundary.
    Exported for tests only (the behavioural harnesses hold it to prove
    serialisation, e.g. test_harness_2500 [17])."""
    return _write_lock


# --------------------------------------------------------------------------------------
# 2.5.0 C-seam: the two connection context managers every DB access goes through.
#
# The seam exists so the storage backend can evolve without touching call sites:
# a future connection pool swaps the internals of these two functions only
# (acquire/release per scope) while every handler keeps its
# `async with connection()/transaction() as db:` shape.
# That is the whole point — no second migration when the backend changes.
#
# 2.5.0b1 (bug-086): reads moved off the writer's connection. WAL snapshot
# isolation only exists *across* connections — on the old shared connection a
# SELECT executed inside another coroutine's open transaction and observed its
# uncommitted rows (dirty read: a dedup probe could match an import row that was
# later rolled back, acknowledging a store as duplicate of a row that ended up
# existing nowhere). The read seam now owns a dedicated read connection.
# --------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def connection():
    """Read seam: yield the dedicated read connection for SELECT use.

    Reads run on their own connection, so WAL gives them snapshot isolation
    from the serialised writer: a reader never observes another coroutine's
    uncommitted transaction (bug-086 dirty-read class). Writing under
    ``connection()`` is a structural-gate violation — a write belongs in
    ``transaction()`` so its commit/rollback boundary is owned.

    Not ``PRAGMA query_only``: FTS5's integrity-check command is spelled as an
    INSERT (``INSERT INTO fts(fts, rank) VALUES('integrity-check', 1)``), so the
    read-only health checks legitimately execute write-shaped statements here.
    Any implicit transaction such a statement opens (sqlite3 BEGINs before DML)
    is rolled back on scope exit below — without that, the connection would
    keep the read transaction open and pin its WAL snapshot forever, serving
    stale rows to every later read in the process."""
    db = await _get_read_db()
    try:
        yield db
    finally:
        if db is not _db and db._conn is not None and db._conn.in_transaction:
            with contextlib.suppress(Exception):
                await db.rollback()


@contextlib.asynccontextmanager
async def transaction():
    """Write seam: write_lock + [writes] + commit, rollback on any exception.

    The single owner of the commit/rollback boundary on the shared connection
    (bug-042/043 serialisation + the bug-068 class: a failed multi-statement
    write must not leave uncommitted rows for the next committer to flush).
    The lock is acquired at the LEAF committer only — a transaction() body must
    never call another transaction()-holding handler (asyncio.Lock is not
    reentrant), and must never perform network I/O (the bug-072 class: an
    embedding HTTP round-trip under the lock stalls every writer; both enforced
    by test_structural_gates)."""
    db = await get_db()
    async with _write_lock:
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def init_db() -> None:
    """Boot-time initialisation: open the connection, run schema + migrations.

    The explicit boot entry point (server startup / test fixtures). Runtime code
    never calls get_db() directly — it goes through connection()/transaction()."""
    await get_db()

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

CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE OF summary, keywords ON episodes
WHEN old.summary <> new.summary OR old.keywords <> new.keywords BEGIN
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

CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE OF content ON memories
WHEN old.content <> new.content BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_db: aiosqlite.Connection | None = None
_read_db: aiosqlite.Connection | None = None
# The write connection _read_db was opened alongside. When the write connection
# is swapped out underneath us (a fresh boot in the test harnesses, close_db +
# re-init), the read connection would otherwise keep serving the OLD database
# file — so _get_read_db() re-keys itself on the current write connection.
_read_db_owner: aiosqlite.Connection | None = None
# Serialises first-touch initialisation: without it two coroutines racing into
# get_db() would both run the (idempotent but committing) migration ladder
# concurrently — the exact contender-commit interleaving the write seam exists
# to prevent (bug-087).
_init_lock = asyncio.Lock()


async def _get_read_db() -> aiosqlite.Connection:
    """Get or create the dedicated read-only connection (bug-086).

    Opened only after get_db() has completed schema + migrations, so the read
    connection never observes a half-initialised database, and re-keyed on the
    current write connection so a rebooted DB (close_db + re-init, or the test
    harnesses' boot simulations) never leaves reads pointed at the old file."""
    global _read_db, _read_db_owner
    write_db = await get_db()
    if _read_db is not None and _read_db_owner is write_db:
        return _read_db
    async with _init_lock:
        write_db = await get_db()
        if _read_db is not None and _read_db_owner is write_db:
            return _read_db
        stale, _read_db, _read_db_owner = _read_db, None, None
        if stale is not None and stale is not write_db:
            with contextlib.suppress(Exception):
                await stale.close()
        if DB_PATH == ":memory:":
            # A second connection to :memory: would open a different, empty DB;
            # fall back to shared-connection semantics for in-memory use.
            _read_db, _read_db_owner = write_db, write_db
            return _read_db
        rdb = await aiosqlite.connect(DB_PATH)
        try:
            await rdb.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            await rdb.close()
            raise
        _read_db, _read_db_owner = rdb, write_db
    return _read_db


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


async def _set_fts_backfill_pending(db: aiosqlite.Connection, pending: bool) -> None:
    """Persist the FTS-backfill-pending flag in PRAGMA user_version bit 0 (bug-060).

    Durable across boots and independent of table presence: set when the FTS
    tables are (re)created or a rebuild fails, cleared only after a rebuild
    succeeds, so a failed backfill is retried on the next boot instead of being
    lost forever once the tables exist. PRAGMA cannot bind params, so the value
    is int-cast and interpolated (never caller-controlled)."""
    row = await db.execute_fetchall("PRAGMA user_version")
    uv = row[0][0] if row else 0
    uv = (uv | 1) if pending else (uv & ~1)
    await db.execute(f"PRAGMA user_version = {int(uv)}")


async def get_db() -> aiosqlite.Connection:
    """Get or create the write connection (schema + migrations complete).

    The global is published only after initialisation succeeds (bug-087): the
    old code assigned ``_db`` right after connect, so a failed migration left
    every later caller on the memoised fast path with a half-initialised
    connection. A failed boot now leaves ``_db`` unset and the connection
    closed; a retried call re-runs the idempotent ladder."""
    global _db
    if _db is not None:
        return _db
    async with _init_lock:
        if _db is not None:
            return _db
        db_dir = os.path.dirname(DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        db = await aiosqlite.connect(DB_PATH)
        try:
            await _init_schema(db)
        except BaseException:
            with contextlib.suppress(Exception):
                await db.close()
            raise
        _db = db
    return _db


async def _init_schema(db: aiosqlite.Connection) -> None:
    """Run pragmas, schema, and the migration ladder on a fresh connection."""
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    # bug-060: a concurrent writer / concurrent boot yields an immediate SQLITE_BUSY
    # under WAL without a busy handler, which would fail the FTS rebuild (and any
    # write) on its first attempt. A short busy_timeout lets SQLite retry-wait
    # instead of erroring out on transient contention.
    await db.execute("PRAGMA busy_timeout=5000")

    await db.executescript(SCHEMA_SQL)

    # bug-026: detect whether the FTS index is being created for the first time on
    # THIS boot (a DB originally created with CPERSONA_FTS_ENABLED=false, now
    # re-enabled). Such a DB was stamped at the current schema with every
    # `current < N and FTS_ENABLED` backfill step skipped, so once the tables are
    # (re)created here they would stay empty for all pre-existing rows and the
    # keyword retriever would silently return zero hits for historical data. The
    # sentinel must be sampled BEFORE executescript(FTS_SQL) creates the tables.
    # External-content FTS5 makes COUNT(*) mirror the content table even when the
    # index is empty, so table-absence is the only reliable "needs backfill" probe.
    fts_created_this_boot = False
    fts_backfill_pending = False
    if FTS_ENABLED:
        existing_fts = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        )
        fts_created_this_boot = not existing_fts
        # bug-060: durable retry flag — a prior boot's rebuild may have failed
        # (transient BUSY/I/O) and set this even though the tables now exist.
        uv_row = await db.execute_fetchall("PRAGMA user_version")
        fts_backfill_pending = bool((uv_row[0][0] if uv_row else 0) & 1)
        # bug-067: ARM the durable pending bit BEFORE creating the (empty) FTS tables and
        # commit it now, whenever a backfill will be needed. executescript(FTS_SQL) does an
        # implicit COMMIT that durably persists the empty tables; if the process is then
        # killed (OOM/redeploy) before the rebuild+commit at the end of get_db(), the old
        # code — which armed the bit only reactively, inside the rebuild's `except` — lost
        # the flag, so the next boot saw tables-present + bit-clear and never re-indexed the
        # historical rows (permanent silent FTS desync). Arming first makes the retry
        # crash-durable: a killed boot leaves bit=1, so the next boot rebuilds regardless.
        if fts_created_this_boot or fts_backfill_pending:
            await _set_fts_backfill_pending(db, True)
            await db.commit()
        await db.executescript(FTS_SQL)

    row = await db.execute_fetchall("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    current = row[0][0] if row else 0

    # Run the migration ladder. Any unexpected failure (locked DB, disk I/O)
    # withholds the version stamp below so the ladder is retried on the next
    # boot rather than being marked complete with a column missing (bug-004).
    # The idempotent existence checks make every step safe to re-run.
    migration_error: Exception | None = None
    try:
        if current < 3:
            await _ensure_column(db, "episodes", "resolved", "INTEGER NOT NULL DEFAULT 0")

        if current < 4 and FTS_ENABLED:
            await db.execute("INSERT OR IGNORE INTO memories_fts(rowid, content) SELECT id, content FROM memories")

        if current < 5 and FTS_ENABLED:
            await db.executescript(
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
            await db.executescript(FTS_SQL)
            await db.execute(
                "INSERT OR IGNORE INTO episodes_fts(rowid, summary, keywords) "
                "SELECT id, summary, keywords FROM episodes"
            )
            await db.execute("INSERT OR IGNORE INTO memories_fts(rowid, content) SELECT id, content FROM memories")

        if current < 6:
            await _ensure_column(db, "memories", "channel", "TEXT NOT NULL DEFAULT ''")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_agent_channel ON memories(agent_id, channel, created_at DESC)"
            )

        if current < 7:
            await _ensure_column(db, "memories", "recall_count", "INTEGER NOT NULL DEFAULT 0")
            await _ensure_column(db, "memories", "last_recalled_at", "TEXT")

        if current < 8:
            await _ensure_column(db, "memories", "locked", "INTEGER NOT NULL DEFAULT 0")

        # v2.4.17: add the project_id γ-semantics axis to all four data tables.
        if current < 9:
            for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
                await _ensure_column(db, table, "project_id", "TEXT NOT NULL DEFAULT ''")

        # v2.4.22: per-channel episodic loop. Episodes gain a `channel` column so
        # archived sessions can be scoped to one conversation channel. Existing
        # episodes default to '' (= unscoped / shared).
        if current < 10:
            await _ensure_column(db, "episodes", "channel", "TEXT NOT NULL DEFAULT ''")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_agent_channel "
                "ON episodes(agent_id, channel, created_at DESC)"
            )

        # v2.4.35 (bug-008): memories_fts lacked an AFTER UPDATE trigger, so
        # in-place content edits (do_update_memory, check_health content fixes)
        # left stale trigrams in the FTS index — old wording kept matching new
        # content (recall contamination). Add the trigger (idempotent via FTS_SQL)
        # and rebuild the index once to clear any contamination already present.
        if current < 11 and FTS_ENABLED:
            await db.executescript(FTS_SQL)
            await db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")

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
            await db.execute(
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
                    await db.execute(index_sql)
                except Exception as e:
                    logger.warning(
                        "dedup unique index creation failed (non-fatal, SELECT dedup remains): %s",
                        e,
                    )

        # bug-012: the v11 AFTER UPDATE triggers fired on every column update,
        # so the recall hot path's recall_count/last_recalled_at bump rewrote
        # each hit's full trigram posting (~20x slower UPDATE, unbounded FTS
        # index bloat). Replace them with column-scoped, content-guarded
        # triggers; the index itself is untouched (identical content), so no
        # rebuild is needed.
        if current < 13 and FTS_ENABLED:
            await db.execute("DROP TRIGGER IF EXISTS memories_fts_au")
            await db.execute("DROP TRIGGER IF EXISTS episodes_au")
            await db.executescript(FTS_SQL)
    except Exception as e:
        migration_error = e
        logger.error(
            "schema migration failed at current=%d (version stamp withheld, will retry next boot): %s",
            current,
            e,
        )

    # bug-026 / bug-060: backfill the FTS index when it was created for the first
    # time this boot (a DB re-enabling FTS) OR when a prior boot's backfill failed
    # and left the durable pending flag set. 'rebuild' repopulates each
    # external-content FTS5 table from its content table, so pre-existing
    # memories/episodes become searchable by the keyword retriever again.
    #
    # bug-060: completion is tracked by the durable user_version bit, NOT by table
    # presence. The old table-absence sentinel fired only the boot the tables were
    # first created, so a swallowed rebuild failure (transient BUSY/I/O) left the
    # historical rows permanently unindexed — the migration ladder still stamped
    # the version and no later boot re-backfilled (tables now existed). Now a
    # failure sets the pending flag so the next boot retries regardless of table
    # presence; success clears it. Still non-fatal so a hiccup never blocks startup
    # (recall degrades to vector/keyword-less RRF, same as a disabled index).
    if FTS_ENABLED and (fts_created_this_boot or fts_backfill_pending):
        try:
            await db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            await db.execute("INSERT INTO episodes_fts(episodes_fts) VALUES ('rebuild')")
            # bug-067: clear the pending bit AND commit it promptly, so the rebuild's
            # durability and the cleared flag land together. The bit was armed+committed
            # before the tables were created; a crash between here and the final commit
            # would at worst leave it armed → one harmless redundant rebuild next boot.
            await _set_fts_backfill_pending(db, False)
            await db.commit()
        except Exception as e:
            # Belt-and-suspenders: the bit was already durably armed before table creation
            # (bug-067), so it stays 1 for the next-boot retry even if this write is lost.
            await _set_fts_backfill_pending(db, True)
            logger.warning("FTS first-boot backfill failed (non-fatal, will retry next boot): %s", e)

    # The isolation index depends on the v2.4.17 project_id column. Run it
    # after the migration so v8 boots get the index once the columns exist;
    # CREATE INDEX IF NOT EXISTS keeps it idempotent. Non-fatal on its own.
    try:
        await db.executescript(ISOLATION_INDEX_SQL)
    except Exception as e:
        logger.warning("isolation index creation failed (non-fatal): %s", e)

    # Only advance the recorded version if the ladder completed cleanly. A
    # withheld stamp leaves `current` unchanged so the next boot re-runs the
    # idempotent steps rather than skipping a step that never applied.
    if migration_error is None and current < SCHEMA_VERSION:
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    await db.commit()



async def close_db():
    """Close the write and read connections."""
    global _db, _read_db, _read_db_owner
    if _read_db is not None:
        if _read_db is not _db:
            await _read_db.close()
        _read_db = None
    _read_db_owner = None
    if _db is not None:
        await _db.close()
        _db = None
