"""Regression tests for the calibration-state fixes in 2.6.12.

Calibration state lives in one JSON sidecar next to the database, shared by every
server process on that database. These tests pin five ways that state could be lost
or poisoned:

  bug-516  an unreadable sidecar was replaced by a runtime calibration with no copy
           kept, a second distinct unreadable file was never copied because the
           backup deduplicated on a fixed tag, and deep_check reported the file as
           never calibrated and recommended the call that destroys it.
  bug-518  a non-finite z_factor was clamped to 10 instead of refused, and a z-score
           threshold above 1.0 (unreachable by any cosine) was applied and persisted.
  bug-521  a long-lived process wrote its boot-time values back over entries another
           process had changed (or removed) since, the next time it wrote the sidecar.
  bug-522  set_recall_precision accepted beta=inf (and NaN), which calibrates the most
           lenient gate and puts the non-JSON token Infinity in responses and on disk.
  bug-523  set_recall_precision answered ok:true when the sidecar write failed, so the
           setting was lost on the next restart without the caller being told.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2612a1_u12.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import errno  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import struct  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import admin_handlers, checks, config, fileperms, session, vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "u12-agent"
DIM = 8


def _reset_calibration_process_state():
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._agent_betas.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._reset_calibration_authority()


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """Point the sidecar at a fresh temp path and isolate every calibration global."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cpersona.db"))
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    _reset_calibration_process_state()
    session.reset_pauses_for_tests()
    yield str(tmp_path / "cpersona.db") + ".calibration.json"
    _reset_calibration_process_state()
    session.reset_pauses_for_tests()
    config.VECTOR_MIN_SIMILARITY = saved_threshold


@pytest_asyncio.fixture
async def corpus():
    """60 embedded memories for AGENT: above the >=10 calibration floor and the
    >=50 rows deep_calibration_staleness treats as worth a warning."""
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    rng = np.random.default_rng(12)
    for i in range(60):
        v = rng.standard_normal(DIM).astype(np.float32)
        v /= float(np.linalg.norm(v))
        await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (AGENT, f"memory number {i}", f"2026-10-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
             struct.pack(f"<{DIM}f", *v.tolist())),
        )
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories WHERE agent_id = ?", (AGENT,))
    await conn.commit()


def _payload(**overrides) -> dict:
    """A complete sidecar payload that a process on this corpus would carry."""
    payload = {
        "embedding_dim": DIM,
        "embedding_model": config.EMBEDDING_MODEL,
        "scoring_version": admin_handlers._live_fingerprint(),
        "global_threshold": 0.5,
        "agent_thresholds": {"other": 0.42},
        "global_fused_gate": None,
        "agent_fused_gates": {"other": 0.33},
        "fused_gate_signal": "confidence",
        "agent_betas": {"other": 2.0},
        "calibrated_at": "2026-10-01T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _write(path: str, payload: dict) -> None:
    with open(path, "w") as fh:
        json.dump(payload, fh)


def _corrupt(path: str, payload: dict, marker: bytes) -> bytes:
    """Write *payload* truncated by one byte plus *marker*: unparseable JSON."""
    raw = json.dumps(payload).encode()[:-1] + marker
    with open(path, "wb") as fh:
        fh.write(raw)
    return raw


def _backup_contents(path: str) -> list[bytes]:
    out = []
    for p in sorted(glob.glob(glob.escape(path) + ".before-*")):
        with open(p, "rb") as fh:
            out.append(fh.read())
    return out


# ==========================================================================
# bug-516 -- an unreadable sidecar must be copied aside before it is replaced.
# ==========================================================================


@pytest.mark.asyncio
async def test_runtime_calibration_backs_up_an_unreadable_sidecar(sidecar, corpus):
    corrupt = _corrupt(sidecar, _payload(), b"#torn")

    result = await admin_handlers.do_calibrate_threshold(AGENT)

    assert result["ok"] is True
    assert corrupt in _backup_contents(sidecar), (
        "calibrate_threshold replaced an unreadable sidecar -- the only copy of every "
        "other agent's thresholds, gates and precision overrides -- without copying the "
        f"bytes aside first (backups: {_backup_contents(sidecar)!r})"
    )


@pytest.mark.asyncio
async def test_runtime_calibration_refuses_to_replace_an_unreadable_sidecar_it_cannot_copy(
    sidecar, corpus, monkeypatch
):
    corrupt = _corrupt(sidecar, _payload(), b"#torn")
    real_open = fileperms.open_private

    def no_backups(path, *a, **k):
        if ".before-" in str(path):
            raise OSError(errno.ENOSPC, "No space left on device", path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(fileperms, "open_private", no_backups)

    result = await admin_handlers.do_calibrate_threshold(AGENT)

    with open(sidecar, "rb") as fh:
        assert fh.read() == corrupt, "the uncopyable unreadable sidecar was overwritten"
    assert result["sidecar_persisted"] is False


@pytest.mark.asyncio
async def test_two_different_unreadable_sidecars_are_both_kept(sidecar, corpus):
    """The backup deduplicates by tag; a fixed 'unreadable' tag made every later
    corrupt file look already copied, so the second one was replaced with no copy."""
    first = _corrupt(sidecar, _payload(agent_betas={"other": 2.0}), b"FIRST")
    await admin_handlers.ensure_calibrated_on_startup(False, True)
    _reset_calibration_process_state()
    second = _corrupt(sidecar, _payload(agent_betas={"other": 3.5}), b"SECOND")
    status = await admin_handlers.ensure_calibrated_on_startup(False, True)

    with open(status["sidecar_backup"], "rb") as fh:
        assert fh.read() == second, "the boot reported another file's backup as this one's"
    kept = _backup_contents(sidecar)
    assert first in kept
    assert second in kept, (
        "the second, different unreadable sidecar was replaced without a copy; the "
        f"backup returned the first file's copy instead ({len(kept)} backup(s) kept)"
    )


@pytest.mark.asyncio
async def test_deep_check_reports_an_unreadable_sidecar_as_unreadable(
    sidecar, corpus, fake_embedding_client
):
    _corrupt(sidecar, _payload(), b"#torn")

    result = await checks.deep_calibration_staleness(corpus, AGENT, False)

    assert result["status"] == "unreadable", (
        f"an unreadable sidecar was reported as {result!r}; 'never_calibrated' sends "
        "the operator to calibrate_threshold, the call that replaces the file"
    )
    assert "run calibrate_threshold" not in result.get("hint", "")


# ==========================================================================
# bug-518 -- a z-score threshold must be finite and reachable.
# ==========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("z", [math.nan, math.inf, -math.inf])
async def test_non_finite_z_factor_is_refused(sidecar, corpus, z):
    result = await admin_handlers.do_calibrate_threshold(AGENT, method="zscore", z_factor=z)

    assert result["ok"] is False, (
        f"z_factor={z} was accepted (reported z_factor={result.get('z_factor')}, "
        f"new_threshold={result.get('new_threshold')})"
    )
    assert AGENT not in vector._agent_thresholds
    assert not os.path.exists(sidecar)


@pytest.mark.asyncio
async def test_a_zscore_threshold_above_one_is_refused_not_installed(sidecar, corpus):
    result = await admin_handlers.do_calibrate_threshold(AGENT, method="zscore", z_factor=10)

    assert result["ok"] is False, (
        f"a cosine threshold of {result.get('new_threshold')} admits no vector at all, "
        "but it was reported ok:true and applied"
    )
    assert AGENT not in vector._agent_thresholds
    assert not os.path.exists(sidecar)


@pytest.mark.asyncio
async def test_a_reachable_zscore_threshold_still_applies(sidecar, corpus):
    result = await admin_handlers.do_calibrate_threshold(AGENT, method="zscore", z_factor=1)

    assert result["ok"] is True
    assert result["new_threshold"] <= 1.0
    assert vector._agent_thresholds[AGENT] == result["new_threshold"]


# ==========================================================================
# bug-521 -- a long-lived process must not revert another process's writes.
# ==========================================================================


def _boot_with(path: str, payload: dict) -> None:
    """What a process that restored the sidecar at boot holds."""
    _write(path, payload)
    admin_handlers._restore_calibration_state(admin_handlers._load_calibration_state())


@pytest.mark.asyncio
async def test_a_value_another_process_changed_is_not_written_back(sidecar, corpus):
    _boot_with(sidecar, _payload(agent_betas={"X": 0.5}, agent_thresholds={"X": 0.4, "Z": 0.3}))
    # Another server process on the same database sets X strict and deletes agent Z.
    _write(sidecar, _payload(agent_betas={"X": 2.0}, agent_thresholds={"X": 0.45}))

    result = await admin_handlers.do_set_recall_precision(AGENT, "strict")

    assert result["ok"] is True
    stored = json.load(open(sidecar))
    assert stored["agent_betas"].get("X") == 2.0, (
        "this process wrote its boot-time beta for X back over the value another "
        f"process persisted since: {stored['agent_betas']!r}"
    )
    assert stored["agent_thresholds"].get("X") == 0.45
    assert "Z" not in stored["agent_thresholds"], "a deleted agent's calibration came back"
    assert stored["agent_betas"][AGENT] == 2.0

    # A second write must not revert it either: what this process adopted is not a
    # change of its own.
    await admin_handlers.do_calibrate_threshold(AGENT)
    stored = json.load(open(sidecar))
    assert stored["agent_betas"].get("X") == 2.0
    assert "Z" not in stored["agent_thresholds"]


@pytest.mark.asyncio
async def test_this_process_own_later_change_still_wins(sidecar, corpus):
    _boot_with(sidecar, _payload(agent_betas={AGENT: 0.5}))
    _write(sidecar, _payload(agent_betas={AGENT: 2.0}))

    result = await admin_handlers.do_set_recall_precision(AGENT, "lenient")

    assert result["ok"] is True
    assert json.load(open(sidecar))["agent_betas"][AGENT] == 0.5

    cleared = await admin_handlers.do_set_recall_precision(AGENT, "")
    assert cleared["ok"] is True and cleared["cleared"] is True
    assert AGENT not in json.load(open(sidecar))["agent_betas"]


@pytest.mark.asyncio
async def test_a_change_already_written_does_not_stay_authoritative(sidecar, corpus):
    """Once this process's own change is on disk, a later change by another process
    to the same entry is the newer one."""
    _boot_with(sidecar, _payload())
    assert (await admin_handlers.do_set_recall_precision(AGENT, "lenient"))["ok"] is True
    stored = json.load(open(sidecar))
    stored["agent_betas"][AGENT] = 2.0
    _write(sidecar, stored)

    await admin_handlers.do_calibrate_threshold(AGENT)

    assert json.load(open(sidecar))["agent_betas"][AGENT] == 2.0


@pytest.mark.asyncio
async def test_a_global_value_another_process_changed_is_not_written_back(sidecar, corpus):
    _boot_with(sidecar, _payload(global_threshold=0.5))
    _write(sidecar, _payload(global_threshold=0.61))

    await admin_handlers.do_calibrate_threshold(AGENT)

    assert json.load(open(sidecar))["global_threshold"] == 0.61


# ==========================================================================
# bug-522 -- set_recall_precision must refuse a non-finite beta.
# ==========================================================================


@pytest.fixture
def _stub_calibrate(monkeypatch):
    calls = []

    async def _fake(agent_id="", **kw):
        calls.append(agent_id)
        return {"ok": True, "sidecar_persisted": True, "scope": "per_agent", "new_threshold": 0.42}

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", _fake)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("precision", ["", "strict"])
@pytest.mark.parametrize("beta", [math.inf, math.nan])
async def test_non_finite_beta_is_refused(sidecar, _stub_calibrate, precision, beta):
    vector._agent_betas[AGENT] = 0.5

    result = await admin_handlers.do_set_recall_precision(AGENT, precision, beta=beta)

    assert result["ok"] is False, f"beta={beta} with precision={precision!r} was accepted: {result!r}"
    assert vector._agent_betas[AGENT] == 0.5, "the stored override changed"
    assert _stub_calibrate == []
    json.dumps(result, allow_nan=False)


# ==========================================================================
# bug-523 -- a failed sidecar write is a failed set_recall_precision.
# ==========================================================================


@pytest.fixture
def full_disk(monkeypatch):
    """Arm a disk-full failure for the sidecar's temp write only (the DB is untouched)."""
    real_open = fileperms.open_private
    armed = {"on": False}

    def maybe_fail(path, *a, **k):
        if armed["on"] and ".calibration.json.tmp" in str(path):
            raise OSError(errno.ENOSPC, "No space left on device", path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(fileperms, "open_private", maybe_fail)
    return armed


@pytest.mark.asyncio
async def test_set_precision_is_not_ok_when_the_sidecar_was_not_written(sidecar, corpus, full_disk):
    full_disk["on"] = True

    result = await admin_handlers.do_set_recall_precision(AGENT, "strict")

    assert result["ok"] is False, (
        f"the sidecar write failed but the call answered {result!r}; the override is "
        "lost on the next restart"
    )
    assert AGENT not in vector._agent_betas, "the un-persisted beta stayed live in memory"


@pytest.mark.asyncio
async def test_clear_precision_is_not_ok_when_the_sidecar_was_not_written(sidecar, corpus, full_disk):
    assert (await admin_handlers.do_set_recall_precision(AGENT, "lenient"))["ok"] is True
    full_disk["on"] = True

    result = await admin_handlers.do_set_recall_precision(AGENT, "")

    assert result["ok"] is False, (
        f"the clear was not written but the call answered {result!r}; the override "
        "returns on the next restart"
    )
    assert vector._agent_betas.get(AGENT) == 0.5
    assert json.load(open(sidecar))["agent_betas"][AGENT] == 0.5
