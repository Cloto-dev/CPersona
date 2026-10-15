"""Health and deep-check behaviours that shipped correct but that no test pinned.

  block index        check_block_index stops a change log that runs with the
                     index off, says nothing when the log is off too, and with
                     the index on reports an absent, an unusable and a behind
                     file -- behind including a schema change after the build,
                     which the change log's clock alone cannot see.
  chmod failure      check_file_permissions records a chmod that fails as an
                     unfixed finding and carries on, so one unwritable path does
                     not cost the rest of the repair pass.
  crashed check      A fix run undoes only the crashed check's writes: the
                     repairs earlier checks made in the same run still commit.
  never calibrated   deep_calibration_staleness reports a corpus of 50 or more
                     embedded rows with no calibration sidecar as
                     never_calibrated, and a smaller one as ok.
  unrecoverable      The plain checkup output prints an anonymous-source result
                     whose only count is ``unrecoverable``.
  capped kinds       A kind that overflows its cap by more than one is named
                     once in capped_kinds and once in the summary.

The msg_id dedup repair's ``locked = 0`` guard is pinned by
test_2611a2_x08.py's two-locked-colliders test, which fails when the guard is
dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import os
import stat
import struct

import aiosqlite
import pytest
import pytest_asyncio

from cpersona import (
    block_index,
    checks,
    checkup,
    config,
    database,
    fileperms,
    findings,
    maintenance_handlers,
    session,
)


@pytest_asyncio.fixture
async def file_db(monkeypatch, tmp_path):
    """A database file of this test's own, as the process-wide connection.

    The block index file lives beside the database, and a fix run goes through
    the shared connection's transaction(), so both paths are pointed here rather
    than at the database the rest of the suite shares.
    """
    # Fresh lock per test: an asyncio.Lock binds to the first loop that contends it.
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    session.reset_pauses_for_tests()
    await database.close_db()
    path = str(tmp_path / "y08.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    block_index._cache.clear()
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        block_index._cache.clear()


@pytest_asyncio.fixture
async def private_db(tmp_path):
    """A connection of this test's own, never the shared one."""
    conn = await aiosqlite.connect(str(tmp_path / "private.db"))
    await database._init_schema(conn)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def db():
    """The suite's shared connection, for a check that writes no row."""
    session.reset_pauses_for_tests()
    return await database.get_db()


# ---------------------------------------------------------------------------
# block index
# ---------------------------------------------------------------------------

_BLOCK_INSERT = (
    "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id,"
    " channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model)"
    " VALUES ('mem', ?, 0, 'y08.agent', '', '', 0, 1, 0, ?, 'm')"
)


@pytest.fixture
def block_index_off(monkeypatch):
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", False)


@pytest.fixture
def block_index_on(monkeypatch):
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)


async def _block_issues(db, fix=False):
    issues, _ = await checks.run_health_checks(db, fix=fix, checks=["block_index"])
    return issues


async def _logging(db) -> int:
    return (await db.execute_fetchall("SELECT logging FROM block_log_clock WHERE id = 0"))[0][0]


async def _seed_blocks(db, n: int = 5) -> None:
    await db.executemany(_BLOCK_INSERT, [(i, bytes([i]) * 4) for i in range(1, n + 1)])
    await db.commit()


@pytest.mark.asyncio
async def test_block_log_without_index_fix_turns_the_log_off(file_db, block_index_off):
    """fixed: true has to mean the change log stopped: every block write adds a
    row to it while it runs, for a file nothing reads."""
    db = file_db
    await db.execute("UPDATE block_log_clock SET logging = 1 WHERE id = 0")
    await db.commit()
    # A block write while the log runs: the row it adds is what the repair empties.
    await _seed_blocks(db, 1)
    assert await db.execute_fetchall("SELECT 1 FROM record_block_changes")

    (detected,) = await _block_issues(db)
    assert detected["type"] == "block_log_without_index" and detected["repairable"] == 1, detected
    assert await _logging(db) == 1, "a report-only run changed the clock"

    (issue,) = await _block_issues(db, fix=True)
    await db.commit()

    assert issue["type"] == "block_log_without_index" and issue["fixed"] is True, issue
    assert await _logging(db) == 0, "fixed: true was reported but the change log still runs"
    assert await db.execute_fetchall("SELECT 1 FROM record_block_changes") == [], (
        "the log was turned off but its rows were kept"
    )
    assert await _block_issues(db) == [], "the next run still reports the log"


@pytest.mark.asyncio
async def test_block_index_off_and_log_off_is_silent(file_db, block_index_off):
    """The ordinary state of a deployment without the block index is no finding."""
    db = file_db
    await db.execute("UPDATE block_log_clock SET logging = 0 WHERE id = 0")
    await db.commit()

    assert await _block_issues(db) == []


@pytest.mark.asyncio
async def test_block_index_on_reports_an_absent_file(file_db, block_index_on):
    db = file_db
    await _seed_blocks(db)
    assert not os.path.exists(block_index.index_path())

    issues = await _block_issues(db)

    assert [(i["type"], i["repairable"]) for i in issues] == [("block_index_absent", 0)], issues


@pytest.mark.asyncio
async def test_block_index_on_reports_an_unusable_file(file_db, block_index_on):
    db = file_db
    await _seed_blocks(db)
    with open(block_index.index_path(), "wb") as fh:
        fh.write(b"not a block index file")

    issues = await _block_issues(db)

    assert [i["type"] for i in issues] == ["block_index_unusable"], issues
    assert "bad magic" in issues[0]["detail"], issues


@pytest.mark.asyncio
async def test_block_index_on_is_silent_when_current_and_behind_after_a_schema_change(
    file_db, block_index_on
):
    """A schema change after the build leaves the change log's clock where it was,
    so only the schema cookie the file was built under says the file is behind."""
    db = file_db
    await _seed_blocks(db)
    built = await block_index.build_block_index()
    assert built["built"], built
    assert await _block_issues(db) == [], "a fresh build is current"

    # Recreate one logging trigger exactly as it was: the clock is unchanged and
    # only PRAGMA schema_version moves.
    (sql,) = (await db.execute_fetchall(
        "SELECT sql FROM sqlite_master WHERE name = 'record_block_log_au'"
    ))[0]
    cookie_before = (await db.execute_fetchall("PRAGMA schema_version"))[0][0]
    await db.execute("DROP TRIGGER record_block_log_au")
    await db.execute(sql)
    await db.commit()
    assert (await db.execute_fetchall("PRAGMA schema_version"))[0][0] != cookie_before

    issues = await _block_issues(db)

    assert [i["type"] for i in issues] == ["block_index_behind"], issues
    assert issues[0]["changes_since_build"] == 0, issues


# ---------------------------------------------------------------------------
# chmod failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    not fileperms.mode_bits_are_enforced(),
    reason="permission bits are not the access control on this platform",
)
async def test_a_chmod_that_fails_on_one_file_does_not_cost_the_others(db, tmp_path, monkeypatch):
    """The first file cannot be narrowed; the second still is, and both are reported."""
    refused = tmp_path / "refused.json"
    narrowed = tmp_path / "narrowed.json"
    for path in (refused, narrowed):
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o644)
    monkeypatch.setattr(
        fileperms,
        "owned_paths",
        lambda: [
            fileperms.OwnedPath(str(refused), "file", "the first file"),
            fileperms.OwnedPath(str(narrowed), "file", "the second file"),
        ],
    )
    real_chmod = os.chmod

    def chmod(path, mode, *args, **kwargs):
        if os.fspath(path) == str(refused):
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), os.fspath(path))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)

    issues, _ = await checks.run_health_checks(db, fix=True, checks=["file_permissions"])

    assert [i["type"] for i in issues] == ["file_reachable_by_other_accounts"] * 2, issues
    by_path = {i["path"]: i for i in issues}
    assert by_path[str(refused)]["fixed"] is False, issues
    assert os.strerror(errno.EPERM) in by_path[str(refused)]["fix_error"], issues
    assert by_path[str(narrowed)]["fixed"] is True, issues
    assert stat.S_IMODE(os.stat(narrowed).st_mode) == 0o600, "the file after the refused one was not narrowed"
    assert stat.S_IMODE(os.stat(refused).st_mode) == 0o644


# ---------------------------------------------------------------------------
# crashed check
# ---------------------------------------------------------------------------

_CRASH_AGENT = "y08.crash"
_CRASH_TRIGGER = "y08_refuse_metadata_repair"


@pytest.mark.asyncio
async def test_a_crashed_check_does_not_discard_an_earlier_checks_repair(file_db):
    """memory_annotation repairs a row; invalid_json, later in the same run, is made
    to fail half way. Only invalid_json's writes are undone."""
    db = file_db
    insert = (
        "INSERT INTO memories (agent_id, content, source, metadata, timestamp, created_at, locked)"
        " VALUES (?, ?, ?, ?, '2026-03-01T00:00:00+00:00', '2026-03-01 00:00:00', 0)"
    )
    annotated = (await db.execute(
        insert, (_CRASH_AGENT, "[Memory from Alice] likes green tea in the morning", "{}", "{}")
    )).lastrowid
    invalid = (await db.execute(
        insert, (_CRASH_AGENT, "a row whose two JSON columns are both invalid", "not json", "also not json")
    )).lastrowid
    # invalid_json writes source first and metadata second; refusing the second
    # write leaves the first one done inside the check when it raises.
    await db.execute(
        f"CREATE TRIGGER {_CRASH_TRIGGER} BEFORE UPDATE OF metadata ON memories FOR EACH ROW"
        f" WHEN OLD.agent_id = '{_CRASH_AGENT}'"
        " BEGIN SELECT RAISE(ABORT, 'test: metadata repair refused'); END"
    )
    await db.commit()

    out = await maintenance_handlers.do_check_health(
        _CRASH_AGENT, fix=True, checks=["memory_annotation", "invalid_json"]
    )

    crashed = [i for i in out["issues"] if i.get("type") == "check_crashed"]
    assert [c["check_name"] for c in crashed] == ["invalid_json"], out["issues"]
    (annotation,) = [i for i in out["issues"] if i.get("type") == "memory_annotation"]
    assert annotation["repairable"] == 1, annotation

    rows = dict(await db.execute_fetchall(
        "SELECT id, content FROM memories WHERE agent_id = ?", (_CRASH_AGENT,)
    ))
    assert rows[annotated] == "likes green tea in the morning", (
        f"memory_annotation's repair was discarded by invalid_json's crash: {rows[annotated]!r}"
    )
    # The other side of the same contract: the crashed check's half-write is gone.
    (source, metadata), = await db.execute_fetchall(
        "SELECT source, metadata FROM memories WHERE id = ?", (invalid,)
    )
    assert (source, metadata) == ("not json", "also not json")


# ---------------------------------------------------------------------------
# never calibrated
# ---------------------------------------------------------------------------

_CAL_AGENT = "y08.calibration"


async def _embedded_rows(db, n: int, agent: str = _CAL_AGENT) -> None:
    blob = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    await db.executemany(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
        [(agent, f"row {i}", f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00", blob) for i in range(n)],
    )
    await db.commit()


@pytest.fixture
def no_sidecar(tmp_path, monkeypatch):
    """A database path whose calibration sidecar does not exist."""
    path = str(tmp_path / "uncalibrated.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    assert not os.path.exists(path + ".calibration.json")


@pytest.mark.asyncio
@pytest.mark.parametrize(("rows", "status"), [(49, "ok"), (50, "never_calibrated")])
async def test_an_uncalibrated_corpus_is_never_calibrated_from_fifty_embedded_rows(
    private_db, no_sidecar, fake_embedding_client, rows, status
):
    """The status the runbook tells an operator to expect after restoring a
    database without its sidecar: the gate runs on defaults, and says so."""
    await _embedded_rows(private_db, rows)
    # Another agent's rows do not make this agent's corpus big enough.
    await _embedded_rows(private_db, 10, agent="y08.someone-else")

    result = await checks.deep_calibration_staleness(private_db, _CAL_AGENT, False)

    assert result["status"] == status, result
    if status == "never_calibrated":
        assert result["embedded_rows"] == rows, result
        assert "calibrate_threshold" in result["hint"], result


# ---------------------------------------------------------------------------
# unrecoverable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_checkup_prints_an_anonymous_source_result_with_only_unrecoverable_rows(
    monkeypatch, capsys
):
    """recoverable 0 / unrecoverable 3 are the rows that need a person, and the
    plain output is what an operator reads."""
    agent = "y08.checkup"

    async def health(agent_id="", fix=False):
        return {"total_memories": 3, "issues": [], "severity_summary": {"critical": 0, "warn": 0, "info": 0}}

    async def deep(agent_id, fix=False):
        return {"results": {
            "anonymous_source": {"recoverable": 0, "unrecoverable": 3},
            "stale_profile": {"count": 0, "threshold_days": 30},
        }}

    async def keep_connections():
        pass

    monkeypatch.setattr(maintenance_handlers, "do_check_health", health)
    monkeypatch.setattr(maintenance_handlers, "do_deep_check", deep)
    monkeypatch.setattr(database, "close_db", keep_connections)
    # A report-only run sets this process-wide; put it back afterwards.
    monkeypatch.setattr(database, "SKIP_BOOT_MIGRATIONS", database.SKIP_BOOT_MIGRATIONS)
    monkeypatch.setattr(config, "EMBEDDING_MODE", "none")

    args = argparse.Namespace(agent=agent, fix=False, strict=False, deep=True, json=False)
    code = await checkup._run(args)

    out = capsys.readouterr().out
    assert code == 0, out
    deep_lines = [line for line in out.splitlines() if line.strip().startswith(f"deep[{agent}]")]
    assert len(deep_lines) == 1, out
    assert '"anonymous_source"' in deep_lines[0] and '"unrecoverable": 3' in deep_lines[0], out
    assert "stale_profile" not in deep_lines[0], "a clean result was printed as a finding"


# ---------------------------------------------------------------------------
# capped kinds
# ---------------------------------------------------------------------------


def test_a_kind_over_its_cap_by_more_than_one_is_named_once():
    severity_map = {"stale_pending": "info", "duplicate_pending": "warn"}
    detector_output = [
        {"kind": "stale_pending", "task_id": 1},
        {"kind": "duplicate_pending", "task_id": 2},
        {"kind": "stale_pending", "task_id": 3},
        {"kind": "stale_pending", "task_id": 4},
        {"kind": "duplicate_pending", "task_id": 5},
        {"kind": "stale_pending", "task_id": 6},
        {"kind": "duplicate_pending", "task_id": 7},
    ]

    delivered = findings.deliver(detector_output, 1, severity_map)

    assert delivered["capped_kinds"] == ["stale_pending", "duplicate_pending"], delivered
    assert [f["task_id"] for f in delivered["findings"]] == [1, 2]
    assert delivered["counts_by_kind"] == {"stale_pending": 1, "duplicate_pending": 1}
    summary = findings.render_summary(delivered)
    assert summary.count("stale_pending") == 2, summary  # once in kinds, once in the cap sentence
    assert "Capped at 1 per kind: stale_pending, duplicate_pending — more exist" in summary, summary
