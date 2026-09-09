"""Per-query view of the fusion delta from the frozen-stage replay.

For each model x task, from $REPLAY_ROOT/replay_<model>/<task>.queries.jsonl:
  1. delta_fusion (S2 - S1) binned by the query's top dense cosine (quartiles):
     does fusion harm concentrate where dense is already confident?
  2. a one-parameter switch rule: use dense-only (S1) when top_cos >= tau,
     else shipped RRF (S2). Sweep tau; report the best tau per task and the
     NDCG at a single tau shared across the model's tasks (is there a
     task-invariant switch at all, on the raw cosine scale?).
  3. oracle per-query choice max(S1, S2) = the ceiling any per-query rule can reach.
  4. gold visibility: share of queries whose gold has no lexical vote / gold
     below the floor / gold missing from the stored corpus (dedup collapse).
"""
import json
import os
import sys
import statistics as st
import numpy as np

H = os.environ.get("REPLAY_ROOT", ".")
MODELS = [tuple(a.split("=")) for a in sys.argv[1:]] or [("jinanano_cap200", "jina-v5-nano"), ("bgem3", "bge-m3"), ("minilm", "MiniLM")]  # argv: dir=label ...
TASKS = ["LMEB_SciFact", "ESGReports", "QASPER", "TMD", "EPBench", "ReMe", "Gorilla",
         "MemBench", "ConvoMem", "LongMemEval", "MLDR"]
TAUS = [round(x, 2) for x in np.arange(0.30, 0.96, 0.05)]


def rows(model, task):
    p = f"{H}/replay_{model}/{task}.queries.jsonl"
    if not os.path.exists(p):
        return []
    return [json.loads(line) for line in open(p)]


def m100(xs):
    return 100 * float(np.mean(xs)) if len(xs) else float("nan")


for model, label in MODELS:
    per_task = {}
    for t in TASKS:
        R = rows(model, t)
        if not R:
            continue
        per_task[t] = R
    if not per_task:
        continue
    print(f"\n===== {label} =====")
    print("1) delta_fusion by top-cosine quartile (mean NDCG pts; n)")
    print(f"{'task':13s} " + " ".join(f"{'Q'+str(i+1):>14s}" for i in range(4)) + "   corr(top_cos, delta)")
    for t, R in per_task.items():
        tc = np.array([r["top_cos"] if r["top_cos"] is not None else 0.0 for r in R])
        d = np.array([r["s2"] - r["s1"] for r in R])
        qs = np.quantile(tc, [0.25, 0.5, 0.75])
        bins = np.digitize(tc, qs)
        cells = []
        for b in range(4):
            sel = bins == b
            cells.append(f"{100*d[sel].mean():+6.2f}(n{sel.sum():4d})" if sel.any() else f"{'-':>14s}")
        corr = np.corrcoef(tc, d)[0, 1] if tc.std() > 0 and d.std() > 0 else float("nan")
        print(f"{t:13s} " + " ".join(cells) + f"   {corr:+.3f}")

    print("\n2) switch rule: dense-only if top_cos >= tau else RRF.  columns: S1(w=0) | S2(shipped) | oracle max(S1,S2) | best tau -> NDCG | NDCG at shared tau")
    # shared tau = the tau maximising the mean over this model's tasks
    shared_scores = {tau: [] for tau in TAUS}
    best_rows = {}
    for t, R in per_task.items():
        s1 = np.array([r["s1"] for r in R])
        s2 = np.array([r["s2"] for r in R])
        tc = np.array([r["top_cos"] if r["top_cos"] is not None else 0.0 for r in R])
        res = {}
        for tau in TAUS:
            v = np.where(tc >= tau, s1, s2)
            res[tau] = 100 * v.mean()
            shared_scores[tau].append(res[tau])
        best_tau = max(res, key=res.get)
        best_rows[t] = (100 * s1.mean(), 100 * s2.mean(), 100 * np.maximum(s1, s2).mean(), best_tau, res)
    shared_tau = max(shared_scores, key=lambda k: np.mean(shared_scores[k]))
    print(f"   shared tau for {label} = {shared_tau}")
    print(f"{'task':13s} {'S1':>6s} {'S2':>6s} {'oracle':>6s} | {'tau*':>5s} {'@tau*':>6s} | {'@shared':>7s} {'vs max(S1,S2)':>13s}")
    for t, (a, b, orc, bt, res) in best_rows.items():
        print(f"{t:13s} {a:6.2f} {b:6.2f} {orc:6.2f} | {bt:5.2f} {res[bt]:6.2f} | {res[shared_tau]:7.2f} {res[shared_tau]-max(a,b):+13.2f}")

    print("\n4) gold visibility (share of queries): gold with no lexical vote at all | any gold below floor | any gold missing (dedup) | lexical LIKE fallback")
    print(f"{'task':13s} {'n':>5s} {'nolex%':>7s} {'belowfloor%':>11s} {'missing%':>9s} {'like%':>6s} {'mean n_lex':>10s} {'mean n_adm':>10s}")
    for t, R in per_task.items():
        n = len(R)
        nolex = sum(1 for r in R if r["gold_lex_rank"] and max(r["gold_lex_rank"]) < 0 and all(x < 0 for x in r["gold_lex_rank"]))
        below = sum(1 for r in R if r["gold_below_floor"] > 0)
        miss = sum(1 for r in R if r["gold_missing"] > 0)
        like = sum(1 for r in R if r.get("lex_like_fallback"))
        print(f"{t:13s} {n:5d} {100*nolex/n:7.1f} {100*below/n:11.1f} {100*miss/n:9.1f} {100*like/n:6.1f} "
              f"{st.mean(r['n_lex'] for r in R):10.1f} {st.mean(r['n_dense_adm'] for r in R):10.1f}")
