"""Build the answer inputs of the V1.5 (2.6.5a1) test from its re-run searches.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-v1_5-a1.md.
The test questions are the 400 not drawn into v1_5_dev_questions.json. Two searches of them ran on
copies of the published store with 2.6.5a1 (search_driver.py, SEARCH_INDICES set to these
questions): the default sequence at the default budget ("items"), which must give the published
contexts for every test question before anything is written, and the registered point
(CPERSONA_RECONSTRUCT_SEQUENCE=evidence, CPERSONA_RECONSTRUCT_FORCED_BUDGET=2800). This writes the
point's results directory, which the harness answers from (--from-step 3): the published arm B
search entries of the test questions with only search_context and search_duration_ms replaced.

usage: OMNIMEMEVAL_DIR=<checkout> python v1_5_build.py <search dir> [--check-only]
       (<search dir>/items/search.json and <search dir>/evidence-b2800/search.json)
"""
import json
import os
import shutil
import sys
from pathlib import Path

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
SRC = H / "results/lme/cpersona-lme1"
DEV = Path(__file__).with_name("v1_5_dev_questions.json")
POINT, RUN = "evidence-b2800", "cpersona-lme1-v15a1-b2800"


def test_questions() -> list[int]:
    dev = {e["index"] for e in json.load(open(DEV))["dev"]}
    assert len(dev) == 100, "expected the 100 development questions"
    return [i for i in range(500) if i not in dev]


def load(search_dir: Path, name: str, test: list[int]) -> dict:
    rows = json.load(open(search_dir / name / "search.json"))
    by_i = {r["i"]: r for r in rows}
    assert len(by_i) == len(rows), f"{name}: a question searched twice"
    assert sorted(by_i) == test, f"{name}: not the test questions"
    assert all(r["search_context"] for r in rows), f"{name}: an empty context"
    return by_i


def main():
    search_dir = Path([a for a in sys.argv[1:] if a != "--check-only"][0])
    test = test_questions()
    data = json.load(open(SRC / "cpersona_lme_search_results.json"))
    pub = {int(uid.rsplit("_", 1)[1]): (uid, conv) for uid, conv in data.items()}
    items = load(search_dir, "items", test)
    same = sum(r["search_context"] == pub[i][1][0]["search_context"] for i, r in items.items())
    print(f"reproduction check: the defaults give the published context for {same}/{len(test)} test questions")
    if same != len(test):
        sys.exit("stop: the defaults do not reproduce the published contexts")
    point = load(search_dir, POINT, test)
    if "--check-only" in sys.argv:
        return
    dst = H / "results/lme" / RUN
    if dst.exists():
        sys.exit(f"stop: {dst} exists")
    dst.mkdir(parents=True)
    out = {}
    for i in test:
        uid, conv = pub[i]
        r = point[i]
        # a number, as the harness wrote it: the judge stage adds it to the answer's duration
        out[uid] = [{**conv[0], "search_context": r["search_context"],
                     "search_duration_ms": float(r["search_duration_ms"])}]
    json.dump(out, open(dst / "cpersona_lme_search_results.json", "w"), ensure_ascii=False, indent=4)
    shutil.copy2(SRC / "cpersona_lme_search_status.json", dst / "cpersona_lme_search_status.json")
    (dst / ".step_1_done").touch()
    (dst / ".step_2_done").touch()
    print(f"{POINT}: wrote {dst.name} ({len(out)} questions)")


if __name__ == "__main__":
    main()
