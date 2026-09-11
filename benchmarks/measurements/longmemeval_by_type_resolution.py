"""What a LongMemEval-by-type comparison can resolve, per question type.

Reads two arms' ranking dumps (what `run_longmemeval_by_type.sh` writes) and,
for each regime and question type, prints the per-query paired NDCG@10
difference: its mean, its standard deviation, how many queries moved at all,
and the minimum detectable mean difference at alpha 0.05 two-sided and 80%
power for a paired t over that many queries (noncentral t, the same
computation the version-comparison pre-registration used at n = 22, where it
gives 0.6264 x sd; this script reproduces that value when asked).

These are planning numbers for a pre-registration: the sd comes from the pair
of arms given, and a later pair need not have it. Run it on the two baseline
arms to get the resolution table a `prereg-longmemeval-<feature>.md` fixes
before its numbers are seen.

Usage:

    python benchmarks/measurements/longmemeval_by_type_resolution.py \
        ref=~/lmeb/lme_by_type/v2.4.41 cand=~/lmeb/lme_by_type/dev-e43ad34 \
        [--lmeb_dir ~/lmeb] [--regimes full,limit10]
    python benchmarks/measurements/longmemeval_by_type_resolution.py --factor 22
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
from pathlib import Path

TASK_SUBDIR = os.path.join("eval_data", "Dialogue", "LongMemEval")


def mde_factor(n: int, alpha: float = 0.05, power: float = 0.8) -> float:
    """Minimum detectable mean difference, in units of the paired sd, for a
    paired t with n pairs: the smallest delta/sd whose power reaches `power`."""
    from scipy import optimize, stats  # local: the reader itself has no scipy

    df = n - 1
    tcrit = stats.t.ppf(1 - alpha / 2, df)

    def power_at(delta: float) -> float:
        nc = delta * math.sqrt(n)
        return 1 - stats.nct.cdf(tcrit, df, nc) + stats.nct.cdf(-tcrit, df, nc)

    # The noncentral t cdf returns NaN for very large noncentrality, so the
    # bracket is kept where the cdf is defined; the root lies well inside it.
    return optimize.brentq(lambda d: power_at(d) - power, 1e-6, 4.5 / math.sqrt(n))


def load_qrels(path: Path) -> dict[str, set[str]]:
    rel: dict[str, set[str]] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if len(row) >= 3 and int(row[2]) > 0:
                rel.setdefault(row[0], set()).add(row[1])
    return rel


def ndcg_at_10(ranked: list[str], relevant: set[str]) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked[:10]) if d in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), 10)))
    return dcg / ideal if ideal else 0.0


def read_dump(path: Path) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("header") or rec.get("task") != "LongMemEval":
                continue
            out.setdefault(rec["subtask"], {})[str(rec["query_id"])] = rec["filtered_ids"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arms", nargs="*", metavar="NAME=DIR", help="exactly two arms: reference first")
    ap.add_argument("--lmeb_dir", default=os.environ.get("LMEB_DIR", os.path.expanduser("~/lmeb")))
    ap.add_argument("--regimes", default="full,limit10")
    ap.add_argument("--factor", type=int, default=None, metavar="N",
                    help="print the MDE/sd factor for N pairs and exit (22 gives 0.6264)")
    args = ap.parse_args()

    if args.factor:
        f = mde_factor(args.factor)
        print(f"n={args.factor}: MDE = {f:.4f} x sd (noncentrality {f * math.sqrt(args.factor):.6f})")
        return 0
    if len(args.arms) != 2:
        raise SystemExit("give exactly two arms, the reference first")
    (ref_name, ref_dir), (cand_name, cand_dir) = [(s.split("=", 1)[0], Path(s.split("=", 1)[1]).expanduser()) for s in args.arms]

    task_dir = Path(args.lmeb_dir) / TASK_SUBDIR
    qrels = {d.name: load_qrels(d / "qrels.tsv") for d in sorted(task_dir.iterdir()) if (d / "qrels.tsv").is_file()}

    for regime in [r.strip() for r in args.regimes.split(",") if r.strip()]:
        a = read_dump(ref_dir / regime / "rankings.jsonl")
        b = read_dump(cand_dir / regime / "rankings.jsonl")
        print(f"### regime `{regime}` — per-query NDCG@10, {cand_name} minus {ref_name}\n")
        print("| question type | n | mean Δ | sd | queries moved | MDE (points) |")
        print("| --- | --- | --- | --- | --- | --- |")
        for qtype, rel_by_q in qrels.items():
            deltas = [
                (ndcg_at_10(b[qtype].get(q, []), rel) - ndcg_at_10(a[qtype].get(q, []), rel)) * 100
                for q, rel in rel_by_q.items()
            ]
            n = len(deltas)
            sd = statistics.stdev(deltas)
            moved = sum(1 for x in deltas if abs(x) > 1e-9)
            print(f"| `{qtype}` | {n} | {statistics.mean(deltas):+.2f} | {sd:.2f} | {moved} | {mde_factor(n) * sd:.2f} |")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
