"""Check the query segmenter's LongMemEval-S searches against the contexts they must equal.

Registration: benchmarks/measurements/prereg-query-segmenter.md.

CPERSONA_QUERY_SEGMENTER=morph changes how the Japanese and Chinese runs of a query are cut into
keyword phrases and builds every other term as before. The questions of LongMemEval-S are English,
so the registration expects no context to change. The 400 test questions (the 500 less
v1_5_dev_questions.json) were searched twice on copies of the published store at the registered
commit, with the published search's settings and morph: with no keyword seat ("morph0"), compared
with the published contexts, and with two ("morph2"), compared with the two-seat search of the
keyword-seats test (prereg-keyword-seats.md). Prints, for each, how many of the 400 are equal, and
the indices of any that are not.

usage: OMNIMEMEVAL_DIR=<checkout> python query_segmenter_check.py <search dir> <keyword-seats search dir>
       (<search dir>/morph0/search.json, <search dir>/morph2/search.json,
        <keyword-seats search dir>/seats2/search.json)
"""

import json
import os
import sys
from pathlib import Path

from v1_5_build import load, test_questions

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
PUBLISHED = H / "results/lme/cpersona-lme1"


def main():
    search_dir, seats_dir = Path(sys.argv[1]), Path(sys.argv[2])
    test = test_questions()
    data = json.load(open(PUBLISHED / "cpersona_lme_search_results.json"))
    pub = {int(uid.rsplit("_", 1)[1]): conv[0]["search_context"] for uid, conv in data.items()}
    assert set(test) <= set(pub), "the published run does not hold every test question"
    seats2 = load(seats_dir, "seats2", test)
    for point, reference, name in (("morph0", pub, "the published contexts"),
                                   ("morph2", {i: r["search_context"] for i, r in seats2.items()},
                                    "the keyword-seats test's two-seat contexts")):
        rows = load(search_dir, point, test)
        differ = [i for i in test if rows[i]["search_context"] != reference[i]]
        print(f"{point}: {len(test) - len(differ)}/{len(test)} questions equal {name}"
              + (f"; differ: {differ}" if differ else ""))


if __name__ == "__main__":
    main()
