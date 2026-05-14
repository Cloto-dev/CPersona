"""Tests for session no-persist mode (Phase 3-β-4).

Covers upstream cloto-mcp-servers/servers/cpersona patch v2.4.19 (23cea89):
the three control tools, write-tool no-op guards, read tools staying live,
the or_queue wrapper not enqueueing under pause, and the check_health /
deep_check fix downgrade. Uses mcp_common.no_persist (module-level state),
so the fixture force-resumes before and after every test.
"""

import os
import tempfile

import pytest
import pytest_asyncio
from mcp_common import no_persist

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_no_persist.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import admin_handlers  # noqa: E402
import maintenance_handlers  # noqa: E402
import memory_handlers  # noqa: E402
import server  # noqa: E402
import tasks  # noqa: E402
from database import get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Fresh DB + guaranteed-resumed no-persist state for each test."""
    no_persist.resume()
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.execute("DELETE FROM profiles")
    await db.execute("DELETE FROM pending_memory_tasks")
    await db.commit()
    tasks._task_queue = None
    yield
    no_persist.resume()


def _msg(content: str, msg_id: str = "") -> dict:
    return {"id": msg_id, "content": content, "source": {"User": "u"}}


def _is_skipped(resp: dict) -> bool:
    """A no-op write response always carries persisted=False / dry_run=True.

    (`id` is only rewritten to the 'no-persist' sentinel when the default body
    already had an `id` key — bodies without one stay id-less.)
    """
    return resp.get("persisted") is False and resp.get("dry_run") is True


# ============================================================
# Control tools — pause / resume / status
# ============================================================


@pytest.mark.asyncio
async def test_pause_resume_status_round_trip():
    status = await server.do_persistence_status()
    assert status["paused"] is False

    paused = await server.do_pause_persistence(ttl_seconds=120)
    assert paused["paused"] is True
    assert no_persist.is_paused() is True

    status = await server.do_persistence_status()
    assert status["paused"] is True
    assert status["ttl_remaining_seconds"] is not None and status["ttl_remaining_seconds"] > 0

    resumed = await server.do_resume_persistence()
    assert resumed["paused"] is False
    assert resumed["was_active"] is True
    assert no_persist.is_paused() is False


@pytest.mark.asyncio
async def test_pause_clamps_ttl_to_max():
    paused = await server.do_pause_persistence(ttl_seconds=10_000_000)
    assert paused["ttl_seconds"] == no_persist.MAX_TTL_SECONDS


# ============================================================
# Write tools — no-op under pause, no DB rows
# ============================================================


@pytest.mark.asyncio
async def test_store_skipped_under_pause():
    no_persist.pause(ttl_seconds=120)
    resp = await memory_handlers.do_store("agent-np", _msg("should not persist"))
    assert _is_skipped(resp)

    no_persist.resume()
    db = await get_db()
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM memories")
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_archive_episode_skipped_under_pause():
    no_persist.pause(ttl_seconds=120)
    resp = await memory_handlers.do_archive_episode("agent-np", history=[], summary="ephemeral episode")
    assert _is_skipped(resp)

    no_persist.resume()
    db = await get_db()
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM episodes")
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_mutation_tools_skipped_under_pause():
    """update_profile / delete / update / lock / unlock all short-circuit."""
    # Seed a real memory + profile while persistence is live.
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
        ("agent-np", "original", "2026-05-14T00:00:00Z"),
    )
    await db.commit()
    mem_id = cur.lastrowid

    no_persist.pause(ttl_seconds=120)
    assert _is_skipped(await admin_handlers.do_update_profile("agent-np", "new profile"))
    assert _is_skipped(await admin_handlers.do_update_memory(mem_id, "edited"))
    assert _is_skipped(await admin_handlers.do_lock_memory(mem_id))
    assert _is_skipped(await admin_handlers.do_unlock_memory(mem_id))
    assert _is_skipped(await admin_handlers.do_delete_memory(mem_id))

    no_persist.resume()
    # The memory is untouched: still present, still unlocked, content unchanged.
    rows = await db.execute_fetchall("SELECT content, locked FROM memories WHERE id = ?", (mem_id,))
    assert rows == [("original", 0)]
    prof = await db.execute_fetchall("SELECT COUNT(*) FROM profiles WHERE agent_id = 'agent-np'")
    assert prof[0][0] == 0


@pytest.mark.asyncio
async def test_archive_episode_or_queue_does_not_enqueue_under_pause():
    """The or_queue wrapper must not leak a row into pending_memory_tasks."""
    tasks._task_queue = tasks.MemoryTaskQueue()
    no_persist.pause(ttl_seconds=120)
    resp = await server.do_archive_episode_or_queue("agent-np", [{"content": "data"}])
    assert _is_skipped(resp)
    assert resp.get("queued") is False

    no_persist.resume()
    db = await get_db()
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
    assert rows[0][0] == 0


# ============================================================
# Read tools — unaffected by pause
# ============================================================


@pytest.mark.asyncio
async def test_read_tools_work_under_pause():
    # Seed data while live.
    await memory_handlers.do_store("agent-np", _msg("recallable memory"))

    no_persist.pause(ttl_seconds=120)
    recall = await memory_handlers.do_recall("agent-np", "recallable", 10, deep=True)
    assert any("recallable memory" in m["content"] for m in recall["messages"])

    listed = await admin_handlers.do_list_memories("agent-np", 100)
    assert listed["count"] == 1

    status = await server.do_persistence_status()
    assert status["paused"] is True  # status itself is a read, still works


# ============================================================
# check_health / deep_check — fix downgrade under pause
# ============================================================


@pytest.mark.asyncio
async def test_check_health_downgrades_fix_under_pause():
    no_persist.pause(ttl_seconds=120)
    result = await maintenance_handlers.do_check_health("agent-np", fix=True)
    assert result["fixed"] is False
    assert result["repairs_skipped"] is True
    assert "no-persist" in result["repairs_skip_reason"]


@pytest.mark.asyncio
async def test_deep_check_downgrades_fix_under_pause():
    no_persist.pause(ttl_seconds=120)
    result = await maintenance_handlers.do_deep_check("agent-np", fix=True)
    assert result["fixed"] is False
    assert result["repairs_skipped"] is True


@pytest.mark.asyncio
async def test_check_health_fix_normal_when_not_paused():
    """Sanity: with persistence live, fix is honoured and no skip flag appears."""
    result = await maintenance_handlers.do_check_health("agent-np", fix=True)
    assert result["fixed"] is True
    assert "repairs_skipped" not in result
