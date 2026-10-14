"""Regression tests for configuration parsing and calibration-sidecar fixes in 2.6.11a2.

  bug-517  CPERSONA_CALIBRATE_METHOD and CPERSONA_RECALL_MODE were read raw. A
           mis-cased or padded value ('Separation', 'RSF', 'rrf ') made every
           calibration that omits `method` fail, or silently sent every recall down
           the cascade path, with no warning.
  bug-520  a calibration persisted the width of its own sample as the sidecar's
           embedding_dim, while the startup guard compares it with the corpus-wide
           modal width. On a mixed-width corpus, calibrating an agent whose width is
           not the corpus mode flipped the next boot to "dimension changed", every
           boot after it too, and dropped the other agents' carried entries.
  bug-562  a malformed CPERSONA_RECONSTRUCT_FORCED_COUNT / FORCED_BUDGET became an
           active operator override (1 item / 4000 characters) instead of staying
           unset, and a malformed DEFAULT_COUNT fell back to 10 rather than following
           MAX_COUNT down, so startup refused a value the operator never wrote.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_w02.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import importlib  # noqa: E402
import struct  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import admin_handlers, config, reconstruct, session, vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402


def _reload(monkeypatch, **env):
    """Re-read config with *env* set (a value of None unsets the variable)."""
    with monkeypatch.context() as m:
        for name, value in env.items():
            if value is None:
                m.delenv(name, raising=False)
            else:
                m.setenv(name, value)
        return importlib.reload(config)


@pytest.fixture
def fresh_config():
    yield
    importlib.reload(config)


def _warned(caplog, name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and name in r.getMessage()]


# ---------------------------------------------------------------------------
# bug-517 -- the two word-valued settings that skipped _parse_choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("Separation", "separation"), ("percentile ", "percentile"), (" ZSCORE", "zscore")],
)
def test_calibrate_method_is_trimmed_and_case_folded(monkeypatch, fresh_config, raw, expected):
    cfg = _reload(monkeypatch, CPERSONA_CALIBRATE_METHOD=raw)
    assert cfg.CALIBRATE_METHOD == expected
    # The handler refuses anything outside the enum, so the parsed default must be in it.
    assert cfg.CALIBRATE_METHOD in cfg.CALIBRATE_METHODS


def test_unknown_calibrate_method_warns_and_falls_back(monkeypatch, fresh_config, caplog):
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_CALIBRATE_METHOD="z-score")
    assert cfg.CALIBRATE_METHOD == "separation"
    assert _warned(caplog, "CPERSONA_CALIBRATE_METHOD"), "an unknown method fell back silently"


@pytest.mark.asyncio
async def test_mis_cased_calibrate_method_still_calibrates(monkeypatch, tmp_path, mixed_corpus):
    """End to end: the value config produces is the one every method-less call uses
    (the startup guard and set_recall_precision both omit `method`)."""
    try:
        parsed = _reload(monkeypatch, CPERSONA_CALIBRATE_METHOD="Percentile").CALIBRATE_METHOD
    finally:
        importlib.reload(config)  # back to the suite's environment before patching it
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cpersona.db"))
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)
    monkeypatch.setattr(admin_handlers, "CALIBRATE_METHOD", parsed)
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    _reset_calibration_process_state()
    try:
        result = await admin_handlers.do_calibrate_threshold(agent_id="w02-a")
        assert result["ok"] is True, result
        assert result["method"] == "percentile"
    finally:
        _reset_calibration_process_state()
        config.VECTOR_MIN_SIMILARITY = saved_threshold


@pytest.mark.parametrize(
    "raw, expected",
    [("RSF", "rsf"), ("rrf ", "rrf"), (" Cascade", "cascade"), ("rsf", "rsf")],
)
def test_recall_mode_is_trimmed_and_case_folded(monkeypatch, fresh_config, raw, expected):
    assert _reload(monkeypatch, CPERSONA_RECALL_MODE=raw).RECALL_MODE == expected


def test_unknown_recall_mode_warns_and_falls_back_to_rrf(monkeypatch, fresh_config, caplog):
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_RECALL_MODE="rfs")
    assert cfg.RECALL_MODE == "rrf"
    assert _warned(caplog, "CPERSONA_RECALL_MODE"), "an unknown recall mode fell back silently"


# ---------------------------------------------------------------------------
# bug-562 -- a malformed forced value is an ignored override, not an active one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["ten", "5 items", " "])
def test_malformed_forced_count_stays_unset(monkeypatch, fresh_config, caplog, raw):
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_RECONSTRUCT_FORCED_COUNT=raw)
    assert cfg.RECONSTRUCT_FORCED_COUNT is None
    msgs = _warned(caplog, "CPERSONA_RECONSTRUCT_FORCED_COUNT")
    assert msgs and any("ignored" in m for m in msgs), msgs
    # reconstruct reads config at call time: the caller's count is the base again.
    effective, policy = reconstruct.resolve_count(5)
    assert effective == 5
    assert policy.get("source") != "operator_forced", policy


def test_malformed_forced_budget_stays_unset(monkeypatch, fresh_config, caplog):
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_RECONSTRUCT_FORCED_BUDGET="4k")
    assert cfg.RECONSTRUCT_FORCED_BUDGET is None
    msgs = _warned(caplog, "CPERSONA_RECONSTRUCT_FORCED_BUDGET")
    assert msgs and any("ignored" in m for m in msgs), msgs
    budget, policy = reconstruct.resolve_budget(12000, 10)
    assert budget == 12000
    assert policy.get("source") != "operator_forced", policy


def test_malformed_default_count_follows_a_lowered_maximum(monkeypatch, fresh_config):
    cfg = _reload(
        monkeypatch,
        CPERSONA_RECONSTRUCT_MAX_COUNT="5",
        CPERSONA_RECONSTRUCT_DEFAULT_COUNT="five",
    )
    assert cfg.RECONSTRUCT_DEFAULT_COUNT == 5
    cfg.validate_reconstruct_counts()  # must not refuse a value nobody wrote


def test_well_formed_count_and_budget_settings_still_apply(monkeypatch, fresh_config):
    cfg = _reload(
        monkeypatch,
        CPERSONA_RECONSTRUCT_MAX_COUNT="8",
        CPERSONA_RECONSTRUCT_DEFAULT_COUNT="4",
        CPERSONA_RECONSTRUCT_FORCED_COUNT="3",
        CPERSONA_RECONSTRUCT_FORCED_BUDGET="6000",
    )
    assert (cfg.RECONSTRUCT_DEFAULT_COUNT, cfg.RECONSTRUCT_FORCED_COUNT) == (4, 3)
    assert cfg.RECONSTRUCT_FORCED_BUDGET == 6000
    unset = _reload(
        monkeypatch,
        CPERSONA_RECONSTRUCT_MAX_COUNT="5",
        CPERSONA_RECONSTRUCT_DEFAULT_COUNT=None,
        CPERSONA_RECONSTRUCT_FORCED_COUNT=None,
        CPERSONA_RECONSTRUCT_FORCED_BUDGET=None,
    )
    assert (unset.RECONSTRUCT_DEFAULT_COUNT, unset.RECONSTRUCT_FORCED_COUNT) == (5, None)
    assert unset.RECONSTRUCT_FORCED_BUDGET is None


# ---------------------------------------------------------------------------
# bug-520 -- the sidecar's width is the corpus's, the quantity the boot compares
# ---------------------------------------------------------------------------


def _reset_calibration_process_state():
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._agent_betas.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._reset_calibration_authority()


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """A fresh sidecar path and isolated calibration globals; the fused gate is off so
    a settled boot is a plain restore (no embedding backend is configured here)."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cpersona.db"))
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    _reset_calibration_process_state()
    session.reset_pauses_for_tests()
    yield str(tmp_path / "cpersona.db") + ".calibration.json"
    _reset_calibration_process_state()
    session.reset_pauses_for_tests()
    config.VECTOR_MIN_SIMILARITY = saved_threshold


async def _insert(conn, agent_id: str, count: int, dim: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    for i in range(count):
        v = rng.standard_normal(dim).astype(np.float32)
        v /= float(np.linalg.norm(v))
        await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (agent_id, f"{agent_id} memory {i}", f"2026-10-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
             struct.pack(f"<{dim}f", *v.tolist())),
        )
    await conn.commit()


@pytest_asyncio.fixture
async def mixed_corpus():
    """Agent w02-a: 30 rows at width 16 (the corpus mode). Agent w02-z is added by the
    tests that need it: rows already re-embedded at width 8 by a new model."""
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await _insert(conn, "w02-a", 30, 16, seed=1)
    yield conn
    await conn.execute("DELETE FROM memories WHERE agent_id IN ('w02-a', 'w02-z')")
    await conn.commit()


async def _boot():
    _reset_calibration_process_state()  # each boot is a fresh process
    return await admin_handlers.ensure_calibrated_on_startup(auto_calibrate=False, on_model_change=True)


@pytest.mark.asyncio
async def test_mixed_width_corpus_settles_after_one_boot(sidecar, mixed_corpus):
    await _insert(mixed_corpus, "w02-z", 20, 8, seed=2)
    assert await admin_handlers._corpus_embedding_dim() == 16
    first = await _boot()
    assert first["action"] == "initial" and sorted(first["agents"]) == ["w02-a", "w02-z"]
    assert admin_handlers._load_calibration_state()["embedding_dim"] == 16
    for _ in range(2):
        later = await _boot()
        assert not later.get("dim_changed"), later
        assert later["action"] == "restored", later


@pytest.mark.asyncio
async def test_calibrating_an_off_mode_agent_keeps_the_corpus_width_and_the_others(sidecar, mixed_corpus):
    assert (await _boot())["action"] == "initial"
    assert (await _boot())["action"] == "restored"
    stored_a = admin_handlers._load_calibration_state()["agent_thresholds"]["w02-a"]

    await _insert(mixed_corpus, "w02-z", 20, 8, seed=2)
    _reset_calibration_process_state()  # a process that did not restore, as after AUTO_CALIBRATE
    result = await admin_handlers.do_calibrate_threshold(agent_id="w02-z")
    assert result["ok"] is True, result
    state = admin_handlers._load_calibration_state()
    assert state["embedding_dim"] == 16
    # The other agent's stored threshold is carried, not dropped as "another width".
    assert state["agent_thresholds"].get("w02-a") == stored_a
    assert "w02-z" in state["agent_thresholds"]

    following = await _boot()
    assert not following.get("dim_changed"), following
    assert following["action"] == "restored", following


# The numeric neighbour of CPERSONA_CALIBRATE_METHOD: every call that omits `percentile`
# uses this default, and the handler refuses one outside (0, 1] (or a percent 1-100), so
# a zero, negative or NaN value made every calibration fail the way a bad method did.
@pytest.mark.parametrize("raw", ["0", "-0.5", "250", "nan"])
def test_unusable_calibrate_percentile_warns_and_falls_back(monkeypatch, fresh_config, caplog, raw):
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_CALIBRATE_PERCENTILE=raw)
    assert cfg.CALIBRATE_PERCENTILE == 0.95
    assert _warned(caplog, "CPERSONA_CALIBRATE_PERCENTILE"), "an unusable percentile was kept silently"


@pytest.mark.parametrize("raw, expected", [("0.9", 0.9), ("90", 90.0), ("1", 1.0)])
def test_usable_calibrate_percentile_is_kept(monkeypatch, fresh_config, raw, expected):
    assert _reload(monkeypatch, CPERSONA_CALIBRATE_PERCENTILE=raw).CALIBRATE_PERCENTILE == expected
