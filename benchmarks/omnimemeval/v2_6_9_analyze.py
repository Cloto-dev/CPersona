"""Read the keyword seat's check as registered (benchmarks/measurements/prereg-omnimemeval-lme-2_6_9.md).

On the 400 test questions, per mode. 2.6.8's answer to a question is the one the Lite and Pro test recorded: Pro's
from its run (all 400), Lite's from its run where its context changed against 2.6.7's lite and the recorded answer
otherwise (as v1_6_analyze.py reads it). 2.6.9a1's answer is the run of this check
(results/lme/cpersona-lme1-v269<mode>) where the context changed against 2.6.8, and 2.6.8's otherwise: the prompt
is the same.

- Lite answers at least 320 of the 400 questions, and Pro at least 350.
- The cap: every Lite and Pro response of the raw searches is within its cap (3,000 / 5,000), counted outside the
  server in cl100k_base, and equal to its used_tokens.
- Beside them: the changed questions and how their answers moved, accuracy per question type, the returned JSON's
  mean / 95th percentile / maximum, and the evidence metrics of evidence_metrics.py.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> V15_USAGE_DIR=<dir> COUNT_CURVE_USAGE_DIR=<dir> \\
       RAW_DIR=<dir> <harness python> v2_6_9_analyze.py
       (RAW_DIR holds lite-test.jsonl and pro-test.jsonl of this check's raw searches)
"""

import os
import sqlite3
import sys
from pathlib import Path

from count_curve_analyze import acc, judged
from v1_5_analyze import evidence_of
from v1_5_build import RUN as A1_RUN
from v1_5_build import test_questions
from v1_5_whole_build import RUN as WHOLE_RUN
from v1_6_analyze import contexts, raw_cap
from v1_6_build import LITE_RUN, PRO_RUN
from v2_6_9_build import RUNS

H = Path(os.environ["OMNIMEMEVAL_DIR"])
FLOORS = {"lite": 320, "pro": 350}


def recorded_268(uids: list[str]) -> tuple[dict, dict]:
    """2.6.8's answers and contexts per mode, as the Lite and Pro test recorded them."""
    base = judged(H / "results/lme" / A1_RUN)
    base.update(judged(H / "results/lme" / WHOLE_RUN))  # 2.6.7 lite: whole's where whole answered
    a1_ctx, whole_ctx = contexts(A1_RUN), contexts(WHOLE_RUN)
    lite_dir = H / "results/lme" / LITE_RUN
    lite_moved = contexts(LITE_RUN)
    lite_answered = judged(lite_dir) if lite_dir.exists() else {}
    lite = {u: lite_answered.get(u, base[u]) for u in uids}
    lite_ctx = {u: lite_moved.get(u, whole_ctx.get(u, a1_ctx[u])) for u in uids}
    pro = judged(H / "results/lme" / PRO_RUN)
    assert sorted(pro) == uids, "the 2.6.8 Pro run did not answer exactly the test questions"
    return {"lite": lite, "pro": pro}, {"lite": lite_ctx, "pro": contexts(PRO_RUN)}


def main():
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    test = set(test_questions())
    uids = sorted(judged(H / "results/lme" / A1_RUN))
    assert {int(u.rsplit("_", 1)[1]) for u in uids} == test, "the 2.6.5a1 test did not answer the test questions"
    old, old_ctx = recorded_268(uids)
    new, new_ctx, moved = {}, {}, {}
    for mode, run in RUNS.items():
        d = H / "results/lme" / run
        moved[mode] = contexts(run)
        answered = judged(d) if d.exists() else {}
        assert set(answered) == set(moved[mode]), f"{mode}: the run did not answer exactly its changed questions"
        assert all(moved[mode][u] != old_ctx[mode][u] for u in moved[mode]), f"{mode}: an answered question did not change"
        new[mode] = {u: answered.get(u, old[mode][u]) for u in uids}
        new_ctx[mode] = {u: moved[mode].get(u, old_ctx[mode][u]) for u in uids}

    ok = True
    for mode in RUNS:
        right = sum(v[0] for v in new[mode].values())
        met = right >= FLOORS[mode]
        ok &= met
        print(f"\n{mode}: {right}/{len(uids)} ({acc(new[mode]):.2f}%), 2.6.8 {sum(v[0] for v in old[mode].values())} "
              f"-> {'MET' if met else 'not met'} (floor {FLOORS[mode]}); changed {len(moved[mode])}")
        for u in sorted(moved[mode], key=lambda u: int(u.rsplit("_", 1)[1])):
            print(f"  {int(u.rsplit('_', 1)[1])}: {old[mode][u][1]}, 2.6.8 {'right' if old[mode][u][0] else 'wrong'} "
                  f"-> 2.6.9a1 {'right' if new[mode][u][0] else 'wrong'}")
    caps = {mode: raw_cap(mode, test) for mode in RUNS}
    cap_ok = all(c["over"] == 0 and c["mismatch"] == 0 for c in caps.values())
    for mode, c in caps.items():
        print(f"cap {mode}: over {c['over']}, used_tokens != count {c['mismatch']}; returned JSON mean {c['mean']:.1f}, "
              f"p95 {c['p95']}, max {c['max']}")
    print(f"cap -> {'held' if cap_ok else 'NOT held'}; floors -> {'MET' if ok else 'not met'}")

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    cats = sorted({v[1] for v in old["pro"].values()})
    print("\n| arm | accuracy | answer sessions shown % | all shown % | evidence turns touched % "
          "| evidence-turn chars quoted % | evidence share % | " + " | ".join(cats) + " |")
    print("| --- |" + " --- |" * (6 + len(cats)))
    for name, answers, cs in (("2.6.9a1 Lite", new["lite"], new_ctx["lite"]), ("2.6.8 Lite", old["lite"], old_ctx["lite"]),
                              ("2.6.9a1 Pro", new["pro"], new_ctx["pro"]), ("2.6.8 Pro", old["pro"], old_ctx["pro"])):
        e = evidence_of(cs, uids, df, conn)
        by = " | ".join(f"{acc({u: v for u, v in answers.items() if v[1] == c}):.1f}" for c in cats)
        print(f"| {name} | {acc(answers):.2f} | {e['shown']:.1f} | {e['all_shown']:.1f} | {e['touched']:.1f} "
              f"| {e['turn_chars']:.1f} | {e['evidence_share']:.1f} | {by} |")
        assert e["unlocated"] == 0, f"{name}: {e['unlocated']} quoted characters not found in the store"


if __name__ == "__main__":
    main()
