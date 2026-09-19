"""Run one retained, offline, fixed-calibration Track B diagnostic arm."""

import argparse
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--lmeb", type=Path, required=True)
    parser.add_argument("--task", choices=["REALTALK", "LongMemEval"], default="REALTALK")
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    env = {
        "CPERSONA_REPO": str(args.repo.resolve()),
        "LMEB_DIR": str(args.lmeb.resolve()),
        "CPERSONA_DB_PATH": str(args.output.resolve() / "benchmark.db"),
        "CPERSONA_EMBEDDING_MODE": "http",
        "CPERSONA_EMBEDDING_URL": "http://localhost:0",
        "CPERSONA_VECTOR_SEARCH_MODE": "local",
        "CPERSONA_STORE_BLOB": "true",
        "CPERSONA_FTS_ENABLED": "true",
        "CPERSONA_TASK_QUEUE_ENABLED": "false",
        "CPERSONA_MAX_MEMORIES": "300000",
        "CPERSONA_RECALL_LIBRARY_MAX_LIMIT": "300000",
        "CPERSONA_VECTOR_MIN_SIMILARITY": str(args.threshold),
        "CPERSONA_RECALL_MODE": "rrf",
        "CPERSONA_CONFIDENCE_ENABLED": "false",
        "CPERSONA_AUTOCUT_ENABLED": "false",
        "CPERSONA_FUSED_GATE_ENABLED": "false",
        "OPENBLAS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
    os.environ.update(env)
    random.seed(1400)
    asyncio.run(run(args, env))


async def run(args, env):
    import numpy as np
    import benchmark_trackb_lmeb as bench
    from budget_batching import _EmbeddingDiskCache
    from mps_accel import install_fast_accel
    from cpersona import config, memory_handlers as mh, server, vector
    from cpersona.database import close_db, get_db

    cache = object.__new__(_EmbeddingDiskCache)
    cache._model = "BAAI/bge-m3"
    assert not Path(str(args.cache) + "-wal").exists(), "Checkpoint cache before using immutable mode"
    cache_stat = args.cache.stat()
    cache._db = sqlite3.connect(args.cache.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)

    class CachedEncoder:
        hits = 0

        def encode(self, texts, normalize_embeddings=True, **kwargs):
            assert normalize_embeddings and not kwargs.get("prompt_name")
            vectors = cache.get_many(texts)
            missing = sum(v is None for v in vectors)
            if missing:
                raise RuntimeError(f"Embedding cache misses: {missing}/{len(texts)}; no inference allowed")
            matrix = np.vstack(vectors)
            assert matrix.shape == (len(texts), 1024) and np.isfinite(matrix).all()
            self.hits += len(texts)
            return matrix

    encoder = CachedEncoder()
    client = bench.LookupEmbeddingClient()
    vector._embedding_client = client
    server._embedding_client = client
    unclamped = "_clamp_limit(limit, 100)" in inspect.getsource(mh)
    if unclamped:
        mh._clamp_limit = lambda limit, cap: max(0, limit)
    assert not mh.CONFIDENCE_ENABLED
    assert config.VECTOR_MIN_SIMILARITY == args.threshold
    subtasks = bench.discover_task_structure(str(args.lmeb / "eval_data" / bench.TASK_MAP[args.task]))
    assert len(subtasks) == (3 if args.task == "REALTALK" else 6)
    corpus_paths = {st["corpus"] for st in subtasks}
    assert len(corpus_paths) == 1
    input_paths = {str(p) for st in subtasks for k, p in st.items() if k != "name" and p}
    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    manifest = {
        "commit": subprocess.check_output(["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": {p.name: sha(p) for p in (args.repo / "cpersona").glob("*.py")},
        "driver_sha256": sha(__file__),
        "harness_sha256": sha(bench.__file__),
        "inputs_sha256": {str(Path(p).relative_to(args.lmeb)): sha(p) for p in sorted(input_paths)},
        "environment": env, "task": args.task, "historical_unclamp": unclamped,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    started = time.monotonic()
    await get_db()
    accel = None if args.native else install_fast_accel(server, vector, mh, selfcheck_rate=0.02)
    probe = bench.VectorAdmissionProbe(mh)
    try:
        corpus = bench.load_jsonl(next(iter(corpus_paths)))
        count = await bench.store_corpus(server, client, encoder, corpus)
        if accel:
            await accel.preload(bench.AGENT_ID)
        scores = {}
        admissions = {}
        coverage = {}
        original_ndcg = bench.compute_ndcg
        with (args.output / "rankings.jsonl").open("x") as rankings:
            def sink(record):
                rankings.write(json.dumps(record) + "\n")

            for st in subtasks:
                probe.reset()
                def capture_scores(qrels, results, k=10):
                    with (args.output / (st["name"] + "-judgments.jsonl")).open("x") as judgments:
                        assert set(qrels) <= set(results), "Missing judged queries"
                        for qid, rels in qrels.items():
                            judgments.write(json.dumps({
                                "query_id": qid, "filtered_ids": results[qid][:20],
                                "ndcg_at_10": original_ndcg({qid: rels}, {qid: results[qid]}, k),
                                "judged": any(v > 0 for v in rels.values()),
                            }) + "\n")
                    return original_ndcg(qrels, results, k)

                bench.compute_ndcg = capture_scores
                scores[st["name"]] = await bench.run_subtask(
                    server, client, encoder, st, corpus_size=count,
                    dump_rankings_sink=sink, task_name=args.task, admission_probe=probe,
                )
                admissions[st["name"]] = probe.summary(count)
                coverage[st["name"]] = len(bench.load_jsonl(st["queries"]))
                rankings.flush()
                print(json.dumps({"subtask": st["name"], "ndcg": scores[st["name"]]}), flush=True)
        result = {
            "task": args.task, "corpus_size": count, "coverage": coverage,
            "subtasks": scores, "mean_ndcg_at_10": sum(scores.values()) / len(scores),
            "threshold": vector._get_vector_threshold(bench.AGENT_ID),
            "admissions": admissions, "cache_hits": encoder.hits,
            "accel": accel.stats if accel else None, "time_s": time.monotonic() - started,
        }
        if accel:
            assert accel.stats["checked"] > 0 and accel.stats["mismatches"] == 0, accel.stats
        assert result["threshold"] == args.threshold
        assert args.cache.stat() == cache_stat, "Cache changed during the measurement"
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    finally:
        await close_db()
        cache._db.close()


if __name__ == "__main__":
    main()
