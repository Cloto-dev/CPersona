"""Vector embedding client and similarity search for CPersona.

Holds the module-level `_embedding_client` singleton, set by `server.main()` at startup.
"""

import hashlib
import heapq
import logging
import os
import struct
import time
from collections import OrderedDict

import aiosqlite
import httpx

import config
from config import (
    EMBEDDING_CACHE_SIZE,
    EMBEDDING_CACHE_TTL,
    MAX_MEMORIES,
    VECTOR_SEARCH_MODE,
)

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """Client for computing vector embeddings via HTTP or API.

    Includes a TTL-based LRU cache for single-text queries (recall dedup).
    """

    def __init__(
        self,
        mode: str,
        http_url: str = "",
        api_key: str = "",
        api_url: str = "",
        model: str = "",
        cache_size: int = EMBEDDING_CACHE_SIZE,
        cache_ttl: int = EMBEDDING_CACHE_TTL,
    ):
        self.mode = mode
        self._http_url = http_url
        self._api_key = api_key
        self._api_url = api_url
        self._model = model
        self._client: httpx.AsyncClient | None = None
        self._cache: OrderedDict[str, tuple[list[float], float]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl
        self.cache_hits = 0
        self.cache_misses = 0

    async def initialize(self):
        """Create persistent HTTP client."""
        timeout = int(os.environ.get("CPERSONA_EMBEDDING_TIMEOUT_SECS", "30"))
        self._client = httpx.AsyncClient(timeout=timeout)
        logger.info(
            "EmbeddingClient initialized (mode=%s, cache=%d, ttl=%ds)",
            self.mode,
            self._cache_size,
            self._cache_ttl,
        )

    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _cache_get(self, text: str) -> list[float] | None:
        """Look up a single text in cache. Returns embedding or None."""
        key = self._cache_key(text)
        entry = self._cache.get(key)
        if entry is None:
            return None
        embedding, ts = entry
        if time.monotonic() - ts > self._cache_ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return embedding

    def _cache_put(self, text: str, embedding: list[float]) -> None:
        """Store a single text→embedding in cache."""
        key = self._cache_key(text)
        self._cache[key] = (embedding, time.monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Compute embeddings with LRU cache for single-text queries.

        Cache is used only for single-text calls (the common recall path).
        Batch calls bypass cache to avoid complexity.
        """
        if self.mode == "none" or not self._client:
            return None

        if len(texts) == 1:
            cached = self._cache_get(texts[0])
            if cached is not None:
                self.cache_hits += 1
                return [cached]
            self.cache_misses += 1

        try:
            if self.mode == "http":
                result = await self._embed_via_http(texts)
            elif self.mode == "api":
                result = await self._embed_via_api(texts)
            else:
                logger.warning("Unknown embedding mode: %s", self.mode)
                return None
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError) as e:
            logger.warning("Embedding request failed: %s", e)
            return None

        if result and len(texts) == 1 and len(result) == 1:
            self._cache_put(texts[0], result[0])

        return result

    async def _embed_via_http(self, texts: list[str]) -> list[list[float]] | None:
        """Call the embedding server's HTTP endpoint."""
        response = await self._client.post(
            self._http_url,
            json={"texts": texts},
        )
        response.raise_for_status()
        data = response.json()
        return data.get("embeddings")

    async def _embed_via_api(self, texts: list[str]) -> list[list[float]] | None:
        """Call OpenAI-compatible embedding API directly."""
        import numpy as np

        response = await self._client.post(
            self._api_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self._model, "input": texts},
        )
        response.raise_for_status()
        data = response.json()
        embeddings = [item["embedding"] for item in data["data"]]

        result = []
        for emb in embeddings:
            vec = np.array(emb, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 1e-9:
                vec = vec / norm
            result.append(vec.tolist())

        return result

    @staticmethod
    def pack_embedding(embedding: list[float]) -> bytes:
        """Pack a float list into a BLOB (little-endian float32)."""
        return struct.pack(f"<{len(embedding)}f", *embedding)

    @staticmethod
    def unpack_embedding(blob: bytes) -> list[float]:
        """Unpack a BLOB into a float list."""
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))


_embedding_client: EmbeddingClient | None = None


async def _search_vector(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_similarity: float | None = None,
    channel: str = "",
) -> list[dict]:
    """Search memories and episodes using vector cosine similarity."""

    if VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url:
        try:
            base_url = _embedding_client._http_url.rsplit("/", 1)[0]
            resp = await _embedding_client._client.post(
                f"{base_url}/search",
                json={
                    "namespace": f"cpersona:{agent_id}",
                    "query": query,
                    "limit": limit,
                    "min_similarity": config.VECTOR_MIN_SIMILARITY,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for hit in data.get("results", []):
                raw_id = hit["id"]
                score = hit["score"]
                if raw_id.startswith("mem:"):
                    mem_id = int(raw_id[4:])
                    if channel:
                        row = await db.execute_fetchall(
                            "SELECT msg_id, content, source, timestamp FROM memories WHERE id = ? AND channel = ?",
                            (mem_id, channel),
                        )
                    else:
                        row = await db.execute_fetchall(
                            "SELECT msg_id, content, source, timestamp FROM memories WHERE id = ?",
                            (mem_id,),
                        )
                    if row:
                        results.append(
                            {
                                "id": mem_id,
                                "_rid": ("mem", mem_id),
                                "_cosine": score,
                                "msg_id": row[0][0],
                                "content": row[0][1],
                                "source": row[0][2],
                                "timestamp": row[0][3],
                            }
                        )
                elif raw_id.startswith("ep:"):
                    ep_id = int(raw_id[3:])
                    row = await db.execute_fetchall(
                        "SELECT summary, start_time, resolved FROM episodes WHERE id = ?",
                        (ep_id,),
                    )
                    if row:
                        results.append(
                            {
                                "id": ep_id,
                                "_rid": ("ep", ep_id),
                                "_cosine": score,
                                "content": f"[Episode] {row[0][0]}",
                                "source": {"System": "episode"},
                                "timestamp": row[0][1] or "",
                                "_resolved": bool(row[0][2]),
                            }
                        )
            return results
        except Exception as e:
            logger.warning("Remote vector search failed, falling back to local: %s", e)

    import numpy as np

    embeddings = await _embedding_client.embed([query])
    if not embeddings or not embeddings[0]:
        return []
    query_vec = np.array(embeddings[0], dtype=np.float32)
    query_dim = len(query_vec)
    effective_min_sim = min_similarity if min_similarity is not None else config.VECTOR_MIN_SIMILARITY

    candidates: list[tuple[float, dict]] = []
    scan_limit = min(MAX_MEMORIES, max(limit * 10, 100))

    if channel:
        rows = await db.execute_fetchall(
            """SELECT id, msg_id, content, source, timestamp, embedding
               FROM memories
               WHERE agent_id = ? AND channel = ? AND embedding IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, channel, scan_limit),
        )
    else:
        rows = await db.execute_fetchall(
            """SELECT id, msg_id, content, source, timestamp, embedding
               FROM memories
               WHERE agent_id = ? AND embedding IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
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
            mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), query_dim)
            sims = mat @ query_vec

            for i, sim_val in enumerate(sims):
                if sim_val >= effective_min_sim:
                    mem_id, msg_id, content, source, timestamp, _ = valid_rows[i]
                    sim = float(sim_val)
                    candidates.append(
                        (
                            sim,
                            {
                                "id": mem_id,
                                "_rid": ("mem", mem_id),
                                "_cosine": sim,
                                "msg_id": msg_id,
                                "content": content,
                                "source": source,
                                "timestamp": timestamp,
                            },
                        )
                    )

    ep_rows = await db.execute_fetchall(
        """SELECT id, summary, start_time, embedding, resolved
           FROM episodes
           WHERE agent_id = ? AND embedding IS NOT NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (agent_id, scan_limit),
    )

    if ep_rows:
        valid_ep_rows = []
        ep_blobs = []
        for row in ep_rows:
            blob = row[3]
            if blob and len(blob) == query_dim * 4:
                valid_ep_rows.append(row)
                ep_blobs.append(blob)

        if valid_ep_rows:
            ep_mat = np.frombuffer(b"".join(ep_blobs), dtype=np.float32).reshape(len(ep_blobs), query_dim)
            ep_sims = ep_mat @ query_vec

            for i, sim_val in enumerate(ep_sims):
                if sim_val >= effective_min_sim:
                    ep_id, summary, start_time, _, ep_resolved = valid_ep_rows[i]
                    sim = float(sim_val)
                    candidates.append(
                        (
                            sim,
                            {
                                "id": ep_id,
                                "_rid": ("ep", ep_id),
                                "_cosine": sim,
                                "content": f"[Episode] {summary}",
                                "source": {"System": "episode"},
                                "timestamp": start_time or "",
                                "_resolved": bool(ep_resolved),
                            },
                        )
                    )

    top_k = heapq.nlargest(limit, candidates, key=lambda x: x[0])
    return [c[1] for c in top_k]
