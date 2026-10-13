"""Build the answer inputs of the 2.6.11 check on LongMemEval-S from its searches.

Registration: benchmarks/measurements/prereg-omnimemeval-2_6_11.md.

The 400 test questions are searched with CPERSONA_SEARCH_MODE=pro and CPERSONA_SEARCH_COUNT=15 through the harness's
search wrapper (search_driver.py), once at 2.6.10 (73d6bca) and once at the candidate (a613114).

- 2.6.10's recorded context is 2.6.10a2's, as the exploration after the 2.6.10 check recorded it: its run
  (results/lme/cpersona-lme1-x2610a2pro) where the context changed against 2.6.9, and 2.6.9's recorded context otherwise.
  A question whose context at 73d6bca differs from it is written to cpersona-lme1-v2611base, to be answered at 2.6.10.
- A question is changed when the candidate's context differs from 2.6.10's (the search at 73d6bca). The changed
  questions are written to cpersona-lme1-v2611pro, to be answered at the candidate.

No directory is written for a set that is empty. Each entry is the 2.6.5a1 test's, with only search_context and
search_duration_ms replaced, which the harness answers from (--from-step 3).

usage: OMNIMEMEVAL_DIR=<checkout> V15_USAGE_DIR=<dir> COUNT_CURVE_USAGE_DIR=<dir> \\
       <harness python> v2_6_11_lme_build.py <candidate search dir> <2.6.10 search dir> [--check-only]
"""

import sys
from pathlib import Path

from v1_5_build import RUN as A1_RUN
from v1_5_build import load, test_questions
from v1_6_build import entries, write

RUNS = {"base": "cpersona-lme1-v2611base", "pro": "cpersona-lme1-v2611pro"}
A2_RUN = "cpersona-lme1-x2610a2pro"


def recorded_2610(uids: list[str]) -> tuple[dict, dict]:
    """2.6.10's recorded Pro answers and contexts by question id: 2.6.10a2's run where it moved, 2.6.9's otherwise."""
    import v2_6_10_analyze as A
    from count_curve_analyze import judged
    from v1_6_analyze import contexts

    old, old_ctx = A.recorded_269(uids)
    moved = contexts(A2_RUN)
    answered = judged(A.H / "results/lme" / A2_RUN)
    assert moved and set(answered) == set(moved), "2.6.10a2's run did not answer exactly its changed questions"
    answers = {u: answered.get(u, old["pro"][u]) for u in uids}
    ctx = {u: moved.get(u, old_ctx["pro"][u]) for u in uids}
    assert sum(v[0] for v in answers.values()) == 354, "2.6.10a2's Pro is not the recorded 354"
    return answers, ctx


def main():
    args = [a for a in sys.argv[1:] if a != "--check-only"]
    cand_dir, base_dir = Path(args[0]), Path(args[1])
    test = test_questions()
    a1 = entries(A1_RUN)
    uid = {i: a1[i][0] for i in test}
    _, rec_ctx = recorded_2610(sorted(uid.values()))
    base, cand = load(base_dir, "pro", test), load(cand_dir, "pro", test)
    moved = [i for i in test if base[i]["search_context"] != rec_ctx[uid[i]]]
    changed = [i for i in test if cand[i]["search_context"] != base[i]["search_context"]]
    print(f"2.6.10 at 73d6bca: {len(moved)} of {len(test)} questions differ from 2.6.10a2's recorded context")
    print(f"candidate: {len(changed)} of {len(test)} questions changed against 2.6.10")
    if "--check-only" in sys.argv:
        return
    for name, chosen, rows in (("base", moved, base), ("pro", changed, cand)):
        if chosen:
            print(f"{name}: wrote {write(RUNS[name], {i: rows[i] for i in chosen}, a1).name} ({len(chosen)} questions)")


if __name__ == "__main__":
    main()
