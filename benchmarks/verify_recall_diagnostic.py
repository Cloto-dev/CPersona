"""Independently recompute retained diagnostic scores from qrels and ranked IDs."""

import argparse
import json
import math
from pathlib import Path


def independent_ndcg(relevant, ranked):
    if not relevant:
        return None
    weights = [1 / math.log2(rank + 2) for rank in range(10)]
    gain = sum(weight for weight, doc_id in zip(weights, ranked) if doc_id in relevant)
    return 100 * gain / sum(weights[:min(10, len(relevant))])


def verify_arm(arm, lmeb):
    result = json.loads((arm / "result.json").read_text())
    manifest = json.loads((arm / "manifest.json").read_text())
    checked = 0
    for subtask, reported in result["subtasks"].items():
        path = arm / f"{subtask}-judgments.jsonl"
        if not path.exists():
            raise ValueError(f"Missing per-query scores: {path}")
        qrel_relative = next(p for p in manifest["inputs_sha256"] if p.endswith(f"/{subtask}/qrels.tsv"))
        relevant = {}
        for line in (lmeb / qrel_relative).read_text().splitlines():
            qid, did, grade = line.split("\t")[:3]
            relevant.setdefault(qid, set())
            if int(grade) > 0:
                relevant[qid].add(did)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if len(rows) != len({r["query_id"] for r in rows}) or {r["query_id"] for r in rows} != set(relevant):
            raise ValueError("Duplicate or missing judged query")
        scores = []
        for row in rows:
            score = independent_ndcg(relevant[row["query_id"]], row["filtered_ids"])
            if score is not None:
                if not math.isclose(score, row["ndcg_at_10"], abs_tol=1e-9):
                    raise ValueError(f"Per-query score mismatch: {row['query_id']}")
                scores.append(score)
        if not math.isclose(sum(scores) / len(scores), reported, abs_tol=1e-9):
            raise ValueError(f"Subtask score mismatch: {subtask}")
        checked += len(rows)
    expected = sum(result["subtasks"].values()) / len(result["subtasks"])
    if not math.isclose(expected, result["mean_ndcg_at_10"], abs_tol=1e-9):
        raise ValueError("Macro score mismatch")
    return {"arm": arm.name, "queries_checked": checked, "mean": expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lmeb", type=Path, required=True)
    parser.add_argument("arms", type=Path, nargs="+")
    args = parser.parse_args()
    for arm in args.arms:
        print(json.dumps(verify_arm(arm, args.lmeb)))


if __name__ == "__main__":
    main()
