"""Build the answer inputs of the whole-against-evidence test from its re-run searches.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-v1_5-whole.md.

The 2.6.5a1 test's 400 questions were searched twice on copies of the published store at the
registered commit, as that test's searches were: the evidence sequence at budget 2,800, which must
give the 2.6.5a1 test's contexts for every test question before anything is written, and the length
floor (CPERSONA_RECONSTRUCT_SEQUENCE=whole) at the same budget. A question is changed when whole's
context differs from its 2.6.5a1 test context. This writes the point's results directory with the
changed questions only, which the harness answers from (--from-step 3): the 2.6.5a1 test's entries
with only search_context and search_duration_ms replaced. Every other question keeps that test's
recorded answer, because it sends the same prompt.

usage: OMNIMEMEVAL_DIR=<checkout> python v1_5_whole_build.py <search dir> [--check-only]
       (<search dir>/evidence-b2800/search.json and <search dir>/whole-b2800/search.json)
"""

import json
import os
import shutil
import sys
from pathlib import Path

from v1_5_build import RUN as A1_RUN
from v1_5_build import load, test_questions

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
A1 = H / "results/lme" / A1_RUN
POINT, RUN = "whole-b2800", "cpersona-lme1-v15whole-b2800"


def a1_entries(test: list[int]) -> dict:
    data = json.load(open(A1 / "cpersona_lme_search_results.json"))
    pub = {int(uid.rsplit("_", 1)[1]): (uid, conv) for uid, conv in data.items()}
    assert sorted(pub) == test, "the 2.6.5a1 test did not answer exactly the test questions"
    return pub


def changed(search_dir: Path, test: list[int], pub: dict) -> tuple[dict, list[int]]:
    """The reproduction check, then whole's searches and the questions whose context changed."""
    evidence = load(search_dir, "evidence-b2800", test)
    same = sum(r["search_context"] == pub[i][1][0]["search_context"] for i, r in evidence.items())
    print(f"reproduction check: the evidence sequence gives the 2.6.5a1 test context for {same}/{len(test)} questions")
    if same != len(test):
        sys.exit("stop: the evidence sequence does not reproduce the 2.6.5a1 test contexts")
    point = load(search_dir, POINT, test)
    return point, [i for i in test if point[i]["search_context"] != pub[i][1][0]["search_context"]]


def main():
    search_dir = Path([a for a in sys.argv[1:] if a != "--check-only"][0])
    test = test_questions()
    pub = a1_entries(test)
    point, moved = changed(search_dir, test, pub)
    print(f"{POINT}: {len(moved)} of {len(test)} questions changed: {moved}")
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
