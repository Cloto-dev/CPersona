"""Build the answer inputs of the keyword-seats test from its searches.

Registration: benchmarks/measurements/prereg-keyword-seats.md.

The 400 test questions (the 500 less v1_5_dev_questions.json) were searched twice on copies of the
published store at the registered commit, with the published search's settings: with no keyword seat
("seats0"), which must give the published contexts for every test question before anything is written,
and with two (CPERSONA_KEYWORD_SEATS=2, "seats2"). A question is changed when its seats2 context
differs from the published one. This writes the point's results directory with the changed questions
only, which the harness answers from (--from-step 3): the published entries with only search_context
and search_duration_ms replaced. Every other question keeps its published answer, because it sends the
same prompt.

usage: OMNIMEMEVAL_DIR=<checkout> python keyword_seats_build.py <search dir> [--check-only]
       (<search dir>/seats0/search.json and <search dir>/seats2/search.json)
"""

import json
import os
import shutil
import sys
from pathlib import Path

from v1_5_build import load, test_questions

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
PUBLISHED = H / "results/lme/cpersona-lme1"
POINT, RUN = "seats2", "cpersona-lme1-kwseats2"


def published(test: list[int]) -> dict:
    data = json.load(open(PUBLISHED / "cpersona_lme_search_results.json"))
    pub = {int(uid.rsplit("_", 1)[1]): (uid, conv) for uid, conv in data.items()}
    assert set(test) <= set(pub), "the published run does not hold every test question"
    return {i: pub[i] for i in test}


def changed(search_dir: Path, test: list[int], pub: dict) -> tuple[dict, list[int]]:
    """The reproduction check, then the seats' searches and the questions whose context changed."""
    seats0 = load(search_dir, "seats0", test)
    same = sum(r["search_context"] == pub[i][1][0]["search_context"] for i, r in seats0.items())
    print(f"reproduction check: no keyword seat gives the published context for {same}/{len(test)} questions")
    if same != len(test):
        sys.exit("stop: no keyword seat does not reproduce the published contexts")
    point = load(search_dir, POINT, test)
    return point, [i for i in test if point[i]["search_context"] != pub[i][1][0]["search_context"]]


def main():
    search_dir = Path([a for a in sys.argv[1:] if a != "--check-only"][0])
    test = test_questions()
    pub = published(test)
    point, moved = changed(search_dir, test, pub)
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
    shutil.copy2(PUBLISHED / "cpersona_lme_search_status.json", dst / "cpersona_lme_search_status.json")
    (dst / ".step_1_done").touch()
    (dst / ".step_2_done").touch()
    print(f"{POINT}: wrote {dst.name} ({len(out)} questions)")


if __name__ == "__main__":
    main()
