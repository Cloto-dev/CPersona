"""Tests for agent_id × project_id γ-isolation read/write paths (Phase 3-β-3b).

Covers the read/write half of upstream cloto-mcp-servers/servers/cpersona
v2.4.17 (e8a1b44) — write paths tag project_id, read paths apply the γ
filter via mcp_common.isolation. γ semantics:
  - write: omitted → '' (global pool)
  - read:  None = no filter, '' = global pool only, 'X' = 'X' ∪ global pool
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_isolation.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import admin_handlers  # noqa: E402
import memory_handlers  # noqa: E402
from database import get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh DB for each test."""
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


def _msg(content: str, msg_id: str = "") -> dict:
    return {"id": msg_id, "content": content, "source": {"User": "u"}}


# ============================================================
# Write path — do_store tags project_id
# ============================================================


@pytest.mark.asyncio
async def test_store_defaults_to_global_pool():
    """A store with no project_id lands in the global pool (project_id = '')."""
    db = await get_db()
    await memory_handlers.do_store("agent-w", _msg("global memory"))
    rows = await db.execute_fetchall(
        "SELECT content, project_id FROM memories WHERE agent_id = 'agent-w'"
    )
    assert rows == [("global memory", "")]


@pytest.mark.asyncio
async def test_store_writes_explicit_project_id():
    """An explicit project_id is persisted on the row."""
    db = await get_db()
    await memory_handlers.do_store("agent-w", _msg("tagged memory"), project_id="proj-a")
    rows = await db.execute_fetchall(
        "SELECT content, project_id FROM memories WHERE agent_id = 'agent-w'"
    )
    assert rows == [("tagged memory", "proj-a")]


@pytest.mark.asyncio
async def test_store_dedup_is_project_scoped():
    """The same msg_id under different projects coexists; same project dedups."""
    db = await get_db()
    r1 = await memory_handlers.do_store("agent-w", _msg("hello", msg_id="m1"), project_id="proj-a")
    r2 = await memory_handlers.do_store("agent-w", _msg("hello", msg_id="m1"), project_id="proj-b")
    r3 = await memory_handlers.do_store("agent-w", _msg("hello", msg_id="m1"), project_id="proj-a")
    assert r1.get("ok") is True and not r1.get("skipped")
    assert r2.get("ok") is True and not r2.get("skipped")  # different project → not a dup
    assert r3.get("skipped") is True and r3.get("reason") == "duplicate msg_id"
    rows = await db.execute_fetchall(
        "SELECT project_id FROM memories WHERE agent_id = 'agent-w' AND msg_id = 'm1' ORDER BY project_id"
    )
    assert [r[0] for r in rows] == ["proj-a", "proj-b"]


@pytest.mark.asyncio
async def test_archive_episode_writes_project_id():
    """do_archive_episode persists project_id on the episode row."""
    db = await get_db()
    await memory_handlers.do_archive_episode(
        "agent-w", history=[], summary="tagged episode", project_id="proj-a"
    )
    rows = await db.execute_fetchall(
        "SELECT summary, project_id FROM episodes WHERE agent_id = 'agent-w'"
    )
    assert rows == [("tagged episode", "proj-a")]


# ============================================================
# Read path — γ filter on do_list_memories / do_list_episodes
# ============================================================


async def _seed_three_buckets():
    """Store one memory + one episode each in global / proj-a / proj-b."""
    for pid, label in [("", "global"), ("proj-a", "a"), ("proj-b", "b")]:
        await memory_handlers.do_store("agent-r", _msg(f"memory-{label}"), project_id=pid)
        await memory_handlers.do_archive_episode(
            "agent-r", history=[], summary=f"episode-{label}", project_id=pid
        )


@pytest.mark.asyncio
async def test_list_memories_gamma_semantics():
    """None = no filter, '' = global only, 'X' = bucket X ∪ global."""
    await _seed_three_buckets()

    no_filter = await admin_handlers.do_list_memories("agent-r", 100, project_id=None)
    assert {m["content"] for m in no_filter["memories"]} == {"memory-global", "memory-a", "memory-b"}

    global_only = await admin_handlers.do_list_memories("agent-r", 100, project_id="")
    assert {m["content"] for m in global_only["memories"]} == {"memory-global"}

    bucket_a = await admin_handlers.do_list_memories("agent-r", 100, project_id="proj-a")
    assert {m["content"] for m in bucket_a["memories"]} == {"memory-global", "memory-a"}

    # response rows surface the bucket
    assert all("project_id" in m for m in no_filter["memories"])


@pytest.mark.asyncio
async def test_list_episodes_gamma_semantics():
    """do_list_episodes honours the same γ semantics as do_list_memories."""
    await _seed_three_buckets()

    no_filter = await admin_handlers.do_list_episodes("agent-r", 100, project_id=None)
    assert {e["summary"] for e in no_filter["episodes"]} == {"episode-global", "episode-a", "episode-b"}

    global_only = await admin_handlers.do_list_episodes("agent-r", 100, project_id="")
    assert {e["summary"] for e in global_only["episodes"]} == {"episode-global"}

    bucket_b = await admin_handlers.do_list_episodes("agent-r", 100, project_id="proj-b")
    assert {e["summary"] for e in bucket_b["episodes"]} == {"episode-global", "episode-b"}
    assert all("project_id" in e for e in no_filter["episodes"])


# ============================================================
# Read path — γ filter through do_recall (keyword path)
# ============================================================


@pytest.mark.asyncio
async def test_recall_keyword_path_gamma_filter():
    """do_recall applies the γ filter. deep=True bypasses the adaptive quality
    gate so the isolation behaviour is visible on a tiny corpus."""
    await memory_handlers.do_store("agent-r", _msg("raspberry pi notes"), project_id="")
    await memory_handlers.do_store("agent-r", _msg("raspberry pi config"), project_id="proj-a")
    await memory_handlers.do_store("agent-r", _msg("raspberry pi tuning"), project_id="proj-b")

    # global pool only — proj-a / proj-b rows excluded
    global_only = await memory_handlers.do_recall("agent-r", "raspberry", 10, deep=True, project_id="")
    contents = {m["content"] for m in global_only["messages"]}
    assert "raspberry pi notes" in contents
    assert "raspberry pi config" not in contents
    assert "raspberry pi tuning" not in contents

    # bucket proj-a → proj-a ∪ global, proj-b excluded
    bucket_a = await memory_handlers.do_recall("agent-r", "raspberry", 10, deep=True, project_id="proj-a")
    contents_a = {m["content"] for m in bucket_a["messages"]}
    assert "raspberry pi notes" in contents_a
    assert "raspberry pi config" in contents_a
    assert "raspberry pi tuning" not in contents_a

    # None → no filter, all three buckets visible
    no_filter = await memory_handlers.do_recall("agent-r", "raspberry", 10, deep=True, project_id=None)
    contents_all = {m["content"] for m in no_filter["messages"]}
    assert contents_all == {"raspberry pi notes", "raspberry pi config", "raspberry pi tuning"}
