"""Pure-function tests for the v2.4.24 threshold calibration (no DB / no event loop).

Covers the formula layer only — sign fix (Tier 0), percentile (Tier 1), and the
two-population separation with a temporal-adjacency positive proxy (Tier 2) — so the
comparison runs in CI without the async DB fixtures used by test_threshold_calibration.
"""
import os
import tempfile

import numpy as np

# admin_handlers imports config which needs a DB path; set one (never opened here).
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "x.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import admin_handlers  # noqa: E402


def _unit(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


# ---- Tier 0 / Tier 1 : formula direction ---------------------------------------


def _bge_like_null():
    rng = np.random.default_rng(42)
    return np.clip(rng.normal(0.51, 0.07, 4000), -1.0, 1.0)


def test_legacy_sign_admitted_majority_but_fixed_sign_rejects():
    null = _bge_like_null()
    legacy = round(max(float(np.mean(null)) - float(np.std(null)), 0.05), 4)  # mean - std
    fixed = admin_handlers._threshold_from_sims(
        null, method="zscore", z_factor=1.0, percentile=0.95, floor=0.05
    )
    assert legacy < float(np.mean(null))           # old: below null mean
    assert float(np.mean(null >= legacy)) > 0.5    # old: admits the majority
    assert fixed["threshold"] > float(np.mean(null))   # new: above null mean
    assert fixed["null_admit_rate"] < 0.2


def test_percentile_admits_complement_of_quantile():
    null = _bge_like_null()
    s = admin_handlers._threshold_from_sims(
        null, method="percentile", z_factor=1.0, percentile=0.95, floor=0.05
    )
    assert abs(s["null_admit_rate"] - 0.05) < 0.02


# ---- Tier 2 : separation + temporal-adjacency proxy ----------------------------


def test_adjacency_core_selects_within_window_pairs():
    # three memories: pair (0,1) within window, (1,2) outside it
    times = [0.0, 60.0, 10_000.0]
    vecs = _unit(np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]))
    sims = admin_handlers._adjacency_sims_core(times, vecs, window_sec=600.0)
    assert len(sims) == 1                       # only the in-window adjacent pair
    assert sims[0] > 0.9                         # vec0 ~ vec1


def test_separation_threshold_lands_between_populations():
    rng = np.random.default_rng(1)
    null = np.clip(rng.normal(0.50, 0.07, 3000), -1, 1)
    pos = np.clip(rng.normal(0.75, 0.06, 600), -1, 1)
    thr, j = admin_handlers._separation_threshold(null, pos, floor=0.05)
    assert float(np.mean(null)) < thr < float(np.mean(pos))
    assert j > 0.7


def _timeline(rng, dim, k, n_sessions, sess_len, gbias, cscale, noise, drift):
    g = _unit(rng.normal(0, 1, dim))
    cents = [_unit(rng.normal(0, 1, dim)) for _ in range(k)]
    vecs, labels, times, t = [], [], [], 0.0
    for _ in range(n_sessions):
        c = rng.integers(k)
        for _ in range(max(2, int(rng.normal(sess_len, 2)))):
            ci = c if rng.random() > drift else rng.integers(k)
            vecs.append(gbias * g + cscale * cents[ci] + noise * rng.normal(0, 1, dim))
            labels.append(ci)
            t += rng.uniform(60, 240)
            times.append(t)
        t += rng.uniform(6 * 3600, 24 * 3600)
    return _unit(np.array(vecs)), np.array(labels), np.array(times)


def test_tier2_temporal_beats_tier1_percentile_on_bge_like_corpus():
    """On a poorly-separated (bge-m3-like) corpus, the temporal-proxy separation
    threshold achieves a strictly better recall/contamination tradeoff (Youden's J
    on ground-truth labels) than the fixed-quantile percentile method."""
    rng = np.random.default_rng(11)
    vecs, labels, times = _timeline(
        rng, dim=16, k=8, n_sessions=40, sess_len=10,
        gbias=0.82, cscale=0.30, noise=0.14, drift=0.15,
    )
    sim = vecs @ vecs.T
    iu = np.triu_indices(len(vecs), k=1)
    pair = sim[iu]
    same = labels[iu[0]] == labels[iu[1]]
    gt_pos, gt_neg = pair[same], pair[~same]

    proxy = admin_handlers._adjacency_sims_core(times, vecs, window_sec=30 * 60.0)
    assert len(proxy) >= 10

    t1 = admin_handlers._threshold_from_sims(pair, method="percentile", z_factor=1.0, percentile=0.95, floor=0.05)["threshold"]
    t2 = admin_handlers._threshold_from_sims(pair, method="separation", z_factor=1.0, percentile=0.95, floor=0.05, pos_sims=proxy)["threshold"]

    def youden(t):
        return float(np.mean(gt_pos >= t)) - float(np.mean(gt_neg >= t))

    assert youden(t2) > youden(t1)
    # the percentile method starves recall on a poorly-separable corpus
    assert float(np.mean(gt_pos >= t1)) < 0.25
    assert float(np.mean(gt_pos >= t2)) > 0.35
