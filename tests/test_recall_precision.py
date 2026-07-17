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

from cpersona import admin_handlers # noqa: E402
from cpersona import config # noqa: E402
from cpersona import vector # noqa: E402
from cpersona._vendored_mcp_common import no_persist  # noqa: E402


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
async def test_failed_calibration_rolls_back_override(monkeypatch):
    """If calibration can't run (ok=False), the in-memory override must not be left set —
    otherwise it diverges from the sidecar, which do_calibrate_threshold never saved."""
    async def _fail(agent_id="", **kw):
        return {"ok": False, "error": "Need at least 10 embeddings, found 3"}
    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", _fail)

    res = await admin_handlers.do_set_recall_precision("tiny", "strict")
    assert res["ok"] is False
    assert "tiny" not in vector._agent_betas  # rolled back, no divergence


@pytest.mark.asyncio
async def test_failed_calibration_restores_prior_override(monkeypatch):
    """A failed re-set restores the previous override rather than dropping it."""
    vector._agent_betas["alice"] = 2.0
    async def _fail(agent_id="", **kw):
        return {"ok": False, "error": "boom"}
    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", _fail)

    res = await admin_handlers.do_set_recall_precision("alice", "lenient")
    assert res["ok"] is False
    assert vector._agent_betas["alice"] == 2.0  # prior value preserved


@pytest.mark.asyncio
async def test_no_persist_skips(_stub_calibrate, monkeypatch):
    monkeypatch.setattr(no_persist, "is_paused", lambda: True)
    res = await admin_handlers.do_set_recall_precision("alice", "strict")
    assert res.get("persisted") is False
    assert "alice" not in vector._agent_betas  # no state change while paused


# ---- get_recall_precision: read-back -------------------------------------------


def test_precision_label_inverts_named_betas():
    assert admin_handlers._precision_label(2.0) == "strict"
    assert admin_handlers._precision_label(1.0) == "balanced"
    assert admin_handlers._precision_label(0.5) == "lenient"
    assert admin_handlers._precision_label(1.5) == "custom"  # raw beta has no named level


@pytest.mark.asyncio
async def test_get_precision_reports_global_default_when_unset():
    config.FUSED_GATE_BETA = 1.0
    res = await admin_handlers.do_get_recall_precision("nobody")
    assert res["ok"] is True
    assert res["overridden"] is False
    assert res["beta"] == 1.0
    assert res["precision"] == "balanced"
    assert res["global_precision"] == "balanced"
    assert res["global_beta"] == 1.0


@pytest.mark.asyncio
async def test_get_precision_reports_per_agent_override():
    config.FUSED_GATE_BETA = 1.0
    vector._agent_betas["alice"] = 2.0
    res = await admin_handlers.do_get_recall_precision("alice")
    assert res["overridden"] is True
    assert res["precision"] == "strict"
    assert res["beta"] == 2.0
    assert res["global_precision"] == "balanced"  # global still the default


@pytest.mark.asyncio
async def test_get_precision_labels_raw_beta_as_custom():
    vector._agent_betas["alice"] = 1.5
    res = await admin_handlers.do_get_recall_precision("alice")
    assert res["overridden"] is True
    assert res["precision"] == "custom"
    assert res["beta"] == 1.5


@pytest.mark.asyncio
async def test_get_precision_missing_agent_id_errors():
    res = await admin_handlers.do_get_recall_precision("")
    assert res["ok"] is False
    assert "agent_id" in res["error"]


@pytest.mark.asyncio
async def test_get_precision_is_read_only_under_no_persist(monkeypatch):
    """Read-back is unaffected by no-persist pause and never mutates state (like recall)."""
    monkeypatch.setattr(no_persist, "is_paused", lambda: True)
    res = await admin_handlers.do_get_recall_precision("nobody")
    assert res["ok"] is True
    assert vector._agent_betas == {}  # pure read, no override created
