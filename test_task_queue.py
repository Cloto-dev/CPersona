"""Tests for CPersona MemoryTaskQueue (Phase 5: background task queue)."""

import asyncio
import json
import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server module
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_cpersona.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"
os.environ["CPERSONA_TASK_QUEUE_ENABLED"] = "true"
os.environ["CPERSONA_TASK_MAX_RETRIES"] = "3"
os.environ["CPERSONA_TASK_RETRY_DELAY"] = "1"  # fast retry for tests
os.environ["CPERSONA_LLM_PROXY_URL"] = "http://127.0.0.1:1/noop"  # will fail → triggers fallback

import server  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh DB for each test."""
    # Reset task queue (but keep DB connection alive across tests)
    server._task_queue = None
    db = await server.get_db()
    # Clean tables
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM profiles")
    await db.execute("DELETE FROM episodes")
    await db.execute("DELETE FROM pending_memory_tasks")
    await db.commit()
    yield


# ============================================================
# Schema tests
# ============================================================


@pytest.mark.asyncio
async def test_pending_memory_tasks_table_exists():
    """pending_memory_tasks table should be created by schema init."""
    db = await server.get_db()
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_memory_tasks'"
    )
    assert len(rows) == 1
    assert rows[0][0] == "pending_memory_tasks"


@pytest.mark.asyncio
async def test_schema_version_is_2():
    """Schema version should be 2 after Phase 5 migration."""
    db = await server.get_db()
    rows = await db.execute_fetchall("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    assert len(rows) == 1
    assert rows[0][0] == 2


# ============================================================
# Enqueue tests
# ============================================================


@pytest.mark.asyncio
async def test_enqueue_creates_row():
    """Enqueue should INSERT a row into pending_memory_tasks."""
    queue = server.MemoryTaskQueue()
    task_id = await queue.enqueue("update_profile", "agent-1", [{"content": "hello"}])
    assert task_id is not None
    assert task_id > 0

    db = await server.get_db()
    rows = await db.execute_fetchall(
        "SELECT id, task_type, agent_id, payload, retries FROM pending_memory_tasks WHERE id = ?",
        (task_id,),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == task_id
    assert row[1] == "update_profile"
    assert row[2] == "agent-1"
    payload = json.loads(row[3])
    assert payload == [{"content": "hello"}]
    assert row[4] == 0  # retries


@pytest.mark.asyncio
async def test_enqueue_multiple_tasks():
    """Multiple tasks should be enqueued in order."""
    queue = server.MemoryTaskQueue()
    id1 = await queue.enqueue("archive_episode", "agent-1", [{"content": "a"}])
    id2 = await queue.enqueue("update_profile", "agent-1", [{"content": "b"}])
    assert id2 > id1

    db = await server.get_db()
    rows = await db.execute_fetchall("SELECT id, task_type FROM pending_memory_tasks ORDER BY id ASC")
    assert len(rows) == 2
    assert rows[0][1] == "archive_episode"
    assert rows[1][1] == "update_profile"


# ============================================================
# Queue status tests
# ============================================================


@pytest.mark.asyncio
async def test_get_status_empty():
    """Queue status should show 0 pending when empty."""
    queue = server.MemoryTaskQueue()
    status = await queue.get_status()
    assert status["enabled"] is True
    assert status["pending"] == 0


@pytest.mark.asyncio
async def test_get_status_with_tasks():
    """Queue status should reflect pending task count."""
    queue = server.MemoryTaskQueue()
    await queue.enqueue("update_profile", "agent-1", [{"content": "x"}])
    await queue.enqueue("archive_episode", "agent-2", [{"content": "y"}])
    status = await queue.get_status()
    assert status["pending"] == 2


# ============================================================
# Background processing tests
# ============================================================


async def _wait_queue_drained(db, timeout: float = 15.0) -> bool:
    """Poll until pending_memory_tasks is empty (no timing assumptions)."""
    for _ in range(int(timeout * 10)):
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
        if rows[0][0] == 0:
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.mark.asyncio
async def test_queue_processes_update_profile():
    """Queue should process update_profile task and create a profile entry."""
    queue = server.MemoryTaskQueue()
    await queue.start()

    history = [
        {"content": "My name is Alice", "source": {"User": "user-1"}},
        {"content": "Nice to meet you", "source": {"Agent": "agent-1"}},
    ]
    await queue.enqueue("update_profile", "agent-test", history)

    db = await server.get_db()
    assert await _wait_queue_drained(db), "Queue did not drain in time"

    rows = await db.execute_fetchall("SELECT content FROM profiles WHERE agent_id = 'agent-test'")
    assert len(rows) == 1
    assert "Alice" in rows[0][0]

    await queue.stop()


@pytest.mark.asyncio
async def test_queue_processes_archive_episode():
    """Queue should process archive_episode task and create an episode entry."""
    queue = server.MemoryTaskQueue()
    await queue.start()

    history = [
        {"content": "Let's discuss the architecture", "source": {"User": "u1"}, "timestamp": "2026-03-13T10:00:00Z"},
        {"content": "Sure, the kernel uses Rust", "source": {"Agent": "a1"}, "timestamp": "2026-03-13T10:01:00Z"},
    ]
    await queue.enqueue("archive_episode", "agent-test", history)

    db = await server.get_db()
    assert await _wait_queue_drained(db), "Queue did not drain in time"

    rows = await db.execute_fetchall("SELECT summary, keywords FROM episodes WHERE agent_id = 'agent-test'")
    assert len(rows) == 1
    assert len(rows[0][0]) > 0  # summary should be non-empty

    await queue.stop()


@pytest.mark.asyncio
async def test_crash_recovery():
    """Tasks persisted before 'crash' should be processed on restart."""
    # Simulate: enqueue without starting the loop
    queue1 = server.MemoryTaskQueue()
    await queue1.enqueue(
        "update_profile",
        "agent-crash",
        [
            {"content": "I live in Tokyo", "source": {"User": "u1"}},
        ],
    )

    # Verify task is in DB
    db = await server.get_db()
    pending = await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
    assert pending[0][0] == 1

    # "Restart": new queue instance picks up the pending task
    queue2 = server.MemoryTaskQueue()
    await queue2.start()

    assert await _wait_queue_drained(db), "Queue did not drain in time"

    rows = await db.execute_fetchall("SELECT content FROM profiles WHERE agent_id = 'agent-crash'")
    assert len(rows) == 1
    assert "Tokyo" in rows[0][0]

    await queue2.stop()


@pytest.mark.asyncio
async def test_fifo_ordering():
    """Tasks should be processed in FIFO order (id ASC)."""
    processed = []
    original_update = server.do_update_profile
    original_archive = server.do_archive_episode

    async def mock_update_profile(agent_id, history):
        processed.append(("update_profile", agent_id))
        return {"ok": True, "profiles_updated": 0}

    async def mock_archive_episode(agent_id, history):
        processed.append(("archive_episode", agent_id))
        return {"ok": True, "episode_id": None}

    server.do_update_profile = mock_update_profile
    server.do_archive_episode = mock_archive_episode

    try:
        queue = server.MemoryTaskQueue()
        await queue.enqueue("archive_episode", "agent-A", [])
        await queue.enqueue("update_profile", "agent-B", [])
        await queue.enqueue("archive_episode", "agent-C", [])

        await queue.start()
        await asyncio.sleep(1)
        await queue.stop()

        assert len(processed) == 3
        assert processed[0] == ("archive_episode", "agent-A")
        assert processed[1] == ("update_profile", "agent-B")
        assert processed[2] == ("archive_episode", "agent-C")
    finally:
        server.do_update_profile = original_update
        server.do_archive_episode = original_archive


# ============================================================
# Tool integration tests
# ============================================================


@pytest.mark.asyncio
async def test_tool_update_profile_queues():
    """update_profile tool should enqueue when task queue is active."""
    server._task_queue = server.MemoryTaskQueue()
    result = await server.do_update_profile_or_queue("agent-q", [{"content": "test", "source": {"User": "u"}}])
    assert result["ok"] is True
    assert result["queued"] is True
    assert "task_id" in result

    db = await server.get_db()
    rows = await db.execute_fetchall(
        "SELECT task_type, agent_id FROM pending_memory_tasks WHERE id = ?",
        (result["task_id"],),
    )
    assert len(rows) == 1
    assert rows[0][0] == "update_profile"
    assert rows[0][1] == "agent-q"


@pytest.mark.asyncio
async def test_tool_archive_episode_queues():
    """archive_episode tool should enqueue when task queue is active."""
    server._task_queue = server.MemoryTaskQueue()
    result = await server.do_archive_episode_or_queue("agent-q", [{"content": "conversation data"}])
    assert result["ok"] is True
    assert result["queued"] is True

    db = await server.get_db()
    rows = await db.execute_fetchall("SELECT task_type FROM pending_memory_tasks")
    assert len(rows) == 1
    assert rows[0][0] == "archive_episode"


@pytest.mark.asyncio
async def test_tool_sync_when_queue_disabled():
    """Tools should run synchronously when task queue is not available."""
    server._task_queue = None
    result = await server.do_update_profile_or_queue(
        "agent-sync", [{"content": "I like Python", "source": {"User": "u"}}]
    )
    # Should return sync result (not queued)
    assert result["ok"] is True
    assert "queued" not in result

    db = await server.get_db()
    pending = await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
    assert pending[0][0] == 0


@pytest.mark.asyncio
async def test_get_queue_status_tool():
    """get_queue_status tool should return correct status."""
    # With queue
    server._task_queue = server.MemoryTaskQueue()
    status = await server.do_get_queue_status()
    assert status["enabled"] is True
    assert status["pending"] == 0

    # Without queue
    server._task_queue = None
    status = await server.do_get_queue_status()
    assert status["enabled"] is False
