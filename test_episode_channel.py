"""Tests for v2.4.22 per-channel episodic loop (episodes.channel).

Covers the channel tag on archived episodes and channel-scoped episode recall:
- ``do_archive_episode`` stores the channel (defaulting to '' = unscoped)
- ``_search_episodes_fts`` filters by channel (exact match; '' = all channels)
- the recall cascade / RRF source_id suppression has a channel exception, so the
  session-start grounding path still surfaces channel-scoped episodes even when
  a per-user ``source_id`` filter is active

Like ``test_recall_source_id``, these exercise the SQL / recall layer directly:
the ``_apply_quality_gate`` policy in ``do_recall`` drops unscored small-pool
results, which would make a tiny E2E test indistinguishable from a real bug.
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_episode_channel.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import memory_handlers  # noqa: E402
from database import close_db, get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialise a fresh DB for each test.

    The teardown closes the aiosqlite connection so running this module on its
    own exits cleanly (an open connection keeps a non-daemon worker thread
    alive, which otherwise hangs interpreter shutdown).
    """
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield
    await close_db()


async def _archive(summary: str, channel: str = "", agent_id: str = "agent-a") -> None:
    await memory_handlers.do_archive_episode(
        agent_id,
        [{"timestamp": "2026-06-14T00:00:00Z"}],
        summary=summary,
        keywords="",
        resolved=False,
        channel=channel,
    )


async def _ep_search(query: str, channel: str = "", agent_id: str = "agent-a") -> list[str]:
    db = await get_db()
    rows = await memory_handlers._search_episodes_fts(db, agent_id, query, 10, channel=channel)
    return [r["content"] for r in rows]


async def _cascade(query: str, channel: str = "", source_id: str = "", agent_id: str = "agent-a") -> list[str]:
    db = await get_db()
    rows = await memory_handlers._recall_cascade(
        db, agent_id, query, 10, False, channel=channel, source_id=source_id
    )
    return [r["content"] for r in rows]


async def _rrf(query: str, channel: str = "", source_id: str = "", agent_id: str = "agent-a") -> list[str]:
    db = await get_db()
    rows = await memory_handlers._recall_rrf(
        db, agent_id, query, 10, False, channel=channel, source_id=source_id
    )
    return [r["content"] for r in rows]


# ============================================================
# Archive stores the channel
# ============================================================


@pytest.mark.asyncio
async def test_archive_episode_persists_channel():
    await _archive("raspberry pi build log", channel="discord:111")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT summary, channel FROM episodes WHERE agent_id = 'agent-a'"
    )
    assert rows == [("raspberry pi build log", "discord:111")]


@pytest.mark.asyncio
async def test_archive_episode_defaults_unscoped_channel():
    await _archive("plain episode summary")
    db = await get_db()
    rows = await db.execute_fetchall("SELECT channel FROM episodes WHERE agent_id = 'agent-a'")
    assert rows == [("",)]


# ============================================================
# Channel-scoped episode FTS search
# ============================================================


@pytest.mark.asyncio
async def test_episode_search_filters_to_channel():
    await _archive("raspberry pi build log", channel="discord:111")
    await _archive("raspberry pi other channel", channel="discord:222")
    contents = await _ep_search("raspberry", channel="discord:111")
    assert contents == ["[Episode] raspberry pi build log"]


@pytest.mark.asyncio
async def test_episode_search_empty_channel_returns_all():
    await _archive("raspberry one", channel="discord:111")
    await _archive("raspberry two", channel="discord:222")
    contents = sorted(await _ep_search("raspberry", channel=""))
    assert contents == ["[Episode] raspberry one", "[Episode] raspberry two"]


@pytest.mark.asyncio
async def test_episode_search_unscoped_episode_not_matched_by_channel_filter():
    await _archive("raspberry unscoped")  # channel = ''
    contents = await _ep_search("raspberry", channel="discord:111")
    assert contents == []


# ============================================================
# source_id suppression has a channel exception (grounding path)
# ============================================================


@pytest.mark.asyncio
async def test_cascade_includes_channel_episode_even_with_source_id():
    await _archive("raspberry grounding", channel="discord:111")
    contents = await _cascade("raspberry", channel="discord:111", source_id="discord:user")
    assert "[Episode] raspberry grounding" in contents


@pytest.mark.asyncio
async def test_cascade_skips_episodes_when_source_id_and_no_channel():
    await _archive("raspberry grounding", channel="discord:111")
    contents = await _cascade("raspberry", channel="", source_id="discord:user")
    assert "[Episode] raspberry grounding" not in contents


@pytest.mark.asyncio
async def test_rrf_includes_channel_episode_even_with_source_id():
    await _archive("raspberry grounding", channel="discord:111")
    contents = await _rrf("raspberry", channel="discord:111", source_id="discord:user")
    assert "[Episode] raspberry grounding" in contents


@pytest.mark.asyncio
async def test_rrf_skips_episodes_when_source_id_and_no_channel():
    await _archive("raspberry grounding", channel="discord:111")
    contents = await _rrf("raspberry", channel="", source_id="discord:user")
    assert "[Episode] raspberry grounding" not in contents


# ============================================================
# Public API — archive_episode accepts the channel kwarg
# ============================================================


@pytest.mark.asyncio
async def test_archive_episode_accepts_channel_kwarg():
    """do_archive_episode accepts channel without raising; '' is the default."""
    r1 = await memory_handlers.do_archive_episode("agent-a", [], summary="s1")
    r2 = await memory_handlers.do_archive_episode("agent-a", [], summary="s2", channel="discord:9")
    assert r1["ok"] is True
    assert r2["ok"] is True
