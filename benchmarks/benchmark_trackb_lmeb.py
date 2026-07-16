"""Track B: CPersona Cascading Recall on LMEB (all 22 tasks).

Directly imports and calls cpersona server.py's actual do_store() and
do_recall() functions. The only substitution is the EmbeddingClient:
instead of HTTP/API, embeddings are pre-computed with sentence-transformers
and returned via a lookup adapter. All DB operations, FTS5 queries,
Cascading Recall logic, and scoring are the real cpersona code paths.

Benchmark notes:
  - Stage 1 (FTS5 episodes): inactive — no episodes in benchmark
  - Stage 2 (Profile): inactive — no profiles in benchmark
  - Time decay: inactive — all docs stored simultaneously
  - Active stages: Stage 0 (Vector) + Stage 3 (FTS5 keyword)

Usage:
  python benchmarks/benchmark_trackb_lmeb.py --tasks LoCoMo
  python benchmarks/benchmark_trackb_lmeb.py --device cuda
  python benchmarks/benchmark_trackb_lmeb.py --tasks LoCoMo,REALTALK --device cuda
"""

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

# --- Setup: inject cpersona into sys.path and configure env BEFORE import ---
# Default to the repo containing this script (benchmarks/ lives at the repo
# root, v2.4.20+ package layout: cpersona/{server,memory_handlers,vector,
# config,database}.py). Override with CPERSONA_REPO to benchmark another
# checkout (e.g. a bisect worktree).
_CPERSONA_REPO = os.environ.get(
    "CPERSONA_REPO", str(Path(__file__).resolve().parents[1])
)
sys.path.insert(0, _CPERSONA_REPO)

logging.basicConfig(
    format="%(levelname)s|%(asctime)s|%(name)s: %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("trackb")

# ---------------------------------------------------------------------------
# LMEB configuration
# ---------------------------------------------------------------------------
EVAL_DATA = os.path.join(
    os.environ.get("LMEB_DIR", os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", "lmeb")),
    "eval_data",
)

TASK_MAP = {
    "EPBench":          "Episodic/EPBench",
    "KnowMeBench":      "Episodic/KnowMeBench",
    "LoCoMo":           "Dialogue/LoCoMo",
    "LongMemEval":      "Dialogue/LongMemEval",
    "REALTALK":         "Dialogue/REALTALK",
    "TMD":              "Dialogue/TMD",
    "MemBench":         "Dialogue/MemBench",
    "ConvoMem":         "Dialogue/ConvoMem",
    "QASPER":           "Semantic/QASPER",
    "NovelQA":          "Semantic/NovelQA",
    "PeerQA":           "Semantic/PeerQA",
    "CovidQA":          "Semantic/Covid-QA",
    "ESGReports":       "Semantic/ESG-Reports",
    "MLDR":             "Semantic/MLDR",
    "LooGLE":           "Semantic/LooGLE",
    "LMEB_SciFact":     "Semantic/SciFact",
    "Gorilla":          "Procedural/Gorilla",
    "ToolBench":        "Procedural/ToolBench",
    "ReMe":             "Procedural/ReMe",
    "Proced_mem_bench":  "Procedural/Proced_mem_bench",
    "MemGovern":        "Procedural/MemGovern",
    "DeepPlanning":     "Procedural/DeepPlanning",
}

ALL_TASKS = list(TASK_MAP.keys())

MEMORY_TYPES = {
    "EPBench": "Episodic", "KnowMeBench": "Episodic", "ReMe": "Episodic",
    "LoCoMo": "Dialogue", "LongMemEval": "Dialogue", "REALTALK": "Dialogue",
    "TMD": "Dialogue", "MemBench": "Dialogue", "ConvoMem": "Dialogue",
    "QASPER": "Semantic", "NovelQA": "Semantic", "PeerQA": "Semantic",
    "CovidQA": "Semantic", "ESGReports": "Semantic", "MLDR": "Semantic",
    "LooGLE": "Semantic", "LMEB_SciFact": "Semantic",
    "Gorilla": "Procedural", "ToolBench": "Procedural",
    "Proced_mem_bench": "Procedural", "MemGovern": "Procedural",
    "DeepPlanning": "Procedural",
}

AGENT_ID = "lmeb-trackb"


# ===================================================================
# Lookup-based EmbeddingClient adapter
# ===================================================================

class LookupEmbeddingClient:
    """Drop-in replacement for cpersona's EmbeddingClient.

    Pre-computed embeddings are stored in a dict and returned synchronously
    via the async embed() interface. This preserves the exact code path in
    do_store() and do_recall() while avoiding HTTP overhead.
    """

    def __init__(self):
        self.mode = "http"  # pretend to be active so do_store/do_recall use us
        self._http_url = ""  # empty → skip remote index/search in do_store/do_recall
        self._client = None  # v2.4.3x health-probe path checks this attribute
        self._lookup: dict[str, list[float]] = {}

    async def initialize(self):
        pass

    async def close(self):
        self._lookup.clear()

    def preload(self, texts: list[str], embeddings: np.ndarray):
        """Batch-register text→embedding mappings."""
        for text, emb in zip(texts, embeddings):
            self._lookup[text] = emb.tolist()

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Return pre-computed embeddings from lookup."""
        results = []
        for text in texts:
            emb = self._lookup.get(text)
            if emb is None:
                return None
            results.append(emb)
        return results

    @staticmethod
    def pack_embedding(embedding: list[float]) -> bytes:
        import struct
        return struct.pack(f"<{len(embedding)}f", *embedding)

    @staticmethod
    def unpack_embedding(blob: bytes) -> list[float]:
        import struct
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))


# ===================================================================
# GPU vector search patch (benchmark-only, does not modify cpersona)
# ===================================================================

def _patch_vector_search_gpu(server_mod, device: str):
    """Monkey-patch cpersona's _search_vector to use PyTorch GPU with preloading.

    After store, call preload_gpu_cache() to load all embeddings into GPU
    memory once. Subsequent recall queries use the cached GPU tensor —
    no SQLite SELECT, no CPU→GPU transfer per query.

    All other logic (threshold, heapq, result format) remains identical
    to cpersona's original code.
    """
    import heapq
    import torch

    gpu_device = torch.device(device)

    # Pre-loaded GPU cache: populated after store, used during recall
    _cache = {
        "agent_id": None,
        "mem_mat": None,       # (N, dim) tensor on GPU
        "mem_metadata": None,  # list of (id, msg_id, content, source, timestamp)
        "query_dim": 0,
    }

    async def preload_gpu_cache(agent_id: str):
        """Load all embeddings from SQLite into GPU memory (called once after store)."""
        db = await server_mod.get_db()
        rows = await db.execute_fetchall(
            "SELECT id, msg_id, content, source, timestamp, embedding "
            "FROM memories WHERE agent_id = ? AND embedding IS NOT NULL",
            (agent_id,),
        )

        if not rows:
            _cache["agent_id"] = agent_id
            _cache["mem_mat"] = None
            _cache["mem_metadata"] = []
            _cache["query_dim"] = 0
            return 0

        # Detect dimension from first blob
        first_blob = rows[0][5]
        dim = len(first_blob) // 4

        metadata = []
        blobs = []
        for row in rows:
            blob = row[5]
            if blob and len(blob) == dim * 4:
                metadata.append(row[:5])  # (id, msg_id, content, source, timestamp)
                blobs.append(blob)

        # Single transfer to GPU
        mat = torch.frombuffer(b"".join(blobs), dtype=torch.float32).reshape(len(blobs), dim).to(gpu_device)

        _cache["agent_id"] = agent_id
        _cache["mem_mat"] = mat
        _cache["mem_metadata"] = metadata
        _cache["query_dim"] = dim

        logger.info(f"    GPU cache: {len(metadata)} embeddings ({mat.nbytes // 1024 // 1024}MB VRAM)")
        return len(metadata)

    async def _search_vector_gpu(db, agent_id, query, limit, min_similarity=None):
        emb_client = server_mod._embedding_client
        if not emb_client:
            return []
        embeddings = await emb_client.embed([query])
        if not embeddings or not embeddings[0]:
            return []

        query_vec = np.array(embeddings[0], dtype=np.float32)
        query_dim = len(query_vec)
        min_sim = min_similarity if min_similarity is not None else server_mod.VECTOR_MIN_SIMILARITY
        candidates = []

        # Use GPU cache if available and matching agent_id
        if _cache["mem_mat"] is not None and _cache["agent_id"] == agent_id:
            q_gpu = torch.from_numpy(query_vec).to(gpu_device)
            sims = (_cache["mem_mat"] @ q_gpu).cpu().numpy()

            for i, sim_val in enumerate(sims):
                if sim_val >= min_sim:
                    mem_id, msg_id, content, source, timestamp = _cache["mem_metadata"][i]
                    sim = float(sim_val)
                    candidates.append((sim, {
                        "id": mem_id, "_rid": ("mem", mem_id), "_cosine": sim,
                        "msg_id": msg_id, "content": content,
                        "source": source, "timestamp": timestamp,
                    }))
        else:
            # Fallback: load from SQLite (no cache)
            scan_limit = min(server_mod.MAX_MEMORIES, max(limit * 10, 100))
            rows = await db.execute_fetchall(
                "SELECT id, msg_id, content, source, timestamp, embedding "
                "FROM memories WHERE agent_id = ? AND embedding IS NOT NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, scan_limit),
            )
            if rows:
                valid_rows = []
                blobs = []
                for row in rows:
                    blob = row[5]
                    if blob and len(blob) == query_dim * 4:
                        valid_rows.append(row)
                        blobs.append(blob)
                if valid_rows:
                    mat = torch.frombuffer(b"".join(blobs), dtype=torch.float32).reshape(len(blobs), query_dim).to(gpu_device)
                    q_gpu = torch.from_numpy(query_vec).to(gpu_device)
                    sims = (mat @ q_gpu).cpu().numpy()
                    for i, sim_val in enumerate(sims):
                        if sim_val >= min_sim:
                            mem_id, msg_id, content, source, timestamp, _ = valid_rows[i]
                            sim = float(sim_val)
                            candidates.append((sim, {
                                "id": mem_id, "_rid": ("mem", mem_id), "_cosine": sim,
                                "msg_id": msg_id, "content": content,
                                "source": source, "timestamp": timestamp,
                            }))

        top_k = heapq.nlargest(limit, candidates, key=lambda x: x[0])
        return [c[1] for c in top_k]

    # Apply patches
    server_mod._search_vector = _search_vector_gpu
    server_mod._preload_gpu_cache = preload_gpu_cache
    logger.info("Patched _search_vector with GPU preload acceleration")


# ===================================================================
# LMEB data loading
# ===================================================================

def load_jsonl(path: str) -> list[dict]:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_qrels(path: str) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 3:
                continue
            qid, did, score = row[0], row[1], int(row[2])
            qrels.setdefault(qid, {})[did] = score
    return qrels


def load_candidates(path: str) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    if not os.path.exists(path):
        return mapping
    for item in load_jsonl(path):
        mapping[str(item["scene_id"])] = set(item["candidate_doc_ids"])
    return mapping


def get_scene_id(query_id: str) -> str:
    parts = query_id.split("_")
    if len(parts) >= 2:
        return "_".join(parts[:2])
    return query_id


def discover_task_structure(task_dir: str) -> list[dict]:
    """Auto-discover subtasks and corpus structure (supports nested hierarchies).

    Handles both flat (task/subtask/queries.jsonl) and nested
    (task/group/subtask/queries.jsonl) layouts used by LMEB tasks
    like EPBench and KnowMeBench.
    """
    task_path = Path(task_dir)
    subtasks = []

    def _find_corpus(directory: Path) -> str | None:
        """Walk up from directory to find the nearest corpus.jsonl."""
        current = directory
        while current >= task_path:
            corpus = current / "corpus.jsonl"
            if corpus.exists():
                return str(corpus)
            current = current.parent
        return None

    def _find_candidates(directory: Path) -> str | None:
        """Walk up from directory to find the nearest candidates.jsonl."""
        current = directory
        while current >= task_path:
            cand = current / "candidates.jsonl"
            if cand.exists():
                return str(cand)
            current = current.parent
        return None

    # Recursively find all directories containing queries.jsonl + qrels.tsv
    for queries_file in sorted(task_path.rglob("queries.jsonl")):
        sub = queries_file.parent
        qrels_file = sub / "qrels.tsv"
        if not qrels_file.exists():
            continue

        corpus_path = _find_corpus(sub)
        if not corpus_path:
            continue

        cand_path = _find_candidates(sub)

        # Build a readable name from the relative path
        rel = sub.relative_to(task_path)
        name = str(rel).replace("\\", "/")

        subtasks.append({
            "name": name,
            "corpus": corpus_path,
            "queries": str(queries_file),
            "qrels": str(qrels_file),
            "candidates": cand_path,
        })

    return subtasks


# ===================================================================
# Metrics
# ===================================================================

def compute_ndcg(
    qrels: dict[str, dict[str, int]],
    results: dict[str, list[str]],
    k: int = 10,
) -> float:
    scores = []
    for qid, rels in qrels.items():
        relevant = {d: s for d, s in rels.items() if s > 0}
        if not relevant:
            continue

        ranked_ids = results.get(qid, [])[:k]

        dcg = 0.0
        for i, doc_id in enumerate(ranked_ids):
            if doc_id in relevant:
                dcg += 1.0 / math.log2(i + 2)

        ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
        scores.append(dcg / ideal if ideal > 0 else 0.0)

    return (sum(scores) / len(scores) * 100) if scores else 0.0


# ===================================================================
# Main benchmark
# ===================================================================

# Per-role encode kwargs, set at model load (asymmetric-prompt models only).
_DOC_ENCODE_KW: dict = {}
_QUERY_ENCODE_KW: dict = {}


async def store_corpus(server_mod, emb_client, st_model, corpus: list[dict], batch_size: int = 256) -> int:
    """Store corpus using cpersona's schema and FTS5 triggers with batch optimization.

    Optimizations (all external to cpersona — no cpersona code changes):
      Level 1: executemany + single COMMIT per batch (1 disk sync per batch)
      Level 2: Pre-compute embeddings with sentence-transformers (batch GPU encode)
      Level 3: Skip dedup SELECT (LMEB corpus IDs are guaranteed unique)

    Recall still uses 100% real do_recall(). Store uses cpersona's actual
    SQLite schema and FTS5 triggers (which fire on INSERT automatically),
    bypassing only do_store()'s per-row commit and dedup check.
    """
    import struct

    total = len(corpus)
    db = await server_mod.get_db()
    source_json = "{}"
    timestamp = "2026-01-01T00:00:00Z"
    metadata_json = "{}"

    for start in range(0, total, batch_size):
        batch = corpus[start:start + batch_size]
        texts = [(doc.get("title", "") + " " + doc.get("text", "")).strip() for doc in batch]

        # Level 2: Batch-encode with sentence-transformers (GPU-accelerated)
        embeddings = st_model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False, **_DOC_ENCODE_KW
        )

        # Level 1 + 3: Batch INSERT with single transaction, no dedup SELECT
        rows = []
        for doc, text, emb in zip(batch, texts, embeddings):
            blob = struct.pack(f"<{len(emb)}f", *emb)
            rows.append((AGENT_ID, str(doc["id"]), text, source_json, timestamp, metadata_json, blob))

        # v2.4.36 enforces UNIQUE(agent_id, project_id, channel, content) and
        # UNIQUE(agent_id, project_id, msg_id) — exact-duplicate corpus docs
        # collapse, which is the shipped dedup behaviour. OR IGNORE mirrors
        # do_store; the dropped count is logged below (no silent truncation).
        await db.executemany(
            "INSERT OR IGNORE INTO memories (agent_id, msg_id, content, source, timestamp, metadata, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()  # FTS5 triggers have already fired on each INSERT

        done = min(start + batch_size, total)
        if done % (batch_size * 4) == 0 or done == total:
            logger.info(f"    Stored {done}/{total}")

    row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (AGENT_ID,)
    )
    stored = row[0][0]
    if stored < total:
        logger.warning(
            f"    {total - stored}/{total} corpus docs collapsed by cpersona's "
            f"UNIQUE dedup (duplicate content or msg_id) — shipped behaviour, "
            f"those doc ids are unretrievable"
        )
    return total


async def run_subtask(
    server_mod,
    emb_client,
    st_model,
    subtask: dict,
    corpus_size: int,
    latencies_full: list[float] | None = None,
    latencies_limit10: list[float] | None = None,
) -> float:
    """Run a single subtask using cpersona's actual do_recall().

    When latency lists are provided, per-query wall-clock of the real
    do_recall() call is appended to them: `latencies_full` for the NDCG pass
    (limit=corpus_size) and `latencies_limit10` for an extra production-shaped
    pass (limit=10, the MCP default) over the same queries.
    """
    queries_data = load_jsonl(subtask["queries"])
    qrels = load_qrels(subtask["qrels"])
    candidates = load_candidates(subtask["candidates"]) if subtask["candidates"] else {}

    # Pre-compute query embeddings
    query_texts = [q["text"] for q in queries_data]
    query_embeddings = st_model.encode(
        query_texts, normalize_embeddings=True, show_progress_bar=False, **_QUERY_ENCODE_KW
    )
    emb_client.preload(query_texts, query_embeddings)

    results: dict[str, list[str]] = {}
    total_q = len(queries_data)

    for i, q in enumerate(queries_data):
        qid = str(q["id"])  # Ensure string for consistent matching with qrels (CSV returns strings)
        qtext = q["text"]

        # Call actual do_recall() with limit=corpus_size for fair Track A comparison.
        # This ensures cpersona can rank ALL documents, not just a small top-K window.
        # The Cascading Recall still applies: Stage 0 (vector) returns docs with
        # cosine >= 0.3, then Stage 3 (FTS5) fills remaining with keyword matches.
        t0 = time.perf_counter()
        recall_result = await server_mod.do_recall(
            agent_id=AGENT_ID,
            query=qtext,
            limit=corpus_size,
        )
        if latencies_full is not None:
            latencies_full.append((time.perf_counter() - t0) * 1000)

        # Extract msg_ids from recall result (same format as MCP response).
        # IMPORTANT: do_recall() reverses results for LLM context (most relevant
        # at the end). We reverse back to get relevance-descending order for NDCG.
        messages = recall_result.get("messages", [])
        messages = list(reversed(messages))
        doc_ids = []
        for msg in messages:
            msg_id = msg.get("id", "")
            if msg_id:
                doc_ids.append(msg_id)

        # Filter by candidates (same as LMEB SubsetRetrieval)
        if candidates:
            scene_id = get_scene_id(qid)
            if scene_id in candidates:
                allowed = candidates[scene_id]
                doc_ids = [d for d in doc_ids if d in allowed]

        results[qid] = doc_ids

        if (i + 1) % 200 == 0:
            logger.info(f"      Queried {i + 1}/{total_q}")

    # Production-shaped latency pass: same queries, MCP default limit=10 and
    # the shipped MAX_MEMORIES=500 scan cap (the benchmark env raises the cap
    # for the NDCG pass; restoring 500 here makes the p50 an honest
    # as-shipped number). NDCG is not computed here.
    if latencies_limit10 is not None:
        # MAX_MEMORIES is imported into both consumer modules at load time —
        # override each module-level binding, not cpersona.config.
        cap_mods = [
            m for m in (sys.modules.get("cpersona.vector"), sys.modules.get("cpersona.memory_handlers"))
            if m is not None and hasattr(m, "MAX_MEMORIES")
        ]
        saved_caps = [(m, m.MAX_MEMORIES) for m in cap_mods]
        for m in cap_mods:
            m.MAX_MEMORIES = 500
        try:
            for q in queries_data:
                t0 = time.perf_counter()
                await server_mod.do_recall(agent_id=AGENT_ID, query=q["text"], limit=10)
                latencies_limit10.append((time.perf_counter() - t0) * 1000)
        finally:
            for m, v in saved_caps:
                m.MAX_MEMORIES = v

    return compute_ndcg(qrels, results, k=10)


async def run_task(
    task_name: str,
    server_mod,
    emb_client,
    st_model,
    output_dir: str,
    batch_size: int = 256,
    auto_calibrate: bool = False,
    subtask_filter: set[str] | None = None,
    skip_latency_pass: bool = False,
) -> dict | None:
    task_dir = os.path.join(EVAL_DATA, TASK_MAP[task_name])
    if not os.path.isdir(task_dir):
        logger.error(f"  Task dir not found: {task_dir}")
        return None

    subtasks = discover_task_structure(task_dir)
    if not subtasks:
        logger.warning(f"  No subtasks found for {task_name}")
        return None
    if subtask_filter:
        subtasks = [st for st in subtasks if st["name"] in subtask_filter]
        logger.info(f"  Subtask filter active: {sorted(subtask_filter)} -> {len(subtasks)} kept")
        if not subtasks:
            logger.error("  Subtask filter matched nothing")
            return None

    logger.info(f"  Found {len(subtasks)} subtasks")

    # Group subtasks by corpus path
    corpus_groups: dict[str, list[dict]] = defaultdict(list)
    for st in subtasks:
        corpus_groups[st["corpus"]].append(st)

    subtask_results: dict[str, float] = {}
    latencies_full: list[float] = []
    latencies_limit10: list[float] = []
    task_start = time.time()

    for corpus_path, group_subtasks in corpus_groups.items():
        # Clean previous data
        await server_mod.do_delete_agent_data(AGENT_ID)

        # Load and store corpus
        corpus = load_jsonl(corpus_path)
        logger.info(f"    Corpus: {len(corpus)} docs")

        store_start = time.time()
        corpus_size = await store_corpus(server_mod, emb_client, st_model, corpus, batch_size=batch_size)
        store_time = time.time() - store_start
        logger.info(f"    Store: {store_time:.1f}s ({len(corpus) / max(store_time, 0.01):.0f} docs/s)")

        # Auto-calibrate threshold if requested
        if auto_calibrate:
            cal = await server_mod.do_calibrate_threshold(AGENT_ID)
            if cal.get("ok"):
                logger.info(f"    Calibrated: {cal['old_threshold']} → {cal['new_threshold']} "
                            f"(z={cal['z_factor']:.1f}, mean={cal['distribution']['mean']:.4f}, "
                            f"std={cal['distribution']['std']:.4f})")

        # GPU preload: load all embeddings into GPU memory once
        if hasattr(server_mod, '_preload_gpu_cache'):
            await server_mod._preload_gpu_cache(AGENT_ID)

        # Run each subtask
        for st in group_subtasks:
            logger.info(f"    Subtask: {st['name']}")
            eval_start = time.time()
            ndcg = await run_subtask(
                server_mod, emb_client, st_model, st, corpus_size=corpus_size,
                latencies_full=latencies_full,
                latencies_limit10=None if skip_latency_pass else latencies_limit10,
            )
            eval_time = time.time() - eval_start
            subtask_results[st["name"]] = ndcg
            logger.info(f"      NDCG@10={ndcg:.2f} ({eval_time:.1f}s)")

    # Final cleanup
    await server_mod.do_delete_agent_data(AGENT_ID)

    task_time = time.time() - task_start
    mean_ndcg = sum(subtask_results.values()) / len(subtask_results) if subtask_results else 0.0

    def _pcts(lat: list[float]) -> dict | None:
        if not lat:
            return None
        s = sorted(lat)
        return {
            "n": len(s),
            "p50_ms": round(s[len(s) // 2], 2),
            "p95_ms": round(s[int(len(s) * 0.95)], 2),
            "p99_ms": round(s[min(int(len(s) * 0.99), len(s) - 1)], 2),
            "mean_ms": round(sum(s) / len(s), 2),
        }

    result = {
        "task": task_name,
        "memory_type": MEMORY_TYPES.get(task_name, "Unknown"),
        "mean_ndcg_at_10": round(mean_ndcg, 2),
        "subtasks": {k: round(v, 2) for k, v in subtask_results.items()},
        "num_subtasks": len(subtask_results),
        "time_s": round(task_time, 1),
        # Real do_recall() wall-clock. "full" = the NDCG pass (limit=corpus
        # size, benchmark-shaped); "limit10" = MCP default limit, the honest
        # production-shaped p50.
        "recall_latency_full": _pcts(latencies_full),
        "recall_latency_limit10": _pcts(latencies_limit10),
    }
    if skip_latency_pass:
        result["accel"] = True
        result["latency_note"] = ("run with --fast acceleration; latency numbers are "
                                  "not representative of as-shipped recall")

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{task_name}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    logger.info(f"  → {task_name} Mean NDCG@10 = {mean_ndcg:.2f} ({task_time:.1f}s)")

    return result


async def async_main(args):
    # --- Import cpersona server module ---
    # Set env vars BEFORE importing to control cpersona's configuration
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="trackb_")
    tmp_db_path = tmp_db.name
    tmp_db.close()

    os.environ["CPERSONA_DB_PATH"] = tmp_db_path
    os.environ["CPERSONA_EMBEDDING_MODE"] = "http"  # so _embedding_client gets created
    os.environ["CPERSONA_EMBEDDING_URL"] = "http://localhost:0"  # dummy — we override the client
    os.environ["CPERSONA_VECTOR_SEARCH_MODE"] = "local"  # use BLOB cosine search
    os.environ["CPERSONA_STORE_BLOB"] = "true"
    os.environ["CPERSONA_FTS_ENABLED"] = "true"
    os.environ["CPERSONA_TASK_QUEUE_ENABLED"] = "false"  # no background LLM tasks
    # Benchmark needs to scan all stored documents, not just the default 500.
    # These are configuration knobs, not code changes — users can set these too.
    os.environ["CPERSONA_MAX_MEMORIES"] = str(args.max_memories)
    os.environ["CPERSONA_VECTOR_MIN_SIMILARITY"] = str(args.min_similarity)
    os.environ["CPERSONA_RECALL_MODE"] = args.recall_mode

    # Import the actual cpersona server module
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod

    # Create lookup-based embedding client
    emb_client = LookupEmbeddingClient()

    # Patch the global embedding client. v2.4.20+ package layout keeps the
    # singleton in cpersona.vector (memory_handlers reads vector._embedding_client);
    # the server_mod attribute is kept for older monolith checkouts via CPERSONA_REPO.
    vector_mod._embedding_client = emb_client
    server_mod._embedding_client = emb_client

    # Initialize the DB (creates schema, FTS5 tables, triggers)
    await server_mod.get_db()
    logger.info(f"cpersona DB initialized at {tmp_db_path}")

    # --unclamp_limit is obsolete since 2.5.0 (Task #190): do_recall's in-library
    # cap is now the scan window (MAX_MEMORIES), so the harness's
    # limit=corpus_size full-ranking convention works against a stock checkout.
    # The flag is still accepted (no-op) so pre-2.5.0 command lines keep working;
    # on a pre-2.5.0 checkout you still need the old monkeypatch build.
    if getattr(args, "unclamp_limit", False):
        logger.info("  --unclamp_limit: no-op since 2.5.0 (do_recall caps at the scan window)")

    # --- Fast acceleration (external, behavior-invariant — see mps_accel.py) ---
    # Preloads each corpus group's embeddings once instead of the per-query
    # SELECT + blob-join + matmul. Patches the memory_handlers import-time
    # binding of _search_vector (the one v2.4.20+ recall paths actually call).
    accel = None
    if args.fast:
        import cpersona.memory_handlers as mh_mod
        from mps_accel import install_fast_accel
        if args.accel_backend == "auto":
            backend = "torch" if args.device in ("mps",) or (args.device or "").startswith("cuda") else "numpy"
        else:
            backend = args.accel_backend
        accel = install_fast_accel(
            server_mod, vector_mod, mh_mod,
            backend=backend,
            device=args.device if backend == "torch" else "cpu",
            selfcheck_rate=args.selfcheck_rate,
        )
    elif args.device and args.device.startswith("cuda"):
        # Legacy CUDA patch (pre-package-layout; kept for old checkouts via
        # CPERSONA_REPO. On the v2.4.20+ package layout it does not reach the
        # recall path — use --fast instead.)
        _patch_vector_search_gpu(server_mod, args.device)
        logger.info(f"  Vector search patched to use GPU ({args.device})")

    # Load sentence-transformers model
    from sentence_transformers import SentenceTransformer
    logger.info(f"Loading model: {args.model_path}")
    model_kwargs = {}
    if args.dtype == "float16":
        import torch
        model_kwargs["model_kwargs"] = {"torch_dtype": torch.float16}
    if args.default_task:
        model_kwargs.setdefault("model_kwargs", {})["default_task"] = args.default_task
    if args.trust_remote_code:
        model_kwargs["trust_remote_code"] = True
    st_model = SentenceTransformer(args.model_path, device=args.device, **model_kwargs)
    if args.budget_encode:
        from budget_batching import install_budget_batching
        install_budget_batching(st_model)
        logger.info("  Token-budget dynamic batching installed")
    logger.info(f"  Device: {st_model.device}, Dim: {st_model.get_sentence_embedding_dimension()}")

    # Prompted models (jina v5: {'query','document'}) need the asymmetric
    # prompts applied explicitly — queries encoded under the document prompt
    # break asymmetric retrieval. Promptless models (bge-m3, MiniLM) get {}
    # and encode exactly as before. Explicit prompt_name also keeps the
    # embedding disk-cache keys identical to Track A's mteb-wrapper encodes.
    global _DOC_ENCODE_KW, _QUERY_ENCODE_KW
    st_prompts = getattr(st_model, "prompts", None) or {}
    _DOC_ENCODE_KW = {"prompt_name": "document"} if "document" in st_prompts else {}
    _QUERY_ENCODE_KW = {"prompt_name": "query"} if "query" in st_prompts else {}
    if _DOC_ENCODE_KW or _QUERY_ENCODE_KW:
        logger.info(f"  Asymmetric prompts active: {st_prompts}")

    # Run tasks
    task_names = [t.strip() for t in args.tasks.split(",")]
    all_results = []

    for task_name in task_names:
        if task_name not in TASK_MAP:
            logger.error(f"Unknown task: {task_name}")
            continue

        # Skip if result already exists (resume support)
        result_path = os.path.join(args.output_dir, f"{task_name}.json")
        if os.path.exists(result_path) and not args.force:
            with open(result_path, "r") as f:
                cached = json.load(f)
            logger.info(f"  Skipping {task_name} (cached: NDCG@10={cached['mean_ndcg_at_10']})")
            all_results.append(cached)
            continue

        # Archive previous result before re-measuring
        if os.path.exists(result_path):
            archive_dir = os.path.join(args.output_dir, "archive")
            os.makedirs(archive_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            archive_path = os.path.join(archive_dir, f"{task_name}_{ts}.json")
            os.rename(result_path, archive_path)
            logger.info(f"  Archived previous result → {archive_path}")

        logger.info(f"\n{'='*60}")
        logger.info(f"Task: {task_name}")
        logger.info(f"{'='*60}")

        result = await run_task(
            task_name, server_mod, emb_client, st_model,
            args.output_dir, batch_size=args.batch_size, auto_calibrate=args.auto_calibrate,
            subtask_filter=set(args.subtasks.split(",")) if args.subtasks else None,
            skip_latency_pass=bool(args.fast),
        )
        if result:
            all_results.append(result)

    # Cleanup
    await server_mod.close_db()
    try:
        os.unlink(tmp_db_path)
    except OSError:
        pass

    # Summary
    if all_results:
        logger.info(f"\n{'='*60}")
        logger.info("SUMMARY — Track B (CPersona Cascading Recall)")
        logger.info(f"{'='*60}")
        logger.info(f"{'Task':<25} {'Type':<12} {'NDCG@10':>8}")
        logger.info("-" * 50)
        for r in sorted(all_results, key=lambda x: x["mean_ndcg_at_10"], reverse=True):
            logger.info(f"{r['task']:<25} {r['memory_type']:<12} {r['mean_ndcg_at_10']:>8.2f}")

        overall_mean = sum(r["mean_ndcg_at_10"] for r in all_results) / len(all_results)
        logger.info("-" * 50)
        logger.info(f"{'Mean':<25} {'':12} {overall_mean:>8.2f}")

        summary = {
            "model": args.model_path,
            "track": "B",
            "pipeline": "CPersona Cascading Recall (Vector + FTS5 keyword)",
            "code_path": "servers/cpersona/server.py do_store() + do_recall()",
            "active_stages": ["Stage 0: Vector (local BLOB cosine)", "Stage 3: FTS5 keyword"],
            "inactive_stages": ["Stage 1: Episodes (no data)", "Stage 2: Profile (no data)"],
            "overall_mean": round(overall_mean, 2),
            "tasks": all_results,
        }
        if accel is not None:
            summary["accel"] = {
                "backend": accel.backend,
                "device": accel.device,
                "selfcheck": dict(accel.stats),
            }
            logger.info(f"Accel stats: {accel.stats}")
            if accel.stats["mismatches"]:
                logger.error("ACCEL SELF-CHECK REPORTED MISMATCHES — treat these "
                             "scores as suspect until investigated")
        summary_path = os.path.join(args.output_dir, "summary.json")
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info(f"\nSummary saved to {summary_path}")


def main():
    parser = argparse.ArgumentParser(description="Track B: CPersona Cascading Recall on LMEB")
    parser.add_argument("--model_path", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--tasks", default=",".join(ALL_TASKS), help="Comma-separated task names")
    parser.add_argument("--output_dir", default="trackb_results")
    parser.add_argument("--device", default=None, help="torch device (cuda, cpu, etc.)")
    parser.add_argument("--batch_size", type=int, default=256)
    parser.add_argument("--max_memories", type=int, default=300000,
                        help="CPERSONA_MAX_MEMORIES — vector scan window size")
    parser.add_argument("--min_similarity", type=float, default=0.3,
                        help="CPERSONA_VECTOR_MIN_SIMILARITY threshold")
    parser.add_argument("--auto_calibrate", action="store_true",
                        help="Auto-calibrate threshold per corpus (overrides --min_similarity)")
    parser.add_argument("--recall_mode", default="cascade", choices=["cascade", "rrf"],
                        help="CPERSONA_RECALL_MODE: cascade (sequential) or rrf (Reciprocal Rank Fusion)")
    parser.add_argument("--subtasks", default=None,
                        help="Comma-separated subtask names to keep (ablation runs)")
    parser.add_argument("--budget_encode", action="store_true",
                        help="Token-budget dynamic batching (required for long-context tasks on MPS)")
    parser.add_argument("--dtype", default="float32", choices=["float16", "float32"],
                        help="model dtype (float16 recommended on MPS)")
    parser.add_argument("--force", action="store_true",
                        help="Re-measure all tasks even if results exist (archives previous results)")
    parser.add_argument("--fast", action="store_true",
                        help="Behavior-invariant acceleration (mps_accel.py): preload each "
                             "corpus group's embeddings once instead of per-query SELECT+matmul; "
                             "skips the production-shaped limit=10 latency pass")
    parser.add_argument("--accel_backend", default="auto", choices=["auto", "numpy", "torch"],
                        help="--fast similarity backend: numpy = bitwise-identical to original; "
                             "torch = GPU (mps/cuda) matmul, ~1e-7 rounding difference; "
                             "auto = torch when --device is mps/cuda else numpy")
    parser.add_argument("--selfcheck_rate", type=float, default=0.01,
                        help="Fraction of --fast queries re-verified against the original "
                             "_search_vector (0 disables)")
    parser.add_argument("--unclamp_limit", action="store_true",
                        help="Obsolete since 2.5.0 (accepted as a no-op): do_recall's "
                             "in-library cap is the scan window, so the limit=corpus_size "
                             "full-ranking convention works without a patch. Only needed "
                             "on pre-2.5.0 checkouts (v2.4.38..v2.4.40 capped at 100)")
    parser.add_argument("--trust_remote_code", action="store_true",
                        help="pass trust_remote_code=True for models with custom modeling "
                             "code (e.g. jinaai/jina-embeddings-v5-text-nano)")
    parser.add_argument("--default_task", default=None,
                        help="default_task model kwarg for task-LoRA models "
                             "(jina v5: 'retrieval' — encoding fails without a task)")
    args = parser.parse_args()

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
