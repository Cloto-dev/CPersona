"""Tests for the recall-quality patches ported in Phase 3-β-1.

Covers four upstream cloto-mcp-servers/servers/cpersona patches:
- v2.4.11 (934ad1e): execute_fetchone → execute_fetchall write-tool bug fix
- v2.4.12 (ca2d041): scale-aware RRF quality gate
- v2.4.13 (0ee1628): AUTOCUT relative gap ratio
- v2.4.14 (dee2ec8): episode boundary soft penalty
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_recall_quality.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import admin_handlers  # noqa: E402
import config  # noqa: E402
from database import get_db  # noqa: E402
from memory_handlers import (  # noqa: E402
    _apply_quality_gate,
    _autocut,
    _episode_boundary_factor,
    _get_episode_boundary_ts,
)
from utils import _parse_timestamp_utc  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh DB for each test."""
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM profiles")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


# ============================================================
# v2.4.13 — AUTOCUT relative gap ratio
# ============================================================


def test_autocut_short_input_returns_unchanged():
    """Fewer than 2 results: nothing to cut."""
    assert _autocut([]) == []
    one = [{"_rrf_score": 0.04}]
    assert _autocut(one) == one


def test_autocut_cuts_at_relative_gap():
    """A gap exceeding AUTOCUT_MIN_GAP_RATIO of the top score is a breakpoint."""
    results = [
        {"_cosine": 0.90},
        {"_cosine": 0.85},
        {"_cosine": 0.30},  # 0.55 gap before this — well over 0.15 * 0.90
        {"_cosine": 0.25},
    ]
    assert _autocut(results) == results[:2]


def test_autocut_ignores_uniform_distribution():
    """Gaps below AUTOCUT_MIN_GAP_RATIO of the top score are treated as noise."""
    results = [
        {"_cosine": 0.90},
        {"_cosine": 0.88},
        {"_cosine": 0.86},
        {"_cosine": 0.84},
    ]
    # max gap 0.02 / max_score 0.90 ≈ 0.022 < 0.15 → no cut
    assert _autocut(results) == results


def test_autocut_works_on_rrf_scale():
    """Relative ratio makes autocut scale-agnostic — RRF (~0–0.05) also cuts."""
    results = [
        {"_rrf_score": 0.0480},
        {"_rrf_score": 0.0450},
        {"_rrf_score": 0.0100},  # 0.035 gap / 0.048 ≈ 0.73 ≫ 0.15
        {"_rrf_score": 0.0090},
    ]
    assert _autocut(results) == results[:2]


def test_autocut_zero_top_score_returns_unchanged():
    """A non-positive top score has no meaningful ratio — return as-is."""
    results = [{"_cosine": 0.0}, {"_cosine": 0.0}]
    assert _autocut(results) == results


def test_autocut_preserves_small_result_set():
    """v2.4.25: below AUTOCUT_MIN_RESULTS, keep every row.

    RSF min-max pins the lowest row to 0.0, so a 2-item set shows a full-scale
    gap (1.0 → 0.0) that would otherwise cut to a single row, dropping a
    still-relevant second hit. The min-results floor (default 3) keeps both.
    """
    two = [{"_rsf_score": 1.0}, {"_rsf_score": 0.0}]
    assert _autocut(two) == two
    # Once the set is large enough, autocut still fires on a real gap.
    four = [
        {"_rsf_score": 1.0},
        {"_rsf_score": 0.95},
        {"_rsf_score": 0.10},  # 0.85 gap ≫ 0.15 * 1.0
        {"_rsf_score": 0.0},
    ]
    assert _autocut(four) == four[:2]


# ============================================================
# v2.4.12 — scale-aware RRF quality gate
# ============================================================


def test_quality_gate_empty_input():
    assert _apply_quality_gate([], 0.3, 100) == []


def test_quality_gate_cosine_threshold():
    """Cosine-scored rows gate directly against min_score."""
    results = [{"id": 1, "_cosine": 0.45}, {"id": 2, "_cosine": 0.15}]
    out = _apply_quality_gate(results, 0.3, 100)
    assert [r["id"] for r in out] == [1]


def test_quality_gate_rrf_uses_scaled_threshold():
    """RRF-scored rows gate against min_score * RRF_MAX_SCALE, not raw min_score.

    This is the v2.4.12 fix: a realistic _rrf_score (~0.04) clears the scaled
    threshold but would never clear the cosine-scale min_score directly.
    """
    rrf_high = 0.040
    rrf_low = 0.001
    scaled = 0.3 * config.RRF_MAX_SCALE
    assert rrf_high >= scaled > rrf_low  # sanity: the test data straddles the gate
    results = [{"id": 1, "_rrf_score": rrf_high}, {"id": 2, "_rrf_score": rrf_low}]
    out = _apply_quality_gate(results, 0.3, 100)
    assert [r["id"] for r in out] == [1]


def test_quality_gate_score_priority_confidence_over_cosine():
    """When both _confidence_score and _cosine exist, confidence wins."""
    # confidence below gate, cosine above — must be blocked (confidence has priority)
    results = [{"id": 1, "_confidence_score": 0.10, "_cosine": 0.99}]
    assert _apply_quality_gate(results, 0.3, 100) == []
    # confidence above gate, cosine below — must pass
    results = [{"id": 2, "_confidence_score": 0.50, "_cosine": 0.01}]
    out = _apply_quality_gate(results, 0.3, 100)
    assert [r["id"] for r in out] == [2]


def test_quality_gate_score_priority_cosine_over_rrf():
    """When both _cosine and _rrf_score exist, cosine wins (the appropriate signal)."""
    # cosine below gate, rrf above scaled gate — must be blocked (cosine priority)
    results = [{"id": 1, "_cosine": 0.10, "_rrf_score": 0.049}]
    assert _apply_quality_gate(results, 0.3, 100) == []


def test_quality_gate_unscored_volume_rule():
    """Unscored rows kept only when memory_count >= 100."""
    results = [{"id": 1}]  # no score keys
    assert _apply_quality_gate(results, 0.3, 100) == results
    assert _apply_quality_gate(results, 0.3, 99) == []


def test_quality_gate_profile_sentinel():
    """Profile injection (id == -1) gates on memory_count >= 50, ignores score."""
    profile = [{"id": -1, "content": "[Profile] ..."}]
    assert _apply_quality_gate(profile, 0.3, 50) == profile
    assert _apply_quality_gate(profile, 0.3, 49) == []


# ============================================================
# v2.4.26 (Goal #132) — calibrated post-fusion gate
# ============================================================


def test_quality_gate_rsf_uses_calibrated_gate_over_heuristic():
    """When fused_gate is supplied, RSF rows gate against it, not the heuristic min_score."""
    # Heuristic min_score=0.20 would admit both; the calibrated gate 0.50 rejects the low one.
    results = [{"id": 1, "_rsf_score": 0.55}, {"id": 2, "_rsf_score": 0.30}]
    out = _apply_quality_gate(results, 0.20, 100, fused_gate=0.50)
    assert [r["id"] for r in out] == [1]
    # Without the calibrated gate, the lax heuristic admits both (the inert-gate bug class).
    out_heuristic = _apply_quality_gate(results, 0.20, 100)
    assert [r["id"] for r in out_heuristic] == [1, 2]


def test_quality_gate_rrf_calibrated_gate_compares_raw_not_rescaled():
    """An RRF calibrated gate is on the raw _rrf_score scale — compared directly, no RRF_MAX_SCALE."""
    results = [{"id": 1, "_rrf_score": 0.040}, {"id": 2, "_rrf_score": 0.010}]
    # gate 0.030 is a raw rrf-scale value; the heuristic path would have rescaled by RRF_MAX_SCALE.
    out = _apply_quality_gate(results, 0.20, 100, fused_gate=0.030)
    assert [r["id"] for r in out] == [1]


def test_quality_gate_none_fused_gate_preserves_legacy():
    """fused_gate=None reproduces the pre-v2.4.26 heuristic behaviour exactly."""
    results = [{"id": 1, "_rsf_score": 0.45}, {"id": 2, "_rsf_score": 0.15}]
    assert _apply_quality_gate(results, 0.30, 100) == _apply_quality_gate(
        results, 0.30, 100, fused_gate=None
    )


def test_quality_gate_cosine_branch_ignores_fused_gate():
    """The fused gate is fusion-only; cascade's cosine branch still uses min_score."""
    results = [{"id": 1, "_cosine": 0.45}, {"id": 2, "_cosine": 0.15}]
    # A high fused_gate must not affect cosine-scored rows (cascade owns precision via the
    # vector threshold upstream, not this gate).
    out = _apply_quality_gate(results, 0.30, 100, fused_gate=0.99)
    assert [r["id"] for r in out] == [1]


# ============================================================
# v2.4.14 — episode boundary soft penalty
# ============================================================


def test_episode_factor_no_boundary_or_ts():
    """Missing memory ts or boundary → no penalty."""
    assert _episode_boundary_factor(None, None) == 1.0
    boundary = _parse_timestamp_utc("2026-05-14T00:00:00Z")
    assert _episode_boundary_factor(None, boundary) == 1.0
    assert _episode_boundary_factor("2026-05-13T00:00:00Z", None) == 1.0


def test_episode_factor_current_session_no_penalty():
    """Memories at or after the boundary are current-session — factor 1.0."""
    boundary = _parse_timestamp_utc("2026-05-14T00:00:00Z")
    assert _episode_boundary_factor("2026-05-14T00:00:00Z", boundary) == 1.0
    assert _episode_boundary_factor("2026-05-15T12:00:00Z", boundary) == 1.0


def test_episode_factor_decays_before_boundary():
    """Older memories decay exponentially, clamped at EPISODE_DECAY_FLOOR."""
    boundary = _parse_timestamp_utc("2026-05-14T00:00:00Z")
    # 24h before → exp(-0.01 * 24) ≈ 0.787
    f24 = _episode_boundary_factor("2026-05-13T00:00:00Z", boundary)
    assert 0.78 < f24 < 0.79
    # very old → clamped at the floor
    f_old = _episode_boundary_factor("2020-01-01T00:00:00Z", boundary)
    assert f_old == config.EPISODE_DECAY_FLOOR
    # decay is monotonic: older ⇒ smaller factor
    f12 = _episode_boundary_factor("2026-05-13T12:00:00Z", boundary)
    assert f24 < f12 < 1.0


@pytest.mark.asyncio
async def test_get_episode_boundary_ts():
    """Returns the latest episode's created_at, or None when no episodes exist."""
    db = await get_db()
    assert await _get_episode_boundary_ts(db, "agent-x") is None

    await db.execute(
        "INSERT INTO episodes (agent_id, summary, created_at) VALUES (?, ?, ?)",
        ("agent-x", "older", "2026-05-10T00:00:00Z"),
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, created_at) VALUES (?, ?, ?)",
        ("agent-x", "newer", "2026-05-13T00:00:00Z"),
    )
    # different agent — must not leak
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, created_at) VALUES (?, ?, ?)",
        ("agent-y", "other", "2026-05-20T00:00:00Z"),
    )
    await db.commit()

    boundary = await _get_episode_boundary_ts(db, "agent-x")
    assert boundary == _parse_timestamp_utc("2026-05-13T00:00:00Z")


# ============================================================
# v2.4.11 — execute_fetchone → execute_fetchall write-tool bug fix
# ============================================================


async def _insert_memory(db, agent_id: str, content: str, locked: int = 0) -> int:
    cursor = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, locked) VALUES (?, ?, ?, ?)",
        (agent_id, content, "2026-05-14T00:00:00Z", locked),
    )
    await db.commit()
    return cursor.lastrowid


@pytest.mark.asyncio
async def test_delete_memory_actually_deletes_row():
    """v2.4.11: do_delete_memory must remove the DB row, not silently AttributeError."""
    db = await get_db()
    mem_id = await _insert_memory(db, "agent-d", "to be deleted")

    result = await admin_handlers.do_delete_memory(mem_id)
    assert result.get("ok") is True

    rows = await db.execute_fetchall("SELECT id FROM memories WHERE id = ?", (mem_id,))
    assert rows == []


@pytest.mark.asyncio
async def test_delete_memory_not_found():
    result = await admin_handlers.do_delete_memory(999999)
    assert "error" in result


@pytest.mark.asyncio
async def test_delete_memory_locked_is_rejected():
    db = await get_db()
    mem_id = await _insert_memory(db, "agent-d", "locked memory", locked=1)
    result = await admin_handlers.do_delete_memory(mem_id)
    assert "error" in result and "locked" in result["error"]
    # row must survive
    rows = await db.execute_fetchall("SELECT id FROM memories WHERE id = ?", (mem_id,))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_update_memory_actually_updates_content():
    db = await get_db()
    mem_id = await _insert_memory(db, "agent-u", "original content")

    result = await admin_handlers.do_update_memory(mem_id, "revised content")
    assert result.get("ok") is True

    rows = await db.execute_fetchall("SELECT content FROM memories WHERE id = ?", (mem_id,))
    assert rows[0][0] == "revised content"


@pytest.mark.asyncio
async def test_update_memory_not_found():
    result = await admin_handlers.do_update_memory(999999, "new content")
    assert "error" in result


@pytest.mark.asyncio
async def test_lock_and_unlock_memory_flip_the_flag():
    db = await get_db()
    mem_id = await _insert_memory(db, "agent-l", "lockable memory")

    lock_result = await admin_handlers.do_lock_memory(mem_id)
    assert lock_result.get("ok") is True
    rows = await db.execute_fetchall("SELECT locked FROM memories WHERE id = ?", (mem_id,))
    assert rows[0][0] == 1

    unlock_result = await admin_handlers.do_unlock_memory(mem_id)
    assert unlock_result.get("ok") is True
    rows = await db.execute_fetchall("SELECT locked FROM memories WHERE id = ?", (mem_id,))
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_lock_memory_not_found():
    result = await admin_handlers.do_lock_memory(999999)
    assert "error" in result


@pytest.mark.asyncio
async def test_lock_memory_ownership_enforced():
    db = await get_db()
    mem_id = await _insert_memory(db, "agent-owner", "owned memory")
    result = await admin_handlers.do_lock_memory(mem_id, agent_id="agent-other")
    assert "error" in result and "not owned" in result["error"]
