"""Median-of-K fused-gate calibration.

The single-draw estimator handed production a 0.1544 gate from the minor mode
of a multimodal separation objective — a value 21 subsequent probe draws never
reproduced (median 0.4288). These tests pin the wrapper's contract: the applied
threshold is the (upper) median across K independent draws, a draw that returns
None degrades gracefully, and an all-None run keeps the single-draw degrade
contract (caller falls back to the heuristic gate).
"""

import pytest

from cpersona import admin_handlers, config


def _stub_draws(monkeypatch, thresholds):
    """Replace _calibrate_fused_gate with a sequence of canned draws."""
    seq = iter(thresholds)

    async def fake_draw(db, agent_id, sample_queries, window_min, beta, floor):
        value = next(seq)
        if value is None:
            return None
        return {"threshold": value, "signal": "confidence", "n_null": 10, "n_pos": 5}

    monkeypatch.setattr(admin_handlers, "_calibrate_fused_gate", fake_draw)


@pytest.mark.asyncio
async def test_median_draw_wins_over_a_minor_mode_outlier(monkeypatch):
    """The production failure shape: one draw lands on the second mode (~0.15),
    the others on the main mode (~0.43). The median must discard the outlier."""
    _stub_draws(monkeypatch, [0.43, 0.1544, 0.44, 0.42, 0.45])

    stats = await admin_handlers._calibrate_fused_gate_median(
        None, "a", 40, 30.0, 1.0, 0.1, draws=5
    )

    assert stats["threshold"] == 0.43
    assert stats["threshold_draws"] == [0.1544, 0.42, 0.43, 0.44, 0.45]


@pytest.mark.asyncio
async def test_failed_draws_are_skipped_and_the_median_is_over_successes(monkeypatch):
    _stub_draws(monkeypatch, [0.44, None, 0.42, 0.41, None])

    stats = await admin_handlers._calibrate_fused_gate_median(
        None, "a", 40, 30.0, 1.0, 0.1, draws=5
    )

    assert stats["threshold"] == 0.42
    assert stats["threshold_draws"] == [0.41, 0.42, 0.44]


@pytest.mark.asyncio
async def test_all_draws_failing_degrades_like_a_single_draw(monkeypatch):
    """None keeps the existing contract: the caller falls back to the heuristic
    gate instead of applying a fabricated threshold."""
    _stub_draws(monkeypatch, [None, None, None])

    stats = await admin_handlers._calibrate_fused_gate_median(
        None, "a", 40, 30.0, 1.0, 0.1, draws=3
    )

    assert stats is None


@pytest.mark.asyncio
async def test_a_single_successful_draw_carries_no_draws_annotation(monkeypatch):
    """With one success the stats dict is exactly that draw's measurement —
    a threshold_draws list of length one would only add noise."""
    _stub_draws(monkeypatch, [None, 0.43, None])

    stats = await admin_handlers._calibrate_fused_gate_median(
        None, "a", 40, 30.0, 1.0, 0.1, draws=3
    )

    assert stats["threshold"] == 0.43
    assert "threshold_draws" not in stats


@pytest.mark.asyncio
async def test_default_draw_count_comes_from_config(monkeypatch):
    calls = []

    async def counting_draw(db, agent_id, sample_queries, window_min, beta, floor):
        calls.append(1)
        return {"threshold": 0.4, "signal": "confidence"}

    monkeypatch.setattr(admin_handlers, "_calibrate_fused_gate", counting_draw)
    monkeypatch.setattr(config, "FUSED_GATE_CALIBRATION_DRAWS", 3)

    await admin_handlers._calibrate_fused_gate_median(None, "a", 40, 30.0, 1.0, 0.1)

    assert len(calls) == 3
