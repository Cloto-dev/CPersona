#!/usr/bin/env python3
"""Cross-check reservation_floor.py against the row-keyed evidence dumps.

An independent path to the same two numbers: the dumps hold the similarity the
retrieval path itself computed, sampled by dense rank rather than uniformly, so
agreement is evidence that the cache-side instrument reads the right vectors
and the right population -- and the lowest gold similarity, which no weighting
can move, is the sharpest of the two.

The dumps keep every row above a rank cut and a fixed sample per deeper bin, so
a deep row stands for its bin: its weight is the bin's population over the
number of its rows that were dumped. Reconstructed that way the weights sum to
the eligible universe.

Usage:
    python benchmarks/measurements/reservation_floor_crosscheck.py \\
        ~/lmeb/evidence_jinanano LMEB_SciFact
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import statistics
from collections import defaultdict


def bin_of(rank: int, edges: list[int]) -> int:
    which = 0
    for i, edge in enumerate(edges):
        if rank >= edge:
            which = i
    return which


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dump_dir", help="an evidence dump directory")
    ap.add_argument("task")
    args = ap.parse_args()

    base = os.path.expanduser(args.dump_dir)
    meta = json.load(open(os.path.join(base, f"{args.task}.meta.json"), encoding="utf-8"))
    edges = meta["bin_edges"]
    keep_all_below = meta["keep_all_below"]
    query_meta = {q["qid"]: q for q in meta["queries"]}

    rows: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    with gzip.open(os.path.join(base, f"{args.task}.rows.csv.gz"), "rt") as f:
        for row in csv.DictReader(f):
            rows[row["qid"]].append((int(row["vr"]), int(row["y"]), float(row["v"])))

    shares: list[float] = []
    gold: list[float] = []
    for qid, dumped in rows.items():
        qm = query_meta.get(qid)
        if qm is None:
            continue
        in_bin: dict[int, int] = defaultdict(int)
        for rank, _, _ in dumped:
            if rank >= keep_all_below:
                in_bin[bin_of(rank, edges)] += 1

        at_or_below = weighted = 0.0
        for rank, is_gold, similarity in dumped:
            if is_gold:
                gold.append(similarity)
                continue
            if rank < keep_all_below:
                weight = 1.0
            else:
                which = bin_of(rank, edges)
                population = qm.get("bin_pop", {}).get(str(which), 0)
                weight = population / in_bin[which] if in_bin[which] else 0.0
            weighted += weight
            if similarity <= 0:
                at_or_below += weight
        if weighted:
            shares.append(at_or_below / weighted)

    if not shares:
        print(f"{args.task}: no query in the dump could be weighted")
        return 1

    mean = statistics.fmean(shares)
    stderr = statistics.pstdev(shares) / (len(shares) ** 0.5)
    print(f"{args.task:<16} queries={len(shares):>4}  "
          f"P(v<=0) weighted mean over queries = {mean:.5f}  (SE {stderr:.5f})")
    print(f"{'':<16} gold rows dumped={len(gold):>4}  "
          f"at or below zero: {sum(1 for v in gold if v <= 0):>3}  "
          f"lowest gold v = {min(gold):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
