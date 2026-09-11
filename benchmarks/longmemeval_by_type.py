"""LongMemEval by question type, across recall regimes and builds.

One table per regime. Rows are the six LongMemEval question types (the LMEB
subtasks: knowledge_update, multi_session, single_session_assistant,
single_session_preference, single_session_user, temporal_reasoning) plus the
macro mean over types. Columns are NDCG@10 / Recall@5 / Recall@10 per arm, and
the delta of every later arm against the first.

Two regimes are read, each from its own directory under an arm:

    full      the Track B regime: limit = corpus size, autocut and the fused
              gate off. Comparable to the shipped Track B numbers.
    limit10   the production regime: limit = 10, autocut and the fused gate at
              their shipped defaults (on). What a caller of the MCP `recall`
              tool actually receives.

Each regime directory is what `run_longmemeval_by_type.sh` writes: the
harness's `LongMemEval.json` and a `rankings.jsonl` produced with
`--dump_rankings`. The reader recomputes NDCG@10 from the dump's
`filtered_ids` and refuses to print a table when that disagrees with the
NDCG the harness recorded — a dump that does not reproduce the measured
number is not the dump of that measurement. It also refuses a dump whose
header says it was produced under a different limit or gate regime than the
directory claims.

Usage:

    python benchmarks/longmemeval_by_type.py v2.4.40=DIR_A dev=DIR_B \
        [--lmeb_dir ~/lmeb] [--regimes full,limit10] [--out results.md]

The first arm is the reference the deltas are taken against. Nothing here
calls the server or a model: the reader is pure arithmetic over files.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

TASK_SUBDIR = os.path.join("eval_data", "Dialogue", "LongMemEval")

# What the dump header must say for a directory to be the regime it claims.
# `full` leaves the two gate layers unconstrained on purpose: autocut refuses
# to cut a rank-fusion-ordered list, and the Track B launcher records the
# fused gate as off, but a full-ranking dump taken without the launcher is
# still a full-ranking dump. `limit10` is the production regime and both
# layers must be on, or the table would be comparing a limit against a limit
# plus a gate.
REGIME_HEADER = {
    "full": {"recall_limit": 0},
    "limit10": {
        "recall_limit": 10,
        "autocut_enabled_effective": True,
        "fused_gate_enabled_effective": True,
    },
}

NDCG_TOLERANCE = 0.005  # the harness rounds its subtask NDCG to two decimals


def load_qrels(path: Path) -> dict[str, set[str]]:
    rel: dict[str, set[str]] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if len(row) < 3:
                continue
            if int(row[2]) > 0:
                rel.setdefault(row[0], set()).add(row[1])
    return rel


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int = 10) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked[:k]) if d in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant)


def read_dump(path: Path) -> tuple[dict, dict[str, dict[str, list[str]]]]:
    """Header, and {subtask: {query_id: filtered_ids}}."""
    header: dict = {}
    per_type: dict[str, dict[str, list[str]]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("header"):
                header = rec
                continue
            if rec.get("task") != "LongMemEval":
                continue
            if "filtered_ids" not in rec:
                raise SystemExit(
                    f"{path}: a record has no `filtered_ids`; this dump predates the "
                    "field and cannot be scored by question type (re-run the harness)."
                )
            per_type.setdefault(rec["subtask"], {})[str(rec["query_id"])] = rec["filtered_ids"]
    if not header:
        raise SystemExit(f"{path}: no header record; not a --dump_rankings file.")
    return header, per_type


def check_regime(regime: str, header: dict, where: Path) -> None:
    for key, want in REGIME_HEADER[regime].items():
        if key not in header:
            raise SystemExit(
                f"{where}: the dump header does not record `{key}`; the `{regime}` regime "
                "cannot be confirmed from this file (the harness pins it since the "
                "per-type reader was added — re-run)."
            )
        if header[key] != want:
            raise SystemExit(
                f"{where}: claims regime `{regime}` but the dump header says "
                f"{key}={header[key]!r} (expected {want!r})."
            )


def score_arm(regime_dir: Path, regime: str, qrels_by_type: dict[str, dict[str, set[str]]]) -> dict:
    """Per question type: n, ndcg10, r5, r10 — cross-checked against the record."""
    record = json.loads((regime_dir / "LongMemEval.json").read_text(encoding="utf-8"))
    header, per_type = read_dump(regime_dir / "rankings.jsonl")
    check_regime(regime, header, regime_dir / "rankings.jsonl")

    out: dict[str, dict[str, float]] = {}
    for qtype, qrels in sorted(qrels_by_type.items()):
        ranked_by_q = per_type.get(qtype)
        if ranked_by_q is None:
            raise SystemExit(f"{regime_dir}: the dump has no records for `{qtype}`.")
        n = ndcg = r5 = r10 = 0.0
        for qid, relevant in qrels.items():
            ranked = ranked_by_q.get(qid, [])
            n += 1
            ndcg += ndcg_at_k(ranked, relevant, 10)
            r5 += recall_at_k(ranked, relevant, 5)
            r10 += recall_at_k(ranked, relevant, 10)
        got = ndcg / n * 100
        recorded = record["subtasks"].get(qtype)
        if recorded is None or abs(got - recorded) > NDCG_TOLERANCE:
            raise SystemExit(
                f"{regime_dir}: NDCG@10 recomputed from the dump for `{qtype}` is {got:.2f}, "
                f"but LongMemEval.json recorded {recorded!r}. The dump is not the dump of "
                "this measurement; refusing to tabulate."
            )
        out[qtype] = {"n": int(n), "ndcg10": got, "r5": r5 / n * 100, "r10": r10 / n * 100}
    macro = {
        "n": sum(v["n"] for v in out.values()),
        "ndcg10": sum(v["ndcg10"] for v in out.values()) / len(out),
        "r5": sum(v["r5"] for v in out.values()) / len(out),
        "r10": sum(v["r10"] for v in out.values()) / len(out),
    }
    out["macro mean"] = macro
    return out


def render(regime: str, arms: list[tuple[str, dict]]) -> str:
    metrics = [("ndcg10", "NDCG@10"), ("r5", "R@5"), ("r10", "R@10")]
    ref_name, ref = arms[0]
    head = ["question type", "n"]
    for name, _ in arms:
        head += [f"{name} {label}" for _, label in metrics]
    for name, _ in arms[1:]:
        head += [f"Δ{label} vs {ref_name} ({name})" for _, label in metrics]
    lines = [f"### regime `{regime}`", "", "| " + " | ".join(head) + " |",
             "| " + " | ".join(["---"] * len(head)) + " |"]
    for qtype in ref:
        row = [f"`{qtype}`" if qtype != "macro mean" else "**macro mean**", str(ref[qtype]["n"])]
        for _, scores in arms:
            row += [f"{scores[qtype][m]:.2f}" for m, _ in metrics]
        for _, scores in arms[1:]:
            row += [f"{scores[qtype][m] - ref[qtype][m]:+.2f}" for m, _ in metrics]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arms", nargs="+", metavar="NAME=DIR",
                    help="an arm: its label and the directory holding one subdirectory per regime")
    ap.add_argument("--lmeb_dir", default=os.environ.get("LMEB_DIR", os.path.expanduser("~/lmeb")))
    ap.add_argument("--regimes", default="full,limit10")
    ap.add_argument("--out", default=None, help="also write the markdown to this path")
    args = ap.parse_args()

    task_dir = Path(args.lmeb_dir) / TASK_SUBDIR
    qrels_by_type = {
        d.name: load_qrels(d / "qrels.tsv")
        for d in sorted(task_dir.iterdir()) if (d / "qrels.tsv").is_file()
    }
    if not qrels_by_type:
        raise SystemExit(f"no <type>/qrels.tsv under {task_dir}")

    arms: list[tuple[str, Path]] = []
    for spec in args.arms:
        if "=" not in spec:
            raise SystemExit(f"arm `{spec}` is not NAME=DIR")
        name, d = spec.split("=", 1)
        arms.append((name, Path(d).expanduser()))

    sections = []
    for regime in [r.strip() for r in args.regimes.split(",") if r.strip()]:
        if regime not in REGIME_HEADER:
            raise SystemExit(f"unknown regime `{regime}` (known: {sorted(REGIME_HEADER)})")
        scored = []
        for name, d in arms:
            rd = d / regime
            if not (rd / "rankings.jsonl").is_file():
                print(f"[skip] {name}: no {rd}/rankings.jsonl", file=sys.stderr)
                continue
            scored.append((name, score_arm(rd, regime, qrels_by_type)))
        if scored:
            sections.append(render(regime, scored))
    if not sections:
        raise SystemExit("nothing to tabulate: no arm had a regime directory with a dump")

    text = "\n".join(sections)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
