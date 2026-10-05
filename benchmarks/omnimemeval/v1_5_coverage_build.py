"""Build the answer inputs of the coverage-against-whole test from its searches.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-v1_5-coverage.md.

The 2.6.5a1 test's 400 questions were searched twice on copies of the published store at the
registered commit, as the earlier tests' searches were: the length floor (whole) at budget 2,800,
which must give the whole test's contexts for every test question before anything is written, and
the coverage order (CPERSONA_RECONSTRUCT_SEQUENCE=coverage) at the same budget. whole's context for a
question is the whole test's when that test answered it, and the 2.6.5a1 test's otherwise. A question
is changed when coverage's context differs from whole's. This writes the point's results directory
with the changed questions only, which the harness answers from (--from-step 3): the 2.6.5a1 test's
entries with only search_context and search_duration_ms replaced. Every other question keeps whole's
recorded answer, because it sends the same prompt.

usage: OMNIMEMEVAL_DIR=<checkout> python v1_5_coverage_build.py <search dir> [--check-only]
       (<search dir>/whole-b2800/search.json and <search dir>/coverage-b2800/search.json)
"""

import json
import os
import shutil
import sys
from pathlib import Path

from v1_5_build import load, test_questions
from v1_5_whole_build import A1, a1_entries
from v1_5_whole_build import RUN as WHOLE_RUN

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
POINT, RUN = "coverage-b2800", "cpersona-lme1-v15cov-b2800"


def whole_contexts(test: list[int], pub: dict) -> dict[int, str]:
    """whole's test context per question: the whole test's where it answered, the 2.6.5a1 test's otherwise."""
    data = json.load(open(H / "results/lme" / WHOLE_RUN / "cpersona_lme_search_results.json"))
    moved = {int(uid.rsplit("_", 1)[1]): conv[0]["search_context"] for uid, conv in data.items()}
    return {i: moved.get(i, pub[i][1][0]["search_context"]) for i in test}


def changed(search_dir: Path, test: list[int], pub: dict) -> tuple[dict, list[int], dict[int, str]]:
    """The reproduction check, then coverage's searches and the questions whose context changed."""
    whole = whole_contexts(test, pub)
    rerun = load(search_dir, "whole-b2800", test)
    same = sum(r["search_context"] == whole[i] for i, r in rerun.items())
    print(f"reproduction check: whole gives the whole test context for {same}/{len(test)} questions")
    if same != len(test):
        sys.exit("stop: whole does not reproduce the whole test contexts")
    point = load(search_dir, POINT, test)
    return point, [i for i in test if point[i]["search_context"] != whole[i]], whole


def main():
    search_dir = Path([a for a in sys.argv[1:] if a != "--check-only"][0])
    test = test_questions()
    pub = a1_entries(test)
    point, moved, _ = changed(search_dir, test, pub)
    print(f"{POINT}: {len(moved)} of {len(test)} questions changed")
    if "--check-only" in sys.argv or not moved:
        return
    dst = H / "results/lme" / RUN
    if dst.exists():
        sys.exit(f"stop: {dst} exists")
    dst.mkdir(parents=True)
    out = {}
    for i in moved:
        uid, conv = pub[i]
        r = point[i]
        # a number, as the harness wrote it: the judge stage adds it to the answer's duration
        out[uid] = [{**conv[0], "search_context": r["search_context"],
                     "search_duration_ms": float(r["search_duration_ms"])}]
    json.dump(out, open(dst / "cpersona_lme_search_results.json", "w"), ensure_ascii=False, indent=4)
    shutil.copy2(A1 / "cpersona_lme_search_status.json", dst / "cpersona_lme_search_status.json")
    (dst / ".step_1_done").touch()
    (dst / ".step_2_done").touch()
    print(f"{POINT}: wrote {dst.name} ({len(out)} questions)")


if __name__ == "__main__":
    main()
