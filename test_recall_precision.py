"""Tests for per-agent recall precision (knob 3, v2.4.29, Goal #120).

Covers the beta resolver, sidecar round-trip of the per-agent override, and the
set_recall_precision handler's resolution / clear / error / no-persist branches —
all without a resident embedding server (do_calibrate_threshold is monkeypatched so
the simulate-query recall pipeline never runs, matching the v2.4.28 lesson that hot-path
calibration must be exercised with a mocked backend rather than skipped).
"""
import os
import tempfile

import pytest

# admin_handlers imports config which needs a DB path; set one (never opened here).
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(_tmpdir, "test_recall_precision.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import admin_handlers  # noqa: E402
import config  # noqa: E402
import vector  # noqa: E402
from _vendored_mcp_common import no_persist  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    """Isolate every calibration global this module touches (the sidecar round-trip tests
    call _restore_calibration_state, which mutates the fused-gate signal / gates too), so
    nothing leaks into a later test file's do_recall quality gate."""
    saved_global = config.FUSED_GATE_BETA
    _clear()
    yield
    _clear()
    config.FUSED_GATE_BETA = saved_global


def _clear():
    vector._agent_betas.clear()
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_thresholds.clear()


# ---- beta resolver -------------------------------------------------------------


def test_get_precision_beta_falls_back_to_global():
    config.FUSED_GATE_BETA = 1.0
    assert vector._get_precision_beta("nobody") == 1.0
    config.FUSED_GATE_BETA = 2.0
    assert vector._get_precision_beta("nobody") == 2.0  # un-set agents track the env


def test_get_precision_beta_per_agent_override_wins():
    config.FUSED_GATE_BETA = 1.0
    vector._agent_betas["alice"] = 0.5
    assert vector._get_precision_beta("alice") == 0.5
    assert vector._get_precision_beta("bob") == 1.0  # other agents unaffected


# ---- sidecar round-trip --------------------------------------------------------


def test_sidecar_round_trips_agent_betas():
    admin_handlers._save_calibration_state(
        embedding_dim=1024,
        embedding_model="bge-m3",
        global_threshold=0.5,
        agent_thresholds={"alice": 0.55},
        global_fused_gate=None,
        agent_fused_gates={"alice": 0.45},
        fused_gate_signal="confidence",
        agent_betas={"alice": 2.0, "bob": 0.5},
    )
    state = admin_handlers._load_calibration_state()
    assert state["agent_betas"] == {"alice": 2.0, "bob": 0.5}

    vector._agent_betas.clear()
    admin_handlers._restore_calibration_state(state)
    assert vector._agent_betas == {"alice": 2.0, "bob": 0.5}
    os.remove(admin_handlers._calibration_sidecar_path())


def test_restore_tolerates_pre_v2429_sidecar():
    """A sidecar without the agent_betas key leaves every agent on the global default."""
    vector._agent_betas["pre"] = 9.9  # should be untouched (update with {} is a no-op)
    admin_handlers._restore_calibration_state({"global_threshold": 0.5})
    assert vector._agent_betas == {"pre": 9.9}


# ---- set_recall_precision: resolution / clear / error --------------------------


@pytest.fixture
def _stub_calibrate(monkeypatch):
    """Replace do_calibrate_threshold with a recorder so no embeddings are needed."""
    calls = []

    async def _fake(agent_id="", **kw):
        calls.append(agent_id)
        return {"ok": True, "scope": "per_agent", "new_threshold": 0.42, "fused_gate": {"beta": vector._get_precision_beta(agent_id)}}

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", _fake)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("level,beta", [("strict", 2.0), ("balanced", 1.0), ("lenient", 0.5)])
async def test_set_named_precision(_stub_calibrate, level, beta):
    res = await admin_handlers.do_set_recall_precision("alice", level)
    assert res["ok"] is True
    assert res["precision"] == level
    assert res["beta"] == beta
    assert vector._agent_betas["alice"] == beta
    assert _stub_calibrate == ["alice"]  # recalibrated exactly once


@pytest.mark.asyncio
async def test_raw_beta_overrides_named(_stub_calibrate):
    res = await admin_handlers.do_set_recall_precision("alice", precision="strict", beta=1.5)
    assert res["beta"] == 1.5
    assert res["precision"] == "strict"  # the label is preserved, beta wins
    assert vector._agent_betas["alice"] == 1.5


@pytest.mark.asyncio
async def test_clear_override(_stub_calibrate):
    vector._agent_betas["alice"] = 2.0
    res = await admin_handlers.do_set_recall_precision("alice")  # empty + beta<=0 = clear
    assert res["cleared"] is True
    assert res["precision"] == "default"
    assert "alice" not in vector._agent_betas
    assert _stub_calibrate == ["alice"]  # still recalibrated (back to global beta)


@pytest.mark.asyncio
async def test_unknown_precision_errors_without_calibrating(_stub_calibrate):
    res = await admin_handlers.do_set_recall_precision("alice", "agressive")
    assert res["ok"] is False
    assert "Unknown precision" in res["error"]
    assert "alice" not in vector._agent_betas
    assert _stub_calibrate == []  # no recalibration on a rejected input


@pytest.mark.asyncio
async def test_missing_agent_id_errors(_stub_calibrate):
    res = await admin_handlers.do_set_recall_precision("", "strict")
    assert res["ok"] is False
    assert "agent_id" in res["error"]
    assert _stub_calibrate == []


@pytest.mark.asyncio
async def test_no_persist_skips(_stub_calibrate, monkeypatch):
    monkeypatch.setattr(no_persist, "is_paused", lambda: True)
    res = await admin_handlers.do_set_recall_precision("alice", "strict")
    assert res.get("persisted") is False
    assert "alice" not in vector._agent_betas  # no state change while paused
    assert _stub_calibrate == []
