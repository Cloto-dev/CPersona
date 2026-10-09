"""Build the answer inputs of the Lite and Pro test from its searches.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-v1_6.md.

The 400 test questions are searched at the registered commit through the harness's search wrapper
(search_driver.py), once with CPERSONA_SEARCH_MODE=lite and CPERSONA_SEARCH_COUNT=10 and once with
CPERSONA_SEARCH_MODE=pro and CPERSONA_SEARCH_COUNT=15.

- Lite: a question is changed when its context differs from 2.6.7's `lite: true` context, which is
  `whole`'s test context (the `whole` test's where it answered the question, the 2.6.5a1 test's
  otherwise). The Lite results directory holds the changed questions only; every other question keeps
  its recorded answer, because it sends the same prompt. If none changed, no directory is written.
- Pro: every test question is answered; its directory holds all 400.

Each entry is the 2.6.5a1 test's, with only search_context and search_duration_ms replaced, which the
harness answers from (--from-step 3).

usage: OMNIMEMEVAL_DIR=<checkout> python v1_6_build.py <search dir> [--check-only]
       (<search dir>/lite/search.json and <search dir>/pro/search.json)
"""

import json
import os
import shutil
import sys
from pathlib import Path

from v1_5_build import RUN as A1_RUN
from v1_5_build import load, test_questions
from v1_5_whole_build import RUN as WHOLE_RUN

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
LITE_RUN, PRO_RUN = "cpersona-lme1-v16lite", "cpersona-lme1-v16pro"


def entries(run: str) -> dict[int, tuple[str, list]]:
    path = H / "results/lme" / run / "cpersona_lme_search_results.json"
    data = json.load(open(path)) if path.exists() else {}
    return {int(uid.rsplit("_", 1)[1]): (uid, conv) for uid, conv in data.items()}


def lite_reference(test: list[int]) -> dict[int, str]:
    """2.6.7 `lite: true`'s test context: `whole`'s, as its registered test recorded it."""
    a1, whole = entries(A1_RUN), entries(WHOLE_RUN)
    assert sorted(a1) == test, "the 2.6.5a1 test did not answer exactly the test questions"
    return {i: (whole.get(i) or a1[i])[1][0]["search_context"] for i in test}


def write(run: str, chosen: dict[int, dict], a1: dict) -> Path:
    dst = H / "results/lme" / run
    if dst.exists():
        sys.exit(f"stop: {dst} exists")
    dst.mkdir(parents=True)
    out = {}
    for i, r in sorted(chosen.items()):
        uid, conv = a1[i]
        # a number, as the harness wrote it: the judge stage adds it to the answer's duration
        out[uid] = [{**conv[0], "search_context": r["search_context"], "search_duration_ms": float(r["search_duration_ms"])}]
    json.dump(out, open(dst / "cpersona_lme_search_results.json", "w"), ensure_ascii=False, indent=4)
    shutil.copy2(H / "results/lme" / A1_RUN / "cpersona_lme_search_status.json", dst / "cpersona_lme_search_status.json")
    (dst / ".step_1_done").touch()
    (dst / ".step_2_done").touch()
    return dst


def main():
    search_dir = Path([a for a in sys.argv[1:] if a != "--check-only"][0])
    test = test_questions()
    reference = lite_reference(test)
    lite, pro = load(search_dir, "lite", test), load(search_dir, "pro", test)
    moved = [i for i in test if lite[i]["search_context"] != reference[i]]
    print(f"lite: {len(moved)} of {len(test)} questions changed against 2.6.7's lite: {moved}")
    same_as_lite = sum(pro[i]["search_context"] == reference[i] for i in test)
    print(f"pro: {len(test)} questions to answer ({same_as_lite} with 2.6.7 lite's context)")
    if "--check-only" in sys.argv:
        return
    a1 = entries(A1_RUN)
    if moved:
        print(f"lite: wrote {write(LITE_RUN, {i: lite[i] for i in moved}, a1).name} ({len(moved)} questions)")
    print(f"pro: wrote {write(PRO_RUN, pro, a1).name} ({len(test)} questions)")


if __name__ == "__main__":
    main()
