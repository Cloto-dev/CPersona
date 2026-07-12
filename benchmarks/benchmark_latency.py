"""Latency benchmark: production-stack recall/store p50 for cpersona.

Measures end-to-end ``do_recall()`` / ``do_store()`` wall clock against a
REAL HTTP embedding backend (CEmbedding ``/embed``) — unlike
benchmark_trackb_lmeb.py, whose LookupEmbeddingClient deliberately removes
encode cost to isolate ranking quality. Two vector-search modes:

  local   query encoded via POST /embed, vectors scanned in-process from
          SQLite blobs (production default)
  remote  query TEXT sent to POST /search; CEmbedding encodes it and runs
          the namespace-resident matrix top-K (CEmbedding >= 0.6.0)

One invocation measures one (corpus_size, mode) point against a persistent
DB, so local and remote runs share identical data:

  fill (skipped when the DB already holds >= corpus_size rows)
  -> calibrate -> timed recall pass -> timed store pass

The fill phase pre-computes document vectors through the same /embed
endpoint (batched, cached to --cache_dir as .npz) and bulk-inserts them the
same way store_corpus() does — fill is plumbing, not measurement. The
measured passes use the real cpersona EmbeddingClient, exactly as
server.main() constructs it.

Remote-mode prerequisite: the CEmbedding index must hold this DB's vectors.
Backfill with the CEmbedding repo's scripts/backfill_embedding_index.py
(direct vector transfer, no re-encode) and restart the CEmbedding server so
the namespace matrix is resident.

Latency regime notes:
  - CPERSONA_MAX_MEMORIES is set to the corpus size so the local scan window
    covers the whole corpus (the shipped default of 10000 would silently
    scan a subset at larger sizes — faster, but not the honest number).
  - Truncation layers (autocut / fused gate) are left at production
    defaults: this is a latency benchmark, production shape is the point.
  - Queries are real LMEB queries (each recall pays one real query encode);
    every query in the timed pass is unique, so the embedding client's LRU
    cache cannot serve repeats.

Usage:
  LMEB_DIR=~/lmeb python benchmarks/benchmark_latency.py \
      --db /tmp/latency.db --corpus_size 10000 --mode local \
      --embed_url http://127.0.0.1:8401/embed --out latency_10k_local.json
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import struct
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

_CPERSONA_REPO = os.environ.get(
    "CPERSONA_REPO", str(Path(__file__).resolve().parents[1])
)

logging.basicConfig(
    format="%(levelname)s|%(asctime)s|%(name)s: %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("latency")

AGENT_ID = "latency-bench"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True, help="persistent SQLite path (shared across modes)")
    p.add_argument("--corpus_size", type=int, required=True)
    p.add_argument("--mode", choices=["local", "remote"], required=True)
    p.add_argument("--embed_url", default="http://127.0.0.1:8401/embed")
    p.add_argument("--num_queries", type=int, default=200)
    p.add_argument("--store_sample", type=int, default=100)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--recall_mode", default="rrf", choices=["rrf", "cascade", "rsf"])
    p.add_argument("--cache_dir", default=os.path.expanduser("~/lmeb/latency_cache"))
    p.add_argument("--encode_batch", type=int, default=64)
    p.add_argument("--max_doc_chars", type=int, default=2000,
                   help="truncate corpus docs to this many chars. cpersona "
                        "memories are chat messages / summaries, not paper "
                        "chapters; untruncated LMEB long-document tasks also "
                        "make the one-off corpus encode CPU-prohibitive")
    p.add_argument("--out", default=None, help="result JSON path")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


args = parse_args()

# Env must be configured BEFORE the cpersona import (config.py reads it then).
os.environ["CPERSONA_DB_PATH"] = args.db
os.environ["CPERSONA_EMBEDDING_MODE"] = "http"
os.environ["CPERSONA_EMBEDDING_URL"] = args.embed_url
os.environ["CPERSONA_VECTOR_SEARCH_MODE"] = args.mode
os.environ["CPERSONA_STORE_BLOB"] = "true"
os.environ["CPERSONA_FTS_ENABLED"] = "true"
os.environ["CPERSONA_TASK_QUEUE_ENABLED"] = "false"
# + the store-pass rows both modes will add: the local scan window must keep
# covering the whole table after those inserts.
os.environ["CPERSONA_MAX_MEMORIES"] = str(args.corpus_size + 2 * args.store_sample)
os.environ["CPERSONA_RECALL_MODE"] = args.recall_mode

sys.path.insert(0, _CPERSONA_REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from benchmark_trackb_lmeb import (  # noqa: E402
    ALL_TASKS, EVAL_DATA, TASK_MAP, discover_task_structure, load_jsonl,
)


# ---------------------------------------------------------------------------
# Corpus / query pool (deterministic across invocations)
# ---------------------------------------------------------------------------

def build_pool(n_docs: int, n_queries: int) -> tuple[list[str], list[str]]:
    """Collect documents and real queries from LMEB, deterministically.

    Tasks are walked in ALL_TASKS order, subtasks in discover order, so the
    same (seed, sizes) always yields the same pool — that is what lets the
    .npz vector cache and the shared DB be reused across mode invocations.
    """
    docs: list[str] = []
    queries: list[str] = []
    seen_docs: set[str] = set()
    seen_corpus: set[str] = set()
    for task in ALL_TASKS:
        task_dir = os.path.join(EVAL_DATA, TASK_MAP[task])
        if not os.path.isdir(task_dir):
            continue
        for sub in discover_task_structure(task_dir):
            if sub["corpus"] not in seen_corpus:
                seen_corpus.add(sub["corpus"])
                if len(docs) < n_docs:
                    for doc in load_jsonl(sub["corpus"]):
                        text = (doc.get("title", "") + " " + doc.get("text", "")).strip()
                        text = text[:args.max_doc_chars]
                        if text and text not in seen_docs:
                            seen_docs.add(text)
                            docs.append(text)
                        if len(docs) >= n_docs:
                            break
            for q in load_jsonl(sub["queries"]):
                text = (q.get("text") or "").strip()
                if text:
                    queries.append(text)
        if len(docs) >= n_docs and len(queries) >= n_queries * 20:
            break
    if len(docs) < n_docs:
        raise SystemExit(f"pool exhausted: only {len(docs)} docs available, need {n_docs}")
    rng = np.random.default_rng(args.seed)
    q_idx = rng.choice(len(queries), size=min(n_queries, len(queries)), replace=False)
    unique_qs = list(dict.fromkeys(queries[i] for i in q_idx))
    return docs, unique_qs


# ---------------------------------------------------------------------------
# HTTP encode (fill phase only — measured passes go through cpersona)
# ---------------------------------------------------------------------------

def http_embed(texts: list[str]) -> np.ndarray:
    req = urllib.request.Request(
        args.embed_url,
        data=json.dumps({"texts": texts}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return np.asarray(data["embeddings"], dtype=np.float32)


def encode_pool(texts: list[str]) -> np.ndarray:
    """Batch-encode via /embed with an on-disk .npz cache."""
    os.makedirs(args.cache_dir, exist_ok=True)
    digest = hashlib.sha256("\x00".join(texts).encode()).hexdigest()[:16]
    cache = os.path.join(args.cache_dir, f"jina_{len(texts)}_{digest}.npz")
    if os.path.exists(cache):
        logger.info("  vector cache hit: %s", cache)
        return np.load(cache)["vectors"]
    # Partial checkpoint: a multi-hour 100k encode must survive interruption
    # (a kill at 86% once cost the whole encode — the cache was end-written).
    part = cache + ".part.npy"
    vecs, resume_at = [], 0
    if os.path.exists(part):
        prev = np.load(part)
        vecs, resume_at = [prev], prev.shape[0]
        logger.info("  resuming encode from checkpoint: %d/%d done", resume_at, len(texts))
    t0 = time.perf_counter()
    for start in range(resume_at, len(texts), args.encode_batch):
        vecs.append(http_embed(texts[start:start + args.encode_batch]))
        done = start + args.encode_batch
        if done % (args.encode_batch * 32) == 0:
            rate = (done - resume_at) / (time.perf_counter() - t0)
            logger.info("  encoded %d/%d (%.0f docs/s)", done, len(texts), rate)
            np.save(part, np.concatenate(vecs, axis=0))
    mat = np.concatenate(vecs, axis=0)
    np.savez_compressed(cache, vectors=mat)
    if os.path.exists(part):
        os.remove(part)
    logger.info("  encoded %d docs in %.0fs -> %s", len(texts),
                time.perf_counter() - t0, cache)
    return mat


# ---------------------------------------------------------------------------
# Fill (plumbing, not measurement — mirrors store_corpus batch INSERT)
# ---------------------------------------------------------------------------

async def fill(server_mod, docs: list[str], vectors: np.ndarray) -> int:
    db = await server_mod.get_db()
    row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (AGENT_ID,)
    )
    if row[0][0] >= len(docs):
        logger.info("  fill skipped: DB already holds %d rows", row[0][0])
        return row[0][0]
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (AGENT_ID,))
    await db.commit()
    batch = 1024
    for start in range(0, len(docs), batch):
        rows = []
        for i in range(start, min(start + batch, len(docs))):
            blob = struct.pack(f"<{vectors.shape[1]}f", *vectors[i])
            rows.append((AGENT_ID, f"lat:{i}", docs[i], "{}",
                         "2026-01-01T00:00:00Z", "{}", blob))
        await db.executemany(
            "INSERT OR IGNORE INTO memories (agent_id, msg_id, content, source, "
            "timestamp, metadata, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
        )
        await db.commit()
        if (start + batch) % (batch * 16) == 0:
            logger.info("  stored %d/%d", start + batch, len(docs))
    row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (AGENT_ID,)
    )
    logger.info("  fill complete: %d rows (dedup-collapsed from %d)", row[0][0], len(docs))
    return row[0][0]


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def pcts(samples: list[float]) -> dict:
    if not samples:
        return {}
    s = sorted(samples)
    at = lambda q: round(s[min(len(s) - 1, int(len(s) * q))], 2)  # noqa: E731
    return {"n": len(s), "p50_ms": at(0.50), "p90_ms": at(0.90),
            "p95_ms": at(0.95), "p99_ms": at(0.99),
            "mean_ms": round(sum(s) / len(s), 2)}


async def async_main():
    # Two disjoint store pools: each mode invocation stores fresh texts.
    # Re-storing the same texts in the second mode would hit cpersona's
    # UNIQUE(agent_id, project_id, channel, content) dedup and measure the
    # dedup-early-return path instead of a real store.
    n_pool = args.corpus_size + 2 * args.store_sample
    docs, queries = build_pool(n_pool, args.num_queries)
    corpus_docs = docs[:args.corpus_size]
    off = args.corpus_size + (args.store_sample if args.mode == "remote" else 0)
    store_docs = docs[off:off + args.store_sample]
    logger.info("pool: %d corpus docs + %d store docs + %d queries",
                len(corpus_docs), len(store_docs), len(queries))

    vectors = encode_pool(corpus_docs)

    import cpersona.server as server_mod
    import cpersona.vector as vector_mod
    import cpersona.memory_handlers as mh_mod
    from cpersona.server import EmbeddingClient

    # Real production embedding client, constructed the way server.main() does.
    client = EmbeddingClient(mode="http", http_url=args.embed_url)
    await client.initialize()
    vector_mod._embedding_client = client
    server_mod._embedding_client = client

    await server_mod.get_db()
    stored = await fill(server_mod, corpus_docs, vectors)

    cal = await server_mod.do_calibrate_threshold(AGENT_ID)
    logger.info("  calibrated: %s", {k: cal.get(k) for k in ("threshold", "method") if k in cal})

    # Reference: bare /embed round-trip for a short query (the encode share).
    embed_ref = []
    for q in queries[:30]:
        t0 = time.perf_counter()
        http_embed([q])
        embed_ref.append((time.perf_counter() - t0) * 1000)

    # Timed recall pass. Unique real queries, production limit. Warmup uses
    # store-pool texts, NOT timed queries — the embedding client has an LRU
    # cache, and a warmed-up query would skip its encode in the timed loop.
    for q in store_docs[:5]:
        await mh_mod.do_recall(AGENT_ID, q[:200], limit=args.limit)  # warmup
    recall_lat, result_counts = [], []
    for q in queries:
        t0 = time.perf_counter()
        res = await mh_mod.do_recall(AGENT_ID, q, limit=args.limit)
        recall_lat.append((time.perf_counter() - t0) * 1000)
        result_counts.append(len(res.get("messages", [])))

    # Timed store pass (real do_store: encode + dedup + INSERT + FTS, and in
    # remote mode the /index push too). Runs AFTER recall so the corpus size
    # seen by the recall pass stays exact.
    store_lat = []
    for i, text in enumerate(store_docs):
        t0 = time.perf_counter()
        await mh_mod.do_store(AGENT_ID, {
            "msg_id": f"latstore:{args.mode}:{i}", "content": text,
            "source": {}, "timestamp": "2026-01-02T00:00:00Z",
        })
        store_lat.append((time.perf_counter() - t0) * 1000)

    result = {
        "benchmark": "recall/store latency (production stack)",
        "mode": args.mode,
        "corpus_size": stored,
        "recall_mode": args.recall_mode,
        "limit": args.limit,
        "embedding_backend": args.embed_url,
        "max_memories": args.corpus_size,
        "recall_latency": pcts(recall_lat),
        "store_latency": pcts(store_lat),
        "embed_roundtrip_ref": pcts(embed_ref),
        "avg_results_per_recall": round(sum(result_counts) / max(1, len(result_counts)), 1),
    }
    logger.info("== %s / %d docs ==", args.mode, stored)
    logger.info("  recall  %s", result["recall_latency"])
    logger.info("  store   %s", result["store_latency"])
    logger.info("  /embed  %s", result["embed_roundtrip_ref"])
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        logger.info("  -> %s", args.out)

    await client.close()
    # Close the shared aiosqlite connection: its worker thread is non-daemon
    # and otherwise keeps the process alive after asyncio.run() returns.
    import cpersona.database as db_mod
    if db_mod._db is not None:
        await db_mod._db.close()


if __name__ == "__main__":
    asyncio.run(async_main())
