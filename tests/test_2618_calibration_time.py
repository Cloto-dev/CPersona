"""bug-611 (the remaining half): each agent's calibration has a time of its own.

The sidecar held one ``calibrated_at`` for every agent, and a per-agent calibration
(``calibrate_threshold`` or ``set_recall_precision``) restamped it, so calibrating one
agent made every other agent's months-old calibration read as fresh in deep_check.
2.6.11a2 fixed the rewrites that measure nothing (a purge keeps the stored time); this
pins the half left: a measurement stamps only the agent it measured.
"""
import datetime

import pytest

from cpersona import admin_handlers, checks, vector
from tests import test_2611a2_x02 as x02
from tests.test_2611a2_x02 import AGENT, OLD_STAMP, TINY, _payload, _read, _write

# The calibration sidecar and corpus fixtures of the 2.6.11a2 tests, shared.
sidecar = x02.sidecar
corpus = x02.corpus


def _fresh(stamp: str) -> bool:
    age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(stamp)
    return age < datetime.timedelta(minutes=5)


@pytest.mark.asyncio
async def test_calibrating_one_agent_leaves_another_agents_old_calibration_stale(
    sidecar, corpus, monkeypatch
):
    _write(sidecar, _payload(agent_calibrated_at={AGENT: OLD_STAMP, "other": OLD_STAMP}))
    monkeypatch.setattr(vector, "_embedding_client", object())

    result = await admin_handlers.do_calibrate_threshold(AGENT, method="percentile")
    assert result["ok"] is True and result["sidecar_persisted"] is True, result

    stored = _read(sidecar)
    assert _fresh(stored["agent_calibrated_at"][AGENT])
    assert stored["agent_calibrated_at"]["other"] == OLD_STAMP
    assert stored["calibrated_at"] == OLD_STAMP, "a per-agent measurement restamped the file"
    assert (await checks.deep_calibration_staleness(corpus, AGENT, False))["status"] == "ok"
    other = await checks.deep_calibration_staleness(corpus, "other", False)
    assert other["status"] == "stale", (
        f"another agent's 200-day-old calibration reads as fresh after {AGENT} was calibrated: {other!r}"
    )


@pytest.mark.asyncio
async def test_a_file_without_agent_times_falls_back_to_its_own_stamp(sidecar, corpus, monkeypatch):
    # A sidecar written before 2.6.18 has no per-agent times.
    _write(sidecar, _payload())
    monkeypatch.setattr(vector, "_embedding_client", object())
    assert (await checks.deep_calibration_staleness(corpus, AGENT, False))["status"] == "stale"


@pytest.mark.asyncio
async def test_a_global_calibration_stamps_the_file(sidecar, corpus):
    _write(sidecar, _payload())

    result = await admin_handlers.do_calibrate_threshold("", method="percentile")

    assert result["ok"] is True and result["sidecar_persisted"] is True, result
    assert _fresh(_read(sidecar)["calibrated_at"])


@pytest.mark.asyncio
async def test_dropping_an_agent_drops_its_time(sidecar, corpus):
    _write(sidecar, _payload(
        agent_thresholds={AGENT: 0.41, TINY: 0.6},
        agent_calibrated_at={AGENT: OLD_STAMP, TINY: OLD_STAMP},
    ))

    await admin_handlers.do_delete_agent_data(TINY)

    stored = _read(sidecar)
    assert TINY not in stored["agent_calibrated_at"]
    assert stored["agent_calibrated_at"][AGENT] == OLD_STAMP
