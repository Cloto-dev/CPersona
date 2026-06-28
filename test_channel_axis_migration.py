"""Tests for the knob2 v2 channel-axis migration + ''=global recall.

Two changes land together (Goal #120, default-flip preparation):

- ``do_migrate_channel_axis`` re-channels bridge-type memories (channel='discord')
  to the concrete channel recovered from ``metadata.session_id``
  ('{channel_id}:{user_id}:{chunk}' | '{channel_id}:shared' → channel_id).
  Non-destructive (only the channel column moves), idempotent, dry-run by default.
- recall paths treat a stored channel of '' as **global**: it matches every
  channel-scoped recall, so old/global memories are never orphaned once recall
  filters by the concrete channel.

Like ``test_episode_channel``, these exercise the SQL / recall layer directly
(EMBEDDING_MODE=none, so the vector path is inert and the keyword/FTS paths carry
the assertions).
"""

import json
import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_channel_axis_migration.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import admin_handlers # noqa: E402
from cpersona import maintenance_handlers # noqa: E402
from cpersona import memory_handlers # noqa: E402
from cpersona.database import close_db, get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield
    await close_db()


async def _insert(content, channel, session_id=None, agent_id="agent-a"):
    db = await get_db()
    metadata = json.dumps({"session_id": session_id} if session_id is not None else {})
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, metadata, channel) "
        "VALUES (?, ?, '{}', '2026-06-14T00:00:00Z', ?, ?)",
        (agent_id, content, metadata, channel),
    )
    await db.commit()


async def _channel_of(content, agent_id="agent-a"):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT channel FROM memories WHERE agent_id = ? AND content = ?",
        (agent_id, content),
    )
    return rows[0][0] if rows else None


async def _kw(query, channel, agent_id="agent-a"):
    db = await get_db()
    rows = await memory_handlers._search_memories_keyword(db, agent_id, query, 10, channel=channel)
    return [r["content"] for r in rows]


# --------------------------------------------------------------------------- #
# migrate_channel_axis
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dry_run_reports_buckets_without_mutating():
    await _insert("user-thread msg", "discord", session_id="111:222:0")
    await _insert("thread shared msg", "discord", session_id="111:shared")
    await _insert("other channel msg", "discord", session_id="333:444:1")
    await _insert("no session id", "discord", session_id=None)
    await _insert("non snowflake", "discord", session_id="chat:abc")

    out = await maintenance_handlers.do_migrate_channel_axis(agent_id="agent-a")  # dry_run defaults True

    assert out["dry_run"] is True
    # 111 (×2) + 333 (×1) are recoverable
    assert out["recoverable_total"] == 3
    by_channel = {r["channel"]: r["count"] for r in out["recoverable_by_channel"]}
    assert by_channel == {"111": 2, "333": 1}
    # the null-session and the non-snowflake row are unrecoverable
    assert out["unrecoverable_total"] == 2
    assert out["migrated"] == 0
    # nothing moved
    assert await _channel_of("user-thread msg") == "discord"
    assert await _channel_of("no session id") == "discord"


@pytest.mark.asyncio
async def test_apply_rechannels_and_is_idempotent():
    await _insert("user-thread msg", "discord", session_id="111:222:0")
    await _insert("thread shared msg", "discord", session_id="111:shared")
    await _insert("other channel msg", "discord", session_id="333:444:1")

    out = await maintenance_handlers.do_migrate_channel_axis(agent_id="agent-a", dry_run=False)
    assert out["dry_run"] is False
    assert out["migrated"] == 3

    assert await _channel_of("user-thread msg") == "111"
    assert await _channel_of("thread shared msg") == "111"
    assert await _channel_of("other channel msg") == "333"

    # content / metadata untouched (non-destructive)
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT metadata FROM memories WHERE content = 'user-thread msg'"
    )
    assert json.loads(rows[0][0])["session_id"] == "111:222:0"

    # second run is a no-op (nothing left under 'discord')
    again = await maintenance_handlers.do_migrate_channel_axis(agent_id="agent-a", dry_run=False)
    assert again["recoverable_total"] == 0
    assert again["migrated"] == 0


@pytest.mark.asyncio
async def test_globalize_unrecoverable_moves_remnant_to_global():
    await _insert("recoverable msg", "discord", session_id="111:222:0")
    await _insert("no session id", "discord", session_id=None)
    await _insert("non snowflake", "discord", session_id="chat:abc")

    out = await maintenance_handlers.do_migrate_channel_axis(
        agent_id="agent-a", dry_run=False, globalize_unrecoverable=True
    )
    assert out["migrated"] == 1
    assert out["globalized"] == 2

    assert await _channel_of("recoverable msg") == "111"
    assert await _channel_of("no session id") == ""
    assert await _channel_of("non snowflake") == ""


@pytest.mark.asyncio
async def test_migration_is_agent_scoped():
    await _insert("agent a msg", "discord", session_id="111:222:0", agent_id="agent-a")
    await _insert("agent b msg", "discord", session_id="999:888:0", agent_id="agent-b")

    await maintenance_handlers.do_migrate_channel_axis(agent_id="agent-a", dry_run=False)

    assert await _channel_of("agent a msg", agent_id="agent-a") == "111"
    # agent-b untouched
    assert await _channel_of("agent b msg", agent_id="agent-b") == "discord"


# --------------------------------------------------------------------------- #
# ''=global recall
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_global_memory_matches_every_channel_scoped_recall():
    await _insert("globalfact about pastry", "")
    await _insert("channelfact about pastry", "111")

    # recall scoped to channel 111 returns BOTH (global '' matches)
    got = await _kw("pastry", channel="111")
    assert "globalfact about pastry" in got
    assert "channelfact about pastry" in got

    # recall scoped to an unrelated channel returns ONLY the global one
    got_other = await _kw("pastry", channel="222")
    assert got_other == ["globalfact about pastry"]


@pytest.mark.asyncio
async def test_channel_scoped_memory_does_not_leak_to_other_channels():
    await _insert("alpha secret", "111")
    await _insert("beta secret", "222")

    assert await _kw("secret", channel="111") == ["alpha secret"]
    assert await _kw("secret", channel="222") == ["beta secret"]


# --------------------------------------------------------------------------- #
# list_memories exposes channel (for kernel per-channel episode grouping)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_memories_returns_channel():
    await _insert("a memory in 111", "111")
    await _insert("a global memory", "")

    out = await admin_handlers.do_list_memories("agent-a", 10)
    by_content = {m["content"]: m["channel"] for m in out["memories"]}
    assert by_content["a memory in 111"] == "111"
    assert by_content["a global memory"] == ""
