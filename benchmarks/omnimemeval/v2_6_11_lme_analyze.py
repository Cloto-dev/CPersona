"""Read the 2.6.11 check on LongMemEval-S as registered (benchmarks/measurements/prereg-omnimemeval-2_6_11.md).

On the 400 test questions, Pro only. 2.6.10's answer to a question is 2.6.10a2's recorded answer (v2_6_11_lme_build.py
recorded_2610), or the answer at 73d6bca (results/lme/cpersona-lme1-v2611base) where the context there differed from
it. The candidate's answer is the run of this check (results/lme/cpersona-lme1-v2611pro) where its context changed
against 2.6.10, and 2.6.10's otherwise: the prompt is the same.

- The net: among the changed questions, L = right at 2.6.10 and wrong at the candidate, W = the reverse. The candidate
  fails only when an exact binomial test of L among L + W at one half gives one-sided P(X >= L) < 0.05.
- The cap: every Pro response of the candidate's raw searches is within 5,000 tokens, counted outside the server in
  cl100k_base, and equal to its used_tokens.
- Beside them: correct answers and their net change, L and W, accuracy per question type, the returned JSON's mean /
  95th percentile / maximum, and the evidence metrics of evidence_metrics.py.

The two searches are read again, and every context the answers stand on must be the context that was searched.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> V15_USAGE_DIR=<dir> COUNT_CURVE_USAGE_DIR=<dir> \\
       RAW_DIR=<dir> <harness python> v2_6_11_lme_analyze.py <candidate search dir> <2.6.10 search dir>
       (RAW_DIR holds pro-test.jsonl of the candidate's raw searches)
"""

import os
import sqlite3
import sys
from pathlib import Path

from count_curve_analyze import acc, judged
from v1_5_analyze import evidence_of
from v1_5_build import RUN as A1_RUN
from v1_5_build import load, test_questions
from v1_6_analyze import contexts, raw_cap
from v2_6_10_analyze import H, upper_tail
from v2_6_11_lme_build import RUNS, recorded_2610

ALPHA = 0.05


def answers_of(run: str, before: dict, before_ctx: dict) -> tuple[dict, dict, dict]:
    """The run's answers where it answered, ``before``'s otherwise; the same for contexts; and the run's contexts."""
    d = H / "results/lme" / run
    moved = contexts(run) if d.exists() else {}
    answered = judged(d) if d.exists() else {}
    assert set(answered) == set(moved), f"{run}: the run did not answer exactly its changed questions"
    assert all(moved[u] != before_ctx[u] for u in moved), f"{run}: an answered question did not change"
    return ({u: answered.get(u, before[u]) for u in before}, {u: moved.get(u, before_ctx[u]) for u in before}, moved)


def main():
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    cand_dir, base_dir = Path(sys.argv[1]), Path(sys.argv[2])
    test = test_questions()
    uids = sorted(judged(H / "results/lme" / A1_RUN))
    assert {int(u.rsplit("_", 1)[1]) for u in uids} == set(test), "the 2.6.5a1 test did not answer the test questions"
    rec, rec_ctx = recorded_2610(uids)
    old, old_ctx, _ = answers_of(RUNS["base"], rec, rec_ctx)
    new, new_ctx, moved = answers_of(RUNS["pro"], old, old_ctx)

    by_uid = {f"lme_exper_user_lme1_{i}": i for i in test}
    base, cand = load(base_dir, "pro", test), load(cand_dir, "pro", test)
    assert all(old_ctx[u] == base[by_uid[u]]["search_context"] for u in uids), "2.6.10's contexts are not its search's"
    assert all(new_ctx[u] == cand[by_uid[u]]["search_context"] for u in uids), "the candidate's contexts are not its search's"

    right, before = sum(v[0] for v in new.values()), sum(v[0] for v in old.values())
    lost = sum(1 for u in moved if old[u][0] and not new[u][0])
    gained = sum(1 for u in moved if new[u][0] and not old[u][0])
    p = upper_tail(lost, gained)
    failed = p < ALPHA
    print(f"pro: {right}/{len(uids)} ({acc(new):.2f}%), 2.6.10 {before} ({acc(old):.2f}%), net {right - before:+d}; "
          f"changed {len(moved)}, lost {lost}, gained {gained}, one-sided p {p:.4f} -> {'FAILED' if failed else 'not clearly worse'}")
    c = raw_cap("pro", set(test))
    cap_ok = c["over"] == 0 and c["mismatch"] == 0
    print(f"cap pro: over {c['over']}, used_tokens != count {c['mismatch']}; returned JSON mean {c['mean']:.1f}, "
          f"p95 {c['p95']}, max {c['max']}")
    print(f"cap -> {'held' if cap_ok else 'NOT held'}; net -> {'FAILED' if failed else 'not clearly worse'}")

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    cats = sorted({v[1] for v in old.values()})
    print("\n| arm | accuracy | answer sessions shown % | all shown % | evidence turns touched % "
          "| evidence-turn chars quoted % | evidence share % | " + " | ".join(cats) + " |")
    print("| --- |" + " --- |" * (6 + len(cats)))
    for name, answers, cs in (("candidate Pro", new, new_ctx), ("2.6.10 Pro", old, old_ctx)):
        e = evidence_of(cs, uids, df, conn)
        by = " | ".join(f"{acc({u: v for u, v in answers.items() if v[1] == c}):.1f}" for c in cats)
        print(f"| {name} | {acc(answers):.2f} | {e['shown']:.1f} | {e['all_shown']:.1f} | {e['touched']:.1f} "
              f"| {e['turn_chars']:.1f} | {e['evidence_share']:.1f} | {by} |")
        assert e["unlocated"] == 0, f"{name}: {e['unlocated']} quoted characters not found in the store"


if __name__ == "__main__":
    main()
