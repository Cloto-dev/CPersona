"""Is a different isolation bucket a valid null population? (read-only probe)

Run against a deployment database:

    PROBE_DB=/path/to/cpersona.db python benchmarks/measurements/null_bucket_probe.py

Set PROBE_PACKAGE_ROOT when the package is not already importable.

The conditional-evidence fusion mode needs a null it can estimate in a
deployment. The candidate is "a different isolation bucket": pairs whose
irrelevance is established by construction rather than assumed. The design says
such a population is not automatically valid -- a different corpus moves the
score marginals through vocabulary and length, for reasons that have nothing to
do with relevance -- and that this must be measured before it is used.

This measures it, on the real database, read-only.

For an ordered pair of buckets (A, B): take queries from A, score them against
A's own rows (the law the estimator would see) and against B's rows (the law it
would use as the null). Under the hypothesis that B is a valid null, the two
laws agree except in the tail where A's own relevant rows sit. A disagreement in
the BULK is compositional, and it enters the estimator as spurious evidence:

    J_spurious(cell) = log p_own(cell) - log p_foreign(cell)

That number is directly comparable with the real conditional evidence measured
on the benchmark (1.2 to 5.3 nats), which is the whole point: if swapping the
null for a foreign bucket moves the ratio as much as relevance does, the foreign
bucket cannot serve as the null.

Writes nothing. Prints JSON aggregates only -- no stored content leaves the host.
"""
import json
import os
import sqlite3
import sys

import numpy as np

DB = os.environ["PROBE_DB"]          # required: no default points at anyone's database
MIN_BUCKET = 90          # buckets smaller than this cannot support a panel
N_QUERIES = 60           # queries sampled per source bucket
SEED = 20260910
COS_BINS = 6             # quantile bins of the own-bucket cosine law
BM25_BINS = 4            # quantile bins among rows that match lexically
MIN_CELL = 30            # a cell below this is reported, never interpreted

if os.environ.get("PROBE_PACKAGE_ROOT"):
    sys.path.insert(0, os.path.expanduser(os.environ["PROBE_PACKAGE_ROOT"]))
os.environ.setdefault("CPERSONA_DB_PATH", "/tmp/__probe_never_used.db")
from cpersona.memory_handlers import _build_fts_query  # noqa: E402  production's own builder


def qbins(x, k):
    """Quantile edges; collapses when the sample is degenerate."""
    if len(x) == 0:
        return np.array([0.0])
    e = np.unique(np.quantile(x, np.linspace(0, 1, k + 1)[1:-1]))
    return e


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    rows = con.execute(
        "SELECT id, COALESCE(NULLIF(project_id,''),'(global)'), agent_id, content, embedding "
        "FROM memories WHERE embedding IS NOT NULL"
    ).fetchall()

    ids = np.array([r[0] for r in rows])
    bucket = np.array([f"{r[1]}/{r[2]}" for r in rows])
    lengths = np.array([len(r[3] or "") for r in rows], dtype=float)
    vecs = np.vstack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
    # the stored contract is unit norm; check rather than assume
    norms = np.linalg.norm(vecs, axis=1)
    texts = {int(r[0]): (r[3] or "") for r in rows}

    names, counts = np.unique(bucket, return_counts=True)
    keep = [str(n) for n, c in zip(names, counts) if c >= MIN_BUCKET]

    out = {
        "db_rows": len(ids),
        "dim": int(vecs.shape[1]),
        "unit_norm": {"min": float(norms.min()), "max": float(norms.max())},
        "buckets": {b: {"n": int((bucket == b).sum()),
                        "len_p10": float(np.percentile(lengths[bucket == b], 10)),
                        "len_p50": float(np.percentile(lengths[bucket == b], 50)),
                        "len_p90": float(np.percentile(lengths[bucket == b], 90))}
                    for b in keep},
        "pairs": {},
        "params": {"n_queries": N_QUERIES, "cos_bins": COS_BINS, "bm25_bins": BM25_BINS,
                   "min_cell": MIN_CELL, "seed": SEED, "min_bucket": MIN_BUCKET},
    }

    rng = np.random.default_rng(SEED)
    idx_of = {b: np.where(bucket == b)[0] for b in keep}

    # one lexical query per sampled query row, sliced per bucket afterwards
    per_query = {}
    picks = {}
    for b in keep:
        pool = idx_of[b]
        pick = rng.choice(pool, size=min(N_QUERIES, len(pool)), replace=False)
        picks[b] = [int(ids[i]) for i in pick]
        for i in pick:
            qid = int(ids[i])
            if qid in per_query:
                continue
            fts = _build_fts_query(texts[qid][:2000])
            bm = {}
            if fts:
                try:
                    for rid, score in con.execute(
                        "SELECT m.id, bm25(memories_fts) FROM memories_fts f "
                        "JOIN memories m ON f.rowid = m.id WHERE memories_fts MATCH ? "
                        "ORDER BY rank LIMIT 4000", (fts,)):
                        bm[int(rid)] = -float(score)      # larger is a better match
                except sqlite3.OperationalError as e:
                    out.setdefault("fts_errors", []).append(str(e)[:120])
            per_query[qid] = (i, bm)
        out["buckets"][b]["queries_used"] = int(len(pick))

    def law(q_rows, target_rows):
        """(cosine, bm25-or-absent) pairs of the given queries against the targets."""
        cos, lex = [], []
        for qid in q_rows:
            i, bm = per_query[qid]
            sims = vecs[target_rows] @ vecs[i]
            for k, t in enumerate(target_rows):
                if t == i:
                    continue                   # never score a row against itself
                cos.append(float(sims[k]))
                lex.append(bm.get(int(ids[t]), -np.inf))
        return np.array(cos), np.array(lex)

    for a in keep:
        qa = picks[a]                       # the same draw the lexical pass used
        cos_own, lex_own = law(qa, idx_of[a])
        # Control: a random half of A is compositionally IDENTICAL to A by
        # construction, so whatever this statistic reports against it is the
        # floor of the machinery -- finite samples, smoothing, and A's own
        # relevant tail. A foreign bucket must be read against this floor, not
        # against zero, or the instrument's own noise is scored as composition.
        half = np.random.default_rng(SEED + 1).permutation(idx_of[a])[: len(idx_of[a]) // 2]
        ce = qbins(cos_own, COS_BINS)
        present = lex_own[np.isfinite(lex_own)]
        le = qbins(present, BM25_BINS)

        def grid(cos, lex):
            ci = np.digitize(cos, ce)
            li = np.where(np.isfinite(lex), np.digitize(lex, le) + 1, 0)   # 0 = absent
            g = np.zeros((len(ce) + 1, len(le) + 2))
            np.add.at(g, (ci, li), 1)
            return g

        g_own = grid(cos_own, lex_own)

        def jstats(g_a, g_b):
            p_a = (g_a + 1) / (g_a.sum() + g_a.size)
            p_b = (g_b + 1) / (g_b.sum() + g_b.size)
            J_ = np.log(p_a / p_b)
            ok_ = (g_a >= MIN_CELL) & (g_b >= MIN_CELL)
            return J_, ok_

        cos_h, lex_h = law(qa, half)
        J_h, ok_h = jstats(g_own, grid(cos_h, lex_h))
        out["pairs"][f"{a} -> RANDOM HALF OF ITSELF (control)"] = {
            "control": True,
            "cells_readable": int(ok_h.sum()),
            "max_abs_J": round(float(np.abs(J_h[ok_h]).max()), 3) if ok_h.any() else None,
            "median_abs_J": round(float(np.median(np.abs(J_h[ok_h]))), 3) if ok_h.any() else None,
            "cos_shift_p50": round(float(np.median(cos_h) - np.median(cos_own)), 4),
            "cos_shift_p99": round(float(np.percentile(cos_h, 99) - np.percentile(cos_own, 99)), 4),
        }
        for b in keep:
            if b == a:
                continue
            cos_f, lex_f = law(qa, idx_of[b])
            g_for = grid(cos_f, lex_f)
            p_own = (g_own + 1) / (g_own.sum() + g_own.size)
            p_for = (g_for + 1) / (g_for.sum() + g_for.size)
            J = np.log(p_own / p_for)
            ok = (g_own >= MIN_CELL) & (g_for >= MIN_CELL)
            cells = []
            for ci in range(J.shape[0]):
                for li in range(J.shape[1]):
                    if ok[ci, li]:
                        cells.append({"cos_bin": ci, "lex_bin": li, "J": round(float(J[ci, li]), 3),
                                      "n_own": int(g_own[ci, li]), "n_foreign": int(g_for[ci, li])})
            out["pairs"][f"{a} -> {b}"] = {
                "pairs_own": int(g_own.sum()), "pairs_foreign": int(g_for.sum()),
                "cells_readable": len(cells),
                "max_abs_J": round(float(np.abs(J[ok]).max()), 3) if ok.any() else None,
                "median_abs_J": round(float(np.median(np.abs(J[ok]))), 3) if ok.any() else None,
                "cos_shift_p50": round(float(np.median(cos_f) - np.median(cos_own)), 4),
                "cos_shift_p99": round(float(np.percentile(cos_f, 99) - np.percentile(cos_own, 99)), 4),
                "lex_absent_own": round(float((~np.isfinite(lex_own)).mean()), 4),
                "lex_absent_foreign": round(float((~np.isfinite(lex_f)).mean()), 4),
                "cells": cells,
            }
    con.close()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
