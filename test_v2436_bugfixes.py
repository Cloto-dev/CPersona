"""Regression tests for the v2.4.36 patch batch (bug-009 .. bug-011).

Each test pins the corrected behaviour of one audit finding so the fix cannot
silently regress:

- bug-009: check_health no longer treats channel='' as corruption; '' is the
           global channel (recall γ semantics / migrate_channel_axis target)
           and must survive check_health(fix=true) untouched.
- bug-010: do_store binds the remote vector entry to cursor.lastrowid (not a
           re-SELECTed max id) and dedup is enforced by UNIQUE indexes +
           INSERT OR IGNORE, closing the SELECT-probe TOCTOU.
- bug-011: do_update_memory recomputes (or NULLs) the embedding for the new
           text instead of leaving the old vector attached to it.
"""

import inspect
import os
import tempfile

import pytest
import pytest_asyncio

# Hermetic DB + embeddings-off before importing any cpersona module.
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_v2436.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import admin_handlers  # noqa: E402
from cpersona import maintenance_handlers  # noqa: E402
from cpersona import memory_handlers  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona._vendored_mcp_common import no_persist  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402
from cpersona.database import get_db  # noqa: E402


def _msg(content: str, msg_id: str = "") -> dict:
    return {"id": msg_id, "content": content, "source": {"User": "u"}}


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    no_persist.resume()
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    saved_client = vector._embedding_client
    yield
    vector._embedding_client = saved_client
    no_persist.resume()


# --------------------------------------------------------------------------
# bug-009 — channel='' is the global channel, not corruption
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_health_leaves_global_channel_untouched():
    """check_health(fix=true) must not rewrite channel='' rows to 'chat'."""
    await memory_handlers.do_store("agent-g", _msg("a globalized rule"), channel="")

    result = await maintenance_handlers.do_check_health(agent_id="agent-g", fix=True)

    db = await get_db()
    ch = (await db.execute_fetchall("SELECT channel FROM memories WHERE agent_id='agent-g'"))[0][0]
    assert ch == ""  # still global — the old fix rewrote this to 'chat'
    assert not any(i.get("type") == "empty_channel" for i in result.get("issues", []))


# --------------------------------------------------------------------------
# bug-010 — lastrowid binding + UNIQUE-index dedup
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v12_unique_dedup_indexes_exist():
    db = await get_db()
    names = {
        r[0]
        for r in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_memories_dedup%'"
        )
    }
    assert names == {"idx_memories_dedup_content", "idx_memories_dedup_msg_id"}


@pytest.mark.asyncio
async def test_unique_index_absorbs_raw_duplicate_insert():
    """The TOCTOU closer: a raw INSERT OR IGNORE of an identical row is a no-op."""
    await memory_handlers.do_store("agent-u", _msg("only once"), channel="c1")
    db = await get_db()
    cur = await db.execute(
        "INSERT OR IGNORE INTO memories (agent_id, content, channel, source, timestamp) "
        "VALUES ('agent-u', 'only once', 'c1', '{}', '2026-01-01T00:00:00Z')"
    )
    await db.commit()
    assert cur.rowcount == 0
    count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='agent-u'"))[0][0]
    assert count == 1


@pytest.mark.asyncio
async def test_msg_id_unique_is_partial():
    """Empty msg_id must stay non-unique (most rows carry no msg_id)."""
    await memory_handlers.do_store("agent-u", _msg("first row"))
    await memory_handlers.do_store("agent-u", _msg("second row"))
    db = await get_db()
    count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='agent-u'"))[0][0]
    assert count == 2  # two rows with msg_id='' coexist
    # ...while a real msg_id is deduped
    await memory_handlers.do_store("agent-u", _msg("third row", msg_id="m-1"))
    dup = await memory_handlers.do_store("agent-u", _msg("changed wording", msg_id="m-1"))
    assert dup.get("skipped") is True


def test_do_store_uses_lastrowid_not_reselect():
    """The remote-index id must come from the INSERT cursor, not a re-SELECT.

    Source-level pin (same style as the v2.4.35 middleware test): the racy
    `ORDER BY id DESC` lookup must be gone and lastrowid in its place.
    """
    src = inspect.getsource(memory_handlers.do_store)
    assert "ORDER BY id DESC" not in src
    assert "lastrowid" in src
    assert "INSERT OR IGNORE INTO memories" in src


# --------------------------------------------------------------------------
# bug-011 — do_update_memory re-embeds (or NULLs) the vector
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_memory_nulls_stale_embedding_without_client():
    """With no embedding client the old vector must be dropped, not kept."""
    await memory_handlers.do_store("agent-e", _msg("original text"))
    db = await get_db()
    mem_id = (await db.execute_fetchall("SELECT id FROM memories WHERE agent_id='agent-e'"))[0][0]
    # Simulate a row that was embedded when a client was still configured.
    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (b"\x00\x01\x02\x03", mem_id))
    await db.commit()

    vector._embedding_client = None
    result = await admin_handlers.do_update_memory(mem_id, "rewritten text", agent_id="agent-e")
    assert result.get("ok") is True

    blob = (await db.execute_fetchall("SELECT embedding FROM memories WHERE id = ?", (mem_id,)))[0][0]
    assert blob is None  # stale vector gone; check_health can re-embed later


@pytest.mark.asyncio
async def test_update_memory_reembeds_with_client():
    """With a client available the new text is embedded in the same call."""
    await memory_handlers.do_store("agent-e", _msg("original text"))
    db = await get_db()
    mem_id = (await db.execute_fetchall("SELECT id FROM memories WHERE agent_id='agent-e'"))[0][0]

    class _StubClient:
        _http_url = None

        async def embed(self, texts):
            return [[0.25, -0.5, 1.0, 0.125] for _ in texts]

    vector._embedding_client = _StubClient()
    result = await admin_handlers.do_update_memory(mem_id, "rewritten text", agent_id="agent-e")
    assert result.get("ok") is True

    blob = (await db.execute_fetchall("SELECT embedding FROM memories WHERE id = ?", (mem_id,)))[0][0]
    assert blob == EmbeddingClient.pack_embedding([0.25, -0.5, 1.0, 0.125])
