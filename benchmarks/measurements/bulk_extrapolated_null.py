"""Can a query's own bulk predict its own null tail? (route (c), tested with labels)

A different isolation bucket cannot serve as the null: its composition moves the
marginals as much as relevance does. What that measurement also showed is where
the remaining route has to live -- inside ONE bucket, because a random split of a
single bucket is the only population whose composition is exact.

So the structural assumption to state and refute is this:

    ASSUMPTION C1. For a given query, the score law of the IRRELEVANT rows is a
    declared family whose parameters are fixed by that query's own central bulk.
    Relevant rows are rare enough (order 1e-3 of pairs) that they cannot move a
    robust centre or spread, so the bulk is estimable without labels; the tail is
    then a prediction, and whatever the observed law holds in excess of it is the
    relevant mass.

It is query-conditional by construction, which is what the identifiability note
requires and what the design page lost when it averaged over queries. It is also
already half-shipped: the calibration sidecar sets its admission threshold from
the mean and standard deviation of sampled pairs, which is this assumption with a
Gaussian family. So this test audits a shipped assumption as well as a proposed
one.

THE TEST. On the benchmark the true null is known, because the labels say which
rows are irrelevant. Fit the family on a query's bulk WITHOUT looking at labels,
predict the null's survival in the decision region, and compare with the labelled
truth. The error is reported in the units the mode would use:

    J_error = log( predicted null survival / true null survival )

against the real conditional evidence of 1.2 to 5.3 nats measured on the same
corpora. If the error is that size, route (c) fails in this form and the mode has
no null it can estimate anywhere.

WHAT REFUTES WHAT, fixed before the numbers are read:
  * the family is refuted for a query if it cannot predict a HELD-OUT part of the
    bulk -- a band that was not used for fitting and that relevance cannot reach;
  * the assumption is refuted for the mode if the median |J_error| over queries
    reaches 1.2 nats, the smallest real signal measured;
  * a query whose deep sample cannot support the fit is abstained on, counted,
    and never scored as a success.

Reads the row-keyed dumps of labelled_evidence_dump.py. The dumps are stratified
samples with known inclusion probabilities, so every estimate here is weighted;
an unweighted read of them would over-represent the head by construction.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
from pathlib import Path

import numpy as np

KEEP_ALL_BELOW = 30          # ranks below this were dumped whole
DEEP_SAMPLE = 20             # per deeper bin
BIN_EDGES = [0, 1, 3, 10, 30, 100, 300, 1000]
#: quantiles of a query's own scores used to fix the family; well inside the bulk
FIT_Q = (0.25, 0.75)
#: a band held out of the fit that relevance cannot reach, used to refute the family
HELDOUT_Q = (0.80, 0.90)
#: survival levels at which the null is predicted, spanning the decision region
TARGET_RANKS = (1, 5, 10, 30)
MIN_DEEP_ROWS = 25           # below this the fit is abstained on


def bin_of(rank: int) -> int:
    for i in range(len(BIN_EDGES) - 1, -1, -1):
        if rank >= BIN_EDGES[i]:
            return i
    return 0


def wquantile(x, w, q):
    """Weighted quantile; x need not be sorted."""
    o = np.argsort(x)
    x, w = x[o], w[o]
    c = np.cumsum(w) - 0.5 * w
    c /= w.sum()
    return np.interp(q, c, x)


def load(dump_dir: Path, task: str):
    meta = json.load(open(dump_dir / f"{task}.meta.json"))
    # A query is (subtask, id). Several tasks number their queries per subtask,
    # so the bare id names a different question against a different corpus in
    # each one; keying on it merges them and the fitted "bulk" becomes a blend
    # of corpora that no query ever saw.
    info = {(m["subtask"], m["qid"]): m for m in meta["queries"]}
    rows: dict[tuple, list] = {}
    with gzip.open(dump_dir / f"{task}.rows.csv.gz", "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.setdefault((r["subtask"], r["qid"]), []).append(
                (int(r["vr"]), float(r["v"]), int(r["y"])))
    return meta, info, rows


def weights(vr, y, qmeta):
    """Inclusion probability of each dumped row, from the dump's own sampling rule."""
    w = np.ones(len(vr))
    pop = {int(k): v for k, v in qmeta["bin_pop"].items()}
    for i, (r, yy) in enumerate(zip(vr, y)):
        if yy == 1 or r < KEEP_ALL_BELOW:
            continue                            # kept with certainty
        n = pop.get(bin_of(int(r)))
        if n and n > DEEP_SAMPLE:
            w[i] = n / DEEP_SAMPLE
    return w


def fit_family(v, w, family):
    """Centre and scale from a robust interval of the query's own scores."""
    lo, hi = wquantile(v, w, FIT_Q[0]), wquantile(v, w, FIT_Q[1])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None
    mu = 0.5 * (lo + hi)
    if family == "gaussian":                     # the shipped calibration's family
        sigma = (hi - lo) / 1.349
        return ("gaussian", mu, sigma)
    if family == "logistic":                     # heavier tail, same two parameters
        s = (hi - lo) / (2 * math.log(3))
        return ("logistic", mu, s)
    raise ValueError(family)


def survival(fit, x):
    kind, mu, s = fit
    if s <= 0:
        return np.nan
    if kind == "gaussian":
        return 0.5 * math.erfc((x - mu) / (s * math.sqrt(2)))
    return 1.0 / (1.0 + math.exp((x - mu) / s))


def run(dump_dir: Path, tasks, family, out_path):
    report = {"dump_dir": str(dump_dir), "family": family, "tasks": {}}
    for task in tasks:
        meta, info, rows = load(dump_dir, task)
        errs = {k: [] for k in TARGET_RANKS}
        held_err, abstain, used = [], 0, 0
        for qid, rr in rows.items():
            qm = info.get(qid)
            if qm is None or qm["lex_fallback"]:
                abstain += 1
                continue
            vr = np.array([a for a, _, _ in rr])
            v = np.array([b for _, b, _ in rr])
            y = np.array([c for _, _, c in rr])
            w = weights(vr, y, qm)
            if (vr >= KEEP_ALL_BELOW).sum() < MIN_DEEP_ROWS:
                abstain += 1
                continue
            fit = fit_family(v, w, family)
            if fit is None:
                abstain += 1
                continue
            used += 1
            n_elig = max(qm["n_elig"], 1)

            # refutation 1: a held-out band of the bulk the fit never saw
            for q in HELDOUT_Q:
                x = wquantile(v, w, q)
                pred = survival(fit, x)
                if pred > 0:
                    held_err.append(math.log(max(pred, 1e-12) / max(1 - q, 1e-12)))

            # refutation 2: the null's survival in the decision region.
            # truth = weighted share of NON-GOLD rows above the cut.
            nz = y == 0
            for k in TARGET_RANKS:
                idx = np.where(vr == k)[0]
                if len(idx) == 0:
                    continue
                x = float(v[idx[0]])
                true_above = float(w[nz & (v >= x)].sum()) / float(w[nz].sum())
                pred = survival(fit, x)
                if true_above > 0 and pred > 0:
                    errs[k].append(math.log(pred / true_above))
            del n_elig
        rec = {"queries_used": used, "abstained": abstain,
               "heldout_bulk_median_log_err":
                   round(float(np.median(np.abs(held_err))), 3) if held_err else None,
               "decision_region": {}}
        for k in TARGET_RANKS:
            e = np.abs(errs[k])
            rec["decision_region"][f"rank{k}"] = {
                "n": int(len(e)),
                "median_abs_J_error": round(float(np.median(e)), 3) if len(e) else None,
                "p90_abs_J_error": round(float(np.percentile(e, 90)), 3) if len(e) else None,
            }
        report["tasks"][task] = rec
    if out_path:
        json.dump(report, open(out_path, "w"), indent=2)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump_dir", required=True)
    ap.add_argument("--tasks", default=None)
    ap.add_argument("--family", default="gaussian", choices=["gaussian", "logistic"])
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    d = Path(os.path.expanduser(a.dump_dir))
    tasks = a.tasks.split(",") if a.tasks else sorted(p.name.split(".")[0] for p in d.glob("*.meta.json"))
    rep = run(d, tasks, a.family, a.out)
    print(f"family = {a.family}   (real conditional evidence for scale: 1.2 - 5.3 nats)")
    print(f"{'task':<14}{'used':>6}{'abst':>6}{'heldout':>9}"
          + "".join(f"{'r'+str(k):>9}" for k in TARGET_RANKS))
    for t, r in rep["tasks"].items():
        cells = []
        for k in TARGET_RANKS:
            m = r["decision_region"][f"rank{k}"]["median_abs_J_error"]
            cells.append(f"{m:>9.2f}" if m is not None else f"{'-':>9}")
        h = r["heldout_bulk_median_log_err"]
        print(f"{t:<14}{r['queries_used']:>6}{r['abstained']:>6}"
              f"{(f'{h:.2f}' if h is not None else '-'):>9}" + "".join(cells))


if __name__ == "__main__":
    main()
