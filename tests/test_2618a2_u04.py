"""Regression tests for the 2.6.18a2 maintenance and health fixes.

  bug-815  A dedup repair that deletes a copy in favour of an equal survivor
           moves the copy's folder filing to the survivor, as it moves its
           associations, unless the survivor is already filed.
  bug-816  fts_integrity does not read a locked database as a build without
           the integrity-check command: the check reports that it did not run.
  bug-921  An agent-scoped report of an unusable index file names the file,
           not the server directory it sits in.
"""

import asyncio
import json
import os
import sqlite3

import numpy as np
import pytest
import pytest_asyncio

from cpersona import (
    block_index,
    checks,
    coarse_index,
    config,
    database,
    folders,
    maintenance_handlers,
    session,
    vector,
    vector_index,
)
from cpersona.memory_handlers import do_store

AGENT = "u04.agent"
STAMP = "2026-10-23T00:00:00+00:00"


@pytest_asyncio.fixture
async def file_db(monkeypatch, tmp_path):
    """A database file of this test's own, as the process-wide connection."""
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    session.reset_pauses_for_tests()
    await database.close_db()
    path = str(tmp_path / "u04.db")
    monkeypatch.setenv("CPERSONA_DB_PATH", path)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    vector_index._cache.clear()
    coarse_index._cache.clear()
    block_index._cache.clear()
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        vector_index._cache.clear()
        coarse_index._cache.clear()
        block_index._cache.clear()


async def _mem(db, content, channel="", project="p1"):
    cur = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp)"
        " VALUES (?, ?, ?, ?, '{}', ?)",
        (AGENT, project, channel, content, STAMP),
    )
    await db.commit()
    return cur.lastrowid


async def _file(ref, path, project="p1"):
    report = await folders.organize(
        AGENT,
        [{"op": "create", "path": path}, {"op": "file", "ref": ref, "path": path}],
        project_id=project,
    )
    assert not report["dropped"], report


async def _members(db):
    return await db.execute_fetchall("SELECT ref, folder_id FROM folder_members ORDER BY ref")


async def _folder_id(db, name):
    return (await db.execute_fetchall("SELECT id FROM folders WHERE name = ?", (name,)))[0][0]


# ==========================================================================
# bug-815: dedup repairs carry the deleted copy's folder filing.
# ==========================================================================
@pytest.mark.asyncio
async def test_bug_815_duplicate_content_repair_moves_the_filing_to_the_survivor(file_db):
    db = file_db
    keep = await _mem(db, "same text", "")
    copy = await _mem(db, "same text", "c1")
    await _file(f"mem:{copy}", ["work"])
    work = await _folder_id(db, "work")

    result = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["duplicate_content"]
    )
    assert [i["type"] for i in result["issues"]] == ["duplicate_content"], result

    assert await db.execute_fetchall("SELECT id FROM memories") == [(keep,)]
    assert await _members(db) == [(f"mem:{keep}", work)], (
        "the deleted copy's filing was dropped instead of moved to the survivor"
    )


@pytest.mark.asyncio
async def test_bug_815_duplicate_repair_keeps_a_filing_the_survivor_already_has(file_db):
    db = file_db
    keep = await _mem(db, "same text", "")
    copy = await _mem(db, "same text", "c1")
    await _file(f"mem:{keep}", ["mine"])
    await _file(f"mem:{copy}", ["theirs"])
    mine = await _folder_id(db, "mine")

    await checks.check_duplicate_content(db, AGENT, True)
    await db.commit()

    assert await db.execute_fetchall("SELECT id FROM memories") == [(keep,)]
    assert await _members(db) == [(f"mem:{keep}", mine)]


@pytest.mark.asyncio
async def test_bug_815_annotation_collision_repair_moves_the_filing_to_the_survivor(file_db):
    db = file_db
    keep = await _mem(db, "plain body")
    copy = await _mem(db, "[Memory from someone] plain body")
    await _file(f"mem:{copy}", ["work"])
    work = await _folder_id(db, "work")

    await checks.check_memory_annotation(db, AGENT, True)
    await db.commit()

    assert await db.execute_fetchall("SELECT id, content FROM memories") == [(keep, "plain body")]
    assert await _members(db) == [(f"mem:{keep}", work)]


# ==========================================================================
# bug-816: a locked database is not an unsupported build.
# ==========================================================================
@pytest.mark.asyncio
async def test_bug_816_a_locked_database_is_reported_as_a_check_that_did_not_run(file_db):
    db = file_db
    stored = await do_store(AGENT, {"content": "a stored note about walnuts", "source": {"type": "User", "id": "u"}})
    assert stored.get("result") == "stored", stored
    con = sqlite3.connect(config.DB_PATH, isolation_level=None)
    con.execute("INSERT INTO memories_fts(rowid, content) VALUES (999999, 'ghost entry')")
    con.close()

    free = await maintenance_handlers.do_check_health(checks=["fts_integrity"])
    assert free["status"] == "unhealthy", free  # control: the damage is seen

    await db.execute("PRAGMA busy_timeout=50")
    other = sqlite3.connect(config.DB_PATH, timeout=0, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        held = await maintenance_handlers.do_check_health(checks=["fts_integrity"])
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert held["status"] != "healthy", held
    crashed = [i for i in held["issues"] if i["type"] == "check_crashed"]
    assert [i["check_name"] for i in crashed] == ["fts_integrity"], held
    assert "locked" in crashed[0]["detail"], crashed


class _Unsupported:
    """The connection, except that every integrity-check statement fails as a
    build without the command does."""

    def __init__(self, db):
        self._inner = db

    async def execute(self, sql, *args):
        if "integrity-check" in sql:
            raise sqlite3.OperationalError("unknown special query: integrity-check")
        return await self._inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_bug_816_an_unsupported_build_is_still_skipped_silently(file_db):
    assert await checks.check_fts_integrity(_Unsupported(file_db), "", False) == []


# ==========================================================================
# bug-921: an agent-scoped report names the index file, not its directory.
# ==========================================================================
def _garbage(path):
    with open(path, "wb") as fh:
        fh.write(b"not an index file")


def _assert_scoped_names_only_the_file(scoped, unscoped, kind, path):
    found = [i for i in scoped if i["type"] == kind]
    assert found, scoped
    assert os.path.basename(path) in found[0]["detail"], found
    assert os.path.dirname(path) not in json.dumps(scoped), found
    # The operator's whole-database view keeps the path it needs to act on.
    whole = [i for i in unscoped if i["type"] == kind]
    assert whole and path in whole[0]["detail"], unscoped


@pytest.mark.asyncio
async def test_bug_921_vector_index_unusable_names_no_server_path_when_scoped(file_db):
    path = vector_index.index_path("memories")
    _garbage(path)
    scoped, _ = await checks.run_health_checks(file_db, agent_id=AGENT, checks=["vector_index"])
    unscoped, _ = await checks.run_health_checks(file_db, agent_id="", checks=["vector_index"])
    _assert_scoped_names_only_the_file(scoped, unscoped, "vector_index_unusable", path)


@pytest.mark.asyncio
async def test_bug_921_coarse_index_unusable_names_no_server_path_when_scoped(file_db, monkeypatch):
    monkeypatch.setattr(vector, "MAX_MEMORIES", 10)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 5)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")
    await file_db.executemany(
        "INSERT INTO memories (agent_id, content, source, timestamp, embedding) VALUES (?, ?, '{}', ?, ?)",
        [
            (AGENT, f"row {n}", STAMP, np.random.default_rng(n).standard_normal(8).astype(np.float32).tobytes())
            for n in range(30)
        ],
    )
    await file_db.commit()
    path = coarse_index.index_path("memories")
    _garbage(path)
    scoped, _ = await checks.run_health_checks(file_db, agent_id=AGENT, checks=["coarse_index"])
    unscoped, _ = await checks.run_health_checks(file_db, agent_id="", checks=["coarse_index"])
    _assert_scoped_names_only_the_file(scoped, unscoped, "coarse_index_unusable", path)


@pytest.mark.asyncio
async def test_bug_921_block_index_unusable_names_no_server_path_when_scoped(file_db, monkeypatch):
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    await file_db.executemany(
        "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id,"
        " channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model)"
        " VALUES ('mem', ?, 0, ?, '', '', 0, 1, 0, ?, 'm')",
        [(i, AGENT, bytes([i]) * 4) for i in range(1, 6)],
    )
    await file_db.commit()
    path = block_index.index_path()
    _garbage(path)
    scoped, _ = await checks.run_health_checks(file_db, agent_id=AGENT, checks=["block_index"])
    unscoped, _ = await checks.run_health_checks(file_db, agent_id="", checks=["block_index"])
    _assert_scoped_names_only_the_file(scoped, unscoped, "block_index_unusable", path)
