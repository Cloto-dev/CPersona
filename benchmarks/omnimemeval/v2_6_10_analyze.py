"""Read the 2.6.10 check as registered (benchmarks/measurements/prereg-omnimemeval-lme-2_6_10.md).

On the 400 test questions, per mode. 2.6.9's answer to a question is the one its own check recorded: the 2.6.9 run
(results/lme/cpersona-lme1-v269<mode>) where its context changed against 2.6.8, and 2.6.8's recorded answer otherwise
(as v2_6_9_analyze.py reads it). 2.6.10a4's answer is the run of this check (results/lme/cpersona-lme1-v2610<mode>)
where the context changed against 2.6.9, and 2.6.9's otherwise: the prompt is the same.

- Neither mode answers worse: among a mode's changed questions, L = right in 2.6.9 and wrong in 2.6.10a4, W = the
  reverse. The mode fails only when an exact binomial test of L among L + W at one half gives one-sided P(X >= L) < 0.05.
- The cap: every Lite and Pro response of the raw searches is within its cap (3,000 / 5,000), counted outside the
  server in cl100k_base, and equal to its used_tokens.
- Beside them: correct answers and their net change, L and W, accuracy per question type, the returned JSON's mean /
  95th percentile / maximum, and the evidence metrics of evidence_metrics.py.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> V15_USAGE_DIR=<dir> COUNT_CURVE_USAGE_DIR=<dir> \\
       RAW_DIR=<dir> <harness python> v2_6_10_analyze.py
       (the two usage directories as for v2_6_9_analyze.py: the shared modules read them; RAW_DIR holds lite-test.jsonl
       and pro-test.jsonl of this check's raw searches)
"""

import os
import sqlite3
import sys
from math import comb
from pathlib import Path

from count_curve_analyze import acc, judged
from v1_5_analyze import evidence_of
from v1_5_build import RUN as A1_RUN
from v1_5_build import test_questions
from v1_6_analyze import contexts, raw_cap
from v2_6_9_analyze import recorded_268
from v2_6_9_build import RUNS as RUNS_269
from v2_6_10_build import RUNS

H = Path(os.environ["OMNIMEMEVAL_DIR"])
ALPHA = 0.05


def recorded_269(uids: list[str]) -> tuple[dict, dict]:
    """2.6.9's answers and contexts per mode, as its check recorded them."""
    old, old_ctx = recorded_268(uids)
    answers, ctx = {}, {}
    for mode, run in RUNS_269.items():
        d = H / "results/lme" / run
        moved = contexts(run) if d.exists() else {}
        answered = judged(d) if d.exists() else {}
        answers[mode] = {u: answered.get(u, old[mode][u]) for u in uids}
        ctx[mode] = {u: moved.get(u, old_ctx[mode][u]) for u in uids}
    return answers, ctx


def upper_tail(lost: int, gained: int) -> float:
    """P(X >= lost) for X ~ Binomial(lost + gained, 1/2); 1.0 when nothing moved."""
    n = lost + gained
    if n == 0:
        return 1.0
    return sum(comb(n, k) for k in range(lost, n + 1)) / 2 ** n


def main():
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    test = set(test_questions())
    uids = sorted(judged(H / "results/lme" / A1_RUN))
    assert {int(u.rsplit("_", 1)[1]) for u in uids} == test, "the 2.6.5a1 test did not answer the test questions"
    old, old_ctx = recorded_269(uids)
    new, new_ctx, moved = {}, {}, {}
    for mode, run in RUNS.items():
        d = H / "results/lme" / run
        moved[mode] = contexts(run) if d.exists() else {}
        answered = judged(d) if d.exists() else {}
        assert set(answered) == set(moved[mode]), f"{mode}: the run did not answer exactly its changed questions"
        assert all(moved[mode][u] != old_ctx[mode][u] for u in moved[mode]), f"{mode}: an answered question did not change"
        new[mode] = {u: answered.get(u, old[mode][u]) for u in uids}
        new_ctx[mode] = {u: moved[mode].get(u, old_ctx[mode][u]) for u in uids}

    ok = True
    for mode in RUNS:
        right, before = sum(v[0] for v in new[mode].values()), sum(v[0] for v in old[mode].values())
        lost = sum(1 for u in moved[mode] if old[mode][u][0] and not new[mode][u][0])
        gained = sum(1 for u in moved[mode] if new[mode][u][0] and not old[mode][u][0])
        p = upper_tail(lost, gained)
        failed = p < ALPHA
        ok &= not failed
        print(f"\n{mode}: {right}/{len(uids)} ({acc(new[mode]):.2f}%), 2.6.9 {before} ({acc(old[mode]):.2f}%), net {right - before:+d}; "
              f"changed {len(moved[mode])}, lost {lost}, gained {gained}, one-sided p {p:.4f} -> {'FAILED' if failed else 'not worse'}")
    caps = {mode: raw_cap(mode, test) for mode in RUNS}
    cap_ok = all(c["over"] == 0 and c["mismatch"] == 0 for c in caps.values())
    for mode, c in caps.items():
        print(f"cap {mode}: over {c['over']}, used_tokens != count {c['mismatch']}; returned JSON mean {c['mean']:.1f}, "
              f"p95 {c['p95']}, max {c['max']}")
    print(f"cap -> {'held' if cap_ok else 'NOT held'}; rule -> {'not worse in either mode' if ok else 'FAILED'}")

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    cats = sorted({v[1] for v in old["pro"].values()})
    print("\n| arm | accuracy | answer sessions shown % | all shown % | evidence turns touched % "
          "| evidence-turn chars quoted % | evidence share % | " + " | ".join(cats) + " |")
    print("| --- |" + " --- |" * (6 + len(cats)))
    for name, answers, cs in (("2.6.10a4 Lite", new["lite"], new_ctx["lite"]), ("2.6.9 Lite", old["lite"], old_ctx["lite"]),
                              ("2.6.10a4 Pro", new["pro"], new_ctx["pro"]), ("2.6.9 Pro", old["pro"], old_ctx["pro"])):
        e = evidence_of(cs, uids, df, conn)
        by = " | ".join(f"{acc({u: v for u, v in answers.items() if v[1] == c}):.1f}" for c in cats)
        print(f"| {name} | {acc(answers):.2f} | {e['shown']:.1f} | {e['all_shown']:.1f} | {e['touched']:.1f} "
              f"| {e['turn_chars']:.1f} | {e['evidence_share']:.1f} | {by} |")
        assert e["unlocated"] == 0, f"{name}: {e['unlocated']} quoted characters not found in the store"


if __name__ == "__main__":
    main()
