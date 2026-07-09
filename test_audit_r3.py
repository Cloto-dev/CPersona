"""Regression tests for the 3rd comprehensive audit round (Goal #157, bug-076..084).

Covers the 9 fixes adopted from the round-3 audit of the frozen 2.4.39 baseline
(commit 125b33b): 5 new defects (bug-076..080) and 4 residuals of earlier 2.4.39
fixes (bug-081..084). Each test pins one fixed defect so verify-issues.sh's
pattern check and these behavioural assertions move together.

Not covered here by design:
- bug-082 (proxy 3xx guard) — the stdio proxy is a thin IO shim with no behavioural
  suite (same precedent as bug-051/063); the registry fix-marker pins the guard.
- bug-083 (no embedding I/O under the write lock) — enforced as a CLASS gate in
  test_structural_gates.py::test_check_health_never_embeds_under_write_lock.
"""

import pytest
import pytest_asyncio

from conftest import FakeEmbeddingClient, fake_embed_one
from cpersona import (
    admin_handlers,
    checks,
    maintenance_handlers,
    memory_handlers,
)
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db
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
# bug-076: the merge episode dedup probe must include project_id/channel so a
# distinct cross-project episode (same summary text) is not dropped — and, in
# move mode, not permanently deleted with the source agent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_episode_dedup_is_project_scoped(clean_db):
    db = clean_db
    # Target already holds the same summary text under a DIFFERENT project bucket.
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, summary) VALUES ('B', 'proj-y', 'Weekly sync notes')"
    )
    # Source's episode is a legitimately distinct γ-bucketed record.
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, summary) VALUES ('A', 'proj-x', 'Weekly sync notes')"
    )
    await db.commit()
    res = await admin_handlers.do_merge_memories("A", "B", mode="move")
    assert res.get("merged_episodes") == 1, res
    rows = await db.execute_fetchall(
        "SELECT project_id FROM episodes WHERE agent_id = 'B' ORDER BY project_id"
    )
    # Pre-fix the proj-x episode was skipped and then deleted with agent A (data loss).
    assert [r[0] for r in rows] == ["proj-x", "proj-y"]
    gone = await db.execute_fetchall("SELECT COUNT(*) FROM episodes WHERE agent_id = 'A'")
    assert gone[0][0] == 0  # move mode still wipes the (now fully merged) source


@pytest.mark.asyncio
async def test_merge_episode_same_bucket_duplicate_still_skipped(clean_db):
    db = clean_db
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, summary) VALUES ('B', 'proj-y', 'Weekly sync notes')"
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, summary) VALUES ('A', 'proj-y', 'Weekly sync notes')"
    )
    await db.commit()
    res = await admin_handlers.do_merge_memories("A", "B")
    assert res.get("skipped_episodes") == 1, res
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM episodes WHERE agent_id = 'B'")
    assert rows[0][0] == 1  # true same-bucket duplicate is still deduped


@pytest.mark.asyncio
async def test_merge_episode_dry_run_counts_match_real(clean_db):
    db = clean_db
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, summary) VALUES ('B', 'proj-y', 'Weekly sync notes')"
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, summary) VALUES ('A', 'proj-x', 'Weekly sync notes')"
    )
    await db.commit()
    preview = await admin_handlers.do_merge_memories("A", "B", dry_run=True)
    real = await admin_handlers.do_merge_memories("A", "B")
    # The bug-071 seen_summary preview key carries the same γ axes as the real probe.
    assert preview.get("merged_episodes") == real.get("merged_episodes") == 1
    assert preview.get("skipped_episodes") == real.get("skipped_episodes") == 0


# ---------------------------------------------------------------------------
# bug-077: a prefetched embedding must never be attached to content that changed
# after the (unlocked) prefetch computed it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reembed_refuses_stale_prefetch_blob(clean_db, fake_embedding_client):
    db = clean_db
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('st', 'old text', '')"
    )
    mid = cur.lastrowid
    await db.commit()
    cache = await checks.prefetch_null_embeddings(db, "st")
    assert mid in cache["memories"]
    # Raced writer: content changes while the embedding stays NULL (the
    # do_update_memory embed-failure path).
    await db.execute("UPDATE memories SET content = 'new text', embedding = NULL WHERE id = ?", (mid,))
    await db.commit()
    clause, params = checks._agent_scope("st")
    n = await checks._reembed_null_rows(db, "memories", "content", clause, params, cache)
    await db.commit()
    assert n == 0
    row = await db.execute_fetchall("SELECT embedding FROM memories WHERE id = ?", (mid,))
    # Pre-fix the old text's vector was silently stamped onto the new content.
    assert row[0][0] is None


@pytest.mark.asyncio
async def test_check_health_rewritten_row_gets_coherent_embedding(clean_db, fake_embedding_client):
    """End-to-end bug-077 + bug-083 second pass: a row whose content is REWRITTEN by a
    sibling fixer during the locked run (annotation strip) must end the same
    check_health(fix=True) call with an embedding computed from the FINAL text."""
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('co', '[Memory from X] hello world', '')"
    )
    await db.commit()
    await maintenance_handlers.do_check_health(agent_id="co", fix=True)
    rows = await db.execute_fetchall("SELECT content, embedding FROM memories WHERE agent_id = 'co'")
    content, blob = rows[0]
    assert "[Memory from" not in content  # the annotation fixer ran
    assert blob is not None  # single-run convergence (the second pass repaired it)
    # Coherence: the stored vector encodes the final text, not the pre-rewrite text.
    assert bytes(blob) == FakeEmbeddingClient.pack_embedding(fake_embed_one(content))


# ---------------------------------------------------------------------------
# bug-078: merge_memories reaches do_delete_agent_data via mode='move' — its
# annotations must declare destructiveHint=True (worst reachable behaviour), like
# the direct delete_agent_data tool, so host-side HITL gates keyed on the hint fire.
# ---------------------------------------------------------------------------


def test_merge_memories_declares_destructive():
    from cpersona import server

    tool = next(t for t in server.registry._tools if t.name == "merge_memories")
    assert tool.annotations is not None and tool.annotations.destructiveHint is True
    # Control: the direct wipe tool it routes to has always declared it.
    wipe = next(t for t in server.registry._tools if t.name == "delete_agent_data")
    assert wipe.annotations is not None and wipe.annotations.destructiveHint is True


# ---------------------------------------------------------------------------
# bug-079: a write-free dry_run import preview must run under no-persist, not be
# short-circuited into a fabricated all-zero no-op (the bug-048 merge fix, applied
# to its import twin).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_dry_run_runs_under_no_persist(clean_db, tmp_path):
    f = tmp_path / "imp.jsonl"
    f.write_text(
        '{"_type": "memory", "agent_id": "np1", "content": "one", "msg_id": "m1"}\n'
        '{"_type": "memory", "agent_id": "np1", "content": "two", "msg_id": "m2"}\n'
    )
    try:
        no_persist.pause(ttl_seconds=60)
        res = await admin_handlers.do_import_memories(str(f), dry_run=True)
    finally:
        no_persist.resume()
    # Real preview counts (2), not the paused all-zero no-op.
    assert res.get("imported_memories") == 2, res
    assert "persisted" not in res or res.get("persisted") is not False
    db = clean_db
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'np1'")
    assert rows[0][0] == 0  # and it really was write-free


@pytest.mark.asyncio
async def test_import_real_run_still_gated_under_no_persist(clean_db, tmp_path):
    db = clean_db
    f = tmp_path / "imp.jsonl"
    f.write_text('{"_type": "memory", "agent_id": "np2", "content": "one", "msg_id": "m1"}\n')
    try:
        no_persist.pause(ttl_seconds=60)
        res = await admin_handlers.do_import_memories(str(f))
    finally:
        no_persist.resume()
    assert res.get("persisted") is False  # the WRITE path stays gated
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'np2'")
    assert rows[0][0] == 0


# ---------------------------------------------------------------------------
# bug-080: the vector episode retriever must honor the documented grounding-path
# contract the FTS drivers implement — a channel filter makes episodes safe to
# return even when source_id is set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_episodes_returned_with_source_id_plus_channel(clean_db, fake_embedding_client):
    db = clean_db
    await memory_handlers.do_archive_episode(
        "a1", [], summary="rocket propulsion design", channel="roomA"
    )
    # source_id + channel (the session-start grounding path): channel-scoped episodes
    # must still be vector-recalled. Pre-fix src_like short-circuited ep_rows to [].
    results = await _search_vector(
        db, "a1", "rocket propulsion", limit=10, channel="roomA", source_id="discord:123"
    )
    assert any(r.get("content", "").startswith("[Episode]") for r in results)
    # source_id WITHOUT channel: episodes stay excluded (no per-user source tagging).
    results_nochan = await _search_vector(
        db, "a1", "rocket propulsion", limit=10, source_id="discord:123"
    )
    assert not any(r.get("content", "").startswith("[Episode]") for r in results_nochan)
    # channel isolation still holds on the grounding path (bug-045).
    results_b = await _search_vector(
        db, "a1", "rocket propulsion", limit=10, channel="roomB", source_id="discord:123"
    )
    assert not any(r.get("content", "").startswith("[Episode]") for r in results_b)


# ---------------------------------------------------------------------------
# bug-081: a negative sample_size must not bypass the bug-053 clamp into
# SQLite's LIMIT -1 (= unbounded corpus scan feeding the O(n^2) matrix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibrate_negative_sample_size_is_clamped(clean_db, fake_embedding_client):
    db = clean_db
    # 12 embedded rows: an unbounded scan would sample all 12 (>= the 10-row minimum)
    # and proceed; a clamped sample_n=1 hits the insufficient-samples early-out.
    for i in range(12):
        blob = FakeEmbeddingClient.pack_embedding(fake_embed_one(f"row {i}"))
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES ('cal', ?, '', ?)",
            (f"row {i}", blob),
        )
    await db.commit()
    res = await admin_handlers.do_calibrate_threshold("cal", sample_size=-1)
    # Clamped to sample_n=1 -> the insufficient-samples early-out. Pre-fix, LIMIT -1
    # loaded the whole 12-row corpus (>= the 10-row minimum) and calibration RAN.
    assert res.get("ok") is False
    assert "found 1" in res.get("error", ""), res


# ---------------------------------------------------------------------------
# bug-084: an episode row must not inherit a colliding memory's recall_count via
# the recall_counts lookup (bug-041 excluded episodes from the dict's construction;
# this pins the lookup side).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_confidence_ignores_colliding_memory_recall_count(clean_db, monkeypatch):
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", False)
    db = clean_db
    # Memory #N: heavily recalled.
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, recall_count, last_recalled_at)"
        " VALUES ('a1', 'deploy runbook', '2026-06-01T00:00:00+00:00', 50, datetime('now'))"
    )
    mem_id = cur.lastrowid
    await db.commit()
    ts = "2026-06-01T00:00:00+00:00"
    # A fused result set where an UNRELATED episode collides on the same id.
    results = [
        {"id": mem_id, "source": '{"type":"User"}', "content": "deploy runbook", "_cosine": 0.6, "timestamp": ts},
        {
            "id": mem_id,
            "source": {"System": "episode"},
            "content": "[Episode] baking sourdough",
            "_cosine": 0.6,
            "timestamp": ts,
            "_resolved": False,
        },
    ]
    scored, time_range_hours, recall_counts = await memory_handlers._apply_recall_scoring(
        db, "a1", results, deep=False
    )
    assert recall_counts.get(mem_id, (0, ""))[0] == 50  # the memory's own data is intact
    episode = next(r for r in scored if memory_handlers._is_episode_result(r))
    memory = next(r for r in scored if not memory_handlers._is_episode_result(r))
    from cpersona.utils import _compute_confidence

    expected_episode = _compute_confidence(
        0.6, ts, resolved=False, deep=False, time_range_hours=time_range_hours,
        recall_count=0, last_recalled_at_str="",
    )["score"]
    expected_memory = _compute_confidence(
        0.6, ts, resolved=False, deep=False, time_range_hours=time_range_hours,
        recall_count=50, last_recalled_at_str=recall_counts[mem_id][1],
    )["score"]
    # Pre-fix the episode inherited the memory's (50, recent) pair and scored equal to it.
    # These two pins are the complete bug-084 contract: the episode is scored with
    # (0, "") and the memory with its own (50, ts). No ordering assertion between the
    # two — the direction depends on the recent-recall penalty window (last_recalled_at
    # = now can penalize the memory below the episode), which is timezone/clock
    # sensitive and not part of the fix.
    assert episode["_confidence_score"] == pytest.approx(expected_episode)
    assert memory["_confidence_score"] == pytest.approx(expected_memory)
