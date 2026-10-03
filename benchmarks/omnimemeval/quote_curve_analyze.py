"""Read the quote curve as registered (benchmarks/measurements/prereg-omnimemeval-lme-quote-curve.md).

Each quote point is compared with its item-count control from the count curve at the same cost,
and with every item: 95% bootstrap intervals of paired accuracy differences (10,000 resamples over
questions, seed 20261003). Point beats control when the lower bound is above 0, control beats point
when the upper bound is below 0. A point holds against every item when the lower bound of point -
full is at or above -2.0 points. A point whose mean context differs from its control's by more than
5% is compared as "not at the same cost".

usage: OMNIMEMEVAL_DIR=<checkout> QUOTE_CURVE_USAGE_DIR=<dir> COUNT_CURVE_USAGE_DIR=<dir> \\
       python quote_curve_analyze.py <Q>:<k> [<Q>:<k> ...]     e.g. 400:4 560:6
"""
import os
import sys
from pathlib import Path

from count_curve_analyze import MARGIN, acc, boot, judged

H = Path(os.environ["OMNIMEMEVAL_DIR"])
QU = Path(os.environ["QUOTE_CURVE_USAGE_DIR"])  # q<Q>-pass<n>.json, each pass's token_usage_answer.json
KU = Path(os.environ["COUNT_CURVE_USAGE_DIR"])  # k<k>-pass<n>.json from the count curve
SAME_COST = 0.05


def api(usage_dir: Path, stem: str):
    import json
    calls = prompt = 0
    for p in sorted(usage_dir.glob(f"{stem}-pass*.json")):
        m = json.loads(p.read_text())["modules"]["ANSWER"]
        calls += m["usage_reported_call_count"]
        prompt += m["prompt_tokens"]
    return calls, prompt


def ctx(p: dict) -> float:
    return sum(v[2] for v in p.values()) / len(p)


def row(name, p, calls, prompt, cats):
    correct = sum(v[0] for v in p.values())
    a = prompt / calls if calls else float("nan")
    per = prompt / correct if correct and calls == len(p) else float("nan")
    by = " | ".join(f"{sum(v[0] for v in p.values() if v[1] == c) / sum(1 for v in p.values() if v[1] == c) * 100:.1f}"
                    for c in cats)
    return f"| {name} | {acc(p):.2f} | {ctx(p):.1f} | {a:.1f} ({calls} calls) | {per:.0f} | {by} |"


def main():
    pairs = [a.split(":") for a in sys.argv[1:]]
    full = judged(H / "results/lme/cpersona-lme1-kfull")
    cats = sorted({v[1] for v in full.values()})
    print("| point | accuracy | context (cl100k) | Context Tokens (API) | per correct | " + " | ".join(cats) + " |")
    print(row("every item", full, *api(KU, "kfull"), cats))
    for q, k in pairs:
        p = judged(H / f"results/lme/cpersona-lme1-q{q}")
        c = judged(H / f"results/lme/cpersona-lme1-k{k}")
        print(row(f"{k} items", c, *api(KU, f"k{k}"), cats))
        print(row(f"quotes {q}/{int(q) // 2}", p, *api(QU, f"q{q}"), cats))
    print()
    for q, k in pairs:
        p = judged(H / f"results/lme/cpersona-lme1-q{q}")
        c = judged(H / f"results/lme/cpersona-lme1-k{k}")
        v = read(p, c, full)
        print(f"quotes {q} vs {k} items: context {ctx(p):.1f} vs {ctx(c):.1f} ({v['gap'] * 100:+.1f}%, "
              f"{'same cost' if v['same_cost'] else 'NOT at the same cost'}); difference {v['d']:+.2f} "
              f"({v['lo']:+.2f} to {v['hi']:+.2f}) -> "
              f"{v['verdict'] if v['same_cost'] else 'compared as not at the same cost: ' + v['verdict']}")
        print(f"quotes {q} vs every item: {v['d_full']:+.2f} ({v['lo_full']:+.2f} to {v['hi_full']:+.2f}) -> "
              f"{'holds' if v['holds'] else 'does not hold'}")


def read(p: dict, c: dict, full: dict) -> dict:
    """The registered reading of one quote point p against its item-count control c and every item."""
    gap = ctx(p) / ctx(c) - 1
    d, lo, hi = boot(p, c)
    d_full, lo_full, hi_full = boot(p, full)
    return {
        "gap": gap, "same_cost": abs(ctx(p) - ctx(c)) <= SAME_COST * ctx(c), "d": d, "lo": lo, "hi": hi,
        "verdict": "shorter quotes better" if lo > 0 else "fewer items better" if hi < 0 else "neither claimed",
        "d_full": d_full, "lo_full": lo_full, "hi_full": hi_full, "holds": lo_full >= MARGIN,
    }


if __name__ == "__main__":
    main()
