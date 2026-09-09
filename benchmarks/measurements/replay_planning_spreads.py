#!/usr/bin/env python3
"""Planning spreads and detectable effect sizes for the adaptive-fusion comparison.

Reads the frozen-stage replay's per-task aggregates and reports, per model, the
per-task differences of the two non-adaptive arms against the dense-only order:

    a - S0   shipped reciprocal rank fusion (lexical weight 1.0)
    b - S0   dense-first with lexical weight 0.1

Their paired spread is the planning proxy for the spread of any adaptive rule's
difference from a constant, which is what the pre-registered comparison has to
detect. From it the script prints the minimum detectable effect of a one-sided
paired t-test over the tasks, and the power that test has against a
one-NDCG-point improvement, at both the nominal level and the strictest level
of the pre-registered Holm family.

The proxy is an assumption, named as such in the accompanying note: the
adaptive rule's paired spread is not this spread, and a materially different
one measured on a development sample refutes the planning numbers here.

Usage (REPLAY_ROOT holds the replay_<model>/ directories; default `.`):

    REPLAY_ROOT=~/lmeb python benchmarks/measurements/replay_planning_spreads.py \
        replay_minilm=MiniLM replay_jinanano_cap200=jina-v5-nano replay_bgem3=bge-m3

With no arguments it uses those three directory names and labels. `--common`
restricts every model to the tasks all of them completed, so the comparison is
not confounded by a model having finished more tasks than another.
"""

from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys

try:
    from scipy import stats  # type: ignore
except ImportError:  # pragma: no cover - reported, not worked around
    stats = None

DEFAULT_MODELS = [
    ("replay_minilm", "MiniLM"),
    ("replay_jinanano_cap200", "jina-v5-nano"),
    ("replay_bgem3", "bge-m3"),
]
# The pre-registered family is ten directional claims under Holm at 0.05, so the
# strictest single-test level is 0.05/10; the nominal column is one unadjusted
# one-sided test, which is the most favourable case and therefore a floor.
ALPHA_NOMINAL = 0.05
ALPHA_HOLM_STRICTEST = 0.005
POWER_TARGETS = (0.80, 0.90)
ONE_POINT = 1.0


def per_task_deltas(root: str, model_dir: str) -> dict[str, tuple[float, float]]:
    """{task: (a - S0, b - S0)} from the per-task aggregates."""
    out: dict[str, tuple[float, float]] = {}
    for path in sorted(glob.glob(os.path.join(root, model_dir, "*.json"))):
        with open(path) as fh:
            j = json.load(fh)
        if "mean" not in j or "w_sweep_mean" not in j:
            continue
        s0 = j["mean"]["S0_dense"]
        sweep = j["w_sweep_mean"]
        out[j["task"]] = (sweep["1.0"] - s0, sweep["0.1"] - s0)
    return out


def mde(sd: float, n: int, alpha: float, power: float) -> float | None:
    """Smallest mean difference a one-sided paired t-test detects at `power`."""
    if stats is None or n < 2:
        return None
    df = n - 1
    crit = stats.t.ppf(1 - alpha, df)
    # Solve for the noncentrality that puts `power` of the noncentral t above the
    # critical value, then convert back to a mean difference.
    lo, hi = 0.0, 50.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if stats.nct.sf(crit, df, mid) < power:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * sd / (n ** 0.5)


def power_at(delta: float, sd: float, n: int, alpha: float) -> float | None:
    if stats is None or n < 2 or sd <= 0:
        return None
    df = n - 1
    crit = stats.t.ppf(1 - alpha, df)
    return float(stats.nct.sf(crit, df, delta * (n ** 0.5) / sd))


def main() -> int:
    root = os.path.expanduser(os.environ.get("REPLAY_ROOT", "."))
    args = [a for a in sys.argv[1:] if a != "--common"]
    common_only = "--common" in sys.argv[1:]
    models = [tuple(a.split("=", 1)) for a in args] or DEFAULT_MODELS

    tables = {label: per_task_deltas(root, d) for d, label in models}
    tables = {k: v for k, v in tables.items() if v}
    if not tables:
        print(f"no replay aggregates under {root}", file=sys.stderr)
        return 1
    if common_only:
        shared = set.intersection(*(set(v) for v in tables.values()))
        tables = {k: {t: v for t, v in d.items() if t in shared} for k, d in tables.items()}

    if stats is None:
        print("scipy is not installed: spreads are reported, power is not", file=sys.stderr)

    print(f"replay root: {root}" + ("  (tasks common to every model)" if common_only else ""))
    header = (
        f"{'model':14s} {'n':>3s} {'mean a-S0':>10s} {'sd':>7s} {'mean b-S0':>10s} {'sd':>7s} "
        f"{'sd(a-b)':>8s} | {'MDE80':>7s} {'MDE90':>7s} {'pow@1pt':>8s} | {'MDE80':>7s} {'pow@1pt':>8s}"
    )
    print(header)
    print(f"{'':56s}   {'--- alpha=' + str(ALPHA_HOLM_STRICTEST) + ' (Holm strictest) ---':>26s}   "
          f"{'--- alpha=' + str(ALPHA_NOMINAL) + ' ---':>18s}")
    for label, table in tables.items():
        a = [v[0] for v in table.values()]
        b = [v[1] for v in table.values()]
        d = [x - y for x, y in zip(a, b)]
        n = len(a)
        sd_d = st.stdev(d) if n > 1 else float("nan")
        cells = []
        for alpha, wants in ((ALPHA_HOLM_STRICTEST, POWER_TARGETS), (ALPHA_NOMINAL, (0.80,))):
            for p in wants:
                m = mde(sd_d, n, alpha, p)
                cells.append(f"{m:7.3f}" if m is not None else f"{'n/a':>7s}")
            pw = power_at(ONE_POINT, sd_d, n, alpha)
            cells.append(f"{100 * pw:7.1f}%" if pw is not None else f"{'n/a':>8s}")
        print(
            f"{label:14s} {n:3d} {st.mean(a):+10.4f} {st.stdev(a) if n > 1 else float('nan'):7.4f} "
            f"{st.mean(b):+10.4f} {st.stdev(b) if n > 1 else float('nan'):7.4f} {sd_d:8.4f} | "
            f"{cells[0]} {cells[1]} {cells[2]} | {cells[3]} {cells[4]}"
        )

    print("\nper-task differences (a - S0 / b - S0), by task:")
    tasks = sorted(set().union(*(set(t) for t in tables.values())))
    print(f"{'task':18s} " + " ".join(f"{lab:>22s}" for lab in tables))
    for t in tasks:
        row = f"{t:18s} "
        for table in tables.values():
            if t in table:
                row += f" {table[t][0]:+10.3f} {table[t][1]:+10.3f}"
            else:
                row += f" {'-':>21s}"
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
