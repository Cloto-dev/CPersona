#!/usr/bin/env python3
"""M3 -- how often the reservation can be the thing that answers.

Reads the per-query records a completed frozen replay leaves behind
(``*.queries.jsonl``), so it costs a file scan rather than a run.

The records carry what each arm brought, not the fused list, so two of the
three readings are bounds rather than counts:

  eligible universe < K   every row is reserved whatever floor exists, and a
                          floor decides only whether the answer is short or
                          empty -- a count, not a bound
  max(dense, lex) < K     necessary for the fused list to fall short of K
  dense + lex < K         sufficient for it to

Usage:
    python benchmarks/measurements/reservation_frequency.py ~/lmeb/replay_bgem3 ...
"""

from __future__ import annotations

import argparse
import json
import os
from glob import glob

DEFAULT_K = 10


def read_dir(directory: str, k: int) -> tuple[dict[str, tuple[int, int, int, int]], tuple[int, int, int, int]]:
    """Per task, and summed: (queries, necessary, sufficient, tiny universe)."""
    per_task: dict[str, tuple[int, int, int, int]] = {}
    totals = [0, 0, 0, 0]
    for path in sorted(glob(os.path.join(os.path.expanduser(directory), "*.queries.jsonl"))):
        task = os.path.basename(path).replace(".queries.jsonl", "")
        counts = [0, 0, 0, 0]
        with open(path, encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                dense = record.get("n_dense_adm", 0)
                lex = record.get("n_lex", 0)
                eligible = record.get("n_eligible", 0)
                counts[0] += 1
                if max(dense, lex) < k:
                    counts[1] += 1
                if dense + lex < k:
                    counts[2] += 1
                if eligible < k:
                    counts[3] += 1
        per_task[task] = tuple(counts)  # type: ignore[assignment]
        totals = [a + b for a, b in zip(totals, counts)]
    return per_task, tuple(totals)  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dirs", nargs="+", help="replay output directories")
    ap.add_argument("-k", type=int, default=DEFAULT_K,
                    help="the reservation target (config.RECALL_RESERVE_ROWS)")
    args = ap.parse_args()

    for directory in args.dirs:
        per_task, (queries, necessary, sufficient, tiny) = read_dir(directory, args.k)
        if not queries:
            print(f"=== {os.path.basename(directory)} === no query records found")
            continue
        print(f"=== {os.path.basename(directory)} ===  "
              f"tasks={len(per_task)} queries={queries}")
        print(f"  eligible universe < {args.k}      : {tiny:>6} ({tiny / queries:.4%})")
        print(f"  both arms under {args.k} (nec.)   : {necessary:>6} ({necessary / queries:.4%})")
        print(f"  arms sum under {args.k} (suff.)   : {sufficient:>6} ({sufficient / queries:.4%})")
        for task, (n, nec, suf, _) in sorted(per_task.items(), key=lambda kv: -kv[1][1]):
            if nec:
                print(f"    {task:<18} {nec}/{n} with both arms under {args.k} "
                      f"({nec / n:.2%}), sufficient {suf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
