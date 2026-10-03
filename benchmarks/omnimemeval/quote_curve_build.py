"""Build the quote-curve answer inputs from the re-run searches.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-quote-curve.md.
Each size's search ran on its own copy of the published store (search_driver.py over the
ingestion's user ids). This writes, per size, a results directory the harness answers from
(--from-step 3): the published arm B search entries with only search_context and
search_duration_ms replaced. The published sizes (800/400) must reproduce the published contexts
for every question before anything is written.

usage: OMNIMEMEVAL_DIR=<checkout> python quote_curve_build.py <search dir> <quote/tail> [...] [--check-only]
       (<search dir>/q<Q>-t<T>/search.json holds a size's contexts)
"""
import json
import os
import shutil
import sys
from pathlib import Path

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
SRC = H / "results/lme/cpersona-lme1"
PUBLISHED = "800/400"


def load(search_dir: Path, size: str) -> dict:
    q, t = size.split("/")
    rows = json.load(open(search_dir / f"q{q}-t{t}" / "search.json"))
    by_i = {r["i"]: r for r in rows}
    assert sorted(by_i) == list(range(500)), f"{size}: questions missing or repeated"
    assert all(r["search_context"] for r in rows), f"{size}: an empty context"
    return by_i


def main():
    args = [a for a in sys.argv[1:] if a != "--check-only"]
    search_dir, sizes = Path(args[0]), args[1:]
    data = json.load(open(SRC / "cpersona_lme_search_results.json"))
    pub = {int(uid.rsplit("_", 1)[1]): conv[0]["search_context"] for uid, conv in data.items()}
    same = sum(r["search_context"] == pub[i] for i, r in load(search_dir, PUBLISHED).items())
    print(f"reproduction check: {PUBLISHED} gives the published context for {same}/500 questions")
    if same != 500:
        sys.exit("stop: the published sizes do not reproduce the published contexts")
    if "--check-only" in sys.argv:
        return
    for size in sizes:
        q = size.split("/")[0]
        rows = load(search_dir, size)
        dst = H / f"results/lme/cpersona-lme1-q{q}"
        if dst.exists():
            sys.exit(f"stop: {dst} exists")
        dst.mkdir(parents=True)
        out = {}
        for uid, conv in data.items():
            r = rows[int(uid.rsplit("_", 1)[1])]
            out[uid] = [{**conv[0], "search_context": r["search_context"],
                         "search_duration_ms": str(r["search_duration_ms"])}]
        json.dump(out, open(dst / "cpersona_lme_search_results.json", "w"), ensure_ascii=False, indent=4)
        shutil.copy2(SRC / "cpersona_lme_search_status.json", dst / "cpersona_lme_search_status.json")
        (dst / ".step_1_done").touch()
        (dst / ".step_2_done").touch()
        print(f"{size}: wrote {dst.name}")


if __name__ == "__main__":
    main()
