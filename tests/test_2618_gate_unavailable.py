"""bug-807: a store the recall gate cannot be calibrated on is not recalibrated every boot.

When the gate's samples were too few (fewer than 10 null or 5 positive scores), the
startup guard restored the sidecar, found no gate signal in it and recalibrated the
whole store again -- the store-wide threshold and every agent's -- beside serving. The
gate still could not calibrate, so the next start repeated it, redrawing the store-wide
threshold each time, and the boot reported ``gate_calibrated``.

Here the gate never calibrates (no embedding client is configured, so the simulated
queries cannot run), which stands in for a store too small or too sparse in time.
"""
import pytest

from cpersona import admin_handlers, config, vector
from cpersona.database import get_db
from tests.test_threshold_calibration import _seed_embeddings, setup_db  # noqa: F401


@pytest.fixture
def calibrations(monkeypatch):
    """Count the calibrations the startup guard runs."""
    calls = []
    real = admin_handlers.do_calibrate_threshold

    async def counting(agent_id, *args, **kwargs):
        calls.append(agent_id)
        return await real(agent_id, *args, **kwargs)

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", counting)
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    return calls


def _signal_less_sidecar():
    admin_handlers._save_calibration_state(
        embedding_dim=8,
        embedding_model="bge-m3",
        global_threshold=0.61,
        agent_thresholds={"agent-small": 0.58},
    )


async def _boot():
    vector._fused_gate_signal = None
    return await admin_handlers.ensure_calibrated_on_startup(auto_calibrate=False, on_model_change=True)


@pytest.mark.asyncio
async def test_the_first_boot_tries_the_gate_records_the_attempt_and_says_so(calibrations):
    await _seed_embeddings(await get_db(), "agent-small", 15)
    _signal_less_sidecar()

    status = await _boot()

    assert calibrations, "the boot did not try to calibrate the gate"
    assert status["action"] == "gate_unavailable", status
    state = admin_handlers._load_calibration_state()
    assert state["gate_unavailable"]["embedded_rows"] == 15
    # Adding the record rewrote the file; what the boot measured is still in it.
    assert "agent-small" in state["agent_thresholds"] and "agent-small" in state["agent_calibrated_at"]


@pytest.mark.asyncio
async def test_the_next_boot_on_the_same_store_restores_without_recalibrating(calibrations):
    await _seed_embeddings(await get_db(), "agent-small", 15)
    _signal_less_sidecar()
    await _boot()
    stored_threshold = admin_handlers._load_calibration_state()["global_threshold"]
    calibrations.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3

    status = await _boot()

    assert calibrations == [], f"the boot recalibrated the store again: {calibrations}"
    assert status["action"] == "restored", status
    assert config.VECTOR_MIN_SIMILARITY == stored_threshold, "the stored threshold was not restored"


@pytest.mark.asyncio
async def test_a_store_that_grew_enough_is_tried_again(calibrations):
    db = await get_db()
    await _seed_embeddings(db, "agent-small", 15)
    _signal_less_sidecar()
    await _boot()
    # 15 -> 25: the larger of 15 + 10 and 15 x 1.25.
    await _seed_embeddings(db, "agent-grown", 10)
    calibrations.clear()

    status = await _boot()

    assert calibrations, "a store that grew past the bound was not tried again"
    assert status["action"] == "gate_unavailable"
    assert admin_handlers._load_calibration_state()["gate_unavailable"]["embedded_rows"] == 25


@pytest.mark.asyncio
async def test_a_store_that_grew_a_little_is_not_tried_again(calibrations):
    db = await get_db()
    await _seed_embeddings(db, "agent-small", 15)
    _signal_less_sidecar()
    await _boot()
    await _seed_embeddings(db, "agent-grown", 9)  # 24, one short of the bound
    calibrations.clear()

    status = await _boot()

    assert calibrations == [] and status["action"] == "restored", status


@pytest.mark.parametrize(
    "seen, now, retry",
    [(15, 24, False), (15, 25, True), (100, 124, False), (100, 125, True), (0, 9, False), (0, 10, True)],
)
def test_the_retry_bound_is_the_larger_of_ten_rows_and_a_quarter(seen, now, retry):
    assert admin_handlers._gate_worth_retrying({"embedded_rows": seen}, now) is retry


@pytest.mark.parametrize("record", ["x", {"embedded_rows": "15"}, {"embedded_rows": -1}, {"embedded_rows": True}, {}])
def test_a_record_that_cannot_be_read_is_worth_retrying(record):
    # Asked with no rows at all, where any count the record could be read as says wait.
    assert admin_handlers._gate_worth_retrying(record, 0) is True


@pytest.mark.asyncio
async def test_dropping_an_agent_keeps_the_record(calibrations):
    db = await get_db()
    await _seed_embeddings(db, "agent-small", 15)
    await _seed_embeddings(db, "agent-gone", 12)
    _signal_less_sidecar()
    await _boot()
    record = admin_handlers._load_calibration_state()["gate_unavailable"]
    assert "agent-gone" in admin_handlers._load_calibration_state()["agent_thresholds"]

    await admin_handlers.do_delete_agent_data("agent-gone")

    state = admin_handlers._load_calibration_state()
    assert "agent-gone" not in state["agent_thresholds"], "the purge did not rewrite the sidecar"
    assert state["gate_unavailable"] == record


@pytest.mark.asyncio
async def test_a_calibration_that_measured_a_gate_drops_the_record(calibrations):
    await _seed_embeddings(await get_db(), "agent-small", 15)
    _signal_less_sidecar()
    await _boot()
    assert "gate_unavailable" in admin_handlers._load_calibration_state()

    vector._fused_gate_signal = "rrf"  # a gate exists now
    result = await admin_handlers.do_calibrate_threshold("agent-small")

    assert result["ok"] is True and result["sidecar_persisted"] is True, result
    assert "gate_unavailable" not in admin_handlers._load_calibration_state()


@pytest.mark.asyncio
async def test_a_calibration_without_a_gate_keeps_the_record(calibrations):
    await _seed_embeddings(await get_db(), "agent-small", 15)
    _signal_less_sidecar()
    await _boot()
    record = admin_handlers._load_calibration_state()["gate_unavailable"]

    result = await admin_handlers.do_calibrate_threshold("agent-small")

    assert result["ok"] is True and result["sidecar_persisted"] is True, result
    assert admin_handlers._load_calibration_state()["gate_unavailable"] == record
