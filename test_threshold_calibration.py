"""Tests for the per-agent threshold + check_health patches ported in Phase 3-β-2b.

Covers two upstream cloto-mcp-servers/servers/cpersona patches:
- v2.4.15 (1c2f37a): per-agent threshold dict + _get_vector_threshold +
  do_calibrate_threshold per-agent / global scope
- eeef65e: check_health embedding-dimension-mismatch fix + episodes re-embed
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_threshold_calibration.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import admin_handlers  # noqa: E402
import config  # noqa: E402
import vector  # noqa: E402
from database import get_db  # noqa: E402
from _vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402


def _remove_sidecar():
    try:
        os.remove(admin_handlers._calibration_sidecar_path())
    except OSError:
        pass


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh DB and reset module-level threshold state for each test."""
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    vector._agent_thresholds.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3
    _remove_sidecar()
    yield
    vector._agent_thresholds.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3
    _remove_sidecar()


# ============================================================
# v2.4.15 — _get_vector_threshold
# ============================================================


def test_get_vector_threshold_falls_back_to_global():
    """Agents with no calibration data use the global config default."""
    assert vector._get_vector_threshold("uncalibrated-agent") == config.VECTOR_MIN_SIMILARITY


def test_get_vector_threshold_per_agent_override():
    """A per-agent entry takes precedence over the global default."""
    vector._agent_thresholds["agent-a"] = 0.55
    assert vector._get_vector_threshold("agent-a") == 0.55
    # other agents still see the global default
    assert vector._get_vector_threshold("agent-b") == config.VECTOR_MIN_SIMILARITY


# ============================================================
# v2.4.15 — do_calibrate_threshold (per-agent vs global scope)
# ============================================================


async def _seed_embeddings(db, agent_id: str, count: int, dim: int = 8) -> None:
    """Insert *count* memories with deterministic non-degenerate embeddings."""
    for i in range(count):
        # Vary the vectors so pairwise sims have a real distribution (non-zero std)
        vec = [float((i + j) % 5) - 2.0 for j in range(dim)]
        blob = EmbeddingClient.pack_embedding(vec)
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            (agent_id, f"memory {i}", "2026-05-14T00:00:00Z", blob),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_calibrate_threshold_insufficient_embeddings():
    """Fewer than 10 embeddings → ok=False, no threshold mutation."""
    db = await get_db()
    await _seed_embeddings(db, "agent-small", 5)
    result = await admin_handlers.do_calibrate_threshold("agent-small")
    assert result["ok"] is False
    assert "agent-small" not in vector._agent_thresholds


@pytest.mark.asyncio
async def test_calibrate_threshold_per_agent_scope():
    """agent_id provided → writes to _agent_thresholds, leaves global untouched."""
    db = await get_db()
    await _seed_embeddings(db, "agent-cal", 15)
    global_before = config.VECTOR_MIN_SIMILARITY

    result = await admin_handlers.do_calibrate_threshold("agent-cal")
    assert result["ok"] is True
    assert result["scope"] == "per_agent"
    assert result["agent_id"] == "agent-cal"
    # per-agent dict was written; global was not touched
    assert "agent-cal" in vector._agent_thresholds
    assert vector._agent_thresholds["agent-cal"] == result["new_threshold"]
    assert config.VECTOR_MIN_SIMILARITY == global_before
    # _get_vector_threshold now reflects the calibrated value
    assert vector._get_vector_threshold("agent-cal") == result["new_threshold"]


@pytest.mark.asyncio
async def test_calibrate_threshold_global_scope():
    """Empty agent_id → calibrates the global config.VECTOR_MIN_SIMILARITY."""
    db = await get_db()
    # seed two agents — the global corpus spans both
    await _seed_embeddings(db, "agent-1", 8)
    await _seed_embeddings(db, "agent-2", 8)

    result = await admin_handlers.do_calibrate_threshold("")
    assert result["ok"] is True
    assert result["scope"] == "global"
    assert config.VECTOR_MIN_SIMILARITY == result["new_threshold"]
    # no per-agent entries were created
    assert vector._agent_thresholds == {}


@pytest.mark.asyncio
async def test_calibrate_threshold_old_threshold_reflects_scope():
    """old_threshold reads the per-agent value when one already exists."""
    db = await get_db()
    await _seed_embeddings(db, "agent-recal", 15)
    vector._agent_thresholds["agent-recal"] = 0.99  # pre-existing per-agent value

    result = await admin_handlers.do_calibrate_threshold("agent-recal")
    assert result["ok"] is True
    assert result["old_threshold"] == 0.99


# ============================================================
# eeef65e — check_health episodes null-embedding detection
# ============================================================


@pytest.mark.asyncio
async def test_check_health_detects_null_episode_embeddings():
    """check_health surfaces episodes with NULL embeddings as a distinct issue."""
    db = await get_db()
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, embedding) VALUES (?, ?, NULL)",
        ("agent-ep", "episode without embedding"),
    )
    await db.commit()

    from maintenance_handlers import do_check_health

    result = await do_check_health("agent-ep", fix=False)
    issue_types = {issue["type"] for issue in result.get("issues", [])}
    assert "null_episode_embedding" in issue_types


# ============================================================
# v2.4.24 — Tier 4: embedding-change guard + persistence
#   (pure formula/comparison tests live in test_calibration_formula.py)
# ============================================================


@pytest.mark.asyncio
async def test_calibrate_records_dim_model_and_writes_sidecar():
    """A calibration records embedding_dim / embedding_model and persists a sidecar."""
    db = await get_db()
    await _seed_embeddings(db, "agent-dim", 15, dim=8)

    result = await admin_handlers.do_calibrate_threshold("agent-dim")
    assert result["ok"] is True
    assert result["embedding_dim"] == 8
    assert result["method"] == config.CALIBRATE_METHOD
    assert "embedding_model" in result

    state = admin_handlers._load_calibration_state()
    assert state is not None
    assert state["embedding_dim"] == 8
    assert state["agent_thresholds"]["agent-dim"] == result["new_threshold"]


@pytest.mark.asyncio
async def test_startup_guard_restores_when_dim_unchanged():
    """Matching sidecar dim + AUTO_CALIBRATE off → restore, no recompute."""
    db = await get_db()
    await _seed_embeddings(db, "agent-keep", 15, dim=8)
    admin_handlers._save_calibration_state(
        embedding_dim=8,
        embedding_model="bge-m3",
        global_threshold=0.61,
        agent_thresholds={"agent-keep": 0.58},
    )

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )
    assert status["action"] == "restored"
    assert vector._get_vector_threshold("agent-keep") == 0.58
    assert config.VECTOR_MIN_SIMILARITY == 0.61


@pytest.mark.asyncio
async def test_startup_guard_recalibrates_on_dim_change():
    """A dimension change (e.g. jina 768 -> bge-m3 1024) forces recalibration."""
    db = await get_db()
    await _seed_embeddings(db, "agent-swap", 15, dim=8)
    # Sidecar claims a previous embedding model with a different dimension.
    admin_handlers._save_calibration_state(
        embedding_dim=4,
        embedding_model="old-model",
        global_threshold=0.99,
        agent_thresholds={"agent-swap": 0.99},
    )

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )
    assert status["action"] == "recalibrated"
    assert status["dim_changed"] is True
    # the stale 0.99 threshold was replaced by a freshly computed value
    assert vector._get_vector_threshold("agent-swap") != 0.99
    # the sidecar now records the live dimension
    assert admin_handlers._load_calibration_state()["embedding_dim"] == 8


@pytest.mark.asyncio
async def test_startup_guard_noop_without_sidecar_when_disabled():
    """No sidecar + AUTO_CALIBRATE off + on_model_change off → no calibration."""
    db = await get_db()
    await _seed_embeddings(db, "agent-none", 15, dim=8)

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=False
    )
    assert status["action"] == "noop"
    assert "agent-none" not in vector._agent_thresholds
