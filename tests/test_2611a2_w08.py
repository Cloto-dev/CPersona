"""check_health(fix=true) against the write lock: what the run holds, and for how long.

bug-550: the coarse index's repair built the file on the fix run's own
connection, inside ``transaction()``. The build reads every embedded record and
writes a file; it writes no row, yet every store, archive, queue drain and
import, in this process and in every other process on the file, waited for it
behind the write lock. The build now runs after the run commits, as the
operator command runs it: the change log goes on in a commit of its own, the
rows are read on a snapshot, and the log is pruned afterwards, so a write that
lands while the build reads is named by the log and read live.

bug-643: the same build read the run's uncommitted state and published the file
before the run committed. A run that then rolled back (a client timeout cancels
it) left a file that omits rows the rollback put back, below its watermark and
unnamed by the log, so the coarse search never offered them.

bug-551: a fix run opened a deferred transaction, so its first read pinned a
snapshot and its first repair, once another process had committed, could not
upgrade to a write: every repair failed with 'database is locked' and nothing
was repaired. ``transaction()`` now takes SQLite's write lock before the body
runs; the regression below pins the health run's side of it.

Another server process is modelled as a second ``sqlite3`` connection on the
same file: to SQLite's locking that is exactly what a second process is.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3

import numpy as np
import pytest
import pytest_asyncio

from cpersona import checks, coarse_index, config, database, maintenance_handlers, vector, vector_index

AGENT = "w08.agent"
DIM = 8
ROWS = 30


def _blob(seed: int) -> bytes:
    return np.random.default_rng(seed).standard_normal(DIM).astype(np.float32).tobytes()


@pytest_asyncio.fixture
async def file_db(monkeypatch, tmp_path):
    """A fresh database file of the test's own, with the index files beside it."""
    # Fresh lock per test: an asyncio.Lock binds to the first loop that contends it.
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    await database.close_db()
    path = str(tmp_path / "w08.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    await database.get_db()
    yield path
    await database.close_db()


@pytest.fixture
def coarse_read(monkeypatch):
    """A setting that reads the coarse index, and a window a few rows reach past."""
    monkeypatch.setattr(vector, "MAX_MEMORIES", 5)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 1)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")


async def _corpus(n: int = ROWS) -> list[int]:
    db = await database.get_db()
    ids = []
    for k in range(n):
        cur = await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at, embedding) VALUES (?, '', '', ?, '{}', ?, ?, ?)",
            (
                AGENT, f"record {k}", f"2026-03-01T00:{k // 60:02d}:{k % 60:02d}+00:00",
                f"2026-03-01 00:{k // 60:02d}:{k % 60:02d}", _blob(k),
            ),
        )
        ids.append(cur.lastrowid)
    await db.commit()
    return ids


def _coarse_issue(result: dict) -> dict:
    found = [i for i in result["issues"] if i.get("check") == "coarse_index"]
    assert len(found) == 1, result
    return found[0]


# ---------------------------------------------------------------------------
# bug-550
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug550_the_coarse_repair_builds_while_other_writers_proceed(file_db, coarse_read, monkeypatch):
    """While the repair reads the store for the file, nothing waits on it to write.

    Measured inside the build, after its rows are pinned: the process's write lock,
    another process's attempt to take SQLite's write lock, and a store in this
    process. The store rewrites a vector the build's snapshot does not see, so the
    file must name that row through the change log and have it read live.
    """
    ids = await _corpus()
    moved = ids[3]
    real = coarse_index.build_coarse_index
    seen: dict = {}

    async def watched(conn, table="memories", path=None):
        # The build's first read pins what it reads; take that read here first.
        await conn.execute_fetchall("SELECT COUNT(*) FROM memories")
        seen["write_lock_held"] = database._write_lock.locked()
        other = sqlite3.connect(file_db, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            seen["other_process_could_write"] = True
        except sqlite3.OperationalError as exc:
            seen["other_process_could_write"] = str(exc)
        finally:
            other.close()

        async def store():
            async with database.transaction() as w:
                await w.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(1000), moved))

        try:
            await asyncio.wait_for(store(), timeout=2)
            seen["store_waited_for_the_build"] = False
        except asyncio.TimeoutError:
            seen["store_waited_for_the_build"] = True
        return await real(conn, table, path)

    monkeypatch.setattr(coarse_index, "build_coarse_index", watched)
    result = await maintenance_handlers.do_check_health(AGENT, fix=True, checks=["coarse_index"])

    issue = _coarse_issue(result)
    assert issue["rebuilt"] is True, issue
    assert seen["write_lock_held"] is False, "the coarse build ran under the shared write lock"
    assert seen["other_process_could_write"] is True, (
        f"another process could not start a write while the coarse build read: {seen['other_process_could_write']}"
    )
    assert seen["store_waited_for_the_build"] is False, "a store waited for the coarse build"

    # The file is read through the change log: the row rewritten while the build
    # read its snapshot is named, so a reader takes it from the live table.
    db = await database.get_db()
    index = coarse_index.load_coarse_index()
    changed, refusal = await vector_index.changed_since_build(db, index, "memories")
    assert refusal == "", refusal
    assert moved in changed.tolist()
    assert index.count == ROWS
    assert (await vector_index.log_clock(db))[0] == 1


@pytest.mark.asyncio
async def test_bug550_a_fix_run_inside_a_transaction_never_builds(file_db, coarse_read, monkeypatch):
    """The runner under a caller's transaction only detects: a build there would hold
    that transaction's lock for the whole read, which is the defect."""
    await _corpus()
    calls = []

    async def counted(*args, **kwargs):
        calls.append(args)
        return {"built": False, "reason": "not expected"}

    monkeypatch.setattr(coarse_index, "build_coarse_index", counted)
    async with database.transaction() as db:
        issues, _ = await checks.run_health_checks(db, agent_id=AGENT, fix=True, checks=["coarse_index"])
    assert [i["type"] for i in issues] == ["coarse_index_absent"]
    assert issues[0]["repairable"] == 1
    assert calls == [], "the coarse file was built inside the caller's transaction"


# ---------------------------------------------------------------------------
# bug-643
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug643_a_fix_run_that_rolls_back_leaves_no_coarse_file_that_hides_a_row(
    file_db, coarse_read, monkeypatch
):
    """A run cut off before its commit must not leave a file built from its uncommitted rows.

    The log is already on (the contiguous index was built, as the operator command
    builds it). The run deletes a record with no content, then is cancelled after
    the coarse check and before the commit, so the record comes back. One later
    logged write moves the log's head on, as any traffic does.
    """
    ids = await _corpus()
    doomed = ids[2]
    db = await database.get_db()
    await db.execute("UPDATE memories SET content = ' ' WHERE id = ?", (doomed,))
    await db.commit()
    await vector_index.start_logging()
    async with database.read_snapshot() as snap:
        assert (await vector_index.build_index(snap, "memories"))["built"]

    block = next(c for c in checks.HEALTH_CHECKS if c.name == "block_index")

    async def cut_off(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(block, "runner", cut_off)
    with pytest.raises(asyncio.CancelledError):
        await maintenance_handlers.do_check_health(
            AGENT, fix=True, checks=["empty_content", "coarse_index", "block_index"]
        )
    assert await db.execute_fetchall("SELECT 1 FROM memories WHERE id = ?", (doomed,)), "the run rolled back"

    async with database.transaction() as w:
        await w.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(2000), ids[5]))

    path = coarse_index.index_path("memories")
    if os.path.exists(path):
        index = coarse_index.load_coarse_index()
        changed, refusal = await vector_index.changed_since_build(db, index, "memories")
        if refusal == "":
            answered = (
                set(index.ids.tolist())
                | set(index.excluded_ids)
                | set(index.unembedded_ids)
                | set(changed.tolist())
            )
            hidden = [i for i in ids if i <= index.watermark and i not in answered]
            assert hidden == [], f"the coarse file answers for rows {hidden} it does not hold"
    assert not os.path.exists(path), "a fix run that rolled back published a coarse file"


# ---------------------------------------------------------------------------
# bug-551
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug551_repairs_converge_when_another_process_writes_after_the_runs_first_read(
    file_db, monkeypatch
):
    """Another process commits a write between the run's first read and its first repair.

    With the run's transaction begun deferred, that read pinned a snapshot the
    repair could not upgrade from, and the repair failed with 'database is
    locked'. Holding SQLite's write lock from the start, the run makes the other
    process wait instead, and the repair writes.
    """
    db = await database.get_db()
    for k in range(5):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, metadata) VALUES (?, ?, ?, '{bad')",
            (AGENT, f"bad json {k}", "2026-03-01T00:00:00+00:00"),
        )
    await db.commit()

    target = next(c for c in checks.HEALTH_CHECKS if c.name == "invalid_json")
    real = target.runner
    seen: dict = {}

    async def after_another_write(conn, agent_id, fix):
        if not fix:
            # The residual re-run after the commit: no run to write inside.
            return await real(conn, agent_id, fix)
        await conn.execute_fetchall("SELECT COUNT(*) FROM memories")
        other = sqlite3.connect(file_db, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT INTO memories (agent_id, content, timestamp) VALUES ('w08.other', 'other process', "
                "'2026-03-01T00:00:00+00:00')"
            )
            other.execute("COMMIT")
            seen["other_committed"] = True
        except sqlite3.OperationalError as exc:
            seen["other_committed"] = str(exc)
        finally:
            if other.in_transaction:
                other.execute("ROLLBACK")
            other.close()
        return await real(conn, agent_id, fix)

    monkeypatch.setattr(target, "runner", after_another_write)
    result = await maintenance_handlers.do_check_health(AGENT, fix=True, checks=["invalid_json"])

    crashed = [i for i in result["issues"] if i["type"] == "check_crashed"]
    assert crashed == [], crashed
    left = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND json_valid(metadata) = 0", (AGENT,)
    )
    assert left[0][0] == 0, "the repair wrote nothing"
    assert seen["other_committed"] is not True, "another process committed inside the fix run"
