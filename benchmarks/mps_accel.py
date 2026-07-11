"""External acceleration for benchmark_trackb_lmeb.py — zero cpersona changes.

What it does
------------
Replaces the per-query hot path of cpersona's ``_search_vector`` (full-table
SELECT + blob join + numpy matmul, repeated for every query) with a
per-corpus-group preloaded matrix. Everything *around* the similarity
computation — threshold rule, candidate dict shape, candidate ordering,
heapq top-k selection, episode handling — replicates the v2.4.40 code path
line-for-line, so the observable behaviour is unchanged. cpersona itself is
never modified; we only rebind the module-level ``_search_vector`` names at
runtime (``cpersona.memory_handlers`` binds it at import time, so both that
binding and ``cpersona.vector`` are patched).

Backends
--------
- ``numpy``: cached matrix + the exact same ``mat @ q`` BLAS call the
  original makes on its per-query rebuilt matrix. Bitwise-identical
  similarities → provably identical results.
- ``torch`` (mps/cuda): cached matrix lives on the GPU; fp32 matmul there
  differs from BLAS by ~1e-7 relative. Faster for large/high-dim corpora.

Safety
------
- Non-default filters (channel / project_id / source_id) or an agent/dim
  mismatch fall back to the original function.
- Optional self-check: for a random fraction of queries the original
  function is also executed and results are compared (ids exactly, cosines
  within tolerance). Mismatches are counted and logged.
"""

import heapq
import logging
import random

import numpy as np

logger = logging.getLogger("mps_accel")


class FastVectorSearch:
    def __init__(self, server_mod, vector_mod, mh_mod, *, backend="numpy",
                 device="cpu", selfcheck_rate=0.0, selfcheck_tol=1e-5):
        self.server_mod = server_mod
        self.vector_mod = vector_mod
        self.mh_mod = mh_mod
        self.original = vector_mod._search_vector
        self.backend = backend
        self.device = device
        self.selfcheck_rate = selfcheck_rate
        self.selfcheck_tol = selfcheck_tol
        self.torch = None
        if backend == "torch":
            import torch
            self.torch = torch
            self.torch_device = torch.device(device)
        self._reset_cache()
        self.stats = {"queries": 0, "fallbacks": 0, "checked": 0, "mismatches": 0}

    def _reset_cache(self):
        self.cache = {
            "agent_id": None,
            "dim": 0,
            # memories: metadata rows in ORDER BY created_at DESC order
            "mem_meta": [],   # list of (id, msg_id, content, source, timestamp)
            "mem_mat": None,  # numpy or torch tensor, aligned with mem_meta
            # episodes, same ordering contract
            "ep_meta": [],    # list of (id, summary, start_time, resolved)
            "ep_mat": None,
        }

    # -- preload -----------------------------------------------------------

    async def preload(self, agent_id: str) -> int:
        """Load all embeddings for agent_id once (call after store/calibrate).

        Uses the same SELECT shape as the original _search_vector (default
        filters, ORDER BY created_at DESC) with LIMIT MAX_MEMORIES, which is
        the upper bound of any per-query scan_limit — per-query slices of
        this list therefore match what the original's own LIMIT would fetch.
        """
        self._reset_cache()
        db = await self.server_mod.get_db()
        max_mem = getattr(self.vector_mod, "MAX_MEMORIES")

        rows = await db.execute_fetchall(
            "SELECT id, msg_id, content, source, timestamp, embedding "
            "FROM memories WHERE agent_id = ? AND embedding IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_id, max_mem),
        )
        ep_rows = await db.execute_fetchall(
            "SELECT id, summary, start_time, embedding, resolved "
            "FROM episodes WHERE agent_id = ? AND embedding IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_id, max_mem),
        )

        dim = 0
        for row in rows:
            if row[5]:
                dim = len(row[5]) // 4
                break
        if not dim:
            for row in ep_rows:
                if row[3]:
                    dim = len(row[3]) // 4
                    break
        if not dim:
            logger.info("preload: no embeddings for %s — cache empty", agent_id)
            return 0

        mem_meta, mem_blobs = [], []
        for row in rows:
            blob = row[5]
            if blob and len(blob) == dim * 4:
                mem_meta.append(row[:5])
                mem_blobs.append(blob)
        ep_meta, ep_blobs = [], []
        for row in ep_rows:
            blob = row[3]
            if blob and len(blob) == dim * 4:
                ep_meta.append((row[0], row[1], row[2], row[4]))
                ep_blobs.append(blob)

        def to_backend(blobs):
            if not blobs:
                return None
            mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), dim)
            if self.backend == "torch":
                return self.torch.from_numpy(mat.copy()).to(self.torch_device)
            return mat

        self.cache.update({
            "agent_id": agent_id,
            "dim": dim,
            "mem_meta": mem_meta,
            "mem_mat": to_backend(mem_blobs),
            "ep_meta": ep_meta,
            "ep_mat": to_backend(ep_blobs),
        })
        mb = (len(mem_meta) + len(ep_meta)) * dim * 4 // (1024 * 1024)
        logger.info("    accel preload: %d memories + %d episodes, dim=%d (%dMB, backend=%s/%s)",
                    len(mem_meta), len(ep_meta), dim, mb, self.backend, self.device)
        return len(mem_meta)

    # -- similarity --------------------------------------------------------

    def _sims(self, mat, n, query_vec):
        """Similarities of query against the first n cached rows."""
        if mat is None or n == 0:
            return None
        if self.backend == "torch":
            q = self.torch.from_numpy(query_vec).to(self.torch_device)
            return (mat[:n] @ q).float().cpu().numpy()
        # numpy: identical BLAS call to the original's per-query mat @ query_vec
        return mat[:n] @ query_vec

    # -- the patched _search_vector ----------------------------------------

    async def search_vector(self, db, agent_id, query, limit, min_similarity=None,
                            channel="", project_id=None, source_id=""):
        c = self.cache
        # Fidelity guard: anything the cache doesn't model goes to the original.
        if (channel or project_id is not None or source_id
                or c["agent_id"] != agent_id or c["dim"] == 0):
            self.stats["fallbacks"] += 1
            return await self.original(db, agent_id, query, limit, min_similarity,
                                       channel, project_id, source_id)

        emb_client = self.vector_mod._embedding_client
        embeddings = await emb_client.embed([query])
        if not embeddings or not embeddings[0]:
            return []
        self.vector_mod.health.observe_ok()
        query_vec = np.array(embeddings[0], dtype=np.float32)
        if len(query_vec) != c["dim"]:
            self.stats["fallbacks"] += 1
            return await self.original(db, agent_id, query, limit, min_similarity,
                                       channel, project_id, source_id)

        effective_min_sim = (min_similarity if min_similarity is not None
                             else self.vector_mod._get_vector_threshold(agent_id))
        # v2.4.40 semantics (bug-085): the scan window is MAX_MEMORIES, fully
        # decoupled from the response limit. (The pre-2.4.40 replica used
        # min(MAX_MEMORIES, max(limit * 10, 100)), which silently re-created
        # the bug-085 window against a fixed 2.4.40 original — caught by the
        # selfcheck as systematic MISMATCH on corpora larger than the window.)
        scan_limit = self.vector_mod.MAX_MEMORIES

        candidates: list[tuple[float, dict]] = []

        n_mem = min(scan_limit, len(c["mem_meta"]))
        sims = self._sims(c["mem_mat"], n_mem, query_vec)
        if sims is not None:
            meta = c["mem_meta"]
            for i in np.nonzero(sims >= effective_min_sim)[0]:
                mem_id, msg_id, content, source, timestamp = meta[i]
                sim = float(sims[i])
                candidates.append((sim, {
                    "id": mem_id, "_rid": ("mem", mem_id), "_cosine": sim,
                    "msg_id": msg_id, "content": content,
                    "source": source, "timestamp": timestamp,
                }))

        n_ep = min(scan_limit, len(c["ep_meta"]))
        ep_sims = self._sims(c["ep_mat"], n_ep, query_vec)
        if ep_sims is not None:
            ep_meta = c["ep_meta"]
            for i in np.nonzero(ep_sims >= effective_min_sim)[0]:
                ep_id, summary, start_time, ep_resolved = ep_meta[i]
                sim = float(ep_sims[i])
                candidates.append((sim, {
                    "id": ep_id, "_rid": ("ep", ep_id), "_cosine": sim,
                    "content": f"[Episode] {summary}",
                    "source": {"System": "episode"},
                    "timestamp": start_time or "",
                    "_resolved": bool(ep_resolved),
                }))

        top_k = heapq.nlargest(limit, candidates, key=lambda x: x[0])
        result = [x[1] for x in top_k]
        self.stats["queries"] += 1

        if self.selfcheck_rate and random.random() < self.selfcheck_rate:
            await self._selfcheck(db, agent_id, query, limit, min_similarity,
                                  channel, project_id, source_id, result)
        return result

    async def _selfcheck(self, db, agent_id, query, limit, min_similarity,
                         channel, project_id, source_id, fast_result):
        ref = await self.original(db, agent_id, query, limit, min_similarity,
                                  channel, project_id, source_id)
        self.stats["checked"] += 1
        ok = len(ref) == len(fast_result)
        if ok:
            for a, b in zip(fast_result, ref):
                if a["_rid"] != b["_rid"] or abs(a["_cosine"] - b["_cosine"]) > self.selfcheck_tol:
                    ok = False
                    break
        if not ok:
            self.stats["mismatches"] += 1
            logger.error("selfcheck MISMATCH (query=%r): fast n=%d vs ref n=%d — "
                         "first divergence logged at debug level",
                         query[:80], len(fast_result), len(ref))
            for i, (a, b) in enumerate(zip(fast_result, ref)):
                if a["_rid"] != b["_rid"] or abs(a["_cosine"] - b["_cosine"]) > self.selfcheck_tol:
                    logger.debug("  rank %d: fast=%s(%.7f) ref=%s(%.7f)",
                                 i, a["_rid"], a["_cosine"], b["_rid"], b["_cosine"])
                    break


def install_fast_accel(server_mod, vector_mod, mh_mod, *, backend="numpy",
                       device="cpu", selfcheck_rate=0.0):
    """Install the accelerated _search_vector. Returns the FastVectorSearch.

    Patches BOTH ``cpersona.vector._search_vector`` and the import-time
    binding ``cpersona.memory_handlers._search_vector`` (the one the recall
    paths actually call in the v2.4.20+ package layout). Also exposes
    ``server_mod._preload_gpu_cache`` so benchmark_trackb_lmeb.py's existing
    post-store hook triggers the preload without harness surgery.
    """
    accel = FastVectorSearch(server_mod, vector_mod, mh_mod, backend=backend,
                             device=device, selfcheck_rate=selfcheck_rate)
    vector_mod._search_vector = accel.search_vector
    mh_mod._search_vector = accel.search_vector
    server_mod._preload_gpu_cache = accel.preload
    logger.info("fast accel installed (backend=%s, device=%s, selfcheck=%.2f%%)",
                backend, device, selfcheck_rate * 100)
    return accel
