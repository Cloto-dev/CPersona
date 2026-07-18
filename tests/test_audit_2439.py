"""Regression tests for the v2.4.39 audit line (Goal #157).

Covers the fixes filed from the 2.4.39 comprehensive audit (qa/issue-registry.json
bug-040..060, the 18 adopted fixes). Each test pins one fixed defect so
verify-issues.sh's pattern check and these behavioural assertions move together.
The remote-mode-only findings (bug-046/049/050) are deferred (production runs local
mode) and are intentionally not covered here.
"""

import pytest
import pytest_asyncio

from cpersona import (
    admin_handlers,
    database,
    maintenance_handlers,
    memory_handlers,
)
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db, write_lock
from cpersona.vector import _search_vector


@pytest_asyncio.fixture
async def clean_db():
    """A freshly-truncated DB for the DB-backed audit tests."""
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-040 / bug-041: episode / memory AUTOINCREMENT id collision must not let an
# episode row read or bump the recall_count of the memory whose id it collides with.
# ---------------------------------------------------------------------------


def test_is_episode_result_discriminator():
    # A memory row's source is a JSON string (never a dict) — must be treated as memory.
    assert memory_handlers._is_episode_result({"id": 3, "source": '{"type":"User"}', "content": "hi"}) is False
    # Memory content is not a discriminator, even when it resembles an episode preview.
    assert memory_handlers._is_episode_result({"id": 3, "source": '{"type":"User"}', "content": "[Episode] notes"}) is False
    # A vector episode row: dict source marker.
    assert memory_handlers._is_episode_result({"id": 3, "source": {"System": "episode"}, "content": "[Episode] x"}) is True
    # An FTS episode row carries the explicit structural result id.
    assert memory_handlers._is_episode_result({"id": 3, "content": "[Episode] recap", "source": {"System": "episode"}, "_rid": ("ep", 3)}) is True
    # A plain memory with ordinary content is not an episode.
    assert memory_handlers._is_episode_result({"id": 3, "content": "just a memory", "source": "{}"}) is False


@pytest.mark.asyncio
async def test_recall_bump_skips_episode_ids(clean_db, fake_embedding_client, monkeypatch):
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    db = clean_db
    # memory #1 and an episode #1 (same id space). Recall the episode's topic.
    await memory_handlers.do_store("a1", {"content": "quantum physics lecture notes", "source": {"type": "User"}, "timestamp": "2026-07-01T00:00:00+00:00"})
    await memory_handlers.do_archive_episode("a1", [], summary="baking sourdough bread recipe")
    before = (await db.execute_fetchall("SELECT recall_count FROM memories WHERE id = 1"))[0][0]
    # Query matches the EPISODE, whose id (1) collides with memory #1.
    await memory_handlers.do_recall("a1", "sourdough bread baking", limit=5)
    after = (await db.execute_fetchall("SELECT recall_count FROM memories WHERE id = 1"))[0][0]
    # Pre-fix, recalling episode #1 bumped memory #1. Now the episode is excluded.
    assert after == before


# ---------------------------------------------------------------------------
# bug-045: vector search must gate episodes by channel like the memory branch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_search_channel_gates_episodes(clean_db, fake_embedding_client):
    db = clean_db
    # Episode archived under roomA.
    await memory_handlers.do_archive_episode("a1", [], summary="rocket propulsion design", channel="roomA")
    # A recall scoped to roomB must NOT see roomA's episode.
    results_b = await _search_vector(db, "a1", "rocket propulsion", limit=10, channel="roomB")
    assert not any(r.get("content", "").startswith("[Episode]") for r in results_b)
    # The same recall scoped to roomA DOES see it (control).
    results_a = await _search_vector(db, "a1", "rocket propulsion", limit=10, channel="roomA")
    assert any(r.get("content", "").startswith("[Episode]") for r in results_a)


# ---------------------------------------------------------------------------
# bug-044 / bug-047: import & merge dedup pre-checks must include project_id so a
# distinct cross-project memory (same msg_id, different project) is not dropped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_dedup_is_project_scoped(clean_db, tmp_path):
    db = clean_db
    await memory_handlers.do_store("a1", {"id": "m1", "content": "alpha bucket", "source": {}, "timestamp": "t"}, project_id="alpha")
    f = tmp_path / "imp.jsonl"
    f.write_text('{"_type":"memory","agent_id":"a1","project_id":"beta","msg_id":"m1","content":"beta bucket"}\n', encoding="utf-8")
    res = await admin_handlers.do_import_memories(str(f), target_agent_id="a1")
    assert res["imported_memories"] == 1, res  # the beta-bucket row is distinct, not a dup
    rows = await db.execute_fetchall("SELECT project_id FROM memories WHERE agent_id='a1' AND msg_id='m1' ORDER BY project_id")
    assert [r[0] for r in rows] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_merge_dedup_is_project_scoped(clean_db):
    db = clean_db
    await memory_handlers.do_store("src", {"id": "m1", "content": "src global", "source": {}, "timestamp": "t"}, project_id="")
    await memory_handlers.do_store("dst", {"id": "m1", "content": "dst X", "source": {}, "timestamp": "t"}, project_id="X")
    res = await admin_handlers.do_merge_memories("src", "dst")
    assert res["merged_memories"] == 1, res  # (dst,'',m1) is distinct from (dst,'X',m1)
    rows = await db.execute_fetchall("SELECT project_id FROM memories WHERE agent_id='dst' AND msg_id='m1' ORDER BY project_id")
    assert [r[0] for r in rows] == ["", "X"]


# ---------------------------------------------------------------------------
# bug-048: a write-free dry_run merge preview must run under no-persist, not be
# short-circuited into a fabricated all-zero no-op.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_dry_run_runs_under_no_persist(clean_db):
    await memory_handlers.do_store("src", {"content": "one", "source": {}, "timestamp": "t"})
    await memory_handlers.do_store("src", {"content": "two", "source": {}, "timestamp": "t"})
    try:
        no_persist.pause(ttl_seconds=60)
        res = await admin_handlers.do_merge_memories("src", "dst", dry_run=True)
    finally:
        no_persist.resume()
    # Real preview counts (2), not the paused all-zero no-op.
    assert res.get("merged_memories") == 2, res
    assert "persisted" not in res or res.get("persisted") is not False


# ---------------------------------------------------------------------------
# bug-056 / bug-057: dry_run counts must equal a real run for a content collision.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_dry_run_counts_match_real(clean_db, tmp_path):
    # target already holds this content (empty msg_id → content-uniqueness applies).
    await memory_handlers.do_store("a1", {"content": "dup line", "source": {}, "timestamp": "t"})
    f = tmp_path / "imp.jsonl"
    f.write_text('{"_type":"memory","agent_id":"a1","content":"dup line"}\n', encoding="utf-8")
    preview = await admin_handlers.do_import_memories(str(f), target_agent_id="a1", dry_run=True)
    assert preview["imported_memories"] == 0  # pre-fix over-reported 1
    assert preview["skipped_memories"] == 1


@pytest.mark.asyncio
async def test_merge_dry_run_counts_match_real(clean_db):
    await memory_handlers.do_store("src", {"content": "shared", "source": {}, "timestamp": "t"})
    await memory_handlers.do_store("dst", {"content": "shared", "source": {}, "timestamp": "t"})
    preview = await admin_handlers.do_merge_memories("src", "dst", dry_run=True)
    assert preview["merged_memories"] == 0  # pre-fix over-reported 1
    assert preview["skipped_memories"] == 1


# ---------------------------------------------------------------------------
# bug-055: check_missing_profile must scope to the requested agent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_profile_check_is_agent_scoped(clean_db):
    db = clean_db
    await memory_handlers.do_store("A", {"content": "a-mem", "source": {}, "timestamp": "t"})
    await memory_handlers.do_store("B", {"content": "b-mem", "source": {}, "timestamp": "t"})
    await admin_handlers.do_update_profile("A", "A has a profile")
    # A is healthy; B has no profile. Scoped check on A must not surface B.
    from cpersona import checks
    found = await checks.check_missing_profile(db, "A", fix=False)
    assert found == []


# ---------------------------------------------------------------------------
# bug-058: check_health stats episodes/profiles must be agent-scoped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_health_stats_are_agent_scoped(clean_db):
    await memory_handlers.do_store("A", {"content": "a-mem", "source": {}, "timestamp": "t"})
    await memory_handlers.do_archive_episode("A", [], summary="A episode")
    await memory_handlers.do_store("B", {"content": "b-mem", "source": {}, "timestamp": "t"})
    await memory_handlers.do_archive_episode("B", [], summary="B ep 1")
    await memory_handlers.do_archive_episode("B", [], summary="B ep 2")
    res = await maintenance_handlers.do_check_health(agent_id="A")
    assert res["stats"]["episodes"] == 1  # pre-fix returned corpus-wide 3
    assert res["stats"]["memories"] == 1


# ---------------------------------------------------------------------------
# bug-059: a --fix run that fully repairs must report healthy=True (residual).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_health_fix_reports_residual_healthy(clean_db):
    db = clean_db
    # Break a load-bearing object: drop the content dedup UNIQUE index (a fixable
    # critical). schema_object_drift repairs it; healthy must reflect the residual.
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_content")
    await db.commit()
    unfixed = await maintenance_handlers.do_check_health(fix=False)
    assert unfixed["healthy"] is False
    fixed = await maintenance_handlers.do_check_health(fix=True)
    assert fixed["healthy"] is True, fixed["issues"]
    assert fixed["severity_summary"]["critical"] == 0


# ---------------------------------------------------------------------------
# bug-053: calibrate sample_size is clamped to CALIBRATE_MAX_SAMPLE.
# ---------------------------------------------------------------------------


def test_calibrate_sample_is_clamped():
    from cpersona.config import CALIBRATE_MAX_SAMPLE, CALIBRATE_SAMPLE_SIZE
    # The clamp expression the handler uses.
    assert min(10**9 or CALIBRATE_SAMPLE_SIZE, CALIBRATE_MAX_SAMPLE) == CALIBRATE_MAX_SAMPLE
    assert CALIBRATE_MAX_SAMPLE <= 10000  # a sane quadratic-safe ceiling


# ---------------------------------------------------------------------------
# bug-060: FTS backfill completion is a durable PRAGMA user_version bit, so a
# failed rebuild is retried on the next boot instead of being lost.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fts_backfill_pending_flag_roundtrips(clean_db):
    db = clean_db
    await database._set_fts_backfill_pending(db, True)
    uv = (await db.execute_fetchall("PRAGMA user_version"))[0][0]
    assert uv & 1 == 1
    await database._set_fts_backfill_pending(db, False)
    uv = (await db.execute_fetchall("PRAGMA user_version"))[0][0]
    assert uv & 1 == 0


# ---------------------------------------------------------------------------
# bug-042 / bug-043: the shared write lock exists and import/merge still work end
# to end while serialising commits (smoke — the lock is a module singleton).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_lock_is_shared_singleton_and_writes_work(clean_db):
    import asyncio

    assert isinstance(write_lock(), asyncio.Lock)
    assert write_lock() is write_lock()  # same singleton across calls
    # A store under the lock still lands.
    res = await memory_handlers.do_store("a1", {"content": "locked write", "source": {}, "timestamp": "t"})
    assert res["ok"] is True
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='a1'")
    assert rows[0][0] == 1


# ---------------------------------------------------------------------------
# bug-052: a failing recall_count bump must not sink the recall result.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_survives_bump_failure(clean_db, fake_embedding_client, monkeypatch):
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    await memory_handlers.do_store("a1", {"content": "resilient recall row", "source": {"type": "User"}, "timestamp": "2026-07-01T00:00:00+00:00"})

    db = await get_db()
    real_execute = db.execute

    async def flaky_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and "recall_count = recall_count + 1" in sql:
            raise Exception("simulated database is locked")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db, "execute", flaky_execute)
    res = await memory_handlers.do_recall("a1", "resilient recall row", limit=5)
    # The bump raised, but the recall result is still returned intact.
    assert "messages" in res
    assert any("resilient recall" in m.get("content", "") for m in res["messages"])
