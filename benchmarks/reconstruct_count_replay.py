"""Replay reconstruction count over frozen top-ranked session documents.

This observes evidence retention and serialized characters, not answer quality.
Run from a checkout with PYTHONPATH=. and pass explicit artifact/data paths.
"""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from cpersona import reconstruct as R


def run(rankings, corpus, qrels_root):
    documents = {}
    with corpus.open() as stream:
        for line in stream:
            row = json.loads(line)
            documents[row["id"]] = row["text"]
    gold = {}
    for path in sorted(qrels_root.glob("*/qrels.tsv")):
        with path.open() as stream:
            for query, doc, score in csv.reader(stream, delimiter="\t"):
                if float(score) > 0:
                    gold.setdefault((path.parent.name, query), set()).add(doc)
    results = []
    with rankings.open() as stream:
        for line in stream:
            row = json.loads(line)
            ids = row["returned_ids"][:20]
            candidates = [R._Candidate({"ref": f"mem:{i+1}", "id": doc,
                "content": documents[doc]}, i) for i, doc in enumerate(ids)]
            uf = R.bundle(candidates, {})
            groups = defaultdict(list)
            for i in range(len(candidates)):
                groups[uf.find(i)].append(i)
            items = []
            for group in groups.values():
                members = [candidates[i] for i in group]
                item, _ = R.structure(members, {}, {}, 40)
                items.append((min(c.rank for c in members), item))
            items.sort(key=lambda x: x[0])
            refs = {c.ref: ids[i] for i, c in enumerate(candidates)}
            relevant = gold[(row["subtask"], row["query_id"])]
            for count in (1, 2, 4, 8, 10):
                selected = [item for _, item in items[:count]]
                retained = {refs[e["ref"]] for item in selected for e in item["claims"]}
                expected = set(ids[:count])
                if retained != expected:
                    raise AssertionError("singleton evidence disagrees with frozen ranks")
                if any(item["content"] != documents[refs[item["claims"][0]["ref"]]] for item in selected):
                    raise AssertionError("head is not a source quotation")
                results.append({"type": row["subtask"], "query": row["query_id"], "count": count,
                    "candidate_count": len(candidates), "cluster_count": len(items),
                    "returned": len(selected), "evidence_recall": len(retained & relevant) / len(relevant),
                    "payload_chars": len(json.dumps(selected, ensure_ascii=False)),
                    "available_at_or_below_count": len(items) <= count})
    summary = []
    for count in (1, 2, 4, 8, 10):
        for kind in sorted({r["type"] for r in results}):
            rows = [r for r in results if r["count"] == count and r["type"] == kind]
            summary.append({"count": count, "type": kind, "queries": len(rows),
                **{key: sum(r[key] for r in rows)/len(rows) for key in
                   ("evidence_recall", "payload_chars", "candidate_count", "cluster_count", "returned", "available_at_or_below_count")}})
    return {"protocol": "frozen full-ranking top20; session singleton fallback; no answer reader",
            "rankings_sha256": hashlib.sha256(rankings.read_bytes()).hexdigest(),
            "corpus_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
            "summary": summary, "rows": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rankings", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--qrels-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(run(args.rankings, args.corpus, args.qrels_root), indent=2))
