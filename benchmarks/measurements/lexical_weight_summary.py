#!/usr/bin/env python3
"""Summarise a lexical-weight sweep against the rule registered for it.

Reads the per-task records a `frozen_replay.py --w_sweep` run leaves behind
(`<task>.json`, field `w_sweep_mean`) and computes exactly the statistics
`prereg-lexical-weight-default.md` fixed in advance:

  * the unit is the task, and every comparison is paired across tasks inside a
    model -- never pooled across models, which rank differently
  * against the status quo: `NDCG(w) - NDCG(1.0)`, mean, paired t, dispersion
  * against a silent arm: `NDCG(w) - NDCG(0)`, the same three
  * a flatness reading for each candidate, because a value that ships must not
    sit on a cliff

Dispersion travels with every mean: the standard deviation of the paired
difference and how many tasks moved in its direction. A mean that rests on two
tasks is reported as that rather than as a mean.

Usage:
    python benchmarks/measurements/lexical_weight_summary.py \\
        --shipping jina=~/lmeb/wsweep_jinanano bge-m3=~/lmeb/wsweep_bgem3 \\
        --reference minilm=~/lmeb/wsweep_minilm
"""

from __future__ import annotations

import argparse
import json
import math
import os
from glob import glob

STATUS_QUO = "1.0"
SILENT = "0.0"
T_GATE = 2.0


def read_model(directory: str) -> dict[str, dict[str, float]]:
    """task -> {w: mean NDCG@10}."""
    out: dict[str, dict[str, float]] = {}
    for path in sorted(glob(os.path.join(os.path.expanduser(directory), "*.json"))):
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        sweep = record.get("w_sweep_mean")
        if sweep:
            out[record.get("task", os.path.basename(path)[:-5])] = sweep
    return out


def paired(values: dict[str, dict[str, float]], w: str, against: str) -> dict:
    """Mean, standard deviation, paired t and direction count of NDCG(w) - NDCG(against)."""
    diffs = [v[w] - v[against] for v in values.values() if w in v and against in v]
    n = len(diffs)
    if n < 2:
        return {"n": n}
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else math.inf * (1 if mean > 0 else -1)
    return {
        "n": n, "mean": mean, "sd": sd, "t": t,
        "positive": sum(1 for d in diffs if d > 0),
        "negative": sum(1 for d in diffs if d < 0),
    }


def ties_only(values: dict[str, dict[str, float]], task: str) -> bool:
    row = values[task]
    return max(row.values()) - min(row.values()) < 1e-9


def grid_of(models: dict[str, dict[str, dict[str, float]]]) -> list[str]:
    seen: set[str] = set()
    for tasks in models.values():
        for row in tasks.values():
            seen.update(row)
    return sorted(seen, key=float)


def table(name: str, values: dict[str, dict[str, float]], grid: list[str]) -> None:
    print(f"\n=== {name} ({len(values)} tasks) ===")
    print(f"{'w':>6} | {'vs 1.0: mean':>12} {'sd':>7} {'t':>7} {'+/-':>7} "
          f"| {'vs 0: mean':>11} {'sd':>7} {'t':>7} {'+/-':>7}")
    for w in grid:
        a = paired(values, w, STATUS_QUO)
        b = paired(values, w, SILENT)
        print(f"{w:>6} | {a['mean']:>12.3f} {a['sd']:>7.3f} {a['t']:>7.2f} "
              f"{a['positive']:>3}/{a['negative']:<3} "
              f"| {b['mean']:>11.3f} {b['sd']:>7.3f} {b['t']:>7.2f} "
              f"{b['positive']:>3}/{b['negative']:<3}")


def flatness(values: dict[str, dict[str, float]], w: str, grid: list[str]) -> list[str]:
    """Each neighbour of w, with the paired difference against w."""
    i = grid.index(w)
    lines = []
    for j in (i - 1, i + 1):
        if 0 <= j < len(grid):
            other = grid[j]
            d = paired(values, other, w)
            verdict = "indistinguishable" if abs(d["t"]) < T_GATE else "distinguishable"
            lines.append(f"{other:>5} vs {w}: mean {d['mean']:+.3f} sd {d['sd']:.3f} "
                         f"t {d['t']:+.2f} -> {verdict}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shipping", nargs="+", required=True, metavar="NAME=DIR",
                    help="the slots the rule is decided on")
    ap.add_argument("--reference", nargs="*", default=[], metavar="NAME=DIR",
                    help="endpoints reported in full but holding no veto")
    args = ap.parse_args()

    def parse(specs: list[str]) -> dict[str, dict[str, dict[str, float]]]:
        out = {}
        for spec in specs:
            name, _, directory = spec.partition("=")
            out[name] = read_model(directory)
        return out

    shipping = parse(args.shipping)
    reference = parse(args.reference)
    everything = {**shipping, **reference}
    grid = grid_of(everything)

    for name, values in everything.items():
        table(name + ("" if name in shipping else "  [reference, no veto]"), values, grid)

    print("\n=== abstention buckets ===")
    task_counts = {name: len(v) for name, v in everything.items()}
    print(f"  incomplete sweeps: {task_counts}")
    for name, values in everything.items():
        tied = [t for t in values if ties_only(values, t)]
        print(f"  every grid point ties ({name}): {tied or 'none'}")
    print(f"  excluded from the decision by rule 1: {list(reference)}")

    print("\n=== the rule ===")
    survivors = []
    for w in grid:
        if w == STATUS_QUO:
            continue
        one = all(paired(v, w, STATUS_QUO)["mean"] > 0 and paired(v, w, STATUS_QUO)["t"] > T_GATE
                  for v in shipping.values())
        two = all(paired(v, w, SILENT)["mean"] > 0 for v in shipping.values())
        mark = "PASS" if (one and two) else "fail"
        print(f"  w={w:<5} rule1(beats 1.0 on both, t>2)={one!s:<5} "
              f"rule2(beats a silent arm on both)={two!s:<5} -> {mark}")
        if one and two:
            survivors.append(w)

    print(f"\n  candidates passing rules 1 and 2: {survivors or 'none'}")

    # Rule 3, decided in code rather than by eye: both neighbours on the grid
    # must be inside the dispersion of the paired difference, on both shipping
    # slots. Where the neighbour is far away on the grid the gap is reported --
    # a wide spacing cannot show a cliff either way, and calling that "flat"
    # would be reading an absent measurement as a result.
    flat = []
    for w in survivors:
        print(f"\n  flatness around w={w}:")
        ok = True
        i = grid.index(w)
        for j in (i - 1, i + 1):
            if not (0 <= j < len(grid)):
                continue
            other = grid[j]
            gap = abs(float(other) - float(w))
            for name, values in shipping.items():
                d = paired(values, other, w)
                near = abs(d["t"]) < T_GATE
                ok = ok and near
                print(f"    [{name}] {other:>5} vs {w}: mean {d['mean']:+.3f} "
                      f"sd {d['sd']:.3f} t {d['t']:+.2f} (grid gap {gap:g}) -> "
                      f"{'indistinguishable' if near else 'DISTINGUISHABLE'}")
        print(f"    rule 3: {'pass' if ok else 'FAIL'}")
        if ok:
            flat.append(w)

    print(f"\n  candidates passing all three rules: {flat or 'none'}")
    if len(flat) > 1:
        print("\n  pairwise, among those (the tie-break applies only where these "
              "are inside the dispersion):")
        separable = []
        for a_i, a in enumerate(flat):
            for b in flat[a_i + 1:]:
                for name, values in shipping.items():
                    d = paired(values, b, a)
                    if abs(d["t"]) >= T_GATE:
                        separable.append((name, a, b, d["t"]))
                    print(f"    [{name}] {b} vs {a}: mean {d['mean']:+.3f} "
                          f"sd {d['sd']:.3f} t {d['t']:+.2f}")
        if separable:
            print(f"\n  NOT one flat region: {separable}")
        else:
            print("\n  every pair is inside the dispersion: one flat region")
    if flat:
        print(f"\n  tie-break (registered: the largest of the indistinguishable) "
              f"-> w={flat[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
