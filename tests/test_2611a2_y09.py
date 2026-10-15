"""Regression tests for the background task queue's attribution and its drain.

  build attribution  A store or an archive queues its record's node and block builds
                     under the caller's own session key. The queue only knows a
                     row's session from what its enqueue was told, so a call site
                     that hands over the shared keyless bucket instead files the
                     build under every keyless caller's pause.
  reconcile chunks   The attribution reconcile reads the live ids in chunks. A
                     reconcile that read only the first chunk would forget every
                     live row past it, and those rows would fall back to the shared
                     bucket.
  profile failure    The real update_profile handler reports a refused write as
                     ``ok: False`` with a reason and no ``error`` key. The drain has
                     to read that shape as a failure (retry, then discard), not as a
                     completion.
  block index        A drained block build is the moment the block index file falls
                     behind, so the drain queues the index rebuild after it.

Every test runs on a database of its own, with a real ``MemoryTaskQueue`` that is
never started: the test drains it by hand, so what ran is what the test chose.
"""

import contextlib
import logging
import os
import shutil
import tempfile

import pytest

from cpersona import (
    admin_handlers,
    block_index,
    blocks,
    config,
    database,
    memory_handlers,
    nodes,
    session,
    tasks,
)

AGENT = "agent.y09"
CALLER = "session-caller"
#: Tokens per window for the conftest token report; a text of more runs past it.
WINDOW = 24
#: Runs well past WINDOW and divides into several clauses, so a write of it queues
#: both a node build and a block build.
LONG = (
    "The first clause names the reason for the change. "
    "The second clause says where the change was made. "
    "The third clause says what a reader should check afterwards. "
    "The fourth clause closes the note with the follow-up that is still open."
)
#: Several clauses, short: queues a block build and no node build.
CLAUSES = "一文目です。二文目です。\n\n三文目です。四文目です。"


@contextlib.asynccontextmanager
async def _queue_db():
    """A database and a stopped queue of this test's own."""
    session.reset_pauses_for_tests()
    directory = tempfile.mkdtemp()
    saved = (database._db, database.DB_PATH, config.DB_PATH, tasks._task_queue)
    database._db = None
    database.DB_PATH = config.DB_PATH = os.path.join(directory, "y09.db")
    block_index._cache.clear()
    queue = tasks.MemoryTaskQueue()
    tasks._task_queue = queue
    try:
        yield queue, await database.get_db()
    finally:
        await database.close_db()
        database._db, database.DB_PATH, config.DB_PATH, tasks._task_queue = saved
        block_index._cache.clear()
        session.reset_pauses_for_tests()
        shutil.rmtree(directory, ignore_errors=True)


async def _drain(queue) -> None:
    queue._running = True
    try:
        await queue._drain(admin_handlers, memory_handlers, nodes)
    finally:
        queue._running = False


async def _pending(db) -> list[tuple[int, str]]:
    return await db.execute_fetchall("SELECT id, task_type FROM pending_memory_tasks ORDER BY id")


# ---------------------------------------------------------------------------
# build attribution: store and archive queue their builds under the caller's key
# ---------------------------------------------------------------------------


async def _store(agent_id: str, text: str, session_key: str) -> dict:
    return await memory_handlers.do_store(agent_id, {"content": text}, session_key=session_key)


async def _archive(agent_id: str, text: str, session_key: str) -> dict:
    return await memory_handlers.do_archive_episode(agent_id, [], summary=text, session_key=session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [_store, _archive], ids=["store", "archive_episode"])
async def test_a_write_queues_its_builds_under_the_callers_session(write, monkeypatch, fake_embedding_client):
    """Both builds of a record written by a declared session belong to that session.

    Each of the four call sites (node and block build, for store and for archive)
    is checked on its own row, so one call site that passes the shared bucket
    instead of the caller's key fails here."""
    fake_embedding_client.token_window = WINDOW
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    async with _queue_db() as (queue, db):
        result = await write(AGENT, LONG, CALLER)
        assert result["ok"] is True, result
        assert result["nodes"] == {"status": "queued"}, result
        assert result["blocks"] == {"status": "queued"}, result

        pending = await _pending(db)
        assert sorted(t for _, t in pending) == sorted([nodes.TASK_TYPE, blocks.TASK_TYPE]), pending
        attributed = {task_type: queue._session_for(task_id) for task_id, task_type in pending}
        assert attributed == {nodes.TASK_TYPE: CALLER, blocks.TASK_TYPE: CALLER}, (
            "a build was queued under another session than the caller's: " + repr(attributed)
        )

        # What the attribution decides: the caller's pause is the one that governs
        # these rows, and a keyless caller's pause is not.
        session.pause_for(session.TRANSPORT_KEY, False, 600)
        assert not any(session.is_paused_for(queue._session_for(i)) for i, _ in pending)
        session.pause_for(CALLER, True, 600)
        assert all(session.is_paused_for(queue._session_for(i)) for i, _ in pending)


# ---------------------------------------------------------------------------
# reconcile chunks: live rows past the first chunk keep their attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconcile_keeps_live_rows_in_every_chunk():
    """Live rows at both edges of the first chunk and inside later ones, up to the
    last id the map can hold. Every other attribution names a vanished row."""
    async with _queue_db() as (queue, db):
        held = list(range(1, queue._MAX_ATTRIBUTED_TASKS + 1))
        chunk = tasks._RECONCILE_READ_CHUNK
        assert len(held) > 4 * chunk, "the map's cap no longer spans several chunks"
        live = [1, chunk, chunk + 1, 4 * chunk + 1, held[-1]]
        queue._task_sessions = {task_id: f"session-{task_id}" for task_id in held}
        await db.executemany(
            "INSERT INTO pending_memory_tasks (id, task_type, agent_id, payload) "
            "VALUES (?, 'update_profile', 'a1', '\"x\"')",
            [(task_id,) for task_id in live],
        )
        await db.commit()

        await queue._forget_vanished_rows()

        assert queue._task_sessions == {task_id: f"session-{task_id}" for task_id in live}, (
            "the reconcile kept "
            + repr(sorted(queue._task_sessions)[:10])
            + f" ({len(queue._task_sessions)} in all) for live rows {live}"
        )


# ---------------------------------------------------------------------------
# profile failure: the real handler's refusal is retried, then discarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("profile, reason", [("", "empty profile"), ("   ", "empty after sanitization")])
async def test_a_refused_profile_write_takes_the_retry_path(profile, reason, monkeypatch, caplog):
    """The real handler, not a double: its refusal carries ``ok: False`` and a
    reason, and no ``error`` key, so only the ``ok is False`` half of the drain's
    check can see it."""
    monkeypatch.setattr(tasks, "TASK_MAX_RETRIES", 3)
    monkeypatch.setattr(tasks, "TASK_RETRY_DELAY", 0)
    real = admin_handlers.do_update_profile
    results: list[dict] = []

    async def counting(agent_id, payload, session_key=""):
        out = await real(agent_id, payload, session_key=session_key)
        results.append(out)
        return out

    monkeypatch.setattr(admin_handlers, "do_update_profile", counting)
    async with _queue_db() as (queue, db):
        await queue.enqueue("update_profile", AGENT, profile)
        with caplog.at_level(logging.INFO, logger=tasks.logger.name):
            await _drain(queue)

        expected = {"ok": False, "profiles_updated": 0, "reason": reason}
        assert results and all(r == expected for r in results), results
        assert len(results) == tasks.TASK_MAX_RETRIES, (
            f"the refusal ran {len(results)} time(s): it was read as a completion, not retried"
        )
        assert await _pending(db) == []
        messages = [r.getMessage() for r in caplog.records]
        assert any("exceeded max retries, discarding" in m for m in messages), messages
        assert not any(m.startswith("MemoryTaskQueue: completed update_profile") for m in messages), messages


# ---------------------------------------------------------------------------
# block index: a drained block build queues the index rebuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_drained_block_build_brings_the_block_index_file_up(monkeypatch, fake_embedding_client):
    """The store queues only the block build; the index build that follows is the
    drain's to queue. Without it no file is written until a restart or a backfill
    pass queues one."""
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    monkeypatch.setattr(block_index, "_declined_until", 0.0)
    ran: list[str] = []
    real_build = block_index.build_task

    async def recording_build(payload):
        ran.append(block_index.TASK_TYPE)
        return await real_build(payload)

    monkeypatch.setattr(block_index, "build_task", recording_build)
    async with _queue_db() as (queue, db):
        result = await memory_handlers.do_store(AGENT, {"content": CLAUSES})
        assert result["blocks"] == {"status": "queued"}, result
        assert [t for _, t in await _pending(db)] == [blocks.TASK_TYPE]
        assert not os.path.exists(block_index.index_path())

        await _drain(queue)

        assert ran == [block_index.TASK_TYPE], "the drained block build queued no index build"
        assert await _pending(db) == []
        index = block_index.load_block_index(block_index.index_path())
        assert index is not None and index.count > 0, "the index file holds none of the new blocks"


@pytest.mark.asyncio
async def test_a_drained_backfill_sweep_brings_the_block_index_file_up(monkeypatch, fake_embedding_client):
    """The same moment on the sweep's branch: records stored before construction was
    on get their blocks from the backfill, and the file then falls behind as well."""
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    monkeypatch.setattr(block_index, "_declined_until", 0.0)
    ran: list[str] = []
    real_build = block_index.build_task

    async def recording_build(payload):
        ran.append(block_index.TASK_TYPE)
        return await real_build(payload)

    monkeypatch.setattr(block_index, "build_task", recording_build)
    async with _queue_db() as (queue, db):
        monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", False)
        result = await memory_handlers.do_store(AGENT, {"content": CLAUSES})
        assert "blocks" not in result, result
        monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
        assert await blocks._enqueue_backfill({})
        assert [t for _, t in await _pending(db)] == [blocks.BACKFILL_TASK_TYPE]

        await _drain(queue)

        assert ran == [block_index.TASK_TYPE], "the drained backfill sweep queued no index build"
        assert await _pending(db) == []
        index = block_index.load_block_index(block_index.index_path())
        assert index is not None and index.count > 0, "the index file holds none of the swept blocks"
