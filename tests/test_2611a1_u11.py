"""Configuration values that used to lose data or settings without saying so.

- bug-563: a write cap below 1 (CPERSONA_MAX_CONTENT_LENGTH / _MAX_PROFILE_LENGTH)
  is not a cap. Every consumer slices with it, so 0 emptied and a negative value
  shortened every write, and check_health(fix=true) rewrote every row through the
  same slice and deleted the rows that then collided on the content index.
- bug-561: an empty CPERSONA_DB_PATH (or an empty embedding-mode chain) was taken
  literally instead of falling back to the documented default, the rule every
  bug-133 parser already applies. SQLite reads '' as a private temporary database,
  a different one per connection.
- bug-554: with boot recalibration disabled, a stale-scoring boot loaded none of
  the stored set_recall_precision overrides, so they were not applied and the next
  calibrate_threshold rewrote the sidecar without them; deep_check meanwhile told
  the operator a restart would repair a state a restart cannot repair.
"""

from __future__ import annotations

import importlib
import os

import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, config, maintenance_handlers, server, session, utils, vector
from cpersona import memory_handlers as M
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db

AGENT = "agent.u11-config"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


async def _memory_rows(conn) -> list[tuple[int, str]]:
    return [
        (r[0], r[1])
        for r in await conn.execute_fetchall(
            "SELECT id, content FROM memories WHERE agent_id = ? ORDER BY id", (AGENT,)
        )
    ]


# ---------------------------------------------------------------------------
# bug-563 — a non-positive write cap
# ---------------------------------------------------------------------------


class _PastStartupValidation(Exception):
    """Raised by the first step after the configuration checks in server.main()."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr, env_key, value",
    [
        ("MAX_CONTENT_LENGTH", "CPERSONA_MAX_CONTENT_LENGTH", 0),
        ("MAX_CONTENT_LENGTH", "CPERSONA_MAX_CONTENT_LENGTH", -1),
        ("MAX_PROFILE_LENGTH", "CPERSONA_MAX_PROFILE_LENGTH", 0),
    ],
)
async def test_server_refuses_to_start_on_a_non_positive_cap(monkeypatch, attr, env_key, value):
    """The refusal has to happen in main(), before the server serves anything.

    The step after the configuration checks is replaced by a sentinel, so the
    unfixed main() is stopped there instead of going on to open the database and
    serve stdio: reaching it means the cap was accepted.
    """
    monkeypatch.delenv("CPERSONA_ACL_FILE", raising=False)
    monkeypatch.setattr(config, attr, value)

    def _sentinel():
        raise _PastStartupValidation("server.main() went past its configuration checks")

    monkeypatch.setattr(server, "_preflight_http_auth", _sentinel)

    with pytest.raises(ValueError, match=env_key):
        await server.main()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, -1])
async def test_oversized_content_repair_refuses_a_non_positive_cap(db, monkeypatch, cap):
    for i in range(3):
        result = await M.do_store(AGENT, {"content": f"important memory number {i}"})
        assert result["result"] == "stored", result
    before = await _memory_rows(db)
    assert len(before) == 3

    monkeypatch.setattr(checks, "MAX_CONTENT_LENGTH", cap)
    report = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["oversized_content"]
    )

    assert await _memory_rows(db) == before, (
        f"check_health(fix=true) under a cap of {cap} rewrote or deleted the corpus"
    )
    crashed = [i for i in report["issues"] if i.get("type") == "check_crashed"]
    assert crashed and "CPERSONA_MAX_CONTENT_LENGTH" in crashed[0]["detail"], report
    assert report["status"] != "healthy", report


@pytest.mark.asyncio
async def test_oversized_profile_repair_refuses_a_non_positive_cap(db, monkeypatch):
    await db.execute(
        """INSERT INTO profiles (agent_id, user_id, content, updated_at)
           VALUES (?, '', ?, datetime('now'))""",
        (AGENT, "prefers short answers"),
    )
    await db.commit()

    monkeypatch.setattr(checks, "MAX_PROFILE_LENGTH", 0)
    report = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["oversized_profile"]
    )

    rows = await db.execute_fetchall("SELECT content FROM profiles WHERE agent_id = ?", (AGENT,))
    assert [r[0] for r in rows] == ["prefers short answers"], "the profile was cut to the cap"
    crashed = [i for i in report["issues"] if i.get("type") == "check_crashed"]
    assert crashed and "CPERSONA_MAX_PROFILE_LENGTH" in crashed[0]["detail"], report


@pytest.mark.asyncio
async def test_store_refuses_a_negative_cap_instead_of_dropping_the_tail(db, monkeypatch):
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", -3)

    with pytest.raises(ValueError, match="CPERSONA_MAX_CONTENT_LENGTH"):
        await M.do_store(AGENT, {"content": "new memory text"})

    assert await _memory_rows(db) == [], "a store under a negative cap kept a shortened row"


@pytest.mark.asyncio
async def test_profile_write_refuses_a_zero_cap(db, monkeypatch):
    monkeypatch.setattr(utils, "MAX_PROFILE_LENGTH", 0)

    with pytest.raises(ValueError, match="CPERSONA_MAX_PROFILE_LENGTH"):
        await admin_handlers.do_update_profile(AGENT, "prefers short answers")


def test_a_positive_cap_passes_the_startup_check(monkeypatch):
    """The boundary: 1 is the smallest cap that still bounds a write."""
    monkeypatch.setattr(config, "MAX_CONTENT_LENGTH", 1)
    monkeypatch.setattr(config, "MAX_PROFILE_LENGTH", 1)
    config.validate_content_caps()


# ---------------------------------------------------------------------------
# bug-561 — an empty value is an unset value
# ---------------------------------------------------------------------------


def test_an_empty_db_path_falls_back_to_the_default(monkeypatch):
    try:
        with monkeypatch.context() as env:
            env.setenv("CPERSONA_DB_PATH", "")

            reloaded = importlib.reload(config)

            assert reloaded.DB_PATH == "data/cpersona.db", (
                f"DB_PATH={reloaded.DB_PATH!r}: SQLite opens '' as a private temporary "
                "database, a different one for every connection"
            )
    finally:
        importlib.reload(config)


@pytest.mark.parametrize(
    "specific, generic, expected",
    [
        ("", "", "none"),
        (None, "", "none"),
        ("", "http", "http"),
        ("api", "", "api"),
    ],
)
def test_an_empty_embedding_mode_falls_back_to_the_default(monkeypatch, specific, generic, expected):
    try:
        with monkeypatch.context() as env:
            if specific is None:
                env.delenv("CPERSONA_EMBEDDING_MODE", raising=False)
            else:
                env.setenv("CPERSONA_EMBEDDING_MODE", specific)
            env.setenv("EMBEDDING_MODE", generic)

            reloaded = importlib.reload(config)

            assert reloaded.EMBEDDING_MODE == expected
            reloaded.assert_embedding_mode_supported()
    finally:
        importlib.reload(config)


# ---------------------------------------------------------------------------
# bug-554 — precision overrides across a stale-scoring boot with recalibration off
# ---------------------------------------------------------------------------


def _reset_calibration_globals() -> None:
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()
    vector._reset_calibration_authority()
    config.VECTOR_MIN_SIMILARITY = 0.3


@pytest_asyncio.fixture
async def stale_sidecar(db, tmp_path, monkeypatch):
    """A sidecar from an older scoring version, holding two precision overrides."""
    _reset_calibration_globals()
    monkeypatch.setattr(
        admin_handlers,
        "_calibration_sidecar_path",
        lambda: os.path.join(str(tmp_path), "sidecar.calibration.json"),
    )
    for agent_id in ("a1", "a2"):
        for i in range(30):
            vec = [float((i + j) % 5) - 2.0 for j in range(8)]
            await db.execute(
                "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
                (agent_id, f"memory {i} of {agent_id}", "2026-05-14T00:00:00Z",
                 EmbeddingClient.pack_embedding(vec)),
            )
    await db.commit()
    assert admin_handlers._save_calibration_state(
        embedding_dim=8,
        embedding_model="m",
        global_threshold=0.5,
        agent_thresholds={"a1": 0.4},
        global_fused_gate=0.3,
        fused_gate_signal="confidence",
        agent_betas={"a1": 0.9, "a2": 0.1},
        scoring_version="old-scoring-fingerprint",
    )
    yield
    for agent_id in ("a1", "a2"):
        await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent_id,))
    await db.commit()
    _reset_calibration_globals()


@pytest.mark.asyncio
async def test_a_noop_boot_still_applies_the_stored_precision_overrides(stale_sidecar):
    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=False
    )
    assert status["action"] == "noop"

    precision = await admin_handlers.do_get_recall_precision("a1")
    assert precision["overridden"] is True and precision["beta"] == 0.9, precision
    # The measurements are still declined: only the preference crosses the boot.
    assert "a1" not in vector._agent_thresholds


@pytest.mark.asyncio
async def test_calibrate_after_a_noop_boot_keeps_the_precision_overrides(
    stale_sidecar, fake_embedding_client
):
    await admin_handlers.ensure_calibrated_on_startup(auto_calibrate=False, on_model_change=False)

    result = await admin_handlers.do_calibrate_threshold("")
    assert result.get("ok") and result.get("sidecar_persisted"), result

    after = admin_handlers._load_calibration_state()
    assert after["scoring_version"] == admin_handlers._live_fingerprint()
    assert after["agent_betas"] == {"a1": 0.9, "a2": 0.1}, (
        f"calibrate_threshold rewrote the sidecar with agent_betas={after['agent_betas']!r}"
    )


@pytest.mark.asyncio
async def test_stale_scoring_hint_does_not_promise_a_restart_when_boot_will_not_recalibrate(
    stale_sidecar, fake_embedding_client, monkeypatch, db
):
    monkeypatch.setattr(config, "AUTO_CALIBRATE", False)
    monkeypatch.setattr(config, "CALIBRATE_ON_MODEL_CHANGE", False)

    result = await checks.deep_calibration_staleness(db, "a1", fix=False)

    assert result["status"] == "stale_scoring_version", result
    assert "recalibrates automatically" not in result["hint"], result["hint"]
    assert "CPERSONA_CALIBRATE_ON_MODEL_CHANGE" in result["hint"], result["hint"]


@pytest.mark.asyncio
async def test_stale_scoring_hint_keeps_the_restart_advice_when_boot_recalibrates(
    stale_sidecar, fake_embedding_client, monkeypatch, db
):
    monkeypatch.setattr(config, "AUTO_CALIBRATE", False)
    monkeypatch.setattr(config, "CALIBRATE_ON_MODEL_CHANGE", True)

    result = await checks.deep_calibration_staleness(db, "a1", fix=False)

    assert result["hint"] == "restart the server (it recalibrates automatically on boot)"

