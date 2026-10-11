"""Read the Lite and Pro test as registered (benchmarks/measurements/prereg-omnimemeval-lme-v1_6.md).

On the 400 test questions. Lite's answer to a question is the Lite run's (results/lme/cpersona-lme1-v16lite)
when its context changed against 2.6.7's `lite: true`, and the recorded answer otherwise (the `whole` test's
where it answered the question, the 2.6.5a1 test's otherwise): the prompt is the same. Pro's answers are the
Pro run's (results/lme/cpersona-lme1-v16pro), all 400.

- Lite: at least 320 of the 400 answered right.
- Pro's direction: the paired accuracy difference, Pro - Lite, has a point estimate of 0 or more. Its 95%
  bootstrap interval (10,000 resamples over the questions, seed 20261009) and the exact two-sided binomial
  test of the discordant questions are printed beside it and decide nothing.
- The cap: every Lite and Pro response of the raw searches is within its cap (3,000 / 5,000), counted
  outside the server in cl100k_base, and equal to its used_tokens.
- Beside them: the changed Lite questions and how their answers moved, accuracy per question type,
  Context Tokens, the retrieved context in cl100k_base, the returned JSON's mean / 95th percentile /
  maximum, the evidence metrics of evidence_metrics.py, and each mode's accuracy against the
  gold-evidence reference (91.25% within 5,000 tokens, 93.50% from the marked turns alone).

Context Tokens: Pro's are the answer-stage prompt tokens the API reported, averaged over its calls. Lite's are
the 2.6.5a1 test's (as for `whole`) plus, summed over the questions whose Lite context differs from the 2.6.5a1
context and divided by 400, the o200k_base tokens of the Lite context minus those of the 2.6.5a1 context.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> V15_USAGE_DIR=<dir> COUNT_CURVE_USAGE_DIR=<dir> \\
       PRO_USAGE_DIR=<dir> LITE_USAGE_DIR=<dir> RAW_DIR=<dir> <harness python> v1_6_analyze.py
       (V15_USAGE_DIR and COUNT_CURVE_USAGE_DIR as for v1_5_whole_analyze.py: the shared modules read them)
       (PRO_USAGE_DIR / LITE_USAGE_DIR hold token_usage_answer.json of each pass, named v16pro-pass<n>.json /
       v16lite-pass<n>.json; RAW_DIR holds lite-test.jsonl and pro-test.jsonl of the raw searches)
"""

import json
import math
import os
import random
import sqlite3
import sys
from pathlib import Path

import tiktoken
from count_curve_analyze import acc, judged
from v1_5_analyze import api_context_tokens, ctx, evidence_of
from v1_5_build import RUN as A1_RUN
from v1_5_build import test_questions
from v1_5_whole_build import RUN as WHOLE_RUN
from v1_6_build import LITE_RUN, PRO_RUN

H = Path(os.environ["OMNIMEMEVAL_DIR"])
SEED, B = 20261009, 10_000
LITE_FLOOR = 320
CAPS = {"lite": 3000, "pro": 5000}
GOLD = {"within 5,000 tokens": 91.25, "marked turns alone": 93.50}


def contexts(run: str) -> dict[str, str]:
    path = H / "results/lme" / run / "cpersona_lme_search_results.json"
    data = json.load(open(path)) if path.exists() else {}
    return {uid: conv[0]["search_context"] for uid, conv in data.items()}


def paired(a: dict, b: dict) -> tuple[float, float, float, int, int, float]:
    """a - b in points: the estimate, the 95% bootstrap interval, the discordant counts and their exact p."""
    ids = sorted(a)
    assert ids == sorted(b), "the two modes answer different question sets"
    diff = [a[i][0] - b[i][0] for i in ids]
    n = len(ids)
    rng = random.Random(SEED)
    stats = sorted(sum(diff[rng.randrange(n)] for _ in range(n)) / n * 100 for _ in range(B))
    only_a, only_b = diff.count(1), diff.count(-1)
    k, m = min(only_a, only_b), only_a + only_b
    p = min(1.0, 2 * sum(math.comb(m, j) for j in range(k + 1)) / 2**m) if m else 1.0
    return sum(diff) / n * 100, stats[int(0.025 * B)], stats[int(0.975 * B) - 1], only_a, only_b, p


def usage(directory: str, prefix: str) -> tuple[int, int]:
    calls = prompt = 0
    for path in sorted(Path(directory).glob(f"{prefix}-pass*.json")):
        module = json.loads(path.read_text())["modules"].get("ANSWER")
        if not module:  # a pass that resumed past the answers made no answer call
            continue
        calls += module["usage_reported_call_count"]
        prompt += module["prompt_tokens"]
    return calls, prompt


def raw_cap(mode: str, test: set[int]) -> dict:
    enc = tiktoken.get_encoding("cl100k_base")
    rows = [json.loads(line) for line in open(Path(os.environ["RAW_DIR"]) / f"{mode}-test.jsonl")]
    assert {r["i"] for r in rows} == test and len(rows) == len(test), f"{mode}: the raw searches are not the test questions"
    counts = sorted(len(enc.encode(json.dumps(r["response"], ensure_ascii=False), disallowed_special=())) for r in rows)
    over = sum(c > CAPS[mode] for c in counts)
    mismatch = sum(len(enc.encode(json.dumps(r["response"], ensure_ascii=False), disallowed_special=())) != r["response"].get("used_tokens") for r in rows)
    return {"over": over, "mismatch": mismatch, "mean": sum(counts) / len(counts), "p95": counts[int(0.95 * (len(counts) - 1))], "max": counts[-1]}


def main():
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    test = set(test_questions())
    recorded = judged(H / "results/lme" / A1_RUN)
    assert {int(u.rsplit("_", 1)[1]) for u in recorded} == test, "the 2.6.5a1 test did not answer the test questions"
    whole_dir = H / "results/lme" / WHOLE_RUN
    recorded.update(judged(whole_dir))  # 2.6.7 lite's recorded answer: whole's where whole answered
    uids = sorted(recorded)
    a1_ctx, whole_ctx = contexts(A1_RUN), contexts(WHOLE_RUN)
    lite_ref_ctx = {u: whole_ctx.get(u, a1_ctx[u]) for u in uids}

    lite_dir = H / "results/lme" / LITE_RUN
    lite_answered = judged(lite_dir) if lite_dir.exists() else {}
    lite_moved = contexts(LITE_RUN)
    assert set(lite_answered) == set(lite_moved), "the Lite run did not answer exactly its changed questions"
    assert all(lite_moved[u] != lite_ref_ctx[u] for u in lite_moved), "a question the Lite run answered did not change"
    lite = {u: lite_answered.get(u, recorded[u]) for u in uids}
    lite_ctx = {u: lite_moved.get(u, lite_ref_ctx[u]) for u in uids}
    pro = judged(H / "results/lme" / PRO_RUN)
    assert sorted(pro) == uids, "the Pro run did not answer exactly the test questions"
    pro_ctx = contexts(PRO_RUN)

    print(f"test questions: {len(uids)}; Lite changed against 2.6.7 lite: {len(lite_moved)}")
    for u in sorted(lite_moved, key=lambda u: int(u.rsplit("_", 1)[1])):
        print(f"  {int(u.rsplit('_', 1)[1])}: {recorded[u][1]}, 2.6.7 lite {'right' if recorded[u][0] else 'wrong'} "
              f"-> Lite {'right' if lite[u][0] else 'wrong'}")

    lite_right = sum(v[0] for v in lite.values())
    d, lo, hi, only_pro, only_lite, p = paired(pro, lite)
    caps = {mode: raw_cap(mode, test) for mode in CAPS}
    cap_ok = all(c["over"] == 0 and c["mismatch"] == 0 for c in caps.values())
    print(f"\nLite: {lite_right}/{len(uids)} ({acc(lite):.2f}%) -> {'MET' if lite_right >= LITE_FLOOR else 'not met'} (floor {LITE_FLOOR})")
    print(f"Pro - Lite: {d:+.2f} points (95% {lo:+.2f} to {hi:+.2f}); right only under Pro {only_pro}, only under Lite "
          f"{only_lite}, exact p {p:.3f} -> direction {'not reversed' if d >= 0 else 'REVERSED'}")
    for mode, c in caps.items():
        print(f"cap {mode}: over {c['over']}, used_tokens != count {c['mismatch']}; returned JSON mean {c['mean']:.1f}, "
              f"p95 {c['p95']}, max {c['max']}")
    print(f"cap -> {'held' if cap_ok else 'NOT held'}")

    calls, a1_ct = api_context_tokens()
    assert calls == len(uids), f"{calls} usage-reported answer calls in the 2.6.5a1 test for {len(uids)} questions"
    enc = tiktoken.get_encoding("o200k_base")
    lite_ct = a1_ct + sum(len(enc.encode(lite_ctx[u])) - len(enc.encode(a1_ctx[u])) for u in uids if lite_ctx[u] != a1_ctx[u]) / len(uids)
    p_calls, p_prompt = usage(os.environ["PRO_USAGE_DIR"], "v16pro")
    assert p_calls == len(uids), f"{p_calls} usage-reported answer calls for the {len(uids)} Pro questions"
    l_calls, l_prompt = usage(os.environ.get("LITE_USAGE_DIR", "/nonexistent"), "v16lite")
    assert l_calls == len(lite_moved), f"{l_calls} usage-reported answer calls for {len(lite_moved)} changed Lite questions"
    print(f"Context Tokens: Lite {lite_ct:.1f}; Pro {p_prompt / p_calls:.1f} (API)"
          + (f"; the changed Lite calls {l_prompt / l_calls:.1f} each (API)" if l_calls else ""))

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    cats = sorted({v[1] for v in recorded.values()})
    print("\n| mode | accuracy | of gold 5,000 | of marked turns | context (cl100k) | answer sessions shown % | all shown % "
          "| evidence turns touched % | evidence-turn chars quoted % | evidence share % | " + " | ".join(cats) + " |")
    print("| --- |" + " --- |" * (9 + len(cats)))
    for name, answers, cs in (("Lite", lite, lite_ctx), ("Pro", pro, pro_ctx), ("2.6.7 lite (recorded)", recorded, lite_ref_ctx)):
        e = evidence_of(cs, uids, df, conn)
        by = " | ".join(f"{acc({u: v for u, v in answers.items() if v[1] == c}):.1f}" for c in cats)
        a = acc(answers)
        print(f"| {name} | {a:.2f} | {a / GOLD['within 5,000 tokens'] * 100:.1f}% | {a / GOLD['marked turns alone'] * 100:.1f}% "
              f"| {ctx(answers):.1f} | {e['shown']:.1f} | {e['all_shown']:.1f} | {e['touched']:.1f} | {e['turn_chars']:.1f} "
              f"| {e['evidence_share']:.1f} | {by} |")
        assert e["unlocated"] == 0, f"{name}: {e['unlocated']} quoted characters not found in the store"


if __name__ == "__main__":
    main()
