"""bug-285: the lost-embedding probe is bounded by the selection's shape, not its size.

The probe that guards the contiguous index (bug-279) bound one SQL parameter per
selected id, and the selection is the scan window. Above the build's
SQLITE_MAX_VARIABLE_NUMBER (32,766 on the default build) that raised
`too many SQL variables`, and because the call sat outside every guard in
`_index_phase1` the error escaped `_search_vector`: every recall failed instead
of falling back to the scan. It reproduced only on builds shipping the default
limit, which is why a 100,000-row window measured fine on one machine and
failed on another.

Three claims, each pinned so that undoing one part of the repair turns exactly
one test red:

1. A contiguous selection is asked as a range: two parameters, whatever the
   window. The limit is lowered below the selection size so the old `IN`
   statement cannot run, and the probe must still *detect* a cleared embedding
   (the answer alone would not show which statement ran — the fallback answers
   correctly too).
2. A scattered selection is asked in chunks that fit the limit, and still
   detects a cleared embedding through the chunk that holds it.
3. Whatever the probe raises, recall returns the scan's answer.

The limit is lowered with `setlimit` on the live connection so the failure is
reproducible on every build rather than only on ones that ship the default.
"""

from __future__ import annotations

import logging
import os
import sqlite3

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from tests.conftest import fake_embed_one

AGENT = "bug285.agent"
TOPIC = "alpha beta gamma"
_SHARED = fake_embed_one(f"{TOPIC} shared")
ROWS = 40
# What the guard logs when the probe raised, and what the probe's caller logs
# when it detected a cleared embedding. The tests tell them apart.
_RAISED = "Vector index probe raised"
_DETECTED = "embedding was cleared since the build"


def _index_paths() -> tuple[str, str]:
    path = vector_index.index_path("memories")
    return path, path + ".tmp"


async def _store(db, content, *, created_at, source_id):
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, ?, ?, ?, ?)",
        (
            AGENT, content,
            '{"type": "User", "id": "%s"}' % source_id,
            "2026-03-01T00:00:00+00:00", created_at,
            np.array(_SHARED, dtype=np.float32).tobytes(),
        ),
    )


@pytest_asyncio.fixture
async def corpus(fake_embedding_client):
    db = await get_db()
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)
    await db.execute("DELETE FROM memories")
    # Alternating source ids: a source filter selects every other row, which
    # is the scattered shape; no filter selects the whole contiguous run.
    for n in range(ROWS):
        await _store(
            db, f"row {n}",
            created_at=f"2026-03-01 00:{n // 60:02d}:{n % 60:02d}",
            source_id="even" if n % 2 == 0 else "odd",
        )
    await db.commit()
    assert (await vector_index.build_index(db, "memories"))["built"]
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)


@pytest_asyncio.fixture
async def variable_limit(corpus):
    """Lower SQLITE_MAX_VARIABLE_NUMBER on the shared connection for one test.

    The sqlite3 connection is owned by aiosqlite's worker thread, so the limit
    calls are handed to that thread rather than made from the test's.
    """
    conn = corpus._conn
    before = await corpus._execute(conn.getlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)

    async def lower(to: int):
        await corpus._execute(conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, to)
        assert await corpus._execute(conn.getlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) == to

    yield lower
    await corpus._execute(conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, before)


async def _search(db, **kw):
    return await vector._search_vector(db, AGENT, TOPIC, 10, min_similarity=-1.0, **kw)


async def _answer_without_the_index(db, **kw):
    keep = {}
    for path in _index_paths():
        if os.path.exists(path):
            keep[path] = open(path, "rb").read()
            os.unlink(path)
    try:
        return await _search(db, **kw)
    finally:
        for path, blob in keep.items():
            with open(path, "wb") as fh:
                fh.write(blob)


async def _clear_embedding_of(db, content: str) -> None:
    await db.execute("UPDATE memories SET embedding = NULL WHERE content = ?", (content,))
    await db.commit()


@pytest.mark.asyncio
async def test_a_contiguous_selection_is_probed_with_two_parameters(
    corpus, variable_limit, caplog
):
    """Limit below the selection size: only the range form can run at all."""
    await variable_limit(14)  # the hydrate binds limit + 2; IN (40) + 1 cannot run
    await _clear_embedding_of(corpus, "row 7")

    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        with_index = await _search(corpus)
    without_index = await _answer_without_the_index(corpus)

    assert with_index == without_index
    assert _RAISED not in caplog.text, "the probe raised instead of running in range form"
    assert _DETECTED in caplog.text, (
        "the probe ran but did not detect the cleared embedding — the answer "
        "agreeing with the scan proves nothing on its own, the fallback agrees too"
    )


@pytest.mark.asyncio
async def test_a_scattered_selection_is_probed_in_chunks(
    corpus, variable_limit, caplog, monkeypatch
):
    """Every other row selected: no range covers it, the chunks must fit."""
    await variable_limit(14)
    monkeypatch.setattr(vector, "_LOST_EMBEDDING_PROBE_CHUNK", 6)
    # The cleared row sits in the LAST chunk of 20 selected ids, so a probe that
    # stopped after the first chunk would miss it.
    await _clear_embedding_of(corpus, "row 2")  # newest-first: row 2 is near the end

    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        with_index = await _search(corpus, source_id="even")
    without_index = await _answer_without_the_index(corpus, source_id="even")

    assert with_index == without_index
    assert _RAISED not in caplog.text, "the probe raised instead of chunking"
    assert _DETECTED in caplog.text, "the chunked probe missed the cleared embedding"
    assert with_index, "an empty answer on both sides would make the agreement vacuous"


@pytest.mark.asyncio
async def test_a_scattered_selection_with_nothing_lost_stays_on_the_index(
    corpus, variable_limit, caplog, monkeypatch
):
    """The positive side of chunking: no false detection, no fallback."""
    await variable_limit(14)
    monkeypatch.setattr(vector, "_LOST_EMBEDDING_PROBE_CHUNK", 6)

    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        with_index = await _search(corpus, source_id="odd")

    assert with_index == await _answer_without_the_index(corpus, source_id="odd")
    assert _RAISED not in caplog.text
    assert _DETECTED not in caplog.text, "nothing was cleared, yet the probe fell back"


@pytest.mark.asyncio
async def test_a_raising_probe_still_returns_the_scans_answer(corpus, monkeypatch, caplog):
    """Not "this error is gone" — whatever the probe raises stays inside recall."""
    expected = await _answer_without_the_index(corpus)

    async def exploding(*args, **kwargs):
        raise sqlite3.OperationalError("too many SQL variables")

    monkeypatch.setattr(vector, "_index_rows_lost_embedding", exploding)
    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        assert await _search(corpus) == expected, "recall did not survive a raising probe"
    assert _RAISED in caplog.text


@pytest.mark.asyncio
async def test_the_probe_is_driven_by_the_id_term_not_the_isolation_index(corpus):
    """The unary plus on agent_id is load-bearing, so the plan is pinned.

    Without it the planner constrains the isolation index with the agent term
    and walks every row of the agent per statement — 43 ms instead of 25 for
    the range form and 3.0 s instead of 41 ms for the chunked form at 100,000
    rows, because every chunk repeats the walk.

    The id term is now served by the partial index rather than by the table's
    rowid (that index is what makes the probe cost microseconds instead of a
    walk); either is the id term driving the statement, and neither is the
    isolation index this test exists to keep out.
    """
    for sql, params in (
        (vector._LOST_EMBEDDING_RANGE_SQL, (1, 40, AGENT)),
        (vector._lost_embedding_chunk_sql(3), (1, 2, 3, AGENT)),
    ):
        plan = " ".join(
            row[-1] for row in await corpus.execute_fetchall("EXPLAIN QUERY PLAN " + sql, params)
        )
        assert "INTEGER PRIMARY KEY" in plan or "idx_memories_lost_embedding" in plan, plan
        assert "idx_memories_isolation" not in plan, plan
        assert "idx_memories_agent" not in plan, plan


@pytest.mark.asyncio
async def test_the_range_form_is_answered_by_the_partial_index(corpus):
    """Boot creates the index, and the range form — the ordinary shape — uses it.

    The partial index holds only the rows that lost their embedding, normally
    none, so the probe stops reading the table it used to walk: 25.4 ms to
    0.003 ms at 50,000 rows. Only the plan can show that from a test, because
    the answer is identical either way.

    The chunked form is deliberately not asserted here — see the test below.
    """
    names = [
        row[0]
        for row in await corpus.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE '%lost_embedding'"
        )
    ]
    assert sorted(names) == ["idx_episodes_lost_embedding", "idx_memories_lost_embedding"], names

    for sql in (vector._LOST_EMBEDDING_RANGE_SQL, vector._lost_embedding_range_sql("episodes")):
        plan = " ".join(
            row[-1]
            for row in await corpus.execute_fetchall(
                "EXPLAIN QUERY PLAN " + sql, (1, ROWS, AGENT)
            )
        )
        assert "lost_embedding" in plan, plan


@pytest.mark.asyncio
async def test_the_chunked_form_keeps_its_rowid_lookups(corpus):
    """The scattered shape is not served by the index, and does not need to be.

    `id IN (…)` over an INTEGER PRIMARY KEY is planned as one rowid lookup per
    id, which is already logarithmic per id — the index would replace it with
    nothing cheaper. Pinned so a future reader does not read the test above as
    a claim about both forms, and so a planner change here is visible.
    """
    plan = " ".join(
        row[-1]
        for row in await corpus.execute_fetchall(
            "EXPLAIN QUERY PLAN " + vector._lost_embedding_chunk_sql(3), (1, 2, 3, AGENT)
        )
    )
    assert "INTEGER PRIMARY KEY" in plan, plan


@pytest.mark.asyncio
async def test_the_probe_answers_the_same_without_the_index(corpus):
    """Dropping it costs time, never an answer — the reason it is 'warn'.

    A database that has not been reopened since the index was added, or one an
    operator dropped, must return exactly what an indexed one returns.
    """
    ids = [
        row[0]
        for row in await corpus.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = ? ORDER BY id", (AGENT,)
        )
    ]
    assert ids, "the corpus fixture stored nothing"
    await _clear_embedding_of(corpus, "row 7")

    with_index = await vector._index_rows_lost_embedding(corpus, ids, agent_id=AGENT)
    await corpus.execute("DROP INDEX idx_memories_lost_embedding")
    await corpus.commit()
    try:
        without_index = await vector._index_rows_lost_embedding(corpus, ids, agent_id=AGENT)
    finally:
        # The database is shared for the whole session: leaving it dropped would
        # make an unrelated later test fail with no mention of this one.
        await corpus.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_lost_embedding"
            " ON memories(id) WHERE embedding IS NULL"
        )
        await corpus.commit()

    assert with_index is True, "the probe did not see the cleared embedding"
    assert without_index == with_index


def test_the_chunk_fits_the_smallest_supported_build():
    """999 is SQLITE_MAX_VARIABLE_NUMBER on every SQLite before 3.32.0."""
    assert vector._LOST_EMBEDDING_PROBE_CHUNK < 999
