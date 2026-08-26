"""A staleness recalibration must leave evidence of what it replaced.

Two production boots showed the same hole from two sides:

- 2.5.4a3 (2026-08-14): the scoring-stale recalibration overwrote the sidecar in
  place. The recall behaviour changed (5 rows -> 2 on the same query) and the
  question "did the gate loosen or tighten?" was UNANSWERABLE — the previous
  effective values existed nowhere. The only ``.before-*`` files next to the
  sidecar were hand-made copies from earlier deploys.
- b1266d2 (2026-08-17): the recalibration collapsed the fused gate 0.4470 ->
  0.1544 (a single optimiser draw landing on a secondary peak) with ZERO log
  lines — the boot log only reports cosine thresholds, and its "from" value is
  the runtime default (the restore was skipped), not anything that ever gated a
  query. Diagnosis was possible only because the sidecar had been copied by hand
  before the boot.

The fix under test: ``ensure_calibrated_on_startup`` (1) backs the sidecar up to
``<sidecar>.before-<old_scoring_version>-<UTC>`` before a staleness recalibration
replaces it, and (2) logs the three quantities an operator needs — stored
(effective until this boot), runtime default (what the "from" in the
per-calibration lines actually is), and new — for the cosine threshold and the
fused gates, plus a machine-readable ``calibration_replaced`` in the status dict.

Scope is pinned deliberately: routine boots (restore, plain AUTO_CALIBRATE with a
current fingerprint, first run) must NOT produce backups — the backup marks
"stored evidence is about to be destroyed by an upgrade", not "calibration ran".

Fixture style follows ``test_gate_remediation`` (same sidecar redirection, same
seeding helper).
"""

from __future__ import annotations

import glob
import json
import logging
import os

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config, vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db
from cpersona.utils import SCORING_VERSION

AGENT = "agent.recal-evidence"


def _reset_calibration_globals() -> None:
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3


@pytest_asyncio.fixture(autouse=True)
async def _fresh_state(tmp_path, monkeypatch):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    _reset_calibration_globals()
    monkeypatch.setattr(
        admin_handlers,
        "_calibration_sidecar_path",
        lambda: os.path.join(str(tmp_path), "sidecar.calibration.json"),
    )
    yield
    _reset_calibration_globals()


async def _seed_embeddings(db, agent_id: str, count: int, dim: int = 8) -> None:
    for i in range(count):
        vec = [float((i + j) % 5) - 2.0 for j in range(dim)]
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            (agent_id, f"memory {i}", "2026-05-14T00:00:00Z", EmbeddingClient.pack_embedding(vec)),
        )
    await db.commit()


def _stale_sidecar() -> dict:
    return {
        "embedding_dim": 8,
        "embedding_model": "bge-m3",
        "scoring_version": "ancient-scoring",
        "global_threshold": 0.5586,
        "agent_thresholds": {AGENT: 0.5979},
        "global_fused_gate": 0.9,
        "agent_fused_gates": {AGENT: 0.4470},
        "fused_gate_signal": "confidence",
        "agent_betas": {},
        "calibrated_at": "2026-07-01T00:00:00+00:00",
    }


def _write_sidecar(payload: dict) -> None:
    with open(admin_handlers._calibration_sidecar_path(), "w") as fh:
        json.dump(payload, fh)


def _backup_files() -> list[str]:
    return sorted(glob.glob(admin_handlers._calibration_sidecar_path() + ".before-*"))


@pytest.mark.asyncio
async def test_staleness_recalibration_backs_up_the_sidecar_and_logs_three_values(caplog):
    """The upgrade boot: backup file, three-value log, machine-readable report."""
    db = await get_db()
    await _seed_embeddings(db, AGENT, 15, dim=8)
    original = _stale_sidecar()
    _write_sidecar(original)

    with caplog.at_level(logging.WARNING, logger="cpersona.admin_handlers"):
        status = await admin_handlers.ensure_calibrated_on_startup(
            auto_calibrate=False, on_model_change=True
        )
    assert status["action"] == "recalibrated_scoring", status

    # (a) The sidecar was copied aside BEFORE the overwrite, byte-identical, named
    # after the scoring version whose evidence it preserves.
    backups = _backup_files()
    assert len(backups) == 1, f"expected exactly one backup, got {backups}"
    assert ".before-ancient-scoring-" in backups[0]
    with open(backups[0]) as fh:
        assert json.load(fh) == original, (
            "the backup does not match the pre-boot sidecar — it preserves nothing"
        )
    assert status["sidecar_backup"] == backups[0]

    # The live sidecar moved on: re-stamped with the current fingerprint.
    assert admin_handlers._load_calibration_state()["scoring_version"] == SCORING_VERSION

    # (b) The status dict carries stored / runtime-default / new for the threshold
    # and stored / new for the gates.
    replaced = status["calibration_replaced"]
    assert replaced["stored_global_threshold"] == 0.5586
    assert replaced["runtime_default_threshold"] == 0.3
    assert isinstance(replaced["new_global_threshold"], float)
    assert replaced["stored_agent_fused_gates"] == {AGENT: 0.4470}
    assert replaced["stored_agent_thresholds"] == {AGENT: 0.5979}
    assert AGENT in replaced["new_agent_thresholds"]

    # (b) The log line reports all three quantities — the stored value marked as the
    # one that was effective, the runtime default the per-calibration lines call
    # "old", and the new value — and it covers the fused gate, which produced zero
    # log lines in the 2026-08-17 incident.
    report = next(
        (r.message for r in caplog.records if "replaced the stored calibration" in r.message),
        None,
    )
    assert report is not None, "no replacement report was logged"
    assert "stored=0.5586" in report
    assert "runtime_default=0.3000" in report
    assert "new=" in report
    assert "fused_gate 0.4470 ->" in report
    assert backups[0] in report, "the report does not say where the evidence went"


@pytest.mark.asyncio
async def test_restore_boot_produces_no_backup():
    """A current sidecar restores as before — no backup, no replacement report."""
    db = await get_db()
    await _seed_embeddings(db, AGENT, 15, dim=8)
    sidecar = _stale_sidecar() | {"scoring_version": SCORING_VERSION}
    _write_sidecar(sidecar)

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )
    assert status["action"] == "restored", status
    assert _backup_files() == []


@pytest.mark.asyncio
async def test_routine_auto_calibrate_produces_no_backup():
    """AUTO_CALIBRATE with a CURRENT fingerprint re-measures every boot; that is
    routine drift, not evidence destruction, and must not litter backups."""
    db = await get_db()
    await _seed_embeddings(db, AGENT, 15, dim=8)
    _write_sidecar(_stale_sidecar() | {"scoring_version": SCORING_VERSION})

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=True, on_model_change=True
    )
    assert status["action"] == "auto", status
    assert _backup_files() == []
    assert status["sidecar_backup"] is None
    assert status["calibration_replaced"] is None


@pytest.mark.asyncio
async def test_backup_failure_does_not_block_the_recalibration(monkeypatch, caplog):
    """The backup is evidence, not a gate: an unwritable backup path must degrade to
    a warning while the recalibration itself proceeds."""
    db = await get_db()
    await _seed_embeddings(db, AGENT, 15, dim=8)
    _write_sidecar(_stale_sidecar())

    real_open = open

    def failing_open(path, *args, **kwargs):
        if ".before-" in str(path):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    with caplog.at_level(logging.WARNING, logger="cpersona.admin_handlers"):
        status = await admin_handlers.ensure_calibrated_on_startup(
            auto_calibrate=False, on_model_change=True
        )
    assert status["action"] == "recalibrated_scoring", status
    assert status["sidecar_backup"] is None
    assert _backup_files() == []
    # Case-insensitive: the assertion is about what the operator is told, not
    # about where the sentence happens to start.
    assert any("could not back up" in r.message.lower() for r in caplog.records)
    # The recalibration still happened and was persisted.
    assert admin_handlers._load_calibration_state()["scoring_version"] == SCORING_VERSION
