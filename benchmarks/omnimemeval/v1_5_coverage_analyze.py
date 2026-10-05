"""Read the coverage-against-whole test as registered (benchmarks/measurements/prereg-omnimemeval-lme-v1_5-coverage.md).

On the 2.6.5a1 test's 400 questions. whole's answer to a question is the whole test's
(results/lme/cpersona-lme1-v15whole-b2800) where that test answered it, and the 2.6.5a1 test's
otherwise. coverage's is the point's (results/lme/cpersona-lme1-v15cov-b2800) when its context
changed from whole's, and whole's otherwise: the context, and so the prompt, is the same. Four items
is the count curve's, restricted to the same questions.
- Primary: coverage's mean retrieved context (cl100k, the harness's count) is within 5% of whole's on
  these questions, and the lower bound of the 95% bootstrap interval of the paired accuracy difference,
  coverage - whole, is at or above -2.0 points (10,000 resamples over questions, seed 20261003).
- Secondary: coverage's Context Tokens are at most 1,000, and the lower bound of coverage - four items
  is above 0. Context Tokens = the 2.6.5a1 test's (the answer-stage prompt tokens the API reported,
  averaged over its calls) + the sum over the questions of (o200k_base tokens of coverage's context -
  those of the 2.6.5a1 context) / the number of questions.
- Beside them: the number of changed questions and their types, accuracy per question type, the
  prompt tokens the API reported for the changed calls, and the evidence metrics of
  evidence_metrics.py on the same questions.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> V15_USAGE_DIR=<dir> COVERAGE_USAGE_DIR=<dir> \\
       COUNT_CURVE_USAGE_DIR=<dir> <harness python> v1_5_coverage_analyze.py
       (V15_USAGE_DIR holds the 2.6.5a1 test's token_usage_answer.json of each pass, named
       v15a1-b2800-pass<n>.json; COVERAGE_USAGE_DIR the point's, named v15cov-b2800-pass<n>.json)
"""

import collections
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
from v1_5_coverage_build import RUN
from v1_5_whole_build import RUN as WHOLE_RUN

H = Path(os.environ["OMNIMEMEVAL_DIR"])
COVERAGE_USAGE = Path(os.environ["COVERAGE_USAGE_DIR"])
FOUR = "cpersona-lme1-k4"


def contexts(run: str) -> dict[str, str]:
    data = json.load(open(H / "results/lme" / run / "cpersona_lme_search_results.json"))
    return {uid: conv[0]["search_context"] for uid, conv in data.items()}


def changed_calls() -> tuple[int, int]:
    calls = prompt = 0
    for p in sorted(COVERAGE_USAGE.glob("v15cov-b2800-pass*.json")):
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
    a1 = judged(H / "results/lme" / A1_RUN)
    assert {int(u.rsplit("_", 1)[1]) for u in a1} == test, "the 2.6.5a1 test did not answer the test questions"
    uids = sorted(a1)
    four = {u: v for u, v in judged(H / "results/lme" / FOUR).items() if u in a1}
    a1_ctx = contexts(A1_RUN)
    whole_answered, whole_moved = judged(H / "results/lme" / WHOLE_RUN), contexts(WHOLE_RUN)
    whole = {u: whole_answered.get(u, a1[u]) for u in uids}
    whole_ctx = {u: whole_moved.get(u, a1_ctx[u]) for u in uids}
    point_dir = H / "results/lme" / RUN
    answered = judged(point_dir) if point_dir.exists() else {}
    moved_ctx = contexts(RUN) if point_dir.exists() else {}
    assert set(answered) == set(moved_ctx), "the point did not answer exactly its changed questions"
    assert all(moved_ctx[u] != whole_ctx[u] for u in moved_ctx), "a question the point answered did not change"
    coverage = {u: answered.get(u, whole[u]) for u in uids}
    cov_ctx = {u: moved_ctx.get(u, whole_ctx[u]) for u in uids}

    calls, a1_ct = api_context_tokens()
    assert calls == len(uids), f"{calls} usage-reported answer calls in the 2.6.5a1 test for {len(uids)} questions"
    enc = tiktoken.get_encoding("o200k_base")
    delta = sum(len(enc.encode(cov_ctx[u])) - len(enc.encode(a1_ctx[u])) for u in uids if cov_ctx[u] != a1_ctx[u])
    cov_ct = a1_ct + delta / len(uids)
    m_calls, m_prompt = changed_calls()
    assert m_calls == len(moved_ctx), f"{m_calls} usage-reported answer calls for {len(moved_ctx)} changed questions"

    flips = collections.Counter((whole[u][0], coverage[u][0]) for u in moved_ctx)
    types = collections.Counter(a1[u][1] for u in moved_ctx)
    print(f"test questions: {len(uids)}; changed: {len(moved_ctx)} ({dict(sorted(types.items()))})")
    print(f"changed answers: right->right {flips[(True, True)]}, wrong->right {flips[(False, True)]}, "
          f"right->wrong {flips[(True, False)]}, wrong->wrong {flips[(False, False)]}")
    print(f"Context Tokens: 2.6.5a1 {a1_ct:.1f} (API); coverage {cov_ct:.1f} (o200k difference {delta:+d})")
    if m_calls:
        print(f"the changed calls' prompt tokens (API): {m_prompt} over {m_calls} calls ({m_prompt / m_calls:.1f} each)")

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    four_ctx = contexts(FOUR)
    cats = sorted({v[1] for v in a1.values()})
    rows = [("coverage 2,800", coverage, cov_ctx), ("whole 2,800", whole, whole_ctx), ("four items", four, four_ctx)]
    print("\n| point | accuracy | context (cl100k) | answer sessions shown % | all shown % | evidence turns touched % "
          "| evidence-turn chars quoted % | evidence share % | " + " | ".join(cats) + " |")
    print("| --- |" + " --- |" * (7 + len(cats)))
    for name, p, cs in rows:
        e = evidence_of(cs, uids, df, conn)
        by = " | ".join(f"{acc({u: v for u, v in p.items() if v[1] == c}):.1f}" for c in cats)
        print(f"| {name} | {acc(p):.2f} | {ctx(p):.1f} | {e['shown']:.1f} | {e['all_shown']:.1f} | {e['touched']:.1f} "
              f"| {e['turn_chars']:.1f} | {e['evidence_share']:.1f} | {by} |")
        assert e["unlocated"] == 0, f"{name}: {e['unlocated']} quoted characters not found in the store"

    gap = ctx(coverage) / ctx(whole) - 1
    same_cost = abs(gap) <= SAME_COST
    d, lo, hi = boot(coverage, whole)
    d4, lo4, hi4 = boot(coverage, four)
    primary = same_cost and lo >= MARGIN
    secondary = cov_ct <= CONTEXT_TOKENS_CAP and lo4 > 0
    print(f"\ncontext against whole: {ctx(coverage):.1f} vs {ctx(whole):.1f} ({gap * 100:+.1f}%, "
          f"{'same cost' if same_cost else 'NOT at the same cost'})")
    print(f"primary: coverage - whole {d:+.2f} ({lo:+.2f} to {hi:+.2f}) -> {'MET' if primary else 'not met'}")
    print(f"secondary: Context Tokens {cov_ct:.1f} {'<=' if cov_ct <= CONTEXT_TOKENS_CAP else '>'} 1,000; "
          f"coverage - four items {d4:+.2f} ({lo4:+.2f} to {hi4:+.2f}) -> {'MET' if secondary else 'not met'}")


if __name__ == "__main__":
    main()
