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
  bug-779  The remote index sync after a repair run kept posting chunk after chunk
           to a service that had stopped answering, one index deadline each.
  bug-785  The coverage ledger read the returned records with one IN (...) over
           every id, where every other id read on the recall path is chunked to
           stay under SQLite's bound-variable limit.
  bug-786  Three "is remote search armed" predicates tested only that an
           embedding URL is set. A URL that does not end in /embed names no route,
           so the remote calls are skipped, and those predicates described remote
           search that never runs.
  bug-790  excerpts.fill_ranges computed each ranked block's governing range
           before asking whether the block already lay inside a range it took.
  bug-793  The block index loader reopened its path for each array, and the cache
           held the replaced index while its successor loaded. The vector and
           coarse loaders read from one handle and let go first (bug-744/745).
  bug-792  check_update(apply=true) steps could prompt on the controlling
           terminal (git credentials, an ssh passphrase), with nothing bounding
           the step.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import struct
import sys

import aiosqlite
import numpy as np
import pytest
import pytest_asyncio

from cpersona import (
    admin_handlers,
    associations,
    block_index,
    blocks,
    checks,
    coarse_notice,
    config,
    coverage,
    database,
    excerpts,
    maintenance_handlers,
    nodes,
    server,
    session,
    tasks,
    update_check,
    utils,
    vector,
    vector_index,
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


# ==========================================================================
# bug-785: the coverage ledger reads its records in chunks
# ==========================================================================


@pytest.mark.asyncio
async def test_bug785_the_coverage_read_stays_under_the_bound_variable_limit(file_db, monkeypatch):
    db = file_db
    ids = [await _insert_memory(db, f"record {n}") for n in range(12)]
    foreign = await _insert_memory(db, "another agent's record", agent=OTHER)
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, summary) VALUES (?, 'an episode summary')", (AGENT,)
    )
    episode = cur.lastrowid
    await db.commit()
    # A connection that binds at most 8 variables a statement, and chunks of 4.
    monkeypatch.setattr(vector, "_ID_FETCH_CHUNK", 4)
    async with database.connection() as read:
        await read._execute(read._connection.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)

    refs = [f"mem:{i}" for i in ids] + [f"mem:{foreign}", f"ep:{episode}", "profile"]
    texts = await coverage.stored_texts(AGENT, refs)

    expected = {f"mem:{i}": f"record {n}" for n, i in enumerate(ids)}
    expected[f"ep:{episode}"] = "an episode summary"
    assert texts == expected


# ==========================================================================
# bug-786: one predicate says whether the remote vector arm runs
# ==========================================================================


_ROUTED = "http://embed.test/embed"
_UNROUTED = "http://embed.test/v1/embeddings"


@pytest.fixture
def remote_mode(monkeypatch, fake_embedding_client):
    for module in (vector, checks, server):
        monkeypatch.setattr(module, "VECTOR_SEARCH_MODE", "remote")

    def configure(url):
        monkeypatch.setattr(fake_embedding_client, "_http_url", url, raising=False)

    return configure


@pytest.mark.parametrize(
    "url, armed",
    [(_ROUTED, True), (_ROUTED + "?key=k", True), (_UNROUTED, False), ("", False)],
)
def test_bug786_the_remote_arm_runs_only_where_a_route_derives(remote_mode, url, armed):
    remote_mode(url)
    assert vector.remote_arm_armed("remote") is armed
    assert vector.remote_arm_armed("local") is False
    assert checks._remote_vector_arm_armed() is armed
    # A recall ranks the local store exactly when the remote arm does not run.
    assert coarse_notice.local_vector_search() is not armed


@pytest.mark.parametrize("url, level", [(_UNROUTED, logging.WARNING), (_ROUTED, logging.INFO)])
def test_bug786_the_boot_notice_names_an_unrouted_endpoint_as_no_arm(remote_mode, monkeypatch, caplog, url, level):
    remote_mode(url)
    monkeypatch.setattr(server, "STORE_BLOB", False)
    with caplog.at_level(logging.INFO, logger=server.logger.name):
        server._log_vector_fallback_trade()
    (record,) = [r for r in caplog.records if "CPERSONA_STORE_BLOB=false" in r.getMessage()]
    assert record.levelno == level, record.getMessage()


# ==========================================================================
# bug-790: fill_ranges skips a block already inside a taken range
# ==========================================================================


def _old_fill_ranges(text, spans, ranked, cap):
    """fill_ranges as it was before bug-790, the reference its answers must equal."""
    chosen = []
    used = 0
    for row in ranked:
        start, end, _ = blocks.context_range(text, spans, row[0])
        if any(start < e and s < end for s, e in chosen):
            continue
        touching = any(start == e or end == s for s, e in chosen)
        cost = (end - start) + (len(excerpts.SEPARATOR) if chosen and not touching else 0)
        if used + cost > cap:
            if not chosen:
                return [(start, start + cap)], True
            break
        chosen.append((start, end))
        used += cost
    return excerpts._joined(sorted(chosen)), False


def _blocks_of(text):
    rows = [(i, s.start, s.end, None) for i, s in enumerate(blocks.segment(text))]
    return rows, [(r[1], r[2]) for r in rows]


def test_bug790_the_ranges_are_the_ones_the_old_scan_chose():
    rng = random.Random(790)
    words = ["alpha", "beta", "however", "but", "so", "the cat sat.", "Yes!", "and then", "end.", "## Head", "x" * 30]
    compared = 0
    for _ in range(1500):
        parts = []
        for _ in range(rng.randint(1, 50)):
            parts.append(" ".join(rng.choice(words) for _ in range(rng.randint(1, 6))))
            parts.append(rng.choice(["\n", "\n\n", " ", ". ", "\n- "]))
        text = "".join(parts)
        rows, spans = _blocks_of(text)
        if len(rows) < 2:
            continue
        ranked = rows[:]
        rng.shuffle(ranked)
        cap = rng.choice([20, 80, 200, 600, 2000, 10**6])
        assert excerpts.fill_ranges(text, spans, ranked, cap) == _old_fill_ranges(text, spans, ranked, cap), text
        compared += 1
    assert compared > 1000
    # Long records of unterminated lines. A governing range reaches into a taken one
    # without containing its own block only where a range stopped at the character
    # limit, which the short records above seldom reach.
    for _ in range(200):
        text = "\n".join(f"item {i} on the list" for i in range(rng.randint(40, 400)))
        rows, spans = _blocks_of(text)
        ranked = rows[:]
        rng.shuffle(ranked)
        cap = rng.choice([600, 2000, 10**6])
        assert excerpts.fill_ranges(text, spans, ranked, cap) == _old_fill_ranges(text, spans, ranked, cap)


def test_bug790_a_block_inside_a_taken_range_gets_no_context_walk(monkeypatch):
    # Lines that end no sentence, so each governing range reaches over its neighbours.
    text = "\n".join(f"item number {i} on the list" for i in range(300))
    rows, spans = _blocks_of(text)
    first = rows[20]
    taken_start, taken_end, _ = blocks.context_range(text, spans, first[0])
    inside = {r[0] for r in rows if taken_start <= r[1] and r[2] <= taken_end} - {first[0]}
    assert len(inside) > 10, "the fixture has to put blocks inside the first range"
    walked = []
    real = blocks.context_range

    def spy(text_, spans_, index, *args, **kwargs):
        walked.append(index)
        return real(text_, spans_, index, *args, **kwargs)

    monkeypatch.setattr(blocks, "context_range", spy)
    excerpts.fill_ranges(text, spans, [first, *[r for r in rows if r is not first]], 10**6)

    assert walked[0] == first[0]
    assert not inside & set(walked), sorted(inside & set(walked))[:5]


# ==========================================================================
# bug-792: an apply step cannot prompt
# ==========================================================================


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="sessions and controlling terminals are POSIX")
async def test_bug792_an_apply_step_is_told_not_to_prompt_and_has_no_terminal(monkeypatch):
    # An operator's own setting is overridden for the step; the rest of the
    # environment reaches it.
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("CPERSONA_SIBLINGS_MARK", "kept")
    probe = (
        "import os; print(os.environ.get('GIT_TERMINAL_PROMPT'), os.environ.get('PIP_NO_INPUT'),"
        " os.environ.get('CPERSONA_SIBLINGS_MARK'), os.getsid(0) == os.getpid())"
    )
    code, output = await update_check._run_step([sys.executable, "-c", probe])
    assert code == 0, output
    assert output.strip().splitlines()[-1] == "0 1 kept True", output


# ==========================================================================
# bug-779: the remote sync stops after a post times out
# ==========================================================================


def _sync_effects():
    """300 deletes and 5 rewrites under one agent: three removal posts, one index post."""
    effects = [(AGENT, row_id, None) for row_id in range(1, 301)]
    effects += [(AGENT, row_id, f"text {row_id}") for row_id in range(301, 306)]
    return effects


@pytest.mark.asyncio
@pytest.mark.parametrize("times_out, posts", [(True, 1), (False, 4)], ids=["timeout", "answered"])
async def test_bug779_the_remote_sync_stops_after_a_timeout(monkeypatch, caplog, times_out, posts):
    sent = []

    async def fake_post(route, payload, *, action, namespace):
        sent.append((route, len(payload.get("ids") or payload.get("items"))))
        return times_out

    monkeypatch.setattr(admin_handlers, "_post_remote_side_effect", fake_post)
    with caplog.at_level(logging.WARNING, logger=maintenance_handlers.logger.name):
        await maintenance_handlers._sync_remote_index(_sync_effects())

    assert len(sent) == posts, sent
    skipped = [r.getMessage() for r in caplog.records if "Skipped the remaining remote index posts" in r.getMessage()]
    if times_out:
        # 300 - 128 removals and 5 rewrites never reached the service.
        assert skipped and "177 more repaired row(s)" in skipped[0], skipped
    else:
        assert sent == [("remove", 128), ("remove", 128), ("remove", 44), ("index", 5)]
        assert not skipped


# ==========================================================================
# bug-793: the block index loads from one handle, and the cache lets go first
# ==========================================================================


async def _two_block_files(db, tmp_path):
    """Two built block index files of one shape: parents 1-3, then 101-103."""
    await db.executemany(_BLOCK_INSERT, [(i, AGENT, bytes([i]) * 4) for i in range(1, 4)])
    await db.commit()
    first, second = str(tmp_path / "first.blk"), str(tmp_path / "second.blk")
    assert (await block_index.build_block_index(first))["built"]
    await db.execute("UPDATE record_blocks SET parent_id = parent_id + 100")
    await db.commit()
    assert (await block_index.build_block_index(second))["built"]
    return first, second


@pytest.mark.asyncio
@pytest.mark.parametrize("maps", [True, False], ids=["mapped", "read"])
async def test_bug793_a_rebuild_landing_mid_load_is_not_mixed_in(file_db, monkeypatch, tmp_path, maps):
    first, second = await _two_block_files(file_db, tmp_path)
    moved = []

    def move_then_answer():
        # Between the header read and the array reads: a rebuild lands on the path.
        if not moved:
            os.replace(second, first)
            moved.append(True)
        return maps

    monkeypatch.setattr(vector_index, "_maps_files", move_then_answer)
    index = block_index.load_block_index(first)

    assert moved, "the hook never ran, so nothing was tested"
    assert sorted(int(v) for v in index.parent_id) == [1, 2, 3]


@pytest.mark.asyncio
async def test_bug793_the_replaced_index_is_released_before_its_successor_loads(file_db, monkeypatch, tmp_path):
    first, second = await _two_block_files(file_db, tmp_path)
    assert sorted(int(v) for v in block_index.cached_block_index(first).parent_id) == [1, 2, 3]
    os.replace(second, first)
    held_during_load = []
    real = block_index.load_block_index

    def watch(src=None):
        held_during_load.append(src in block_index._cache)
        return real(src)

    monkeypatch.setattr(block_index, "load_block_index", watch)
    reloaded = block_index.cached_block_index(first)

    assert held_during_load == [False], "the replaced index was still cached while its successor loaded"
    assert sorted(int(v) for v in reloaded.parent_id) == [101, 102, 103]


# ---------------------------------------------------------------------------
# bug-797 — an empty transport is refused, and the refusal says it was empty
# ---------------------------------------------------------------------------


def test_bug797_an_empty_transport_is_refused_as_empty(monkeypatch):
    monkeypatch.setenv("CPERSONA_TRANSPORT", "")
    with pytest.raises(ValueError, match="Unknown transport") as refused:
        config.validate_transport()

    message = str(refused.value)
    assert "CPERSONA_TRANSPORT is set but empty" in message
    assert "unset it to use stdio" in message


def test_bug797_a_misspelt_transport_keeps_its_own_refusal(monkeypatch):
    monkeypatch.setenv("CPERSONA_TRANSPORT", "http")
    with pytest.raises(ValueError) as refused:
        config.validate_transport()

    assert str(refused.value) == "Unknown transport: http (expected one of stdio, streamable-http)"


# ---------------------------------------------------------------------------
# bug-783 — a lone surrogate is refused at the tool boundary, for every tool
# ---------------------------------------------------------------------------

LONE = "\udfff"


def _string_arguments():
    cases = []
    for tool in server.registry._tools:
        for name, spec in (tool.inputSchema or {}).get("properties", {}).items():
            kinds = spec.get("type")
            if "string" in (kinds if isinstance(kinds, list) else [kinds]):
                cases.append(pytest.param(tool.name, name, id=f"{tool.name}.{name}"))
    return cases


def _refusal_text(response: dict) -> str:
    return str(response.get("error") or response.get("reason") or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "argument"), _string_arguments())
async def test_bug783_every_string_argument_is_refused_at_the_boundary(file_db, tool, argument):
    response = await asyncio.wait_for(server.registry._handlers[tool]({argument: LONE}), 30)

    assert response.get("ok") is False, response
    assert f"{argument} holds a lone UTF-16 surrogate" in _refusal_text(response), response


@pytest.mark.asyncio
async def test_bug783_a_nested_string_is_named_by_its_path(file_db):
    arguments = {"agent_id": "agent.a", "associations": {"entities": [{"name": "fine"}, {"name": LONE}]}}
    response = await server.registry._handlers["declare_associations"](arguments)

    assert response["error"].startswith("associations.entities[1].name holds a lone UTF-16 surrogate")
    assert (response["result"], response["entities"], response["relations"]) == ("rejected", [], [])


@pytest.mark.asyncio
async def test_bug783_a_key_is_refused_without_quoting_it(file_db):
    arguments = {"agent_id": "agent.a", "content": "fine", "metadata": {LONE: 1}}
    response = await server.registry._handlers["store"](arguments)

    assert response["result"] == "rejected", response
    assert response["reason"].startswith("a key in metadata holds a lone UTF-16 surrogate")
    response["reason"].encode("utf-8")  # the refusal does not carry the character it refuses


# The tools that refuse a lone surrogate themselves (bug-633): the boundary answers
# them with exactly what they answer, so a caller cannot tell which layer refused.
_SELF_CHECKED = [
    ("store", {"agent_id": "agent.a", "content": LONE}),
    ("store", {"agent_id": "agent.a", "content": LONE, "lock": True}),
    ("archive_episode", {"agent_id": "agent.a", "summary": LONE}),
    ("recall", {"agent_id": "agent.a", "query": LONE}),
    ("recall_with_context", {"agent_id": "agent.a", "query": LONE}),
    ("reconstruct", {"agent_id": "agent.a", "query": LONE}),
    ("get_contents", {"agent_id": LONE, "refs": ["mem:1"]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments"), _SELF_CHECKED, ids=lambda v: v if isinstance(v, str) else "")
async def test_bug783_the_boundary_answers_as_the_tool_itself_does(file_db, tool, arguments):
    own_handler = server.registry._handlers[tool]._acl_inner.__wrapped__
    own = await own_handler(dict(arguments))
    boundary = await server.registry._handlers[tool](dict(arguments))

    assert "lone UTF-16 surrogate" in _refusal_text(own), own
    assert boundary == own


@pytest.mark.asyncio
async def test_bug783_update_profile_is_refused_in_its_own_shape(file_db):
    own = await server.registry._handlers["update_profile"]({"agent_id": "agent.a", "profile": ""})
    boundary = await server.registry._handlers["update_profile"]({"agent_id": "agent.a", "profile": LONE})

    assert own == {"ok": False, "profiles_updated": 0, "reason": "empty profile"}
    assert set(boundary) == set(own)
    assert boundary["profiles_updated"] == 0


# ---------------------------------------------------------------------------
# bug-780 — a new precision is not stored beside a gate measured at another
# ---------------------------------------------------------------------------

CAL_AGENT = "agent.cal"
CAL_DIM = 8


def _reset_calibration_state():
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._agent_betas.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._reset_calibration_authority()


@pytest_asyncio.fixture
async def degraded_gate(file_db, monkeypatch):
    """60 embedded memories for CAL_AGENT and a fused-gate measurement that gives nothing,
    as with no embedding client or a failing backend. Yields the sidecar's path."""
    monkeypatch.setattr(config, "VECTOR_MIN_SIMILARITY", config.VECTOR_MIN_SIMILARITY)
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)

    async def nothing_measured(*args, **kwargs):
        return None

    monkeypatch.setattr(admin_handlers, "_calibrate_fused_gate_median", nothing_measured)
    _reset_calibration_state()
    rng = np.random.default_rng(780)
    for i in range(60):
        v = rng.standard_normal(CAL_DIM).astype(np.float32)
        v /= float(np.linalg.norm(v))
        await file_db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (CAL_AGENT, f"memory {i}", f"2026-10-01T00:{i:02d}:00+00:00", struct.pack(f"<{CAL_DIM}f", *v.tolist())),
        )
    await file_db.commit()
    try:
        yield admin_handlers._calibration_sidecar_path()
    finally:
        _reset_calibration_state()


def _hold(axis: str, value: float) -> None:
    getattr(vector, f"_{axis}")[CAL_AGENT] = value
    vector._record_agent_calibration_change(CAL_AGENT, axis)


def _stored(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "precision"), [(None, "strict"), (0.5, "")], ids=["set", "clear"])
async def test_bug780_a_precision_whose_gate_was_not_measured_is_not_applied(degraded_gate, current, precision):
    if current is not None:
        _hold("agent_betas", current)
    _hold("agent_fused_gates", 0.31)

    result = await admin_handlers.do_set_recall_precision(CAL_AGENT, precision)

    assert result["ok"] is False and "not applied" in result["error"], result
    assert vector._agent_betas.get(CAL_AGENT) == current
    stored = _stored(degraded_gate)  # what a restart restores
    assert stored["agent_betas"].get(CAL_AGENT) == current
    assert stored["agent_fused_gates"][CAL_AGENT] == 0.31


@pytest.mark.asyncio
async def test_bug780_an_agent_holding_no_gate_still_records_its_precision(degraded_gate):
    result = await admin_handlers.do_set_recall_precision(CAL_AGENT, "strict")

    assert result["ok"] is True and result["fused_gate"] is None, result
    assert vector._agent_betas[CAL_AGENT] == 2.0
    assert _stored(degraded_gate)["agent_betas"][CAL_AGENT] == 2.0


@pytest.mark.asyncio
async def test_bug780_a_precision_that_keeps_the_beta_is_applied(degraded_gate):
    _hold("agent_betas", 2.0)
    _hold("agent_fused_gates", 0.31)

    result = await admin_handlers.do_set_recall_precision(CAL_AGENT, "strict")

    assert result["ok"] is True, result
    stored = _stored(degraded_gate)
    assert (stored["agent_betas"][CAL_AGENT], stored["agent_fused_gates"][CAL_AGENT]) == (2.0, 0.31)
