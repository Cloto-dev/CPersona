"""Does the lexical arm carry evidence the dense arm does not already have?

Reads the row-keyed dump of ``benchmarks/labelled_evidence_dump.py`` and answers
the question the pre-registration
(``prereg-conditional-lexical-evidence.md``) fixed in advance:

    is the relevance label Y dependent on the lexical score L given the dense
    score V, and how large is that dependence?

That dependence is J(v, l) = log f1(l|v) - log f0(l|v), the second term of the
likelihood-ratio factorisation. J identically zero means the fused order is the
dense order and the conditional-evidence fusion mode has nothing to recover.

**The statistic.** Rows are stratified by (query, dense-rank block). A stratum
never spans two queries, so every cross-query difference in lexical scale -- this
arm's scale was measured to differ by about a factor of two between languages --
is absorbed by construction rather than normalised away. Within a stratum the
dense score is approximately fixed, which is the conditioning J requires. The
statistic is the stratified AUC: the probability that a gold row outscores a
non-gold row of the same query at a comparable dense rank. 0.5 is exactly J = 0
in rank terms.

**The null test is exact, not simulated.** Under within-stratum label
permutation the strata are independent, so the statistic's null mean and
tie-corrected variance are known in closed form; the normal approximation they
give is checked against real permutations in the self-test rather than assumed.

**The interval is a cluster bootstrap over queries**, because a query with
several gold rows contributes several correlated strata.

Usage:

    python benchmarks/measurements/conditional_lexical_evidence.py --selftest
    python benchmarks/measurements/conditional_lexical_evidence.py \
        --dump_dir ~/lmeb/evidence_jinanano --model jina-v5-nano \
        --replay_dir ~/lmeb/replay_d1d2_jinanano --out report.json
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import numpy as np

#: Lexical-rank bins for the J grid. Absence is its own bin, never a rank.
LEX_BINS = [0, 1, 3, 10, 30, 100]
LEX_ABSENT = -1
#: Dense-rank block widths for the granularity sweep. "bins" is the dump's own
#: bin_edges; the integers are fixed-width blocks of consecutive dense ranks.
SWEEP_WIDTHS = [2, 5, 10, 30]
#: Block widths in cosine for the value-stratified arm, coarse to fine.
CALIPERS = [0.02, 0.01, 0.005, 0.002]
NEG_INF = -np.inf


def _bin_index(edges: list[int], x: int) -> int:
    for i in range(len(edges) - 1, -1, -1):
        if x >= edges[i]:
            return i
    return 0


def load_task(dump_dir: Path, task: str):
    """Rows and per-query meta for one task. Rows come back as arrays."""
    meta = json.load(open(dump_dir / f"{task}.meta.json"))
    # A query is identified by (subtask, id), never by id alone: several tasks
    # here number their queries per subtask, so "query_1" names a different
    # question against a different corpus in each of them. Keying on the bare id
    # merges them into one stratum and compares a gold row of one corpus with
    # non-gold rows of another, which is not the comparison this measures.
    fallback = {(m["subtask"], m["qid"]) for m in meta["queries"] if m["lex_fallback"]}
    qid, vr, v, y, lscore, lrank = [], [], [], [], [], []
    with gzip.open(dump_dir / f"{task}.rows.csv.gz", "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if (r["subtask"], r["qid"]) in fallback:          # abstention bucket 1
                continue
            qid.append(f'{r["subtask"]}\x1f{r["qid"]}')
            vr.append(int(r["vr"]))
            v.append(float(r["v"]))
            y.append(int(r["y"]))
            lscore.append(float(r["l"]) if r["l"] != "" else NEG_INF)
            lrank.append(int(r["lr"]) if r["lr"] != "" else LEX_ABSENT)
    # Qualification on the real dump, not on synthetic rows: the number of
    # distinct query keys must equal the number of query records the dump wrote.
    # The synthetic self-tests below cannot catch a key that merges queries,
    # because they give every query a unique id by construction -- so this is the
    # check that would have caught the 2026-09-10 correction, and it runs on
    # every load.
    expected = sum(1 for m in meta["queries"] if not m["lex_fallback"])
    seen = len(set(qid))
    if seen != expected:
        raise SystemExit(
            f"{task}: {seen} distinct query keys but {expected} queries in the dump -- "
            "the key merges or splits queries, and every stratum below would be wrong")
    return (meta, np.array(qid), np.asarray(vr, dtype=np.int64), np.asarray(v, dtype=np.float64),
            np.asarray(y, dtype=np.int8), np.asarray(lscore, dtype=np.float64),
            np.asarray(lrank, dtype=np.int64))


def _stratum_stats(lex: np.ndarray, y: np.ndarray):
    """(U, n1*n2, Var[U]) for one stratum under within-stratum label permutation.

    U is the Mann-Whitney count of gold over non-gold with ties at one half.
    E[U] = n1*n2/2 and the tie-corrected variance is exact for the permutation
    null, so no simulation is needed to test it -- the self-test checks the
    normal approximation those moments give against real permutations.
    """
    n1 = int(y.sum())
    n2 = len(y) - n1
    if n1 == 0 or n2 == 0:                    # abstention bucket 2
        return 0.0, 0.0, 0.0
    g, n = lex[y == 1], lex[y == 0]
    with np.errstate(invalid="ignore"):
        d = g[:, None] - n[None, :]
    # -inf minus -inf is nan: two absent rows are an exact tie, not a comparison
    eq = np.isnan(d) | (d == 0)
    gt = (~eq) & (d > 0)
    u = float(gt.sum()) + 0.5 * float(eq.sum())
    N = n1 + n2
    if N < 2:
        return u, float(n1 * n2), 0.0
    # np.unique folds every absent row into one group, which is what the tie
    # correction needs: absence is one category, not many distinct scores.
    _, counts = np.unique(lex, return_counts=True)
    tie_corr = float(((counts ** 3 - counts).sum()) / (N * (N - 1)))
    var = (n1 * n2 / 12.0) * ((N + 1) - tie_corr)
    return u, float(n1 * n2), var


def stratified_auc(qid, block, lex, y):
    """Stratified AUC plus the per-query aggregates the bootstrap resamples.

    Strata are (query, block). Returns the point estimate, the exact null z, and
    per-query (U, denominator) so that resampling queries is a ratio of sums.
    """
    key = defaultdict(list)
    for i in range(len(qid)):
        key[(qid[i], int(block[i]))].append(i)
    per_q_u = defaultdict(float)
    per_q_d = defaultdict(float)
    var_tot = 0.0
    n_strata = n_used = 0
    for (q, _b), idx in key.items():
        n_strata += 1
        ii = np.asarray(idx)
        u, d, v = _stratum_stats(lex[ii], y[ii])
        if d == 0:
            continue
        n_used += 1
        per_q_u[q] += u
        per_q_d[q] += d
        var_tot += v
    qs = sorted(per_q_d)
    uq = np.array([per_q_u[q] for q in qs])
    dq = np.array([per_q_d[q] for q in qs])
    den = dq.sum()
    if den == 0:
        return dict(auc=float("nan"), z=float("nan"), p=float("nan"), n_strata=n_strata,
                    n_strata_used=0, n_queries=0, pairs=0.0), (np.array([]), np.array([]))
    auc = uq.sum() / den
    z = (uq.sum() - den / 2.0) / math.sqrt(var_tot) if var_tot > 0 else float("nan")
    p = math.erfc(abs(z) / math.sqrt(2)) if not math.isnan(z) else float("nan")
    return (dict(auc=float(auc), z=float(z), p=float(p), n_strata=n_strata,
                 n_strata_used=n_used, n_queries=len(qs), pairs=float(den)),
            (uq, dq))


def cluster_bootstrap(uq, dq, n=2000, seed=0):
    """Percentile interval, resampling QUERIES with replacement."""
    if len(uq) == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uq), size=(n, len(uq)))
    num = uq[idx].sum(axis=1)
    den = dq[idx].sum(axis=1)
    ok = den > 0
    b = num[ok] / den[ok]
    return float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))


def blocks(vr: np.ndarray, width, bin_edges: list[int]) -> np.ndarray:
    """Dense-rank block id for each row: the dump's bins, or fixed-width blocks."""
    if width == "bins":
        return np.array([_bin_index(bin_edges, int(r)) for r in vr], dtype=np.int64)
    return vr // int(width)


def value_blocks(v: np.ndarray, delta: float) -> np.ndarray:
    """Blocks of dense SCORE, not of dense rank.

    J conditions on v, and a rank block does not fix v: at the head of a cosine
    ranking, adjacent ranks are far apart in score, so a two-rank block can still
    carry most of the dense difference it was meant to remove. Blocks of width
    `delta` in cosine bound that difference directly, and the dense control says
    when delta is small enough -- it goes to one half exactly when the block stops
    carrying dense signal.
    """
    return np.floor(v / delta).astype(np.int64)


def j_grid(vr, lrank, y, bin_edges):
    """log f1(lex bin | dense bin) - log f0(...) with add-one smoothing.

    Reported with the count in every cell: the tails are where the signal lives
    and where estimation noise competes with it, so a cell's number is unreadable
    without its n.
    """
    dbin = np.array([_bin_index(bin_edges, int(r)) for r in vr])
    lbin = np.array([LEX_ABSENT if r == LEX_ABSENT else _bin_index(LEX_BINS, int(r)) for r in lrank])
    cells = [LEX_ABSENT] + list(range(len(LEX_BINS)))
    out = {}
    for b in sorted(set(dbin.tolist())):
        m = dbin == b
        g, n = lbin[m & (y == 1)], lbin[m & (y == 0)]
        if len(g) == 0 or len(n) == 0:
            continue
        row = {}
        for cbin in cells:
            c1, c0 = int((g == cbin).sum()), int((n == cbin).sum())
            p1 = (c1 + 1) / (len(g) + len(cells))
            p0 = (c0 + 1) / (len(n) + len(cells))
            row[str(cbin)] = {"J": round(math.log(p1 / p0), 4), "n_gold": c1, "n_null": c0}
        out[str(b)] = {"n_gold": int(len(g)), "n_null": int(len(n)), "cells": row}
    return out


# ----------------------------------------------------------------- self-tests

def _synth(n_q, n_rows, delta, seed, rng_gold=None):
    """Synthetic rows: l depends on the dense rank, and on y only through delta.

    delta = 0 is the redundancy world -- l carries nothing beyond v, so a correct
    estimator must return 0.5 and reject at the nominal rate, no more.
    """
    rng = np.random.default_rng(seed)
    qid, vr, y, lex = [], [], [], []
    for q in range(n_q):
        gold_rank = int(rng.integers(0, n_rows))          # depends on nothing but chance
        for r in range(n_rows):
            is_g = int(r == gold_rank)
            qid.append(f"q{q}")
            vr.append(r)
            y.append(is_g)
            # mean depends on the dense rank only; delta is the conditional signal
            lex.append(-0.05 * r + rng.random() + delta * is_g)
    return (np.array(qid), np.asarray(vr, dtype=np.int64), np.asarray(y, dtype=np.int8),
            np.asarray(lex, dtype=np.float64))


def _perm_auc_null(qid, block, lex, y, n_perm, seed):
    """Real within-stratum permutations, for checking the closed-form moments."""
    rng = np.random.default_rng(seed)
    key = defaultdict(list)
    for i in range(len(qid)):
        key[(qid[i], int(block[i]))].append(i)
    groups = [(np.asarray(v), int(y[np.asarray(v)].sum())) for v in key.values()]
    groups = [(g, k) for g, k in groups if 0 < k < len(g)]
    out = []
    yp = np.zeros(len(y), dtype=np.int8)
    for _ in range(n_perm):
        yp[:] = 0
        for g, k in groups:
            yp[rng.choice(g, k, replace=False)] = 1
        s, _ = stratified_auc(qid, block, lex, yp)
        out.append(s["auc"])
    return np.array(out)


def selftest() -> int:
    """Every check names what refutes it; a failure returns non-zero."""
    fails = []
    print("instrument qualification")

    # 1. Redundancy returns nothing: false-positive rate must sit near alpha.
    rej = 0
    trials = 200
    for s in range(trials):
        qid, vr, y, lex = _synth(60, 12, delta=0.0, seed=1000 + s)
        stat, _ = stratified_auc(qid, blocks(vr, 3, []), lex, y)
        rej += stat["p"] <= 0.05
    rate = rej / trials
    ok = rate <= 0.10
    print(f"  redundancy: rejection rate {rate:.3f} at alpha=0.05  "
          f"(refuted above 0.10) -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("redundancy")

    # 2. A known effect is recovered. The truth is measured on a large sample of
    #    the same generator, not asserted from the parameter.
    qid, vr, y, lex = _synth(20000, 12, delta=0.115, seed=7)
    truth, _ = stratified_auc(qid, blocks(vr, 3, []), lex, y)
    qid, vr, y, lex = _synth(1200, 12, delta=0.115, seed=11)
    est, _ = stratified_auc(qid, blocks(vr, 3, []), lex, y)
    ok = abs(est["auc"] - truth["auc"]) <= 0.02
    print(f"  known effect: truth {truth['auc']:.4f}, estimate {est['auc']:.4f}, "
          f"|diff| {abs(est['auc']-truth['auc']):.4f} (refuted above 0.02) -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("known-effect")

    # 3. The closed-form null moments must match real permutations -- otherwise
    #    every p-value in the report is a guess dressed as arithmetic.
    qid, vr, y, lex = _synth(150, 10, delta=0.0, seed=3)
    blk = blocks(vr, 3, [])
    null = _perm_auc_null(qid, blk, lex, y, n_perm=400, seed=5)
    stat, _ = stratified_auc(qid, blk, lex, y)
    sd_perm = float(null.std(ddof=1))
    # the closed form's sd, recovered from the z it reports
    sd_closed = abs(stat["auc"] - 0.5) / abs(stat["z"]) if stat["z"] else float("nan")
    ratio = sd_perm / sd_closed
    ok = 0.9 <= ratio <= 1.1
    print(f"  null moments: permutation sd {sd_perm:.5f} vs closed form {sd_closed:.5f} "
          f"(ratio {ratio:.3f}, refuted outside 0.9-1.1) -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("null-moments")

    # 4. Abstention is not scored: a stratum with no gold, or no non-gold,
    #    must reach neither numerator nor denominator.
    qid = np.array(["a", "a", "b", "b"])
    blk = np.array([0, 0, 0, 0])
    y = np.array([0, 0, 1, 1], dtype=np.int8)      # query a: no gold; query b: no null
    lex = np.array([1.0, 2.0, 3.0, 4.0])
    stat, _ = stratified_auc(qid, blk, lex, y)
    ok = stat["pairs"] == 0.0 and stat["n_strata_used"] == 0 and math.isnan(stat["auc"])
    print(f"  abstention: pairs={stat['pairs']}, strata used={stat['n_strata_used']}, "
          f"auc={stat['auc']} -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("abstention")

    # 5. Absence is the lowest category and ties among absences score one half.
    qid = np.array(["a"] * 4)
    blk = np.zeros(4, dtype=np.int64)
    y = np.array([1, 0, 0, 0], dtype=np.int8)
    lex = np.array([NEG_INF, NEG_INF, 1.0, 2.0])     # gold absent, two non-gold present
    stat, _ = stratified_auc(qid, blk, lex, y)
    ok = abs(stat["auc"] - (0.5 / 3.0)) < 1e-12
    print(f"  absence: auc={stat['auc']:.6f}, expected {0.5/3.0:.6f} -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("absence")

    print("PASS" if not fails else f"FAIL: {', '.join(fails)}")
    return 1 if fails else 0


# --------------------------------------------------------------------- report

def analyse(dump_dir: Path, tasks: list[str], replay_dir: Path | None, seed: int):
    out = {"dump_dir": str(dump_dir), "tasks": {}}
    for task in tasks:
        meta, qid, vr, v, y, lex, lrank = load_task(dump_dir, task)
        edges = meta["bin_edges"]
        n_fallback = sum(1 for m in meta["queries"] if m["lex_fallback"])
        rec = {"model": meta["model"], "cache_label": meta["cache_label"],
               "queries_dumped": len(meta["queries"]),
               "abstain_lex_fallback": n_fallback,
               "sign_check": meta["sign_check"], "rows": int(len(y)),
               "gold_rows": int(y.sum())}

        blk0 = blocks(vr, "bins", edges)
        primary, (uq, dq) = stratified_auc(qid, blk0, lex, y)
        lo, hi = cluster_bootstrap(uq, dq, seed=seed)
        primary.update(ci_lo=lo, ci_hi=hi)
        ctrl0, _ = stratified_auc(qid, blk0, -vr.astype(np.float64), y)
        primary["control_auc_dense"] = ctrl0["auc"]
        rec["primary"] = primary

        # How much of the effect survives as the conditioning on v tightens, and
        # -- the control -- how much residual dense signal each width leaves.
        # Running the identical statistic on the dense score itself measures the
        # leakage the rival hypothesis needs, instead of arguing about it: if a
        # width truly fixes v, gold is no more likely than chance to hold the
        # better dense score inside its own block.
        rec["sweep"] = {}
        for w in SWEEP_WIDTHS:
            blk = blocks(vr, w, edges)
            s, (u2, d2) = stratified_auc(qid, blk, lex, y)
            s_lo, s_hi = cluster_bootstrap(u2, d2, seed=seed)
            s.update(ci_lo=s_lo, ci_hi=s_hi)
            ctrl, _ = stratified_auc(qid, blk, -vr.astype(np.float64), y)
            s["control_auc_dense"] = ctrl["auc"]
            rec["sweep"][str(w)] = s

        # Blocks of dense score. The rank sweep above cannot separate the effect
        # from the residual dense difference inside a block; this can, because the
        # block's width in cosine is the bound on that difference, and the control
        # reports when it has actually been removed.
        rec["caliper"] = {}
        for delta in CALIPERS:
            blk = value_blocks(v, delta)
            s_, (u3, d3) = stratified_auc(qid, blk, lex, y)
            s_lo, s_hi = cluster_bootstrap(u3, d3, seed=seed)
            s_.update(ci_lo=s_lo, ci_hi=s_hi)
            ctrl, _ = stratified_auc(qid, blk, v, y)
            s_["control_auc_dense"] = ctrl["auc"]
            rec["caliper"][str(delta)] = s_

        # restricted to strata where the lexical arm says anything at all
        present = lrank != LEX_ABSENT
        share_tied = 1.0 - float(present.mean()) if len(present) else 1.0
        rec["lex_absent_share"] = round(share_tied, 4)
        rec["j_grid"] = j_grid(vr, lrank, y, edges)

        if replay_dir is not None:
            f = replay_dir / f"{task}.json"
            if f.exists():
                rec["replay_d_fusion"] = json.load(open(f))["mean"].get("d_fusion")
        out["tasks"][task] = rec
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dump_dir")
    ap.add_argument("--tasks", default=None, help="default: every task in the dump dir")
    ap.add_argument("--replay_dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    d = Path(os.path.expanduser(a.dump_dir))
    tasks = a.tasks.split(",") if a.tasks else sorted(p.name.split(".")[0] for p in d.glob("*.meta.json"))
    rep = analyse(d, tasks, Path(os.path.expanduser(a.replay_dir)) if a.replay_dir else None, a.seed)
    print(f"{'task':<14}{'AUC':>8}{'95% CI':>18}{'ctrl':>7}{'z':>8}{'strata':>9}{'gold':>8}{'absent':>8}{'dfus':>8}")
    for t, r in rep["tasks"].items():
        p = r["primary"]
        ci = f"[{p['ci_lo']:.3f}, {p['ci_hi']:.3f}]"
        df = r.get("replay_d_fusion")
        print(f"{t:<14}{p['auc']:>8.4f}{ci:>18}{p['control_auc_dense']:>7.3f}"
              f"{p['z']:>8.1f}{p['n_strata_used']:>9}"
              f"{r['gold_rows']:>8}{r['lex_absent_share']:>8.2f}"
              f"{('' if df is None else f'{df:+.2f}'):>8}")
    if a.out:
        with open(os.path.expanduser(a.out), "w") as fh:
            json.dump(rep, fh, indent=2)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
