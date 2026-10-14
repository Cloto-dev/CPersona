"""The contiguous index against rows that changed after its build, and builds that race a writer.

Two divergences between the index path and the live scan it stands in for.

A row deleted after the build still sits in the index file. When it is one of
the threshold survivors, the hydrate notices (bug-315). When it is not -- it
scores below the threshold, or falls outside the top cut -- it still takes one
of the window's positions, so the index ranks one live row fewer than the scan
and silently drops the row at the window edge. Each claim below is the index
answer measured against the same query with the index file removed, next to a
counter showing which path produced it: every failure of this design ends in a
silent fallback, so "the answers match" is only worth anything next to "and the
index answered when it could".

The builders read the row plan and the embedding blobs in two statements. When
those see different states of the database, a count-neutral pair of writes
between them (one planned row deleted, one row that had no embedding filled)
used to pass the row-count check and pair ids with their neighbours' vectors.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import numpy as np
import pytest
import pytest_asyncio

from cpersona import admin_handlers, coarse_index, config, vector, vector_index
from cpersona.database import connection, get_db
from tests.conftest import fake_embed_one

AGENT = "u05.agent"
OTHER = "u05.other"
QUERY = "orchard telescope lantern harbour"
THRESHOLD = 0.5


def _paths() -> list[str]:
    out = []
    for table in vector_index.INDEXED_TABLES:
        out += [vector_index.index_path(table), vector_index.index_path(table) + ".tmp"]
    for table in coarse_index.COARSE_TABLES:
        out += [coarse_index.index_path(table), coarse_index.index_path(table) + ".tmp"]
    return out


def _drop_files() -> None:
    for path in _paths():
        if os.path.exists(path):
            os.unlink(path)


def _blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


async def _memory(db, content, *, second, agent_id=AGENT, project_id="", embedding=None) -> int:
    vec = fake_embed_one(content) if embedding is None else embedding
    cur = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, ?, '', ?, ?, ?, ?, ?)",
        (
            agent_id, project_id, content, '{"type": "User", "id": "user-1"}',
            "2026-03-01T00:00:00+00:00", f"2026-03-01 00:00:{second:02d}", _blob(vec),
        ),
    )
    return cur.lastrowid


async def _episode(db, summary, *, second, embedding=None) -> int:
    vec = fake_embed_one(summary) if embedding is None else embedding
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, embedding,"
        " start_time, resolved, created_at) VALUES (?, '', '', ?, '', ?, NULL, 0, ?)",
        (AGENT, summary, _blob(vec), f"2026-03-01 00:00:{second:02d}"),
    )
    return cur.lastrowid


@pytest_asyncio.fixture
async def clean(fake_embedding_client):
    db = await get_db()
    _drop_files()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield db
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    _drop_files()


@pytest.fixture
def window_of_three(monkeypatch):
    """A three-row scan window, so a four-row corpus saturates it."""
    monkeypatch.setattr(vector, "MAX_MEMORIES", 3)


@pytest.fixture
def taken(monkeypatch):
    """Phase-1 answers per table: the index's, and the scan's (`<table>_fallback`)."""
    counter = {"memories": 0, "episodes": 0, "memories_fallback": 0, "episodes_fallback": 0}
    real = vector._index_phase1

    async def counting(*args, **kwargs):
        result = await real(*args, **kwargs)
        table = kwargs.get("table", "memories")
        counter[table if result is not None else f"{table}_fallback"] += 1
        return result

    monkeypatch.setattr(vector, "_index_phase1", counting)
    return counter


async def _search(db, **kw):
    return [
        (row["_rid"], round(float(row["_cosine"]), 6))
        for row in await vector._search_vector(db, AGENT, QUERY, 10, min_similarity=THRESHOLD, **kw)
    ]


async def _search_without_index(db, **kw):
    """The same query with every index file set aside: the scan's own answer."""
    saved = {}
    for path in _paths():
        if os.path.exists(path):
            with open(path, "rb") as fh:
                saved[path] = fh.read()
            os.unlink(path)
    try:
        return await _search(db, **kw)
    finally:
        for path, blob in saved.items():
            with open(path, "wb") as fh:
                fh.write(blob)


def _fillers(n: int) -> list[list[float]]:
    """Vectors well below the threshold against the query, checked rather than assumed."""
    query = np.asarray(fake_embed_one(QUERY))
    out, k = [], 0
    while len(out) < n:
        vec = fake_embed_one(f"unrelated filler {k}")
        k += 1
        if abs(float(np.dot(query, vec))) < THRESHOLD / 2:
            out.append(vec)
    return out


# --------------------------------------------------------------------------------------
# A deleted row that is not a survivor must not take a window position.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deleted_window_row_does_not_push_the_edge_row_out(clean, window_of_three, taken):
    db = clean
    match = await _memory(db, "the oldest row, the query's match", second=1, embedding=fake_embed_one(QUERY))
    fillers = [
        await _memory(db, f"filler {n}", second=n, embedding=vec)
        for n, vec in zip((2, 3, 4), _fillers(3))
    ]
    await db.commit()
    assert (await vector_index.build_index(db, "memories"))["built"]

    # The window holds the three fillers; the match is one position past it.
    assert await _search(db) == []
    assert taken["memories"] == 1 and taken["memories_fallback"] == 0

    # Delete the newest row, which the query does not rank: it is not a survivor,
    # so the hydrate never sees it.
    assert (await admin_handlers.do_delete_memory(fillers[-1], agent_id=AGENT))["ok"]

    scan = await _search_without_index(db)
    assert scan == [(("mem", match), 1.0)], "the oracle must reach the edge row"
    assert await _search(db) == scan


@pytest.mark.asyncio
async def test_a_deleted_row_in_a_scattered_selection_does_not_shorten_it(clean, window_of_three, taken):
    """A project filter that skips every other id: the probe's scattered form."""
    db = clean
    vecs = iter(_fillers(6))
    match = await _memory(db, "match", second=1, project_id="p1", embedding=fake_embed_one(QUERY))
    rows = {}
    for n in range(2, 8):
        project = "p2" if n % 2 == 0 else "p1"
        rows[n] = await _memory(db, f"filler {n}", second=n, project_id=project, embedding=next(vecs))
    await db.commit()
    assert (await vector_index.build_index(db, "memories"))["built"]

    assert await _search(db, project_id="p1") == []
    assert taken["memories"] == 1

    assert (await admin_handlers.do_delete_memory(rows[7], agent_id=AGENT))["ok"]

    scan = await _search_without_index(db, project_id="p1")
    assert scan == [(("mem", match), 1.0)]
    assert await _search(db, project_id="p1") == scan


@pytest.mark.asyncio
async def test_a_deleted_episode_does_not_push_the_edge_episode_out(clean, window_of_three, taken):
    db = clean
    match = await _episode(db, "match", second=1, embedding=fake_embed_one(QUERY))
    fillers = [
        await _episode(db, f"filler {n}", second=n, embedding=vec)
        for n, vec in zip((2, 3, 4), _fillers(3))
    ]
    await db.commit()
    assert (await vector_index.build_index(db, "episodes"))["built"]

    assert await _search(db) == []
    assert taken["episodes"] == 1

    assert (await admin_handlers.do_delete_episode(fillers[-1], agent_id=AGENT))["ok"]

    scan = await _search_without_index(db)
    assert scan == [(("ep", match), 1.0)]
    assert await _search(db) == scan


@pytest.mark.asyncio
async def test_a_deletion_outside_the_selection_keeps_the_index_path(clean, window_of_three, taken):
    """The check is about the rows this query selected, not about the table.

    Another agent's deleted row is in no window of this agent's, so handing this
    agent's queries back to the scan for it would cost latency for nothing.
    """
    db = clean
    match = await _memory(db, "match", second=4, embedding=fake_embed_one(QUERY))
    for n, vec in zip((1, 2, 3), _fillers(3)):
        await _memory(db, f"filler {n}", second=n, embedding=vec)
    foreign = await _memory(db, "another agent's row", second=5, agent_id=OTHER)
    await db.commit()
    assert (await vector_index.build_index(db, "memories"))["built"]

    assert (await admin_handlers.do_delete_memory(foreign, agent_id=OTHER))["ok"]
    assert await _search(db) == [(("mem", match), 1.0)]
    assert taken["memories"] == 1 and taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_the_existence_probe_reads_the_agent_index_not_the_rows(clean):
    """The probe is on the index path of every recall, so its plan is pinned.

    Counting the selected ids against the table's rows walks one row (one page,
    for a stored vector) per id: measured 4.8 ms at a 10,000-row window over
    100,000 rows of 768 dimensions, eight times the rest of phase 1. Bounded by
    the oldest selected created_at, the same count is a walk of the agent's
    `(agent_id, created_at)` index entries, which hold the rowid: 0.28 ms.
    """
    for table in ("memories", "episodes"):
        for sql, params in (
            (vector._gone_range_sql(table), (AGENT, "2026-03-01 00:00:00", 1, 9)),
            (vector._gone_scattered_sql(table), (AGENT, "2026-03-01 00:00:00", "[1, 3]")),
        ):
            rows = await clean.execute_fetchall("EXPLAIN QUERY PLAN " + sql, params)
            plan = " ".join(r[-1] for r in rows)
            assert f"COVERING INDEX idx_{table}_agent" in plan, (table, plan)


# --------------------------------------------------------------------------------------
# The builders' two passes must describe one row set.
# --------------------------------------------------------------------------------------

_DIM = 8


def _vec(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(_DIM).astype(np.float32)


async def _race_corpus(db) -> None:
    """Rows 1-6, row 5 with no embedding yet."""
    for i in range(1, 7):
        await db.execute(
            "INSERT INTO memories (id, agent_id, content, source, timestamp, created_at, embedding)"
            " VALUES (?, ?, ?, '{}', ?, ?, ?)",
            (
                i, AGENT, f"row {i}", f"2026-03-01T00:00:0{i}+00:00", f"2026-03-01 00:00:0{i}",
                None if i == 5 else _vec(i).tobytes(),
            ),
        )
    await db.commit()


def _count_neutral_writes(db_path: str) -> None:
    """Another process commits: one planned row deleted, one unembedded row filled."""
    w = sqlite3.connect(db_path)
    try:
        w.execute("DELETE FROM memories WHERE id = 2")
        w.execute("UPDATE memories SET embedding = ? WHERE id = 5", (_vec(5).tobytes(),))
        w.commit()
    finally:
        w.close()


@pytest.fixture
def race_after_plan(monkeypatch):
    """Commit the writes once, after the plan is read and before the blobs are."""
    real = vector_index.plan_rows
    fired = []

    async def racing(db, table="memories", **kw):
        plan = await real(db, table, **kw)
        if not fired:
            fired.append(1)
            _count_neutral_writes(config.DB_PATH)
        return plan

    monkeypatch.setattr(vector_index, "plan_rows", racing)
    return fired


def _foreign_vectors(path: str) -> list:
    index = vector_index.load_index("memories", path)
    return [
        int(rid) for pos, rid in enumerate(index.ids.tolist())
        if not np.array_equal(np.asarray(index.embeddings[pos]), _vec(rid))
    ]


def _foreign_bits(path: str) -> list:
    index = coarse_index.load_coarse_index("memories", path)
    return [
        int(rid) for pos, rid in enumerate(index.ids.tolist())
        if not np.array_equal(np.asarray(index.bits[pos]), coarse_index.sign_bits(_vec(rid)[None, :])[0])
    ]


@pytest.mark.asyncio
async def test_a_build_never_pairs_an_id_with_another_rows_vector(clean, race_after_plan, tmp_path):
    db = clean
    await _race_corpus(db)
    out = str(tmp_path / "race.vecindex")
    async with connection() as rdb:
        try:
            result = await vector_index.build_index(rdb, "memories", out)
        except vector_index.IndexUnusable:
            result = {"built": False}
    assert race_after_plan, "the writes must land between the passes"
    if result["built"]:
        assert _foreign_vectors(out) == [], "ids carry their neighbours' vectors"
    else:
        assert not os.path.exists(out) and not os.path.exists(out + ".tmp")


@pytest.mark.asyncio
async def test_a_coarse_build_never_pairs_an_id_with_another_rows_bits(clean, race_after_plan, tmp_path):
    db = clean
    await _race_corpus(db)
    out = str(tmp_path / "race.coarseindex")
    async with connection() as rdb:
        try:
            result = await coarse_index.build_coarse_index(rdb, "memories", out)
        except vector_index.IndexUnusable:
            result = {"built": False}
    assert race_after_plan
    if result["built"]:
        assert _foreign_bits(out) == [], "ids carry their neighbours' sign bits"
    else:
        assert not os.path.exists(out) and not os.path.exists(out + ".tmp")


_CLI_SEED = """
import asyncio, sys
import numpy as np
from cpersona.database import get_db, close_db

def vec(seed):
    return np.random.default_rng(seed).standard_normal(%d).astype(np.float32)

async def seed():
    db = await get_db()
    for i in range(1, 7):
        await db.execute(
            "INSERT INTO memories (id, agent_id, content, source, timestamp, created_at, embedding)"
            " VALUES (?, 'u05.agent', ?, '{}', ?, ?, ?)",
            (i, f"row {i}", f"2026-03-01T00:00:0{i}+00:00", f"2026-03-01 00:00:0{i}",
             None if i == 5 else vec(i).tobytes()),
        )
    await db.commit()
    await close_db()

asyncio.run(seed())
""" % _DIM

_CLI_RACE = """
import sqlite3, sys
import numpy as np
from cpersona import vector_index

path = sys.argv[1]
real = vector_index.plan_rows
fired = []

async def racing(db, table="memories", **kw):
    plan = await real(db, table, **kw)
    if not fired:
        fired.append(1)
        w = sqlite3.connect(path)
        w.execute("DELETE FROM memories WHERE id = 2")
        w.execute("UPDATE memories SET embedding = ? WHERE id = 5",
                  (np.random.default_rng(5).standard_normal(%d).astype(np.float32).tobytes(),))
        w.commit()
        w.close()
    return plan

vector_index.plan_rows = racing
raise SystemExit(vector_index.main(["--db", path, "--json", "build"]))
""" % _DIM


def test_the_operator_build_reads_one_snapshot_while_a_server_writes(tmp_path):
    """The documented cron build against a live server: it succeeds, and its files are right.

    A subprocess, as the CLI's own tests run it, so the operator entry point is
    exercised whole and the shared test connection is never closed under the
    other tests.
    """
    db_path = str(tmp_path / "race-cli.db")
    env = dict(os.environ, CPERSONA_EMBEDDING_MODE="none", CPERSONA_DB_PATH=db_path)
    seeded = subprocess.run([sys.executable, "-c", _CLI_SEED], env=env, capture_output=True, text=True,
                            timeout=120)
    assert seeded.returncode == 0, seeded.stderr

    env.pop("CPERSONA_DB_PATH")
    done = subprocess.run([sys.executable, "-c", _CLI_RACE, db_path], env=env, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["built"] and result["coarse"]["built"], result
    # One snapshot: the state before the writes, row 5 still a named hole.
    assert result["count"] == 5 and result["unembedded"] == 1, result
    assert _foreign_vectors(db_path + ".memories.vecindex") == []
    assert _foreign_bits(db_path + ".memories.coarseindex") == []
