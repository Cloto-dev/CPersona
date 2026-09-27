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
"""

import pytest
import pytest_asyncio

from cpersona import checks
from cpersona.database import connection, get_db


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
