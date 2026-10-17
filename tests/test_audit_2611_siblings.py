"""Regression tests for siblings the 2.6.11 audit's fixes did not cover.

Each fix in that audit was checked for the same defect elsewhere in the package.
These are places where it was found:

  bug-781  check_memory_annotation's repair stripped ``[Memory from ...]`` with one
           regex pass. One pass can assemble a new annotation out of the text on
           either side of the one it removed, so a repaired row could keep an
           annotation and the next run rewrote it again. The write seam strips to a
           fixed point (bug-616), and the repair now uses the same function.
  bug-782  The content-rewriting repairs (annotation, mention, oversized content)
           changed a row's text without queueing its node and block builds. The
           content triggers drop both sets with the old text, so the record had
           neither until a later sweep came round to it. update_memory and
           merge_memories queue them after they rewrite text.
  bug-787  The v12 migration's two dedup steps chose their survivor without regard
           to locks. When the survivor they chose was unlocked and the group had a
           locked row, both rows stayed and the unique index was skipped.
  bug-788  check_duplicate_content kept MIN(id) of each group, so a group whose
           oldest row was unlocked and whose newer copy was locked kept both, and
           the group was reported again on every run.
  bug-789  check_block_index's block_index_behind finding reported the store-wide
           change count in an agent-scoped call, which told a caller with read on
           one agent how many block changes every other agent had made.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
import pytest_asyncio

from cpersona import (
    associations,
    block_index,
    blocks,
    checks,
    config,
    database,
    maintenance_handlers,
    nodes,
    session,
    tasks,
    utils,
)

AGENT = "siblings.agent"
OTHER = "siblings.other"
STAMP = "2026-01-01T00:00:00+00:00"
LONG = " ".join(f"word{i}" for i in range(40))
WINDOW = 16


@pytest_asyncio.fixture
async def file_db(monkeypatch, tmp_path):
    """A database file of this test's own, as the process-wide connection.

    A fix run goes through the shared connection's transaction(), and the block
    index file lives beside the database, so both paths point here.
    """
    # Fresh lock per test: an asyncio.Lock binds to the first loop that contends it.
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    session.reset_pauses_for_tests()
    await database.close_db()
    path = str(tmp_path / "siblings.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    block_index._cache.clear()
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        block_index._cache.clear()
        session.reset_pauses_for_tests()


@pytest.fixture
def queue(monkeypatch):
    """A task queue that is never started, so what a call queued stays in the table."""
    q = tasks.MemoryTaskQueue()
    q._running = True
    monkeypatch.setattr(tasks, "_task_queue", q)
    return q


@pytest.fixture
def builds_on(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    fake_embedding_client.token_window = WINDOW
    return fake_embedding_client


async def _insert_memory(db, content, *, agent=AGENT, channel="", locked=0, msg_id="") -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, channel, content, msg_id, source, timestamp, locked)"
        " VALUES (?, ?, ?, ?, '{}', ?, ?)",
        (agent, channel, content, msg_id, STAMP, locked),
    )
    await db.commit()
    return cur.lastrowid


async def _contents(db, agent=AGENT) -> dict[int, str]:
    rows = await db.execute_fetchall("SELECT id, content FROM memories WHERE agent_id = ?", (agent,))
    return dict(rows)


async def _queued(db, queue) -> set[tuple[str, str, int, str]]:
    """(task type, agent, row id, session) for every build left in the queue."""
    import json

    rows = await db.execute_fetchall("SELECT id, task_type, agent_id, payload FROM pending_memory_tasks")
    return {
        (task_type, agent, json.loads(payload)["id"], queue._session_for(task_id))
        for task_id, task_type, agent, payload in rows
    }


# ==========================================================================
# bug-781: the annotation repair strips to the fixed point the write seam does
# ==========================================================================


@pytest.mark.asyncio
async def test_bug781_the_annotation_repair_leaves_no_annotation_behind(file_db):
    db = file_db
    # One pass removes '[Memory from a] ' and joins the rest into '[Memory from b] '.
    raw = "[Memory fr[Memory from a] om b] keep this"
    row = await _insert_memory(db, raw)

    (found,) = await checks.check_memory_annotation(db, AGENT, fix=True)
    await db.commit()

    assert (found["count"], found["repairable"]) == (1, 1), found
    stored = (await _contents(db))[row]
    assert stored == "keep this", stored
    # The text the write seam would have stored for the same input.
    assert stored == utils._sanitize_with_cap(raw, 10_000)[0]
    assert await checks.check_memory_annotation(db, AGENT, fix=False) == [], (
        "the repaired row still holds an annotation, so the next run rewrites it again"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<@<@1>2> keep this", "keep this"),
        ("<@!<@<@3>2>1>x", "x"),
        ("a <@1> b <@!22> c", "a  b  c"),
        ("5<@1>> <@> <@!> <@x1> !1>", "5> <@> <@!> <@x1> !1>"),
    ],
)
async def test_bug806_the_mention_repair_leaves_no_mention_behind(file_db, raw, expected):
    db = file_db
    row = await _insert_memory(db, raw)

    await checks.check_discord_mention(db, AGENT, fix=True)
    await db.commit()

    assert (await _contents(db))[row] == expected
    assert utils._MENTION_PATTERN.search(expected) is None
    leftover = await checks.check_discord_mention(db, AGENT, fix=False)
    assert all(i["repairable"] == 0 for i in leftover), leftover


# ==========================================================================
# bug-782: a repair that rewrites a row's text queues its node and block builds
# ==========================================================================


_REWRITES = [
    ("memory_annotation", "[Memory from x] " + LONG),
    ("discord_mention", "<@123> " + LONG),
    ("oversized_content", LONG + " tail"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("check, raw", _REWRITES, ids=[c for c, _ in _REWRITES])
async def test_bug782_a_rewritten_row_gets_its_builds_queued(
    file_db, queue, builds_on, monkeypatch, check, raw
):
    db = file_db
    monkeypatch.setattr(checks, "MAX_CONTENT_LENGTH", len(LONG))
    row = await _insert_memory(db, raw)

    result = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=[check], session_key="session-782"
    )

    assert "error" not in result, result
    assert (await _contents(db))[row] == LONG, "the repair did not rewrite the row"
    assert await _queued(db, queue) == {
        (blocks.TASK_TYPE, AGENT, row, "session-782"),
        (nodes.TASK_TYPE, AGENT, row, "session-782"),
    }


@pytest.mark.asyncio
async def test_bug782_a_report_only_run_and_a_deleted_row_queue_nothing(file_db, queue, builds_on):
    """The builds follow the rows a committed repair left rewritten, and nothing else."""
    db = file_db
    kept = await _insert_memory(db, LONG)
    # Rewrites into the text the row above holds, so the repair deletes it instead.
    doomed = await _insert_memory(db, "[Memory from x] " + LONG)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False, checks=["memory_annotation"])
    assert await _queued(db, queue) == set(), "a report-only run queued builds"

    await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["memory_annotation"], session_key="session-782"
    )
    assert list(await _contents(db)) == [kept], "the colliding row was not deleted"
    assert await _queued(db, queue) == set(), f"a build was queued for the deleted row {doomed}"


# ==========================================================================
# bug-787: the v12 migration keeps a locked row as each group's survivor
# ==========================================================================


@pytest_asyncio.fixture
async def v11_db(monkeypatch, tmp_path):
    """A schema 11 database file, opened through get_db() by the test.

    With its full-text index, as a schema 11 database has: a row the migration
    deletes is removed from it by trigger, which needs the row to be there.
    """
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    await database.close_db()
    path = str(tmp_path / "v11.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    conn = await aiosqlite.connect(path)
    await conn.executescript(database.SCHEMA_SQL)
    await conn.executescript(database.FTS_SQL)
    await conn.execute("INSERT INTO schema_version (version) VALUES (11)")
    yield conn
    await database.close_db()


async def _raw_insert(conn, content, msg_id, locked):
    cur = await conn.execute(
        "INSERT INTO memories (agent_id, project_id, msg_id, content, timestamp, locked)"
        " VALUES ('a', '', ?, ?, ?, ?)",
        (msg_id, content, STAMP, locked),
    )
    return cur.lastrowid


async def _index_names(db) -> set[str]:
    (version,) = (await db.execute_fetchall("SELECT MAX(version) FROM schema_version"))[0]
    assert version == database.SCHEMA_VERSION, f"the migration stopped at {version}"
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
        " AND name IN ('idx_memories_dedup_content', 'idx_memories_dedup_msg_id')"
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_bug787_the_content_step_keeps_the_locked_copy(v11_db):
    older = await _raw_insert(v11_db, "same text", "", locked=0)
    newer_locked = await _raw_insert(v11_db, "same text", "", locked=1)
    await v11_db.commit()
    await v11_db.close()

    db = await database.get_db()

    rows = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = 'a'")
    assert [r[0] for r in rows] == [newer_locked], f"expected the locked copy alone, older was {older}"
    assert "idx_memories_dedup_content" in await _index_names(db), "the content unique index was skipped"


@pytest.mark.asyncio
async def test_bug787_the_msg_id_step_keeps_the_locked_rows_msg_id(v11_db):
    older_locked = await _raw_insert(v11_db, "first text", "m-1", locked=1)
    newer = await _raw_insert(v11_db, "second text", "m-1", locked=0)
    await v11_db.commit()
    await v11_db.close()

    db = await database.get_db()

    msg_ids = dict(await db.execute_fetchall("SELECT id, msg_id FROM memories WHERE agent_id = 'a'"))
    assert msg_ids == {older_locked: "m-1", newer: ""}, msg_ids
    assert "idx_memories_dedup_msg_id" in await _index_names(db), "the msg_id unique index was skipped"


# ==========================================================================
# bug-788: the duplicate-content repair keeps a locked copy as the survivor
# ==========================================================================


async def _without_content_dedup_index(db) -> None:
    """A corpus from before the unique index, which can hold exact duplicates."""
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_content")
    await db.commit()


@pytest.mark.asyncio
async def test_bug788_a_locked_newer_copy_survives_and_the_group_clears(file_db):
    db = file_db
    await _without_content_dedup_index(db)
    older = await _insert_memory(db, "same text")
    locked = await _insert_memory(db, "same text", locked=1)

    (before,) = await checks.check_duplicate_content(db, AGENT, fix=False)
    assert (before["total_extra"], before["repairable"]) == (1, 1), before

    await checks.check_duplicate_content(db, AGENT, fix=True)
    await db.commit()

    assert list(await _contents(db)) == [locked], f"the unlocked copy {older} was kept"
    assert await checks.check_duplicate_content(db, AGENT, fix=False) == [], (
        "the group is still reported after the repair"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["", "c1"], ids=["shared-channel", "named-channel"])
async def test_bug788_declarations_move_to_the_locked_survivor(file_db, channel):
    db = file_db
    await _without_content_dedup_index(db)
    older = await _insert_memory(db, "same text", channel=channel)
    locked = await _insert_memory(db, "same text", channel=channel, locked=1)
    report = await associations.declare(AGENT, {"entities": [{"name": "Tea"}]}, anchor_ref=f"mem:{older}")
    assert report.get("ok", True) is not False, report

    await checks.check_duplicate_content(db, AGENT, fix=True)
    await db.commit()

    refs = {r[0] for r in await db.execute_fetchall("SELECT ref FROM entity_mentions")}
    assert refs == {f"mem:{locked}"}, refs


@pytest.mark.asyncio
async def test_bug788_an_unlocked_group_still_keeps_its_oldest_copy(file_db):
    """With no locked copy the survivor is the oldest, as before."""
    db = file_db
    await _without_content_dedup_index(db)
    older = await _insert_memory(db, "same text")
    await _insert_memory(db, "same text")

    await checks.check_duplicate_content(db, AGENT, fix=True)
    await db.commit()

    assert list(await _contents(db)) == [older]


# ==========================================================================
# bug-789: an agent-scoped block index check reports no store-wide count
# ==========================================================================


_BLOCK_INSERT = (
    "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id,"
    " channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model)"
    " VALUES ('mem', ?, 0, ?, '', '', 0, 1, 0, ?, 'm')"
)


@pytest.mark.asyncio
async def test_bug789_only_a_whole_store_call_reports_the_change_count(file_db, monkeypatch):
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    db = file_db
    await db.executemany(_BLOCK_INSERT, [(i, AGENT, bytes([i]) * 4) for i in range(1, 4)])
    await db.commit()
    built = await block_index.build_block_index()
    assert built["built"], built
    # Another agent's writes after the build, which the change log counts.
    await db.executemany(_BLOCK_INSERT, [(i, OTHER, bytes([i]) * 4) for i in range(10, 13)])
    # A few logged changes are read live and leave the file current, so a schema
    # change puts it behind: recreating one logging trigger moves only the cookie.
    (sql,) = (await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE name = 'record_block_log_au'"))[0]
    await db.execute("DROP TRIGGER record_block_log_au")
    await db.execute(sql)
    await db.commit()

    whole, _ = await checks.run_health_checks(db, agent_id="", checks=["block_index"])
    scoped, _ = await checks.run_health_checks(db, agent_id=AGENT, checks=["block_index"])

    assert [i["type"] for i in whole] == ["block_index_behind"], whole
    assert whole[0]["changes_since_build"] == 3, whole
    assert [i["type"] for i in scoped] == ["block_index_behind"], scoped
    assert "changes_since_build" not in scoped[0], (
        "an agent-scoped call reported how many block changes other agents made: " + repr(scoped)
    )
