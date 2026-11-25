"""Regression tests for calibration state left stale or unreported (bug-810, bug-811, bug-812)."""

import contextlib
import glob
import json
import os
import struct

import numpy as np
import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, config, session, vector
from cpersona.database import get_db
from tests.test_threshold_calibration import _seed_embeddings


def _reset_calibration_state():
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._agent_betas.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._reset_calibration_authority()


@pytest_asyncio.fixture
async def clean_store():
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    session.reset_pauses_for_tests()
    _reset_calibration_state()
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    with contextlib.suppress(OSError):
        os.remove(admin_handlers._calibration_sidecar_path())
    yield db
    session.reset_pauses_for_tests()
    _reset_calibration_state()
    config.VECTOR_MIN_SIMILARITY = saved_threshold
    await db.execute("DELETE FROM memories")
    await db.commit()


def _write_sidecar_raw(text: str) -> None:
    with open(admin_handlers._calibration_sidecar_path(), "w") as fh:
        fh.write(text)


# JSON that parses but is not an object: every shape a hand edit or another tool can leave.
_NON_OBJECT_SIDECARS = ["[1]", "[]", '"x"', "42", "null", "true"]


# ---------------------------------------------------------------------------
# bug-810
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", _NON_OBJECT_SIDECARS)
def test_bug_810_non_object_sidecar_reads_as_unreadable(clean_store, raw):
    _write_sidecar_raw(raw)
    state, unreadable = admin_handlers._read_calibration_sidecar()
    assert state is None
    assert unreadable is True, f"sidecar {raw!r} parsed as JSON but is not an object; it must count as unreadable"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _NON_OBJECT_SIDECARS)
async def test_bug_810_startup_backs_up_non_object_sidecar_and_recalibrates(clean_store, raw):
    await _seed_embeddings(clean_store, "agent-810", 15)
    _write_sidecar_raw(raw)
    status = await admin_handlers.ensure_calibrated_on_startup(auto_calibrate=False, on_model_change=True)
    backup = status.get("sidecar_backup")
    assert backup, f"the startup guard replaced the sidecar {raw!r} without copying it aside: {status}"
    with open(backup) as fh:
        assert fh.read() == raw
    with open(admin_handlers._calibration_sidecar_path()) as fh:
        rewritten = json.load(fh)
    assert isinstance(rewritten, dict) and "agent-810" in rewritten["agent_thresholds"]


@pytest.mark.asyncio
async def test_bug_810_calibrate_threshold_with_non_object_sidecar(clean_store):
    await _seed_embeddings(clean_store, "agent-810", 15)
    _write_sidecar_raw("[1]")
    result = await admin_handlers.do_calibrate_threshold("agent-810")
    assert result["ok"] is True and result["sidecar_persisted"] is True
    backups = glob.glob(glob.escape(admin_handlers._calibration_sidecar_path()) + ".before-unreadable-*")
    assert len(backups) == 1


@pytest.mark.asyncio
async def test_bug_810_deep_check_reports_non_object_sidecar_unreadable(clean_store, fake_embedding_client):
    _write_sidecar_raw("[1]")
    out = await checks.deep_calibration_staleness(clean_store, "agent-810", False)
    assert out["status"] == "unreadable"


@pytest.mark.asyncio
async def test_bug_810_delete_agent_data_runs_every_purge_with_non_object_sidecar(clean_store, monkeypatch):
    await _seed_embeddings(clean_store, "agent-810", 5)
    remote_calls = []

    async def _record(agent_id):
        remote_calls.append(agent_id)

    monkeypatch.setattr(admin_handlers, "_purge_agent_remote_namespace", _record)
    _write_sidecar_raw("[1]")
    result = await admin_handlers.do_delete_agent_data("agent-810")
    assert result["ok"] is True and result["deleted_memories"] == 5
    assert remote_calls == ["agent-810"]
    # The purge does not replace a file it cannot read.
    with open(admin_handlers._calibration_sidecar_path()) as fh:
        assert fh.read() == "[1]"


# ---------------------------------------------------------------------------
# bug-811
# ---------------------------------------------------------------------------

AGENT_811 = "agent-811"
DIM_811 = 8


@pytest_asyncio.fixture
async def corpus_811(clean_store, monkeypatch):
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)

    async def measure(db, agent_id, sample_queries, window_min, beta, floor, draws=None):
        # The gate depends on the beta it is measured at, as the separation curve does.
        return {"threshold": round(0.1 * beta, 4), "signal": "rsf", "beta": beta}

    monkeypatch.setattr(admin_handlers, "_calibrate_fused_gate_median", measure)
    rng = np.random.default_rng(7)
    for i in range(60):
        v = rng.standard_normal(DIM_811).astype(np.float32)
        v /= float(np.linalg.norm(v))
        await clean_store.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (AGENT_811, f"memory {i}", f"2026-10-01T00:{i:02d}:00+00:00", struct.pack(f"<{DIM_811}f", *v.tolist())),
        )
    await clean_store.commit()
    yield clean_store


@contextlib.contextmanager
def _lock_not_taken():
    yield False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["save_fails", "lock_not_taken"])
async def test_bug_811_refused_precision_change_leaves_previous_gate(corpus_811, monkeypatch, failure):
    ok = await admin_handlers.do_set_recall_precision(AGENT_811, "lenient")
    assert ok["ok"] is True
    assert vector._agent_betas[AGENT_811] == 0.5 and vector._agent_fused_gates[AGENT_811] == 0.05

    real_save = admin_handlers._save_calibration_state
    real_lock = admin_handlers._calibration_sidecar_locked
    if failure == "save_fails":
        monkeypatch.setattr(admin_handlers, "_save_calibration_state", lambda *a, **k: False)
    else:
        monkeypatch.setattr(admin_handlers, "_calibration_sidecar_locked", _lock_not_taken)
    refused = await admin_handlers.do_set_recall_precision(AGENT_811, "strict")
    assert refused["ok"] is False and "not applied" in refused["error"]
    assert vector._get_precision_beta(AGENT_811) == 0.5
    assert vector._agent_fused_gates[AGENT_811] == 0.05, (
        "the refused change left the gate measured at beta 2.0 in force beside beta 0.5"
    )

    # The next successful calibration of any agent must not persist the unpaired gate.
    monkeypatch.setattr(admin_handlers, "_save_calibration_state", real_save)
    monkeypatch.setattr(admin_handlers, "_calibration_sidecar_locked", real_lock)
    cal = await admin_handlers.do_calibrate_threshold("", method="percentile")
    assert cal["ok"] is True and cal["sidecar_persisted"] is True
    with open(admin_handlers._calibration_sidecar_path()) as fh:
        stored = json.load(fh)
    assert stored["agent_betas"][AGENT_811] == 0.5
    assert stored["agent_fused_gates"][AGENT_811] == 0.05


@pytest.mark.asyncio
async def test_bug_811_accepted_precision_change_applies_gate_and_beta_together(corpus_811):
    await admin_handlers.do_set_recall_precision(AGENT_811, "lenient")
    res = await admin_handlers.do_set_recall_precision(AGENT_811, "strict")
    assert res["ok"] is True
    assert vector._get_precision_beta(AGENT_811) == 2.0
    assert vector._agent_fused_gates[AGENT_811] == 0.2
    with open(admin_handlers._calibration_sidecar_path()) as fh:
        stored = json.load(fh)
    assert stored["agent_betas"][AGENT_811] == 2.0
    assert stored["agent_fused_gates"][AGENT_811] == 0.2
    # A plain calibration afterwards keeps the pair (the gate is not lost from memory).
    cal = await admin_handlers.do_calibrate_threshold("", method="percentile")
    assert cal["sidecar_persisted"] is True
    with open(admin_handlers._calibration_sidecar_path()) as fh:
        stored = json.load(fh)
    assert stored["agent_betas"][AGENT_811] == 2.0
    assert stored["agent_fused_gates"][AGENT_811] == 0.2


# ---------------------------------------------------------------------------
# bug-812
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_812_keyless_pause_during_startup_guard_does_not_skip_agents(clean_store, monkeypatch):
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    await _seed_embeddings(clean_store, "a1", 15)
    await _seed_embeddings(clean_store, "a2", 15)

    real = admin_handlers.do_calibrate_threshold

    async def pause_after_first_agent(agent_id, *args, **kwargs):
        result = await real(agent_id, *args, **kwargs)
        # A client's keyless pause lands while the guard is between agents.
        if agent_id == "a1":
            session.pause_for(session.TRANSPORT_KEY, False, 600)
        return result

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", pause_after_first_agent)
    status = await admin_handlers.ensure_calibrated_on_startup(auto_calibrate=False, on_model_change=True)
    stored = admin_handlers._load_calibration_state()
    assert status["agents"] == ["a1", "a2"]
    missing = [a for a in status["agents"] if a not in (stored.get("agent_thresholds") or {})]
    assert not missing, f"the guard reported {status['agents']} calibrated but the sidecar lacks {missing}"


@pytest.mark.asyncio
async def test_bug_812_keyless_pause_still_skips_a_client_calibration(clean_store):
    await _seed_embeddings(clean_store, "a1", 15)
    session.pause_for(session.TRANSPORT_KEY, False, 600)
    result = await admin_handlers.do_calibrate_threshold("a1")
    assert result.get("persisted") is False and result["new_threshold"] is None
    assert "a1" not in vector._agent_thresholds
    assert not os.path.exists(admin_handlers._calibration_sidecar_path())
