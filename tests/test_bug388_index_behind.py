"""bug-388: how far behind the index is counts every row a query reads exactly.

The query path reads three groups from the table on every query: rows written
since the build, and the two groups the build named as holes (a created_at the
file format cannot spell, and rows that had no embedding yet). `status` and the
health check counted only the first, so the ordinary repair -- filling NULL
embeddings, which is what check_health(fix=True) does -- turned a named hole
into a per-query cost that both reported as zero.
"""

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import checks, vector, vector_index
from cpersona.database import get_db
from cpersona.isolation import isolation_where

AGENT = "idxbehind.agent"
DIM = 8


def _blob(seed: int) -> bytes:
    return np.random.default_rng(seed).standard_normal(DIM).astype(np.float32).tobytes()


async def _insert(db, seeds, *, embedded=True, created_at=None):
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, '{}', ?, ?, ?)",
        [
            (
                AGENT, f"row {s}", "2026-03-01T00:00:00+00:00",
                created_at or f"2026-03-01 00:{s // 60 % 60:02d}:{s % 60:02d}",
                _blob(s) if embedded else None,
            )
            for s in seeds
        ],
    )
    await db.commit()


async def _fill_missing_embeddings(db):
    """What check_health(fix=True) does to a row with no embedding."""
    rows = await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND embedding IS NULL", (AGENT,)
    )
    for (row_id,) in rows:
        await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(1000 + row_id), row_id))
    await db.commit()
    return len(rows)


def _clean_index():
    path = vector_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    _clean_index()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    _clean_index()


async def _tail_read(db, index):
    """The rows the query path reads from the table, with a limit that cuts nothing."""
    rows = await vector._index_tail_rows(
        db, index, agent_id=AGENT, project_id=None, channel="", source_id="", scan_limit=10_000
    )
    assert rows is not None
    return rows


@pytest.mark.asyncio
async def test_an_unembedded_hole_filled_after_the_build_is_counted(db):
    # The registry's reproduction: 3 embedded rows, then 5 with no embedding,
    # all below the watermark the build records.
    await _insert(db, range(3))
    await _insert(db, range(3, 8), embedded=False)
    built = await vector_index.build_index(db, "memories")
    assert built["built"] is True, built
    assert built["excluded"] == 0
    assert built["unembedded"] == 5

    assert await _fill_missing_embeddings(db) == 5
    index = vector_index.load_index("memories")
    assert len(await _tail_read(db, index)) == 5

    status = await vector_index._status("memories")
    # rows_since_build keeps its meaning: nothing was written after the build.
    assert status["rows_since_build"] == 0
    # Measured before the fix: the report said 0 while each query read 5.
    assert status["rows_read_exactly"] == 5
    assert status["unembedded"] == 5 and status["excluded"] == 0

    text = vector_index._render(status, as_json=False)
    assert "5 rows read exactly on every query" in text
    assert "5 named at the build" in text


@pytest.mark.asyncio
async def test_a_hole_still_without_an_embedding_costs_nothing_yet(db):
    await _insert(db, range(3))
    await _insert(db, range(3, 8), embedded=False)
    await vector_index.build_index(db, "memories")
    index = vector_index.load_index("memories")
    # The query path skips a row with no embedding, so it is not behind yet.
    assert await _tail_read(db, index) == []
    status = await vector_index._status("memories")
    assert status["rows_read_exactly"] == 0
    assert status["unembedded"] == 5


@pytest.mark.asyncio
async def test_an_excluded_row_is_counted(db):
    await _insert(db, range(5))
    await _insert(db, [5], created_at="2026-03-01T00:00:05Z")  # not the canonical form
    built = await vector_index.build_index(db, "memories")
    assert built["excluded"] == 1 and built["unembedded"] == 0
    index = vector_index.load_index("memories")
    assert len(await _tail_read(db, index)) == 1
    status = await vector_index._status("memories")
    assert status["rows_since_build"] == 0
    assert status["rows_read_exactly"] == 1


@pytest.mark.asyncio
async def test_the_count_is_the_query_paths_read_on_every_group(db):
    """All three groups at once, against the rows the query path actually reads."""
    await _insert(db, range(10))
    await _insert(db, [10], created_at="2026-03-01T00:00:10Z")
    await _insert(db, range(11, 14), embedded=False)
    await vector_index.build_index(db, "memories")
    await _fill_missing_embeddings(db)
    await _insert(db, range(20, 24))  # written since the build
    index = vector_index.load_index("memories")

    read = await _tail_read(db, index)
    counted = await vector_index.rows_read_exactly(
        db, index, "memories", isolation_where(agent_id=AGENT)
    )
    assert counted == len(read) == 1 + 3 + 4
    status = await vector_index._status("memories")
    assert status["rows_since_build"] == 4
    assert status["rows_read_exactly"] == 8


@pytest.mark.asyncio
async def test_the_health_check_reports_filled_holes(db):
    await _insert(db, range(10))
    await _insert(db, range(10, 15), embedded=False)
    await vector_index.build_index(db, "memories")
    assert await checks.check_vector_index(db, AGENT, False) == []

    await _fill_missing_embeddings(db)
    issues = await checks.check_vector_index(db, AGENT, False)
    # Before the fix: no finding, because nothing was written since the build.
    assert [i["type"] for i in issues] == ["vector_index_tail_grown"]
    assert issues[0]["rows_past_watermark"] == 0
    assert issues[0]["rows_read_exactly"] == 5
    assert issues[0]["indexed_rows"] == 10


@pytest.mark.asyncio
async def test_the_health_check_threshold_is_unchanged_for_a_plain_tail(db):
    """Control: with no holes, the number is the tail and the line sits where it sat."""
    await _insert(db, range(50))
    await vector_index.build_index(db, "memories")
    await _insert(db, range(100, 110))  # 20% of the indexed rows: not past the ratio
    assert await checks.check_vector_index(db, AGENT, False) == []
    await _insert(db, [110])
    issues = await checks.check_vector_index(db, AGENT, False)
    assert [i["type"] for i in issues] == ["vector_index_tail_grown"]
    assert issues[0]["rows_past_watermark"] == issues[0]["rows_read_exactly"] == 11
