"""Regression tests for the calibration sidecar under concurrency (2.6.11a2).

The calibration state lives in process dicts and in one JSON sidecar next to the
database. A calibration awaits for seconds to minutes between reading the corpus
and writing what it measured, and since the startup guard runs beside serving,
other tools run inside those awaits. These tests pin four ways that went wrong:

  bug-611  a sidecar rewrite that measured nothing (dropping a deleted agent) stamped
           the file as calibrated now, so deep_check reported a stale calibration of
           every other agent as fresh.
  bug-612  a calibration in flight wrote the threshold and gate of an agent whose data
           delete_agent_data purged during its awaits back into memory and onto disk;
           the startup restore did the same from a sidecar it read before an await.
  bug-613  a set_recall_precision that failed and answered ok:false had its beta
           written to the sidecar by another agent's calibration that saved while the
           beta was tentative, so the next restart applied it.
  bug-726  a calibration that measured an agent's gate at the old beta replaced the
           gate set_recall_precision had just measured at the new one, leaving the new
           beta paired with the old gate in memory and in the sidecar.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x02.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import datetime  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import admin_handlers, checks, config, session, vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "x02-agent"
TINY = "x02-tiny"
DIM = 8
OLD_STAMP = (
    datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=200)
).isoformat()


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


async def _insert(conn, agent_id: str, count: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    for i in range(count):
        v = rng.standard_normal(DIM).astype(np.float32)
        v /= float(np.linalg.norm(v))
        await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (agent_id, f"{agent_id} memory {i}", f"2026-10-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
             struct.pack(f"<{DIM}f", *v.tolist())),
        )


@pytest_asyncio.fixture
async def corpus():
    """60 embedded memories for AGENT and 5 for TINY (below the calibration floor)."""
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await _insert(conn, AGENT, 60, 26)
    await _insert(conn, TINY, 5, 27)
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories WHERE agent_id IN (?, ?)", (AGENT, TINY))
    await conn.commit()


def _payload(**overrides) -> dict:
    """A complete, current sidecar payload for this corpus."""
    payload = {
        "embedding_dim": DIM,
        "embedding_model": config.EMBEDDING_MODEL,
        "scoring_version": admin_handlers._live_fingerprint(),
        "global_threshold": 0.5,
        "agent_thresholds": {AGENT: 0.41, "other": 0.42},
        "global_fused_gate": None,
        "agent_fused_gates": {AGENT: 0.31, "other": 0.33},
        "fused_gate_signal": "rsf",
        "agent_betas": {AGENT: 0.5, "other": 2.0},
        "calibrated_at": OLD_STAMP,
    }
    payload.update(overrides)
    return payload


def _write(path: str, payload: dict) -> None:
    with open(path, "w") as fh:
        json.dump(payload, fh)


def _read(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _stored_axes_for(path: str, agent_id: str) -> dict:
    """The agent's entry on each per-agent axis of the sidecar (absent ones omitted)."""
    if not os.path.exists(path):
        return {}
    state = _read(path)
    return {
        axis: state[axis][agent_id]
        for axis in vector._CALIBRATION_AGENT_AXES
        if agent_id in (state.get(axis) or {})
    }


def _live_axes_for(agent_id: str) -> dict:
    live = {
        "agent_thresholds": vector._agent_thresholds,
        "agent_fused_gates": vector._agent_fused_gates,
        "agent_betas": vector._agent_betas,
    }
    return {axis: d[agent_id] for axis, d in live.items() if agent_id in d}


def _gate_at(beta: float) -> dict:
    """A fused-gate measurement whose threshold names the beta it was taken at."""
    return {"threshold": round(0.1 * beta, 4), "signal": "rsf", "beta": beta}


# ==========================================================================
# bug-611 -- a rewrite that measured nothing keeps the stored calibration time.
# ==========================================================================


@pytest.mark.asyncio
async def test_dropping_an_agent_keeps_the_stored_calibration_time(sidecar, corpus, monkeypatch):
    _write(sidecar, _payload(agent_thresholds={AGENT: 0.41, "scratch": 0.6}))
    monkeypatch.setattr(vector, "_embedding_client", object())
    before = await checks.deep_calibration_staleness(corpus, AGENT, False)
    assert before["status"] == "stale", before

    await admin_handlers.do_delete_agent_data("scratch")

    stored = _read(sidecar)
    assert "scratch" not in stored["agent_thresholds"], "the purge did not rewrite the sidecar"
    assert stored["calibrated_at"] == OLD_STAMP, (
        "dropping one agent's entries re-measured nothing, but the rewrite stamped the "
        f"sidecar as calibrated at {stored['calibrated_at']}"
    )
    after = await checks.deep_calibration_staleness(corpus, AGENT, False)
    assert after["status"] == "stale", (
        f"{AGENT}'s 200-day-old calibration reads as fresh after an unrelated agent was "
        f"deleted: {after!r}"
    )


@pytest.mark.asyncio
async def test_a_move_that_empties_its_source_keeps_the_stored_calibration_time(sidecar, corpus):
    """The merge move tears the emptied source down through the same rewrite."""
    _write(sidecar, _payload(agent_thresholds={AGENT: 0.41, TINY: 0.6}))

    result = await admin_handlers.do_merge_memories(TINY, "x02-target", mode="move")

    assert result.get("ok", True) is not False, result
    stored = _read(sidecar)
    assert TINY not in stored["agent_thresholds"], "the move did not purge the emptied source"
    assert stored["calibrated_at"] == OLD_STAMP, stored["calibrated_at"]
    await corpus.execute("DELETE FROM memories WHERE agent_id = 'x02-target'")
    await corpus.commit()


@pytest.mark.asyncio
async def test_a_calibration_still_stamps_the_time_it_measured(sidecar, corpus, monkeypatch):
    _write(sidecar, _payload())

    result = await admin_handlers.do_calibrate_threshold(AGENT, method="percentile")

    assert result["ok"] is True and result["sidecar_persisted"] is True, result
    stamp = datetime.datetime.fromisoformat(_read(sidecar)["calibrated_at"])
    assert datetime.datetime.now(datetime.timezone.utc) - stamp < datetime.timedelta(minutes=5)


# ==========================================================================
# bug-612 -- a delete that lands inside a calibration's awaits stays deleted.
# ==========================================================================


@pytest.mark.asyncio
async def test_a_delete_during_the_sample_window_is_not_undone_by_the_calibration(
    sidecar, corpus, monkeypatch
):
    _write(sidecar, _payload())
    real = admin_handlers._temporal_adjacency_sims

    async def delete_then_read(db, agent_id, *args, **kwargs):
        # The delete lands between the sample read and the positive-proxy read.
        await admin_handlers.do_delete_agent_data(AGENT)
        return await real(db, agent_id, *args, **kwargs)

    monkeypatch.setattr(admin_handlers, "_temporal_adjacency_sims", delete_then_read)

    result = await admin_handlers.do_calibrate_threshold(AGENT, method="separation")

    assert _live_axes_for(AGENT) == {}, (
        f"the calibration wrote the deleted agent's state back: {_live_axes_for(AGENT)!r}"
    )
    assert _stored_axes_for(sidecar, AGENT) == {}, (
        f"the sidecar holds the deleted agent again: {_stored_axes_for(sidecar, AGENT)!r}"
    )
    assert result["ok"] is False, f"a discarded measurement was reported as applied: {result!r}"
    assert "deleted" in result["error"]


@pytest.mark.asyncio
async def test_a_delete_during_the_gate_measurement_is_not_undone_by_the_calibration(
    sidecar, corpus, monkeypatch
):
    _write(sidecar, _payload())
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)

    async def delete_then_measure(db, agent_id, sample_queries, window_min, beta, floor, draws=None):
        # The threshold is already applied when the gate measurement starts.
        assert agent_id in vector._agent_thresholds
        await admin_handlers.do_delete_agent_data(AGENT)
        return _gate_at(beta)

    monkeypatch.setattr(admin_handlers, "_calibrate_fused_gate_median", delete_then_measure)

    result = await admin_handlers.do_calibrate_threshold(AGENT, method="percentile")

    assert _live_axes_for(AGENT) == {}, (
        f"the calibration wrote the deleted agent's gate back: {_live_axes_for(AGENT)!r}"
    )
    assert _stored_axes_for(sidecar, AGENT) == {}, _stored_axes_for(sidecar, AGENT)
    assert result["ok"] is False, result


@pytest.mark.asyncio
async def test_a_precision_change_racing_a_delete_reports_failure_and_leaves_nothing(
    sidecar, corpus, monkeypatch
):
    real = admin_handlers._temporal_adjacency_sims

    async def delete_then_read(db, agent_id, *args, **kwargs):
        await admin_handlers.do_delete_agent_data(AGENT)
        return await real(db, agent_id, *args, **kwargs)

    monkeypatch.setattr(admin_handlers, "_temporal_adjacency_sims", delete_then_read)
    monkeypatch.setattr(admin_handlers, "CALIBRATE_METHOD", "separation")

    result = await admin_handlers.do_set_recall_precision(AGENT, "strict")

    assert result["ok"] is False, (
        f"set_recall_precision answered {result!r} for an agent whose data was deleted "
        "while it measured, and whose override the delete removed"
    )
    assert _live_axes_for(AGENT) == {}, _live_axes_for(AGENT)
    assert _stored_axes_for(sidecar, AGENT) == {}, _stored_axes_for(sidecar, AGENT)


@pytest.mark.asyncio
async def test_the_startup_restore_does_not_bring_back_an_agent_deleted_while_it_awaited(
    sidecar, corpus, monkeypatch
):
    _write(sidecar, _payload())

    async def delete_during_the_width_probe():
        # bug-258: the guard runs beside serving, so a delete can land in its first await.
        await admin_handlers.do_delete_agent_data(AGENT)
        return DIM

    monkeypatch.setattr(admin_handlers, "_corpus_embedding_dim", delete_during_the_width_probe)

    status = await admin_handlers.ensure_calibrated_on_startup(False, True)

    assert status["action"] == "restored", status
    assert _live_axes_for(AGENT) == {}, (
        f"the restore applied a sidecar read before the delete: {_live_axes_for(AGENT)!r}"
    )
    assert vector._agent_thresholds.get("other") == 0.42, "the restore itself did not run"


@pytest.mark.asyncio
async def test_the_startup_restore_honours_a_purge_whose_rewrite_failed(
    sidecar, corpus, monkeypatch
):
    _write(sidecar, _payload())
    real_save = admin_handlers._save_calibration_state
    monkeypatch.setattr(admin_handlers, "_save_calibration_state", lambda *a, **k: False)
    await admin_handlers.do_delete_agent_data(AGENT)
    monkeypatch.setattr(admin_handlers, "_save_calibration_state", real_save)
    assert _stored_axes_for(sidecar, AGENT), "the failed rewrite left the agent in the file"

    status = await admin_handlers.ensure_calibrated_on_startup(False, True)

    assert status["action"] == "restored", status
    assert _live_axes_for(AGENT) == {}, (
        "the restore brought back the calibration of an agent this process deleted, "
        f"because the removal had not reached the file yet: {_live_axes_for(AGENT)!r}"
    )
    assert vector._agent_betas.get("other") == 2.0, "the restore itself did not run"


@pytest.mark.asyncio
async def test_the_startup_restore_leaves_a_measured_unsaved_value_alone(sidecar, corpus):
    """A calibration that has measured and not yet saved is newer than the file."""
    _write(sidecar, _payload(global_threshold=0.5, fused_gate_signal="rsf"))
    config.VECTOR_MIN_SIMILARITY = 0.77
    vector._record_global_calibration_change("global_threshold")
    vector._agent_thresholds[AGENT] = 0.66
    vector._record_agent_calibration_change(AGENT, "agent_thresholds")

    status = await admin_handlers.ensure_calibrated_on_startup(False, True)

    assert status["action"] == "restored", status
    assert config.VECTOR_MIN_SIMILARITY == 0.77, (
        "the restore replaced a measured global threshold with the stored one"
    )
    assert vector._agent_thresholds[AGENT] == 0.66
    assert vector._agent_fused_gates[AGENT] == 0.31, "the restore itself did not run"


# ==========================================================================
# bug-613 -- a failed precision change never reaches the sidecar.
# ==========================================================================


@pytest.mark.asyncio
async def test_a_failed_precision_change_is_not_persisted_by_another_agents_calibration(
    sidecar, corpus, monkeypatch
):
    other_saved = asyncio.Event()
    real_sample = admin_handlers._sample_embeddings

    async def sample(db, agent_id, sample_n):
        if agent_id == TINY:
            # TINY's read queues behind AGENT's calibration, which saves first.
            await other_saved.wait()
        return await real_sample(db, agent_id, sample_n)

    monkeypatch.setattr(admin_handlers, "_sample_embeddings", sample)

    precision = asyncio.create_task(admin_handlers.do_set_recall_precision(TINY, "strict"))
    await asyncio.sleep(0)
    calibrated = await admin_handlers.do_calibrate_threshold(AGENT, method="percentile")
    other_saved.set()
    result = await precision

    assert calibrated["ok"] is True and calibrated["sidecar_persisted"] is True, calibrated
    assert result["ok"] is False and "embeddings" in result["error"], result
    stored_betas = _read(sidecar)["agent_betas"]
    assert TINY not in stored_betas, (
        f"the sidecar holds {TINY}: {stored_betas[TINY]} although set_recall_precision "
        f"answered {result!r}"
    )
    assert TINY not in vector._agent_betas
    # What the next restart would apply.
    _reset_calibration_process_state()
    admin_handlers._restore_calibration_state(_read(sidecar))
    read_back = await admin_handlers.do_get_recall_precision(TINY)
    assert read_back["overridden"] is False, read_back


@pytest.mark.asyncio
async def test_a_successful_precision_change_is_live_and_persisted(sidecar, corpus, monkeypatch):
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)

    async def measure(db, agent_id, sample_queries, window_min, beta, floor, draws=None):
        return _gate_at(beta)

    monkeypatch.setattr(admin_handlers, "_calibrate_fused_gate_median", measure)
    vector._agent_betas[AGENT] = 0.5
    vector._record_agent_calibration_change(AGENT, "agent_betas")

    result = await admin_handlers.do_set_recall_precision(AGENT, "strict")

    assert result["ok"] is True and result["fused_gate"]["threshold"] == 0.2, result
    assert vector._agent_betas[AGENT] == 2.0
    assert vector._agent_fused_gates[AGENT] == 0.2
    stored = _read(sidecar)
    assert stored["agent_betas"][AGENT] == 2.0 and stored["agent_fused_gates"][AGENT] == 0.2

    cleared = await admin_handlers.do_set_recall_precision(AGENT)

    assert cleared["ok"] is True and cleared["cleared"] is True, cleared
    assert AGENT not in vector._agent_betas
    assert vector._agent_fused_gates[AGENT] == _gate_at(config.FUSED_GATE_BETA)["threshold"]
    stored = _read(sidecar)
    assert AGENT not in stored["agent_betas"], stored["agent_betas"]


# ==========================================================================
# bug-726 -- a gate measured at an older beta does not replace the current one.
# ==========================================================================


@pytest.fixture
def held_gate(monkeypatch):
    """Gate measurement that holds the run measuring at the default beta until released."""
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def measure(db, agent_id, sample_queries, window_min, beta, floor, draws=None):
        if beta == config.FUSED_GATE_BETA:
            entered.set()
            await release.wait()
        return _gate_at(beta)

    monkeypatch.setattr(admin_handlers, "_calibrate_fused_gate_median", measure)
    return entered, release


@pytest.mark.asyncio
async def test_a_gate_measured_at_the_old_beta_does_not_replace_the_one_precision_applied(
    sidecar, corpus, held_gate
):
    entered, release = held_gate
    startup = asyncio.create_task(admin_handlers.do_calibrate_threshold(AGENT, method="percentile"))
    await entered.wait()

    result = await admin_handlers.do_set_recall_precision(AGENT, "strict")
    release.set()
    await startup

    assert result["ok"] is True and result["fused_gate"]["threshold"] == 0.2, result
    assert vector._get_precision_beta(AGENT) == 2.0
    assert vector._agent_fused_gates[AGENT] == 0.2, (
        "the live gate was replaced by one measured at beta "
        f"{config.FUSED_GATE_BETA} while the agent's beta is 2.0: "
        f"{vector._agent_fused_gates[AGENT]}"
    )
    stored = _read(sidecar)
    assert stored["agent_betas"][AGENT] == 2.0
    assert stored["agent_fused_gates"][AGENT] == 0.2, (
        f"the sidecar pairs beta 2.0 with the gate {stored['agent_fused_gates'][AGENT]}"
    )


@pytest.mark.asyncio
async def test_a_gate_applied_in_the_window_before_the_save_is_not_left_paired_with_the_new_beta(
    sidecar, corpus, held_gate, monkeypatch
):
    """The precision change applies its gate and its beta in the same step."""
    entered, release = held_gate
    real_bg = admin_handlers.background_connection
    held = asyncio.Event()
    go = asyncio.Event()

    @contextlib.asynccontextmanager
    async def background_connection():
        async with real_bg() as db:
            yield db
        if asyncio.current_task().get_name() == "precision":
            # The precision change has measured its gate and not yet saved it.
            held.set()
            await go.wait()

    monkeypatch.setattr(admin_handlers, "background_connection", background_connection)

    startup = asyncio.create_task(admin_handlers.do_calibrate_threshold(AGENT, method="percentile"))
    await entered.wait()
    precision = asyncio.create_task(
        admin_handlers.do_set_recall_precision(AGENT, "strict"), name="precision"
    )
    await held.wait()
    release.set()
    await startup
    go.set()
    result = await precision

    assert result["ok"] is True and result["fused_gate"]["threshold"] == 0.2, result
    assert vector._get_precision_beta(AGENT) == 2.0
    assert vector._agent_fused_gates[AGENT] == 0.2, vector._agent_fused_gates[AGENT]
    stored = _read(sidecar)
    assert (stored["agent_betas"][AGENT], stored["agent_fused_gates"][AGENT]) == (2.0, 0.2)


@pytest.mark.asyncio
async def test_a_gate_whose_beta_did_not_change_is_still_applied(sidecar, corpus, held_gate):
    entered, release = held_gate
    release.set()

    result = await admin_handlers.do_calibrate_threshold(AGENT, method="percentile")

    expected = _gate_at(config.FUSED_GATE_BETA)["threshold"]
    assert result["ok"] is True and result["fused_gate"]["threshold"] == expected, result
    assert vector._agent_fused_gates[AGENT] == expected
    assert _read(sidecar)["agent_fused_gates"][AGENT] == expected
