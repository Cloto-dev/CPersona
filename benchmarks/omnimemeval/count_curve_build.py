"""Build the count-curve inputs: arm B's published search results cut to the first k items.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-count-curve.md.
The harness builds search_context as "Conversation memories:\n\n" + "\n".join(blocks), and
the adapter renders one block per reconstruct item, "[<as_of>]\n<quote>[\n…\n<excerpt>]\n".
A block starts at a line that is exactly an ISO-8601 header; 18 contexts hold other lines
starting with "[", so only that exact form delimits. Every split is checked to join back
to the original context byte for byte before anything is written.

usage: OMNIMEMEVAL_DIR=<checkout> python count_curve_build.py [--check-only]
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
SRC = H / "results/lme/cpersona-lme1"
PREFIX = "Conversation memories:\n\n"
HEADER = re.compile(r"(?m)^\[\d{4}-\d{2}-\d{2}T[^\]\n]+\]$")
POINTS = [1, 2, 3, 4, 5, 6, 8, 10, "full"]


def split(ctx: str) -> list[str]:
    assert ctx.startswith(PREFIX), "context without the harness prefix"
    body = ctx[len(PREFIX):]
    starts = [m.start() for m in HEADER.finditer(body)]
    assert starts and starts[0] == 0, "first block does not start with a header"
    # blocks are joined by one "\n"; each block's own text ends with "\n"
    blocks, ends = [], starts[1:] + [len(body) + 1]
    for s, e in zip(starts, ends):
        blocks.append(body[s:e - 1])
    assert PREFIX + "\n".join(blocks) == ctx, "split does not join back"
    return blocks


def main():
    data = json.load(open(SRC / "cpersona_lme_search_results.json"))
    status = json.load(open(SRC / "cpersona_lme_search_status.json"))
    counts = {}
    for conv in data.values():
        for row in conv:
            n = len(split(row["search_context"]))
            counts[n] = counts.get(n, 0) + 1
    print("all 500 split and join back; blocks per question:", dict(sorted(counts.items())))
    if "--check-only" in sys.argv:
        return
    for k in POINTS:
        dst = H / f"results/lme/cpersona-lme1-k{k}"
        if dst.exists():
            sys.exit(f"stop: {dst} exists")
        dst.mkdir(parents=True)
        out = {}
        for conv_id, conv in data.items():
            out[conv_id] = []
            for row in conv:
                blocks = split(row["search_context"])
                kept = blocks if k == "full" else blocks[:k]
                out[conv_id].append({**row, "search_context": PREFIX + "\n".join(kept)})
        json.dump(out, open(dst / "cpersona_lme_search_results.json", "w"), ensure_ascii=False, indent=4)
        shutil.copy2(SRC / "cpersona_lme_search_status.json", dst / "cpersona_lme_search_status.json")
        (dst / ".step_1_done").touch()
        (dst / ".step_2_done").touch()
        if k == "full":
            same = all(a["search_context"] == b["search_context"]
                       for ca, cb in zip(data.values(), out.values()) for a, b in zip(ca, cb))
            assert same, "full point differs from the published contexts"
        print(f"k={k}: wrote {dst.name}")
    del status


if __name__ == "__main__":
    main()
