"""bug-258: the startup calibration guard must not hold the transport closed.

Awaiting ensure_calibrated_on_startup inline in main() kept the port unbound for
the guard's whole runtime, and a recalibration is minutes of real embedding
calls (median-of-K multiplied it by the draw count, once per agent) — so a
scoring-version bump turned every deploy into a multi-minute full outage
(measured on the production host: 3.5 minutes for two agents on a 2400-row
corpus, 502 at the tunnel for the duration). The guard is now scheduled as a
background task; until it lands, recall runs on the same heuristic fallback it
uses when calibration fails, which is a degraded answer instead of no answer.
"""

import asyncio
import logging

import pytest

from cpersona import server, vector


@pytest.mark.asyncio
async def test_scheduler_returns_before_a_slow_guard_completes(monkeypatch):
    """The task is scheduled, not awaited — a guard that takes minutes must not
    gate the caller (this is the whole fix)."""
    release = asyncio.Event()
    ran = {}

    async def slow_guard(auto, on_change):
        await release.wait()
        ran["status"] = {"action": "recalibrated"}
        return ran["status"]

    monkeypatch.setattr(server, "ensure_calibrated_on_startup", slow_guard)

    task = server._schedule_startup_calibration()
    await asyncio.sleep(0)  # let the task start and block on the event
    assert not task.done(), "the guard was awaited inline; binding would have waited on it"

    release.set()
    await asyncio.wait_for(task, timeout=5)
    assert ran["status"] == {"action": "recalibrated"}


@pytest.mark.asyncio
async def test_failed_guard_is_reported_the_moment_it_fails(monkeypatch, caplog):
    """A background task's exception is silent until someone awaits it; the
    done-callback is what turns a failed guard into an operator-visible line
    (same silent-death class as an unguarded writer task)."""

    async def failing_guard(auto, on_change):
        raise RuntimeError("embedding backend unreachable")

    monkeypatch.setattr(server, "ensure_calibrated_on_startup", failing_guard)

    with caplog.at_level(logging.ERROR, logger="cpersona.server"):
        task = server._schedule_startup_calibration()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(task, timeout=5)
        await asyncio.sleep(0)  # let the done-callback run

    assert any(
        "Startup calibration failed" in rec.message and "calibrate_threshold" in rec.message
        for rec in caplog.records
    ), f"the failure was not reported: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_cancelled_guard_is_not_reported_as_a_failure(monkeypatch, caplog):
    """Shutdown cancels a still-running guard deliberately; the callback must
    not dress that up as an error."""
    started = asyncio.Event()

    async def hanging_guard(auto, on_change):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "ensure_calibrated_on_startup", hanging_guard)

    with caplog.at_level(logging.ERROR, logger="cpersona.server"):
        task = server._schedule_startup_calibration()
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert not caplog.records, f"a deliberate cancel was logged as a failure: {caplog.records}"


def test_unset_gates_mean_heuristic_not_sidecar():
    """While the guard is in flight nothing consults the sidecar: the gate
    lookup answers from module state alone, and unset state means the
    pool-size heuristic (None), never a stale persisted value."""
    saved_gates = dict(vector._agent_fused_gates)
    saved_global = vector._global_fused_gate
    try:
        vector._agent_fused_gates.clear()
        vector._global_fused_gate = None
        assert vector._get_fused_gate("any-agent") is None
    finally:
        vector._agent_fused_gates.update(saved_gates)
        vector._global_fused_gate = saved_global
