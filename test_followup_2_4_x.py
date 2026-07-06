"""Regression tests for the post-2.4.38 follow-up batch.

Three small, self-contained hardening fixes carried over from the 2.4.38 audit:

- bug-035: ``do_recall_with_context`` must skip a malformed external_context entry
  (explicit ``content: null`` or a non-string/non-dict entry) instead of aborting
  the whole call with an ``AttributeError`` from ``None.strip()``.
- bug-033: the remote ``/search`` POST must carry a bounded per-call timeout so a
  hung endpoint falls back to local search fast instead of blocking ~30s.
- migrate rowcount: ``do_migrate_channel_axis`` must report a genuine 0 migrated
  (full ``UPDATE OR IGNORE`` collision) as 0, not fall back to the recoverable
  estimate — the fallback is only for a driver that gives no rowcount at all.
"""

import json

import httpx
import pytest
import pytest_asyncio

from cpersona import database, maintenance_handlers, memory_handlers, vector
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    """A freshly-truncated DB for the DB-backed follow-up tests."""
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-035: null / malformed external_context entries must be skipped, not fatal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_with_context_skips_null_content(clean_db):
    """An entry with content=None (JSON null) must not crash recall_with_context.

    Before the fix, ``entry.get("content", "").strip()`` reached ``None.strip()``
    (the '' default only applies when the key is *absent*) and the whole call
    aborted into an opaque {error}. The one malformed entry must be dropped while
    valid conversation entries still merge.
    """
    ctx = [
        {"role": "user", "content": None, "name": "u1"},  # explicit null -> skip
        {"role": "user", "content": 12345, "name": "u2"},  # non-string -> skip
        None,  # non-dict entry -> skip
        {"role": "user", "content": "  visible conversation text  ", "name": "u3"},
    ]

    res = await memory_handlers.do_recall_with_context("agent-035", "query", external_context=ctx)

    assert "error" not in res, f"recall_with_context aborted on malformed entry: {res}"
    contents = [m["content"] for m in res["messages"]]
    assert "visible conversation text" in contents, "valid entry not merged (stripped)"
    # the malformed entries contributed nothing
    assert 12345 not in contents
    assert None not in contents


@pytest.mark.asyncio
async def test_recall_with_context_null_content_not_in_exclude(clean_db):
    """The exclude-list build must also tolerate null content (same call site)."""
    # A stored memory that a valid ctx entry would exclude, plus a null entry that
    # must not blow up the exclude-list comprehension.
    await clean_db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, metadata) "
        "VALUES (?, 'shared phrase', '{}', '2026-01-01T00:00:00Z', '{}')",
        ("agent-035b",),
    )
    await clean_db.commit()

    ctx = [{"content": None}, {"role": "user", "content": "shared phrase", "name": "u"}]
    res = await memory_handlers.do_recall_with_context("agent-035b", "shared", external_context=ctx)
    assert "error" not in res


# ---------------------------------------------------------------------------
# bug-033: the remote /search POST must not block the recall hot path for 30s.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_search_falls_back_to_local_on_timeout(clean_db, monkeypatch):
    """A hung remote endpoint (POST raises) must fall back to local, not propagate.

    Combined with the timeout-kwarg assertion in test_v2438_hardening, this locks
    that a flapping /search endpoint degrades to local search instead of raising
    or blocking the caller.
    """

    class _HungHTTP:
        async def post(self, url, json=None, **kwargs):
            # Simulate the endpoint hanging past the (now bounded) timeout.
            raise httpx.ReadTimeout("simulated hang")

    class _FakeClient:
        _http_url = "http://x/embed"
        _client = _HungHTTP()

        async def embed(self, texts):
            # Local fallback needs a query embedding; empty corpus -> [] result.
            return [[0.0] * 8 for _ in texts]

    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _FakeClient())

    # Must return (empty local corpus) rather than raise or hang.
    out = await vector._search_vector(clean_db, "agent-033", "q", 10)
    assert out == [], "remote timeout did not degrade cleanly to local search (bug-033)"


# ---------------------------------------------------------------------------
# migrate rowcount: a genuine 0 (full collision) must be reported as 0.
# ---------------------------------------------------------------------------


async def _seed_discord_row(db, agent, content, session_id, extra_channel_row=True):
    if extra_channel_row:
        await db.execute(
            "INSERT INTO memories (agent_id, channel, content, source, timestamp, metadata) "
            "VALUES (?, '123', ?, '{}', '2026-01-01T00:00:00Z', '{}')",
            (agent, content),
        )
    await db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, metadata) "
        "VALUES (?, 'discord', ?, '{}', '2026-01-01T00:00:00Z', ?)",
        (agent, content, json.dumps({"session_id": session_id})),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_migrate_full_collision_reports_zero_migrated(clean_db):
    """Full OR IGNORE collision -> migrated must be 0, not the recoverable estimate.

    The single recoverable 'discord' row recovers to channel '123' where an
    identical-content row already exists, so UPDATE OR IGNORE skips it (rowcount 0).
    The old ``rowcount or recoverable_total`` fallback wrongly reported 1 migrated.
    """
    db = clean_db
    A = "agent-rc-full"
    await _seed_discord_row(db, A, "dup", "123:u1:0", extra_channel_row=True)

    res = await maintenance_handlers.do_migrate_channel_axis(agent_id=A, dry_run=False)
    assert res["recoverable_total"] == 1, "setup: exactly one recoverable row expected"
    assert res["migrated"] == 0, "full collision over-reported migrated count (rowcount fix)"


@pytest.mark.asyncio
async def test_migrate_partial_reports_actual_count(clean_db):
    """A non-colliding recoverable row still reports an accurate positive count."""
    db = clean_db
    A = "agent-rc-part"
    # This discord row recovers to channel '999' where nothing collides -> migrates.
    await _seed_discord_row(db, A, "unique-content", "999:u1:0", extra_channel_row=False)

    res = await maintenance_handlers.do_migrate_channel_axis(agent_id=A, dry_run=False)
    assert res["recoverable_total"] == 1
    assert res["migrated"] == 1, "non-colliding row was not counted as migrated"
    rows = {
        r[0]
        for r in await db.execute_fetchall("SELECT channel FROM memories WHERE agent_id=?", (A,))
    }
    assert "999" in rows and "discord" not in rows, "row not actually migrated to recovered channel"


# ---------------------------------------------------------------------------
# bug-026: enabling FTS on a DB first created with FTS disabled must backfill
# the (freshly created) FTS index from pre-existing rows, not leave it empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fts_backfill_when_enabled_after_created_disabled(tmp_path, monkeypatch):
    """A DB stamped at the current schema with FTS off must not lose historical
    rows from the keyword index when FTS is later turned on.

    Boot 1 (FTS off): rows land with no FTS tables/triggers, version stamped 13.
    Boot 2 (FTS on): the version-gated backfill steps are all behind current=13,
    so without the first-boot backfill the just-created FTS index would stay empty
    and MATCH would return nothing for the historical row.
    """
    dbfile = str(tmp_path / "fts_toggle.db")
    monkeypatch.setattr(database, "DB_PATH", dbfile)
    saved = database._db
    database._db = None
    try:
        # --- boot 1: FTS disabled ---
        monkeypatch.setattr(database, "FTS_ENABLED", False)
        db = await database.get_db()
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp, metadata) "
            "VALUES ('a', 'raspberry sentinel phrase', '{}', '2026-01-01T00:00:00Z', '{}')"
        )
        await db.commit()
        assert (
            await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            )
        ) == [], "FTS table should not exist when created with FTS disabled"
        await db.close()
        database._db = None

        # --- boot 2: FTS enabled ---
        monkeypatch.setattr(database, "FTS_ENABLED", True)
        db2 = await database.get_db()
        hit = await db2.execute_fetchall(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'raspberry'"
        )
        assert hit, "historical row not backfilled into FTS after enabling (bug-026)"
        # Idempotency: a subsequent boot with FTS already present must NOT re-backfill
        # (the tables now exist, so fts_created_this_boot is False).
        await db2.close()
        database._db = None
        db3 = await database.get_db()
        hit3 = await db3.execute_fetchall(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'raspberry'"
        )
        assert hit3, "FTS hit lost on a normal (already-has-FTS) boot"
        await db3.close()
    finally:
        database._db = saved
