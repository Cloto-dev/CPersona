"""Read the whole-against-evidence test as registered (benchmarks/measurements/prereg-omnimemeval-lme-v1_5-whole.md).

On the 2.6.5a1 test's 400 questions. whole's answer to a question is the point's
(results/lme/cpersona-lme1-v15whole-b2800) when its context changed, and the 2.6.5a1 test's recorded
answer otherwise: the context, and so the prompt, is the same. evidence is the 2.6.5a1 test's recorded
answers; four items is the count curve's, restricted to the same questions.
- Primary: whole's mean retrieved context (cl100k, the harness's count) is within 5% of evidence's on
  these questions, and the lower bound of the 95% bootstrap interval of the paired accuracy difference,
  whole - evidence, is at or above -2.0 points (10,000 resamples over questions, seed 20261003).
- Secondary: whole's Context Tokens are at most 1,000, and the lower bound of whole - four items is
  above 0. whole's Context Tokens = the 2.6.5a1 test's (the answer-stage prompt tokens the API
  reported, averaged over its calls) + the sum over the changed questions of (o200k_base tokens of
  whole's context - those of the 2.6.5a1 context) / the number of questions.
- Beside them: the changed questions and their types, accuracy per question type, the prompt tokens the
  API reported for the changed calls, and the evidence metrics of evidence_metrics.py on the same
  questions.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> V15_USAGE_DIR=<dir> WHOLE_USAGE_DIR=<dir> \\
       COUNT_CURVE_USAGE_DIR=<dir> <harness python> v1_5_whole_analyze.py
       (V15_USAGE_DIR holds the 2.6.5a1 test's token_usage_answer.json of each pass, named
       v15a1-b2800-pass<n>.json; WHOLE_USAGE_DIR the point's, named v15whole-b2800-pass<n>.json)
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import tiktoken
from count_curve_analyze import MARGIN, acc, boot, judged
from v1_5_analyze import CONTEXT_TOKENS_CAP, SAME_COST, api_context_tokens, ctx, evidence_of
from v1_5_build import RUN as A1_RUN
from v1_5_build import test_questions
from v1_5_whole_build import RUN

H = Path(os.environ["OMNIMEMEVAL_DIR"])
WHOLE_USAGE = Path(os.environ["WHOLE_USAGE_DIR"])
FOUR = "cpersona-lme1-k4"


def contexts(run: str) -> dict[str, str]:
    data = json.load(open(H / "results/lme" / run / "cpersona_lme_search_results.json"))
    return {uid: conv[0]["search_context"] for uid, conv in data.items()}


def changed_calls() -> tuple[int, int]:
    calls = prompt = 0
    for p in sorted(WHOLE_USAGE.glob("v15whole-b2800-pass*.json")):
        m = json.loads(p.read_text())["modules"].get("ANSWER")
        if not m:  # a pass that resumed past the answers made no answer call
            continue
        calls += m["usage_reported_call_count"]
        prompt += m["prompt_tokens"]
    return calls, prompt


def main():
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    test = set(test_questions())
    evidence = judged(H / "results/lme" / A1_RUN)
    assert {int(u.rsplit("_", 1)[1]) for u in evidence} == test, "the 2.6.5a1 test did not answer the test questions"
    uids = sorted(evidence)
    four = {u: v for u, v in judged(H / "results/lme" / FOUR).items() if u in evidence}
    a1_ctx = contexts(A1_RUN)
    point_dir = H / "results/lme" / RUN
    answered = judged(point_dir) if point_dir.exists() else {}
    moved_ctx = contexts(RUN) if point_dir.exists() else {}
    assert set(answered) == set(moved_ctx), "the point did not answer exactly its changed questions"
    assert all(moved_ctx[u] != a1_ctx[u] for u in moved_ctx), "a question the point answered did not change"
    whole = {u: answered.get(u, evidence[u]) for u in uids}
    whole_ctx = {u: moved_ctx.get(u, a1_ctx[u]) for u in uids}

    calls, a1_ct = api_context_tokens()
    assert calls == len(uids), f"{calls} usage-reported answer calls in the 2.6.5a1 test for {len(uids)} questions"
    enc = tiktoken.get_encoding("o200k_base")
    delta = sum(len(enc.encode(whole_ctx[u])) - len(enc.encode(a1_ctx[u])) for u in moved_ctx)
    whole_ct = a1_ct + delta / len(uids)
    m_calls, m_prompt = changed_calls()
    assert m_calls == len(moved_ctx), f"{m_calls} usage-reported answer calls for {len(moved_ctx)} changed questions"

    print(f"test questions: {len(uids)}; changed: {len(moved_ctx)}")
    for u in sorted(moved_ctx, key=lambda u: int(u.rsplit("_", 1)[1])):
        print(f"  {int(u.rsplit('_', 1)[1])}: {evidence[u][1]}, evidence {'right' if evidence[u][0] else 'wrong'} "
              f"-> whole {'right' if whole[u][0] else 'wrong'}")
    print(f"Context Tokens: evidence {a1_ct:.1f} (API); whole {whole_ct:.1f} "
          f"(o200k difference over the changed questions {delta:+d})")
    if m_calls:
        print(f"the changed calls' prompt tokens (API): {m_prompt} over {m_calls} calls ({m_prompt / m_calls:.1f} each)")

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    four_ctx = contexts(FOUR)
    cats = sorted({v[1] for v in evidence.values()})
    rows = [("whole 2,800", whole, whole_ctx), ("evidence 2,800", evidence, a1_ctx), ("four items", four, four_ctx)]
    print("\n| point | accuracy | context (cl100k) | answer sessions shown % | all shown % | evidence turns touched % "
          "| evidence-turn chars quoted % | evidence share % | " + " | ".join(cats) + " |")
    print("| --- |" + " --- |" * (7 + len(cats)))
    for name, p, cs in rows:
        e = evidence_of(cs, uids, df, conn)
        by = " | ".join(f"{acc({u: v for u, v in p.items() if v[1] == c}):.1f}" for c in cats)
        print(f"| {name} | {acc(p):.2f} | {ctx(p):.1f} | {e['shown']:.1f} | {e['all_shown']:.1f} | {e['touched']:.1f} "
              f"| {e['turn_chars']:.1f} | {e['evidence_share']:.1f} | {by} |")
        assert e["unlocated"] == 0, f"{name}: {e['unlocated']} quoted characters not found in the store"

    gap = ctx(whole) / ctx(evidence) - 1
    same_cost = abs(gap) <= SAME_COST
    d, lo, hi = boot(whole, evidence)
    d4, lo4, hi4 = boot(whole, four)
    primary = same_cost and lo >= MARGIN
    secondary = whole_ct <= CONTEXT_TOKENS_CAP and lo4 > 0
    print(f"\ncontext against evidence: {ctx(whole):.1f} vs {ctx(evidence):.1f} ({gap * 100:+.1f}%, "
          f"{'same cost' if same_cost else 'NOT at the same cost'})")
    print(f"primary: whole - evidence {d:+.2f} ({lo:+.2f} to {hi:+.2f}) -> {'MET' if primary else 'not met'}")
    print(f"secondary: Context Tokens {whole_ct:.1f} {'<=' if whole_ct <= CONTEXT_TOKENS_CAP else '>'} 1,000; "
          f"whole - four items {d4:+.2f} ({lo4:+.2f} to {hi4:+.2f}) -> {'MET' if secondary else 'not met'}")


if __name__ == "__main__":
    main()
