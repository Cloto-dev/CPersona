"""bug-439: quick_check must not call an intact FTS5 index malformed.

FTS5 caches a table's segment structure on the connection that last read it, and
PRAGMA quick_check verifies the index against that cache. check_sqlite_integrity
runs on the shared read connection, which lives as long as the process: once it
has read the index and the write connection then optimizes it (or writes enough
to merge segments), quick_check on the read connection reports "malformed
inverted index" for an index every other connection reads as intact. The health
report then said critical corruption, telling an operator to restore from backup.

It reached the suite as an order-dependent failure: enough FTS writes in earlier
test files moved the index under the read connection's cache.

Older SQLite does not look inside FTS5 indexes in quick_check at all (3.34.1 and
3.40.1, Debian 12's, return ok for an index with every block overwritten). There
the false alarm cannot happen and the damage cannot be reported by quick_check, so
the two tests that need it are skipped, and the last test holds the check that
does report it on every SQLite.
"""

import sqlite3

import pytest
import pytest_asyncio

from cpersona import checks
from cpersona.database import connection, get_db


def _quick_check_reads_fts5() -> bool:
    """Whether this SQLite's quick_check inspects an FTS5 index at all.

    Asked of the library, not of its version number: a scratch index is damaged
    the way the tests below damage memories_fts, and quick_check either sees it
    or does not.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        conn.executemany("INSERT INTO t(x) VALUES (?)", [(f"row {i} apples bananas",) for i in range(30)])
        # Committed before the damage: until then FTS5 holds the new terms in
        # memory and t_data has no leaf blocks to overwrite.
        conn.commit()
        conn.execute("UPDATE t_data SET block = X'00' WHERE id > 10")
        conn.commit()
        return conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


needs_quick_check_over_fts5 = pytest.mark.skipif(
    not _quick_check_reads_fts5(),
    reason="this SQLite's PRAGMA quick_check does not inspect FTS5 indexes",
)


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


async def _index_rows_then_cache_the_structure_on_the_read_connection(db):
    for i in range(30):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES ('bug439', ?, '')",
            (f"row {i} apples bananas",),
        )
    await db.commit()
    async with connection() as rdb:
        await rdb.execute_fetchall("SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'apples'")


@needs_quick_check_over_fts5
@pytest.mark.asyncio
async def test_an_index_optimized_under_the_read_connections_cache_is_not_reported_corrupt(db):
    await _index_rows_then_cache_the_structure_on_the_read_connection(db)
    await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('optimize')")
    await db.commit()

    async with connection() as rdb:
        # The precondition this test exists for: the read connection's own
        # quick_check does report the stale cache. Without it the test proves nothing.
        assert await rdb.execute_fetchall("PRAGMA quick_check") != [("ok",)]
        assert await checks.check_sqlite_integrity(rdb, "", fix=False) == []


@needs_quick_check_over_fts5
@pytest.mark.asyncio
async def test_a_genuinely_damaged_index_is_still_reported(db):
    await _index_rows_then_cache_the_structure_on_the_read_connection(db)
    await db.execute("UPDATE memories_fts_data SET block = X'00' WHERE id > 10")
    await db.commit()
    try:
        async with connection() as rdb:
            (issue,) = await checks.check_sqlite_integrity(rdb, "", fix=False)
        assert issue["type"] == "sqlite_integrity_failure"
        assert issue["severity"] == "critical"
        assert any("fts5" in str(m).lower() or "malformed" in str(m).lower() for m in issue["detail"])
    finally:
        await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        await db.commit()


@pytest.mark.asyncio
async def test_a_damaged_index_is_reported_by_the_fts_check_whatever_quick_check_sees(db):
    # Not skipped anywhere: where quick_check does not inspect FTS5 this is the
    # health check that reports the damage, and the read connection is the one
    # a report-only health run gives it.
    await _index_rows_then_cache_the_structure_on_the_read_connection(db)
    await db.execute("UPDATE memories_fts_data SET block = X'00' WHERE id > 10")
    await db.commit()
    try:
        async with connection() as rdb:
            issues = await checks.check_fts_integrity(rdb, "", fix=False)
        memories = [i for i in issues if i["table"] == "memories"]
        assert [(i["type"], i["severity"]) for i in memories] == [("fts_integrity_failure", "critical")]
        # The probe that skips the two tests above must agree with what quick_check
        # says about this same damage, or it would skip them where they can run.
        seen = await checks._quick_check_on_a_fresh_connection()
        assert (seen != ["ok"]) == _quick_check_reads_fts5()
    finally:
        await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        await db.commit()
