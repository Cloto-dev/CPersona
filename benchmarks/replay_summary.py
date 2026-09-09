"""Tabulate the frozen-stage replay (frozen_replay.py) across models.

Reads $REPLAY_ROOT/replay_<model>/<task>.json (REPLAY_ROOT defaults to the current directory) (frozen_replay.py output) and the
Track A references, prints the stage decomposition per task and model plus
the harm classification and the lexical-weight sweep.
"""
import json
import os
import sys

H = os.environ.get("REPLAY_ROOT", ".")
LMEB = os.path.expanduser(os.environ.get("LMEB_DIR", "~/lmeb"))
MODELS = [tuple(a.split("=")) for a in sys.argv[1:]] or [("jinanano_cap200", "jina-v5-nano"), ("bgem3", "bge-m3"), ("minilm", "MiniLM")]  # argv: dir=label ...
TASKS = ["LMEB_SciFact", "ESGReports", "QASPER", "TMD", "EPBench", "ReMe", "Gorilla",
         "MemBench", "ConvoMem", "LongMemEval", "MLDR"]
# Track A references: the raw-embedding runs of 2026-07-14 (jina) and 2026-09-08 (bge-m3, per-task json under $LMEB_DIR).
TRACK_A = {"jinanano": {"EPBench": 80.84, "TMD": 30.07, "MemBench": 69.62, "ConvoMem": 65.05, "QASPER": 48.50,
                        "ESGReports": 49.11, "MLDR": 79.98, "LMEB_SciFact": 82.18, "Gorilla": 35.89, "ReMe": 65.24,
                        "LongMemEval": 77.42}}
bge = {}
_summary = f"{LMEB}/lmeb_results_bgem3_20260908/bge-m3/_summary.json"
if os.path.exists(_summary):
    # per_task holds the task-level mean over subsets on the 0-1 scale
    bge = {t: round(100 * v, 2) for t, v in json.load(open(_summary)).get("per_task", {}).items()}
TRACK_A["bgem3"] = bge


def load(model, task):
    p = f"{H}/replay_{model}/{task}.json"
    return json.load(open(p)) if os.path.exists(p) else None


print("stage decomposition (mean over subtasks; d_* = transition deltas; A = Track A reference, S0 should equal it)")
hdr = f"{'task':13s} {'model':12s} {'A':>6s} {'S0':>6s} {'S1':>6s} {'S2':>6s} {'S3':>6s} | {'adm':>6s} {'fus':>6s} {'gate':>5s} | {'H':>5s} {'C':>5s} | {'S2nf-S0':>7s} | id"
print(hdr)
for t in TASKS:
    for m, label in MODELS:
        j = load(m, t)
        if not j:
            continue
        mm = j["mean"]
        a = TRACK_A.get(m.split("_")[0], {}).get(t)
        idn = j["identity"]
        idtxt = f"{idn['checked']}/{idn['s2_mismatch']}/{idn['s3_mismatch']}"
        print(f"{t:13s} {label:12s} {a if a is not None else float('nan'):6.2f} {mm['S0_dense']:6.2f} {mm['S1_admitted']:6.2f} "
              f"{mm['S2_rrf']:6.2f} {mm['S3_gated']:6.2f} | {mm['d_admission']:+6.2f} {mm['d_fusion']:+6.2f} {mm['d_gate']:+5.2f} | "
              f"{mm['fusion_H']:5.2f} {mm['fusion_C']:5.2f} | {mm['d_fusion_nofloor_vs_dense']:+7.2f} | {idtxt}")

print("\nlexical-weight sweep (mean NDCG@10 at w; w=0 is dense+admission, w=1 is shipped RRF)")
print(f"{'task':13s} {'model':12s} " + " ".join(f"{w:>6s}" for w in ("0.0", "0.1", "0.25", "0.5", "0.75", "1.0")) + "   best_w")
for t in TASKS:
    for m, label in MODELS:
        j = load(m, t)
        if not j:
            continue
        ws = j["w_sweep_mean"]
        best = max(ws, key=ws.get)
        print(f"{t:13s} {label:12s} " + " ".join(f"{ws[w]:6.2f}" for w in ("0.0", "0.1", "0.25", "0.5", "0.75", "1.0")) + f"   {best}")

print("\ntop-10 moves under fusion (summed over subtasks): intruders by origin, gold pushed out by lexical vote, gold pulled in by origin")
print(f"{'task':13s} {'model':12s} {'harmed':>6s} {'helped':>6s} {'same':>6s} | {'intr_lexonly':>12s} {'intr_dense':>10s} | {'out_nolex':>9s} {'out_lex':>7s} | {'in_lexonly':>10s} {'in_dense':>8s} | {'lex_like_fb%':>12s}")
for t in TASKS:
    for m, label in MODELS:
        j = load(m, t)
        if not j:
            continue
        agg = {}
        h = he = s = 0
        for v in j["subtasks"].values():
            h += v["n_harmed"]
            he += v["n_helped"]
            s += v["n_unchanged"]
            for kk, vv in v["top10_moves"].items():
                agg[kk] = agg.get(kk, 0) + vv
        # LIKE-fallback share from the per-query file
        qf = f"{H}/replay_{m}/{t}.queries.jsonl"
        fb = n = 0
        if os.path.exists(qf):
            for line in open(qf):
                r = json.loads(line)
                n += 1
                fb += 1 if r.get("lex_like_fallback") else 0
        print(f"{t:13s} {label:12s} {h:6d} {he:6d} {s:6d} | {agg.get('intruder_lexical_only',0):12d} {agg.get('intruder_dense_boosted',0):10d} | "
              f"{agg.get('gold_out_no_lex_vote',0):9d} {agg.get('gold_out_had_lex_vote',0):7d} | {agg.get('gold_in_lexical_only',0):10d} {agg.get('gold_in_dense_boosted',0):8d} | {100*fb/max(n,1):11.1f}%")

print("\ncorpus groups: lines vs stored (dedup collapse), calibration source, floor, pool")
for t in TASKS:
    for m, label in MODELS:
        j = load(m, t)
        if not j:
            continue
        for g in j["groups"]:
            c = g["calibration"]
            print(f"{t:13s} {label:12s} {g['corpus'][:40]:40s} lines={g['lines']:6d} stored={g['stored']:6d} "
                  f"cal={c['source']}/{c.get('method')} thr={c.get('threshold')} J={c.get('youden_j')} floor={g['floor']:.4f} pool={g['pool']}")
