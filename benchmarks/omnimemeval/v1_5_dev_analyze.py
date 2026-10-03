"""V1.5 development points against the recorded controls, on the 100 development questions only.

docs/EVIDENCE_ALLOCATION_DESIGN.md section 7: settings are chosen on these 100 questions; the claim
is measured once on the other 400. No model is called here. For each search-only point
(search_driver.py) and each recorded control run (results/lme/<run>), on the same questions:
mean cl100k context tokens (the harness's count), and the evidence metrics of evidence_metrics.py
computed with its own functions (locate / evidence_spans), without grades. Controls also show their
recorded accuracy on these questions. "items" must reproduce the published contexts.

Each point is a directory <points dir>/<name>/search.json written by search_driver.py with
SEARCH_INDICES=v1_5_dev_questions.json, against a copy of the published store with its calibration
file, the server at 2.6.5a1: "items" at the defaults, "evidence-b<B>" with
CPERSONA_RECONSTRUCT_SEQUENCE=evidence and CPERSONA_RECONSTRUCT_FORCED_BUDGET=<B>.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> <harness python> v1_5_dev_analyze.py \
       v1_5_dev_questions.json <points dir> [control run ...]
"""
import json
import os
import sqlite3
import statistics
import sys
from pathlib import Path

import tiktoken

DEV, POINTS = Path(sys.argv[1]), Path(sys.argv[2])
CONTROLS = sys.argv[3:] or ["cpersona-lme1-k4", "cpersona-lme1-k6", "cpersona-lme1-kfull",
                            "cpersona-lme1-q320", "cpersona-lme1-q500"]
H = Path(os.environ["OMNIMEMEVAL_DIR"])
sys.path.insert(0, str(Path(DEV).parent))
sys.path.insert(0, str(H / "scripts"))
from evidence_metrics import evidence_spans, locate, merged, overlap, records  # noqa: E402
from longmemeval.lme_data import load_lme_dataframe  # noqa: E402

enc = tiktoken.get_encoding("cl100k_base")
toks = lambda s: len(enc.encode(s, disallowed_special=())) if s else 0  # noqa: E731
dev = [e["index"] for e in json.load(open(DEV))["dev"]]
df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
agent = lambda i: f"lme_exper_user_lme1_{i}"  # noqa: E731

pub = json.load(open(H / "results/lme/cpersona-lme1/cpersona_lme_search_results.json"))
pub_ctx = {i: pub[agent(i)][0]["search_context"] for i in dev}

cache = {}
for i in dev:
    recs = records(conn, agent(i))
    ans_js, turns, _missing = evidence_spans(df.iloc[i], recs)
    cache[i] = (recs, ans_js, turns)


def metrics(ctxs, grades=None):
    rows, unlocated = [], 0
    for i in dev:
        recs, ans_js, turns = cache[i]
        spans, items, _c = locate(ctxs[i], recs, frozenset(ans_js))
        unlocated += _c.get("quoted_chars_unlocated", 0)
        m = {j: merged(v) for j, v in spans.items()}
        quoted = sum(e - s for v in m.values() for s, e in v)
        t_chars = sum(e - s for _, s, e, _ in turns)
        rows.append({
            "tokens": toks(ctxs[i]), "items": len(items),
            "shown": (sum(1 for j in ans_js if j in m) / len(ans_js)) if ans_js else None,
            "all_shown": (all(j in m for j in ans_js)) if ans_js else None,
            "touched": (sum(1 for t in turns if overlap(m.get(t[0], []), t[1], t[2]) > 0) / len(turns)) if turns else None,
            "turn_chars": (sum(overlap(m.get(j, []), s, e) for j, s, e, _ in turns) / t_chars) if t_chars else None,
            "in_ans": sum(e - s for j in ans_js for s, e in m.get(j, [])), "quoted": quoted,
            "correct": grades.get(i) if grades else None,
        })
    def avg(k):
        xs = [r[k] for r in rows if r[k] is not None]
        return statistics.mean(xs) * (100 if k not in ("tokens", "items") else 1) if xs else float("nan")
    q = sum(r["quoted"] for r in rows)
    acc = (statistics.mean(r["correct"] for r in rows) * 100) if grades else None
    return {"tokens": avg("tokens"), "items": avg("items"), "shown": avg("shown"), "all_shown": avg("all_shown"),
            "touched": avg("touched"), "turn_chars": avg("turn_chars"),
            "evidence_share": sum(r["in_ans"] for r in rows) / q * 100 if q else float("nan"), "acc": acc,
            "unlocated": unlocated}


def grades_of(run):
    j = json.load(open(H / "results/lme" / run / "cpersona_lme_judged.json"))
    out = {}
    for i in dev:
        r = j[agent(i)]
        r = r[0] if isinstance(r, list) else r
        out[i] = bool(r["llm_judgments"]["judgment_1"])
    return out


print(f"development questions: {len(dev)}")
print("| point | cl100k tokens | items | answer sessions shown % | all shown % | evidence turns touched % | evidence-turn chars quoted % | evidence share % | accuracy (recorded) | unlocated chars |")
print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
for run in CONTROLS:
    res = json.load(open(H / "results/lme" / run / "cpersona_lme_search_results.json"))
    ctxs = {i: res[agent(i)][0]["search_context"] for i in dev}
    s = metrics(ctxs, grades_of(run))
    print(f"| {run} | {s['tokens']:.1f} | {s['items']:.2f} | {s['shown']:.1f} | {s['all_shown']:.1f} | {s['touched']:.1f} | {s['turn_chars']:.1f} | {s['evidence_share']:.1f} | {s['acc']:.2f} | {s['unlocated']} |")
for d in sorted(POINTS.iterdir(), key=lambda p: (p.name != "items", p.name)):
    f = d / "search.json"
    if not f.exists():
        continue
    rows = {r["i"]: r["search_context"] for r in json.load(open(f))}
    assert set(rows) == set(dev), f"{d.name}: not the development questions"
    s = metrics(rows)
    same = sum(rows[i] == pub_ctx[i] for i in dev)
    tag = f" (same as published {same}/{len(dev)})" if d.name == "items" else ""
    print(f"| {d.name}{tag} | {s['tokens']:.1f} | {s['items']:.2f} | {s['shown']:.1f} | {s['all_shown']:.1f} | {s['touched']:.1f} | {s['turn_chars']:.1f} | {s['evidence_share']:.1f} | — | {s['unlocated']} |")
