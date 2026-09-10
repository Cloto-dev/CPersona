#!/usr/bin/env python3
"""What a similarity floor on the reserved rows would cost, and what zero means.

The reservation appends the dense arm's pre-floor top rows when the qualified
rows fall short, and it has no floor of its own, so on a small corpus it can
return a row whose similarity is negative. Whether zero is a defensible line is
a question about the score distribution each shipping encoder produces, and
that is measurable.

Three readings, all from vectors that are already on disk (the Track A/B
embedding cache), so no encoder runs and no benchmark is replayed:

  M0  the vectors are unit-norm, so calling the dot product a cosine is true
      and the line at zero means orthogonal
  M1  the distribution of that similarity over query x row pairs that are not
      answers -- P(s <= 0) and where zero sits as a percentile
  M2  the same similarity over the gold pairs -- how many sit at or below zero,
      which is exactly what a floor at zero would delete

The decision rule these feed is fixed in prereg-reservation-floor.md and is not
repeated here.

Sampling follows that pre-registration, with one bound it did not state: the
non-answer draw is 500 rows per query, taken from a pool of at most POOL rows
per subtask rather than from the corpus each time, so the number of vectors
read stays bounded on the large corpora. Where the eligible universe is no
larger than the pool the pool IS the universe and the draw is exact.

Usage:
    LMEB_DIR=~/lmeb python benchmarks/measurements/reservation_floor.py \\
        --cache ~/lmeb/embcache_jinanano/embcache.sqlite3 \\
        --label jinaai/jina-embeddings-v5-text-nano \\
        --out ~/lmeb/reservation_floor_jinanano.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

import numpy as np

LMEB_DIR = Path(os.environ.get("LMEB_DIR", Path.home() / "lmeb")).expanduser()
EVAL_DATA = LMEB_DIR / "eval_data"

# Kept in step with benchmarks/benchmark_trackb_lmeb.py TASK_MAP.
TASK_MAP = {
    "EPBench": "Episodic/EPBench",
    "KnowMeBench": "Episodic/KnowMeBench",
    "LoCoMo": "Dialogue/LoCoMo",
    "LongMemEval": "Dialogue/LongMemEval",
    "REALTALK": "Dialogue/REALTALK",
    "TMD": "Dialogue/TMD",
    "MemBench": "Dialogue/MemBench",
    "ConvoMem": "Dialogue/ConvoMem",
    "QASPER": "Semantic/QASPER",
    "NovelQA": "Semantic/NovelQA",
    "PeerQA": "Semantic/PeerQA",
    "CovidQA": "Semantic/Covid-QA",
    "ESGReports": "Semantic/ESG-Reports",
    "MLDR": "Semantic/MLDR",
    "LooGLE": "Semantic/LooGLE",
    "LMEB_SciFact": "Semantic/SciFact",
    "Gorilla": "Procedural/Gorilla",
    "ToolBench": "Procedural/ToolBench",
    "ReMe": "Procedural/ReMe",
    "Proced_mem_bench": "Procedural/Proced_mem_bench",
    "MemGovern": "Procedural/MemGovern",
    "DeepPlanning": "Procedural/DeepPlanning",
}

# Histogram over the whole possible range of a cosine, fine enough that the
# percentile of zero is read off it rather than interpolated.
HIST_EDGES = np.round(np.arange(-1.0, 1.0 + 1e-9, 0.005), 6)


class CacheVectors:
    """sha256(label + '\\0' + text) -> float32 vector, from the Track A/B cache.

    The key derivation is budget_batching._EmbeddingDiskCache's, as
    scan_window_ab.CacheVectors already copies it -- with the part that copy
    did not need: a model that declares prompts is cached under
    ``f"{prompt}\\x1f{text}"``, because the same text under two prompts is two
    vectors. Which form a cache actually holds is resolved by probing it
    (``resolve_tag``) rather than assumed, and the resolution is reported.

    A miss is counted and reported rather than fatal: a missing gold vector
    biases M2 and has to be visible in the result, and dying on the first one
    would hide how many there are.
    """

    def __init__(self, path: Path, label: str):
        if not path.exists():
            raise SystemExit(f"embedding cache not found: {path}")
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._label = label
        self.doc_tag = ""
        self.query_tag = ""

    def _key(self, text: str) -> bytes:
        return hashlib.sha256((self._label + "\x00" + text).encode("utf-8")).digest()

    def _has(self, text: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM emb WHERE k=?", (self._key(text),)
        ).fetchone() is not None

    def resolve_tag(self, sample: str, tag: str) -> tuple[str, dict]:
        """Which key form this cache holds for `sample`: tagged or bare.

        The tagged form wins when both are present -- it is the one a prompted
        model writes -- and the agreement between the two is measured and
        returned, so a reader can see whether the choice could have mattered.
        """
        tagged, bare = f"{tag}\x1f{sample}", sample
        has_tagged, has_bare = self._has(tagged), self._has(bare)
        if not has_tagged and not has_bare:
            raise SystemExit(
                f"neither key form is cached for label {self._label!r}: the corpus "
                "this instrument reads was encoded under some other identity"
            )
        info = {"tagged": has_tagged, "bare": has_bare}
        if has_tagged and has_bare:
            a = self.get_many([tagged])[0][0]
            b = self.get_many([bare])[0][0]
            info["max_abs_delta"] = float(np.abs(a - b).max())
        return (tag if has_tagged else ""), info

    def doc_texts(self, texts: list[str]) -> list[str]:
        return [f"{self.doc_tag}\x1f{t}" for t in texts] if self.doc_tag else texts

    def query_texts(self, texts: list[str]) -> list[str]:
        return [f"{self.query_tag}\x1f{t}" for t in texts] if self.query_tag else texts

    def get_many(self, texts: list[str]) -> tuple[dict[int, np.ndarray], int]:
        """index -> vector for the texts that are cached, plus the miss count."""
        keys = [self._key(t) for t in texts]
        found: dict[bytes, np.ndarray] = {}
        CHUNK = 500
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            ph = ",".join("?" * len(chunk))
            for k, dim, v in self._db.execute(
                f"SELECT k, dim, v FROM emb WHERE k IN ({ph})", chunk
            ):
                found[bytes(k)] = np.frombuffer(v, dtype=np.float32, count=dim)
        out: dict[int, np.ndarray] = {}
        misses = 0
        for i, k in enumerate(keys):
            v = found.get(k)
            if v is None:
                misses += 1
            else:
                out[i] = v
        return out, misses


def load_jsonl(path: Path) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            qrels.setdefault(parts[0], {})[parts[1]] = int(parts[2])
    return qrels


def doc_text(doc: dict) -> str:
    """The text the store harness indexes, so the cache keys match."""
    return (doc.get("title", "") + " " + doc.get("text", "")).strip()


def scene_of(ident: str) -> str | None:
    parts = ident.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 else None


def discover_subtasks(task_dir: Path) -> list[dict]:
    """Every directory holding queries.jsonl + qrels.tsv, with the nearest
    corpus.jsonl and candidates.jsonl above it. Mirrors
    benchmark_trackb_lmeb.discover_task_structure."""
    subtasks = []

    def _nearest(directory: Path, name: str) -> Path | None:
        current = directory
        while current >= task_dir:
            p = current / name
            if p.exists():
                return p
            current = current.parent
        return None

    for queries_file in sorted(task_dir.rglob("queries.jsonl")):
        sub = queries_file.parent
        qrels_file = sub / "qrels.tsv"
        if not qrels_file.exists():
            continue
        corpus = _nearest(sub, "corpus.jsonl")
        if corpus is None:
            continue
        subtasks.append({
            "name": str(sub.relative_to(task_dir)).replace("\\", "/"),
            "corpus": corpus,
            "queries": queries_file,
            "qrels": qrels_file,
            "candidates": _nearest(sub, "candidates.jsonl"),
        })
    return subtasks


class Accum:
    """Counts and a histogram, so no per-pair array is kept."""

    def __init__(self):
        self.n = 0
        self.n_le0 = 0
        self.total = 0.0
        self.total_sq = 0.0
        self.hist = np.zeros(len(HIST_EDGES) - 1, dtype=np.int64)
        self.minimum = float("inf")
        # |s| < 1e-3 is where two encodings of the same text (measured to agree
        # to 7e-4) could disagree on the sign, so the exposure is counted.
        self.n_near_zero = 0

    def add(self, sims: np.ndarray) -> None:
        if sims.size == 0:
            return
        self.n += int(sims.size)
        self.n_le0 += int((sims <= 0).sum())
        self.n_near_zero += int((np.abs(sims) < 1e-3).sum())
        self.total += float(sims.sum(dtype=np.float64))
        self.total_sq += float((sims.astype(np.float64) ** 2).sum())
        self.hist += np.histogram(np.clip(sims, -1.0, 1.0), bins=HIST_EDGES)[0]
        self.minimum = min(self.minimum, float(sims.min()))

    def summary(self) -> dict:
        if self.n == 0:
            return {"n": 0}
        mean = self.total / self.n
        var = max(self.total_sq / self.n - mean * mean, 0.0)
        cum = np.cumsum(self.hist)
        zero_bin = int(np.searchsorted(HIST_EDGES, 0.0, side="right") - 1)
        pct_of_zero = float(cum[min(zero_bin, len(cum) - 1)]) / self.n

        def q(p: float) -> float:
            idx = int(np.searchsorted(cum, p * self.n, side="left"))
            return float(HIST_EDGES[min(idx + 1, len(HIST_EDGES) - 1)])

        return {
            "n": self.n,
            "n_le_zero": self.n_le0,
            "p_le_zero": self.n_le0 / self.n,
            "n_within_1e3_of_zero": self.n_near_zero,
            "mean": mean,
            "sd": var ** 0.5,
            "min": self.minimum,
            "percentile_of_zero": pct_of_zero,
            "q001": q(0.001), "q01": q(0.01), "q05": q(0.05),
            "q50": q(0.50), "q95": q(0.95),
        }


def run_task(task: str, cache: CacheVectors, args, rng: random.Random) -> dict:
    task_dir = EVAL_DATA / TASK_MAP[task]
    if not task_dir.exists():
        return {"task": task, "skipped": "task directory not found"}

    null_acc, gold_acc = Accum(), Accum()
    per_query_le0: list[float] = []
    pooled = unscoped_queries = chunks = 0
    norms: list[float] = []
    gold_misses = doc_misses = query_misses = 0
    n_queries = n_gold_pairs = 0
    lowest_gold: list[tuple[float, str, str]] = []
    universe_sizes: list[int] = []

    for st in discover_subtasks(task_dir):
        corpus = load_jsonl(st["corpus"])
        if not corpus:
            continue
        ids = [str(d.get("_id", d.get("id", i))) for i, d in enumerate(corpus)]
        texts = [doc_text(d) for d in corpus]
        pos = {doc_id: i for i, doc_id in enumerate(ids)}

        queries = load_jsonl(st["queries"])[: args.max_queries]
        if not queries:
            continue
        qrels = load_qrels(st["qrels"])

        # Field names are benchmark_trackb_lmeb.load_candidates's, and a file
        # that parses to nothing is a parse failure rather than a task without
        # candidates: read the wrong keys and every query silently widens to
        # the whole corpus, which is a different population than the one the
        # retrieval path ranks.
        allowed: dict[str, set[str]] = {}
        if st["candidates"] is not None:
            for row in load_jsonl(st["candidates"]):
                allowed[str(row["scene_id"])] = {str(c) for c in row["candidate_doc_ids"]}
            if not allowed:
                raise SystemExit(f"candidates file parsed to nothing: {st['candidates']}")

        # Where a query's non-answers are drawn from. With no candidate list
        # every query shares one universe, so the pool is drawn ONCE for the
        # subtask and every query draws its full sample from it: bounding the
        # vectors read must not quietly shrink the per-query sample, which is
        # what a pool intersected after the fact does.
        global_pool: list[int] | None = None
        if not allowed:
            base = list(range(len(corpus)))
            global_pool = base if len(base) <= args.pool else rng.sample(base, args.pool)
            pooled += 1 if len(base) > args.pool else 0

        needed: set[int] = set()
        per_query: list[tuple[dict, list[int], list[int]]] = []
        for q in queries:
            qid = str(q.get("_id", q.get("id", "")))
            gold_ids = [d for d, s in qrels.get(qid, {}).items() if s > 0]
            gold_idx = [pos[d] for d in gold_ids if d in pos]

            if global_pool is not None:
                source, universe_n = global_pool, len(corpus)
            else:
                # A scene the map does not carry is unrestricted, exactly as the
                # replay reads it (`if sc in cands`), and is counted as such.
                scene = scene_of(qid)
                if scene in allowed:
                    source = [pos[d] for d in allowed[scene] if d in pos]
                else:
                    unscoped_queries += 1
                    source = list(range(len(corpus)))
                universe_n = len(source)
            universe_sizes.append(universe_n)

            gold_set = set(gold_idx)
            non_gold = [i for i in source if i not in gold_set]
            if len(non_gold) > args.null_docs:
                non_gold = rng.sample(non_gold, args.null_docs)

            per_query.append((q, gold_idx, non_gold))

        # Read in chunks whose vector set fits the bound, rather than dropping
        # the queries that do not fit: taking the first N of a file is a
        # selection, and the tail of a query file is not a random sample of it.
        cursor = 0
        while cursor < len(per_query):
            chunk: list[tuple[dict, list[int], list[int]]] = []
            needed: set[int] = set()
            while cursor < len(per_query):
                q, gold_idx, non_gold = per_query[cursor]
                prospective = needed | set(non_gold) | set(gold_idx)
                if chunk and len(prospective) > args.pool_total:
                    break
                needed = prospective
                chunk.append(per_query[cursor])
                cursor += 1
            chunks += 1

            needed_list = sorted(needed)
            vecs, misses = cache.get_many(cache.doc_texts([texts[i] for i in needed_list]))
            doc_misses += misses
            by_row = {needed_list[k]: v for k, v in vecs.items()}
            if not by_row:
                continue
            dim = len(next(iter(by_row.values())))
            if len(norms) < args.norm_sample:
                norms.extend(
                    float(np.linalg.norm(v))
                    for v in list(by_row.values())[: args.norm_sample - len(norms)]
                )

            qvecs, qmiss = cache.get_many(cache.query_texts([q["text"] for q, _, _ in chunk]))
            query_misses += qmiss

            for k, (q, gold_idx, non_gold) in enumerate(chunk):
                if k not in qvecs:
                    continue
                qvec = qvecs[k]
                n_queries += 1

                rows = [i for i in non_gold if i in by_row]
                if rows:
                    mat = np.stack([by_row[i] for i in rows])
                    sims = mat @ qvec
                    null_acc.add(sims)
                    per_query_le0.append(float((sims <= 0).mean()))

                g_rows = [i for i in gold_idx if i in by_row]
                gold_misses += len(gold_idx) - len(g_rows)
                n_gold_pairs += len(gold_idx)
                if g_rows:
                    gmat = np.stack([by_row[i] for i in g_rows])
                    sims = gmat @ qvec
                    gold_acc.add(sims)
                    for i, sv in zip(g_rows, sims):
                        lowest_gold.append((float(sv), str(q.get("_id", q.get("id", ""))), ids[i]))
                        lowest_gold.sort()
                        del lowest_gold[10:]
            del by_row, qvecs

    return {
        "task": task,
        "queries": n_queries,
        "vector_read_chunks": chunks,
        "subtasks_sampled_to_pool": pooled,
        "queries_without_candidate_scope": unscoped_queries,
        "gold_pairs": n_gold_pairs,
        "gold_pairs_without_vector": gold_misses,
        "doc_cache_misses": doc_misses,
        "query_cache_misses": query_misses,
        "eligible_universe": {
            "median": float(np.median(universe_sizes)) if universe_sizes else 0.0,
            "min": min(universe_sizes) if universe_sizes else 0,
            "max": max(universe_sizes) if universe_sizes else 0,
        },
        "vector_norm": {
            "n": len(norms),
            "min": min(norms) if norms else None,
            "max": max(norms) if norms else None,
        },
        "dim": dim if norms else None,
        "non_answer": null_acc.summary(),
        "non_answer_p_le_zero_mean_over_queries": (
            float(np.mean(per_query_le0)) if per_query_le0 else None
        ),
        "gold": gold_acc.summary(),
        "lowest_gold": [
            {"similarity": s, "qid": qid, "doc": doc} for s, qid, doc in lowest_gold
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--tasks", default=",".join(TASK_MAP))
    ap.add_argument("--max_queries", type=int, default=200)
    ap.add_argument("--null_docs", type=int, default=500)
    ap.add_argument("--pool", type=int, default=5000,
                    help="rows drawn once per subtask to serve as the non-answer universe")
    ap.add_argument("--pool_total", type=int, default=40000,
                    help="cap on distinct corpus vectors read per subtask")
    ap.add_argument("--norm_sample", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1344)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cache = CacheVectors(Path(os.path.expanduser(args.cache)), args.label)
    rng = random.Random(args.seed)
    task_names = [t.strip() for t in args.tasks.split(",") if t.strip()]

    # Which key form this cache holds is measured, not assumed: a prompted
    # model writes `prompt\x1f text` and a promptless one writes the text, and
    # reading the wrong one returns nothing at all (or, where both forms are
    # present, the other run's vectors).
    probe_subtasks = discover_subtasks(EVAL_DATA / TASK_MAP[task_names[0]])
    if not probe_subtasks:
        raise SystemExit(f"no subtask found under {task_names[0]} to resolve the key form")
    st0 = probe_subtasks[0]
    doc_sample = doc_text(load_jsonl(st0["corpus"])[0])
    query_sample = load_jsonl(st0["queries"])[0]["text"]
    cache.doc_tag, doc_info = cache.resolve_tag(doc_sample, "document")
    cache.query_tag, query_info = cache.resolve_tag(query_sample, "query")
    key_form = {
        "doc_tag": cache.doc_tag or None, "doc_probe": doc_info,
        "query_tag": cache.query_tag or None, "query_probe": query_info,
        "probed_on": f"{task_names[0]}/{st0['name']}",
    }
    print(f"key form: doc={cache.doc_tag or '(bare)'} query={cache.query_tag or '(bare)'} "
          f"{json.dumps({k: v for k, v in key_form.items() if k.endswith('probe')})}\n")

    results = []
    for task in task_names:
        if task not in TASK_MAP:
            print(f"unknown task {task!r}", file=sys.stderr)
            return 2
        res = run_task(task, cache, args, rng)
        results.append(res)
        na, go = res.get("non_answer", {}), res.get("gold", {})
        print(
            f"{task:<18} q={res.get('queries', 0):>5} "
            f"null n={na.get('n', 0):>8} P(s<=0)={na.get('p_le_zero', float('nan')):.5f} "
            f"| gold n={go.get('n', 0):>6} <=0: {go.get('n_le_zero', 0):>4} "
            f"min={go.get('min', float('nan')):.4f}",
            flush=True,
        )

    out = {
        "model": args.label,
        "cache": str(args.cache),
        "seed": args.seed,
        "max_queries_per_subtask": args.max_queries,
        "null_docs_per_query": args.null_docs,
        "pool_per_subtask": args.pool,
        "vector_read_cap_per_subtask": args.pool_total,
        "key_form": key_form,
        "tasks": results,
    }
    with open(os.path.expanduser(args.out), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
