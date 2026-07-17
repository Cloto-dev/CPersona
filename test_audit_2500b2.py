"""Regression tests for the 2.5.0b1 post-release audit fixes (bug-114..122, → 2.5.0b2).

Covers the b1 comprehensive-audit confirmed defects (CSC Task #243/#244,
report: .agents-artifacts/reviews/b1q-audit-report-2026-07-16.md):
naive-timestamp UTC anchoring (bug-114), episode-penalty ranking no-op under
default config (bug-115), mixed-format orphan-episode false positives
(bug-116), preview truncation of ref-less injected rows (bug-117), FTS-gated
migration skip (bug-118), no-persist skeleton shape drift (bug-119),
structural-gate substring/interpolation blindness (bug-120), and empty-content
exclude-filter drops (bug-121).
"""
import ast
from datetime import datetime, timezone

import pytest
import pytest_asyncio

import test_structural_gates as tsg
from cpersona import admin_handlers, config, database, memory_handlers, server
from cpersona._vendored_mcp_common import no_persist
from cpersona.checks import deep_orphaned_episodes
from cpersona.database import get_db
from cpersona.utils import _content_excluded, _format_memory_timestamp, _parse_timestamp_utc


@pytest_asyncio.fixture
async def clean_db():
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-114 (C07): naive timestamps are UTC by invariant. astimezone() on a naive
# datetime assumes system-local time — on a JST host every datetime('now')
# value was shifted 9 hours, corrupting recall-boost decay and the episode
# boundary factor.
# ---------------------------------------------------------------------------


def test_parse_timestamp_naive_is_utc():
    dt = _parse_timestamp_utc("2026-07-16 08:00:00")
    assert dt == datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc), (
        "naive DB timestamp must be anchored as UTC, not shifted by the host's local offset"
    )


def test_parse_timestamp_aware_unchanged():
    dt = _parse_timestamp_utc("2026-07-16T17:00:00+09:00")
    assert dt == datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc)
    dt_z = _parse_timestamp_utc("2026-07-16T08:00:00Z")
    assert dt_z == datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc)


def test_format_timestamp_naive_matches_aware_utc():
    # The display helper must treat a naive value and its explicit-UTC twin
    # identically, whatever the host timezone is.
    assert _format_memory_timestamp("2026-07-16 08:00:00") == _format_memory_timestamp(
        "2026-07-16T08:00:00+00:00"
    )


# ---------------------------------------------------------------------------
# bug-115 (C10): under default config (confidence off, episode penalty on) the
# penalty multiplied fusion scores in place but nothing re-sorted, so ranking
# and downstream truncation ignored it entirely.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_penalty_resorts_fusion_results(clean_db, monkeypatch):
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    # Boundary: an episode archived now — memories long before it get penalised.
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) "
        "VALUES ('pen-agent', 's', 'k', datetime('now'))"
    )
    await clean_db.commit()
    old_row = {
        "id": 1,
        "content": "old cross-session hit",
        "timestamp": "2026-01-01T00:00:00+00:00",  # far before the boundary
        "_rrf_score": 0.05,
    }
    new_row = {
        "id": 2,
        "content": "current-session hit",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_rrf_score": 0.045,
    }
    results, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, "pen-agent", [dict(old_row), dict(new_row)], deep=False
    )
    assert results[0]["id"] == 2, (
        "penalised fusion scores must re-order the output (the penalty was a ranking "
        "no-op under default config)"
    )


@pytest.mark.asyncio
async def test_episode_penalty_keeps_cascade_stage_order(clean_db, monkeypatch):
    # Cascade rows carry no fusion score on every row — stage order is by design
    # (bug-018) and must survive the bug-115 re-sort.
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) "
        "VALUES ('pen-agent2', 's', 'k', datetime('now'))"
    )
    await clean_db.commit()
    rows = [
        {"id": 1, "content": "vector stage", "timestamp": "2026-01-01T00:00:00+00:00", "_cosine": 0.9},
        {"id": 2, "content": "keyword stage", "timestamp": "2026-01-01T00:00:00+00:00"},
    ]
    results, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, "pen-agent2", rows, deep=False
    )
    assert [r["id"] for r in results] == [1, 2], "cascade stage order must be preserved"


# ---------------------------------------------------------------------------
# bug-116 (C25): orphan detection compared mixed-format timestamp strings
# lexicographically ('T' > ' '), reporting episodes whose window clearly
# contains memories as orphaned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_check_survives_mixed_timestamp_formats(clean_db):
    await clean_db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('orph-agent', 'inside the window', '{}', '2026-07-16T08:00:00+00:00')"
    )
    # Naive, space-separated window (datetime('now') format) around the memory.
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time) "
        "VALUES ('orph-agent', 's', 'k', '2026-07-16 07:00:00', '2026-07-16 09:00:00')"
    )
    await clean_db.commit()
    result = await deep_orphaned_episodes(clean_db, "orph-agent", fix=False)
    assert result["count"] == 0, (
        "episode containing a memory was reported orphaned (lexicographic mixed-format compare)"
    )


# ---------------------------------------------------------------------------
# bug-117 (C20): preview truncation of injected rows without a `ref` handle
# made their full content permanently unreachable (no get_contents path).
# ---------------------------------------------------------------------------


def test_preview_skips_refless_rows(monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 10)
    long_text = "x" * 50
    result = {
        "messages": [
            {"content": long_text, "ref": "mem:1"},
            {"content": long_text},  # injected row ([Profile] / external_context)
        ]
    }
    out = server._apply_preview(result)
    assert out["messages"][0]["content_truncated"] is True
    assert len(out["messages"][0]["content"]) == 10
    assert out["messages"][1]["content"] == long_text, (
        "ref-less rows must never be truncated — there is no handle to fetch the full text"
    )
    assert "content_truncated" not in out["messages"][1]


# ---------------------------------------------------------------------------
# bug-118 (C36): FTS-gated migrations were permanently skipped when the schema
# version advanced while FTS_ENABLED=false — re-enabling FTS later kept the OLD
# trigger bodies (CREATE TRIGGER IF NOT EXISTS keeps an existing trigger). The
# DDL probe must detect the stale body and modernise it on boot.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_fts_trigger_modernised_on_boot(monkeypatch, tmp_path):
    dbfile = str(tmp_path / "stale-fts.db")
    monkeypatch.setattr(database, "DB_PATH", dbfile)
    # bug-124: re-pointing the globals must go through close_db() — a bare
    # `database._db = None` orphans the session-shared connection, whose
    # non-daemon aiosqlite worker thread then blocks interpreter exit (the
    # 6-hour CI hang). close_db() also resets the dedicated read connection.
    await database.close_db()
    db = await database.get_db()
    # Simulate the skipped-v13 state: replace the column-scoped trigger with the
    # old full-column body (no "OF content").
    await db.execute("DROP TRIGGER IF EXISTS memories_fts_au")
    await db.execute(
        """CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
             INSERT INTO memories_fts(memories_fts, rowid, content)
               VALUES('delete', old.id, old.content);
             INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
           END"""
    )
    await db.commit()
    await database.close_db()

    db = await database.get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memories_fts_au'"
        )
        assert rows and "AFTER UPDATE OF content" in rows[0][0], (
            "stale FTS trigger body survived a boot — the skipped FTS-gated migration "
            "was not recovered by the DDL probe"
        )
        # bug-123: the recovery must rebuild in the SAME boot — the old condition
        # armed the bit on fts_triggers_stale but deferred the rebuild to the
        # next boot, leaving the pending bit set.
        uv = await db.execute_fetchall("PRAGMA user_version")
        assert not (uv[0][0] & 1), "backfill pending bit left armed after the recovery rebuild"
    finally:
        await database.close_db()


# ---------------------------------------------------------------------------
# bug-119 (C45, bug-111 sibling class): no-persist skeletons must mirror the
# real success payload's keys, and pure input validation must still return the
# real error response while paused.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibrate_skeleton_mirrors_real_shape():
    no_persist.pause(ttl_seconds=60)
    try:
        out = await admin_handlers.do_calibrate_threshold("skel-agent")
        assert out["persisted"] is False
        for key in (
            "ok", "sidecar_persisted", "scope", "agent_id", "sampled_embeddings",
            "num_pairs", "method", "z_factor", "percentile", "embedding_dim",
            "embedding_model", "distribution", "null_admit_rate", "old_threshold",
            "new_threshold",
        ):
            assert key in out, f"calibrate no-persist skeleton missing real-shape key {key!r}"
        assert "sample_size" not in out, "phantom key from the pre-b2 skeleton is back"
        # Validation still real under pause:
        bad = await admin_handlers.do_calibrate_threshold("skel-agent", percentile=200)
        assert bad["ok"] is False and "percentile" in bad["error"]
    finally:
        no_persist.resume()


@pytest.mark.asyncio
async def test_set_recall_precision_skeleton_mirrors_real_shape():
    no_persist.pause(ttl_seconds=60)
    try:
        out = await admin_handlers.do_set_recall_precision("skel-agent", precision="strict")
        assert out["persisted"] is False
        for key in ("ok", "agent_id", "precision", "beta", "cleared", "fused_gate", "calibrate"):
            assert key in out, f"set_recall_precision skeleton missing real-shape key {key!r}"
        assert out["precision"] == "strict"
        assert out["beta"] == config._PRECISION_BETA["strict"]
        assert out["cleared"] is False
        # Unknown precision still returns the real error while paused:
        bad = await admin_handlers.do_set_recall_precision("skel-agent", precision="bogus")
        assert bad["ok"] is False and "Unknown precision" in bad["error"]
        # The in-memory override must NOT have been applied by the skeleton path.
        from cpersona import vector

        assert "skel-agent" not in vector._agent_betas
    finally:
        no_persist.resume()


# ---------------------------------------------------------------------------
# bug-120 (C03/C04): gate teeth — the isolation gate must reject substring
# tricks and see interpolated DML targets.
# ---------------------------------------------------------------------------


def _violations_for(src: str):
    return tsg._agent_dml_violations(ast.parse(src))


def test_gate_rejects_or_weakened_predicate():
    src = 'q = "DELETE FROM memories WHERE locked = 0 OR agent_id IS NULL"'
    assert _violations_for(src), "OR-weakened agent_id mention must not count as scoping"


def test_gate_rejects_commented_agent_id():
    src = 'q = "SELECT id FROM memories WHERE locked = 0 -- agent_id checked upstream"'
    assert _violations_for(src), "a commented agent_id must not count as scoping"


def test_gate_accepts_real_conjunct():
    src = 'q = "DELETE FROM memories WHERE agent_id = ? AND locked = 0"'
    assert not _violations_for(src)


def test_gate_sees_interpolated_dml_target():
    src = 'q = f"DELETE FROM {table} WHERE locked = 0"'
    assert _violations_for(src), "interpolated DML target must not be invisible to the gate"


def test_gate_skips_fts_control_commands():
    src = "q = f\"INSERT INTO {fts}({fts}) VALUES('rebuild')\""
    assert not _violations_for(src), "FTS5 self-referencing control commands are not row DML"


# ---------------------------------------------------------------------------
# bug-121 (C29): an empty content string starts-with-matched every exclude
# entry, silently dropping legitimately-empty-content memories.
# ---------------------------------------------------------------------------


def test_content_excluded_empty_content_not_dropped():
    assert _content_excluded("", {"some filter"}) is False
    assert _content_excluded("   ", {"some filter"}) is False


def test_content_excluded_still_matches():
    assert _content_excluded("Some Filter and more", {"some filter"}) is True
    assert _content_excluded("anything", set()) is False
