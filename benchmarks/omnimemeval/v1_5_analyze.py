"""Read the V1.5 (2.6.5a1) test as registered (benchmarks/measurements/prereg-omnimemeval-lme-v1_5-a1.md).

On the 400 test questions only (those not in v1_5_dev_questions.json): the registered point
(evidence sequence, budget 2,800) against four items and against every item, whose answers are the
count curve's, restricted to the same questions.

- Primary: the point's Context Tokens (the answer-stage prompt tokens the API reported, averaged
  over its calls) are at most 1,000, and the lower bound of the 95% bootstrap interval of the
  paired accuracy difference, point - four items, is above 0 (10,000 resamples over questions,
  seed 20261003). A point whose mean retrieved context differs from four items' on the same
  questions by more than 5% is reported as not at the same cost.
- Secondary: the lower bound of point - every item is at or above -2.0 points.
- Beside them: accuracy per question type, mean retrieved context (cl100k), and the evidence
  metrics of evidence_metrics.py on the same questions.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> V15_USAGE_DIR=<dir> COUNT_CURVE_USAGE_DIR=<dir> \\
       <harness python> v1_5_analyze.py
       (V15_USAGE_DIR holds the point's token_usage_answer.json of each pass, named v15a1-b2800-pass<n>.json)
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

from count_curve_analyze import MARGIN, acc, boot, judged
from evidence_metrics import evidence_spans, locate, merged, overlap, records
from v1_5_build import RUN, test_questions

H = Path(os.environ["OMNIMEMEVAL_DIR"])
USAGE = Path(os.environ["V15_USAGE_DIR"])
CONTEXT_TOKENS_CAP, SAME_COST = 1000.0, 0.05
CONTROLS = {"four items": "cpersona-lme1-k4", "every item": "cpersona-lme1-kfull"}


def api_context_tokens() -> tuple[int, float]:
    calls = prompt = 0
    for p in sorted(USAGE.glob("v15a1-b2800-pass*.json")):
        m = json.loads(p.read_text())["modules"].get("ANSWER")
        if not m:  # a pass that resumed past the answers made no answer call
            continue
        calls += m["usage_reported_call_count"]
        prompt += m["prompt_tokens"]
    return calls, (prompt / calls if calls else float("nan"))


def ctx(p: dict) -> float:
    return sum(v[2] for v in p.values()) / len(p)


def evidence(run: str, uids: list[str], df, conn) -> dict:
    ctxs = json.load(open(H / "results/lme" / run / "cpersona_lme_search_results.json"))
    return evidence_of({uid: ctxs[uid][0]["search_context"] for uid in uids}, uids, df, conn)


def evidence_of(contexts: dict[str, str], uids: list[str], df, conn) -> dict:
    shown, all_shown, touched, turn_q, turn_t, in_ans, quoted, unlocated = [], [], [], 0, 0, 0, 0, 0
    for uid in uids:
        i = int(uid.rsplit("_", 1)[1])
        recs = records(conn, uid)
        ans_js, turns, _missing = evidence_spans(df.iloc[i], recs)
        spans, _items, c = locate(contexts[uid], recs, frozenset(ans_js))
        unlocated += c.get("quoted_chars_unlocated", 0)
        m = {j: merged(v) for j, v in spans.items()}
        if ans_js:
            n = sum(1 for j in ans_js if j in m)
            shown.append(n / len(ans_js))
            all_shown.append(n == len(ans_js))
        if turns:
            touched.append(sum(1 for t in turns if overlap(m.get(t[0], []), t[1], t[2]) > 0) / len(turns))
        turn_t += sum(e - s for _, s, e, _ in turns)
        turn_q += sum(overlap(m.get(j, []), s, e) for j, s, e, _ in turns)
        in_ans += sum(e - s for j in ans_js for s, e in m.get(j, []))
        quoted += sum(e - s for v in m.values() for s, e in v)
    mean = lambda xs: sum(xs) / len(xs) * 100 if xs else float("nan")  # noqa: E731
    return {"shown": mean(shown), "all_shown": mean(all_shown), "touched": mean(touched),
            "turn_chars": turn_q / turn_t * 100 if turn_t else float("nan"),
            "evidence_share": in_ans / quoted * 100 if quoted else float("nan"), "unlocated": unlocated}


def main():
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    test = set(test_questions())
    point = judged(H / "results/lme" / RUN)
    assert {int(u.rsplit("_", 1)[1]) for u in point} == test, "the point did not answer exactly the test questions"
    uids = sorted(point)
    ctrl = {name: {u: v for u, v in judged(H / "results/lme" / run).items() if u in point}
            for name, run in CONTROLS.items()}
    calls, api_ct = api_context_tokens()
    assert calls == len(point), f"{calls} usage-reported answer calls for {len(point)} questions"

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    cats = sorted({v[1] for v in point.values()})
    rows = [("evidence 2,800", point, RUN)] + [(n, ctrl[n], CONTROLS[n]) for n in CONTROLS]
    print(f"test questions: {len(point)}; point Context Tokens (API): {api_ct:.1f} over {calls} calls\n")
    print("| point | accuracy | context (cl100k) | answer sessions shown % | all shown % | evidence turns touched % "
          "| evidence-turn chars quoted % | evidence share % | " + " | ".join(cats) + " |")
    print("| --- |" + " --- |" * (7 + len(cats)))
    for name, p, run in rows:
        e = evidence(run, uids, df, conn)
        by = " | ".join(f"{acc({u: v for u, v in p.items() if v[1] == c}):.1f}" for c in cats)
        print(f"| {name} | {acc(p):.2f} | {ctx(p):.1f} | {e['shown']:.1f} | {e['all_shown']:.1f} | {e['touched']:.1f} "
              f"| {e['turn_chars']:.1f} | {e['evidence_share']:.1f} | {by} |")
        assert e["unlocated"] == 0, f"{name}: {e['unlocated']} quoted characters not found in the store"

    four, full = ctrl["four items"], ctrl["every item"]
    gap = ctx(point) / ctx(four) - 1
    same_cost = abs(gap) <= SAME_COST
    d, lo, hi = boot(point, four)
    d_f, lo_f, hi_f = boot(point, full)
    primary = api_ct <= CONTEXT_TOKENS_CAP and lo > 0
    print(f"\ncontext against four items: {ctx(point):.1f} vs {ctx(four):.1f} ({gap * 100:+.1f}%, "
          f"{'same cost' if same_cost else 'NOT at the same cost'})")
    print(f"primary: Context Tokens {api_ct:.1f} {'<=' if api_ct <= CONTEXT_TOKENS_CAP else '>'} 1,000; "
          f"point - four items {d:+.2f} ({lo:+.2f} to {hi:+.2f}) -> {'MET' if primary else 'not met'}")
    print(f"secondary: point - every item {d_f:+.2f} ({lo_f:+.2f} to {hi_f:+.2f}) -> "
          f"{'holds' if lo_f >= MARGIN else 'does not hold'}")


if __name__ == "__main__":
    main()
