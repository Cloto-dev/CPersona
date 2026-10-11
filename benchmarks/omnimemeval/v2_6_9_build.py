"""Build the answer inputs of the keyword seat's check from its searches.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-2_6_9.md.

The 400 test questions are searched at the candidate commit through the harness's search wrapper
(search_driver.py), once with CPERSONA_SEARCH_MODE=lite and CPERSONA_SEARCH_COUNT=10 and once with
CPERSONA_SEARCH_MODE=pro and CPERSONA_SEARCH_COUNT=15, as in the Lite and Pro test.

A question is changed in a mode when its context differs from that mode's recorded 2.6.8 context: the Lite and Pro
test's searches (<2.6.8 search dir>/lite/search.json and /pro/search.json). Each mode's results directory holds its
changed questions only; every other question sends the same prompt and keeps its recorded answer. If none changed in
a mode, no directory is written for it.

Each entry is the 2.6.5a1 test's, with only search_context and search_duration_ms replaced, which the harness answers
from (--from-step 3).

usage: OMNIMEMEVAL_DIR=<checkout> python v2_6_9_build.py <search dir> <2.6.8 search dir> [--check-only]
"""

import sys
from pathlib import Path

from v1_5_build import load, test_questions
from v1_6_build import entries, write
from v1_5_build import RUN as A1_RUN

RUNS = {"lite": "cpersona-lme1-v269lite", "pro": "cpersona-lme1-v269pro"}


def main():
    args = [a for a in sys.argv[1:] if a != "--check-only"]
    new_dir, old_dir = Path(args[0]), Path(args[1])
    test = test_questions()
    changed = {}
    for mode in RUNS:
        new, old = load(new_dir, mode, test), load(old_dir, mode, test)
        changed[mode] = [i for i in test if new[i]["search_context"] != old[i]["search_context"]]
        print(f"{mode}: {len(changed[mode])} of {len(test)} questions changed against 2.6.8: {changed[mode]}")
    if "--check-only" in sys.argv:
        return
    a1 = entries(A1_RUN)
    for mode, run in RUNS.items():
        if changed[mode]:
            new = load(new_dir, mode, test)
            print(f"{mode}: wrote {write(run, {i: new[i] for i in changed[mode]}, a1).name} ({len(changed[mode])} questions)")


if __name__ == "__main__":
    main()
