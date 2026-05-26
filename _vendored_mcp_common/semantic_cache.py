"""In-memory semantic cache for high-quality research results.

Uses embedding similarity to match queries and return cached responses,
avoiding redundant multi-step RAG pipelines for previously-answered queries.

Design based on KS2.1 SemanticCache architecture.
"""

import hashlib
import logging
import time
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

# Default embedding server (same endpoint as tool.embedding MCP server)
_DEFAULT_EMBEDDING_URL = "http://127.0.0.1:8401/embed"


def _dot_product(a: list[float], b: list[float]) -> float:
    """Cosine similarity via dot product (assumes L2-normalized vectors)."""
    return sum(x * y for x, y in zip(a, b))


class SemanticCache:
    """TTL-based LRU cache with embedding similarity lookup.

    Stores (query_vector, result_dict, score, timestamp) per entry.
    Lookup computes query embedding and finds the best match above threshold.
    """

    def __init__(
        self,
        embedding_url: str = _DEFAULT_EMBEDDING_URL,
        max_entries: int = 100,
        ttl: int = 3600,
        similarity_threshold: float = 0.95,
        min_score: int = 8,
    ):
        self._embedding_url = embedding_url
        self._max_entries = max_entries
        self._ttl = ttl
        self._threshold = similarity_threshold
        self._min_score = min_score
        # key=hash, value=(query_vec, result, score, timestamp)
        self._cache: OrderedDict[str, tuple[list[float], dict, int, float]] = OrderedDict()
        self._client: httpx.AsyncClient | None = None
        self.hits = 0
        self.misses = 0

    async def initialize(self):
        """Create HTTP client for embedding calls."""
        self._client = httpx.AsyncClient(timeout=10)
        logger.info(
            "SemanticCache initialized (max=%d, ttl=%ds, threshold=%.2f)",
            self._max_entries,
            self._ttl,
            self._threshold,
        )

    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _embed(self, text: str) -> list[float] | None:
        """Compute L2-normalized embedding for a single text."""
        if not self._client:
            return None
        try:
            resp = await self._client.post(self._embedding_url, json={"texts": [text]})
            resp.raise_for_status()
            embeddings = resp.json().get("embeddings", [])
            if not embeddings:
                return None
            vec = embeddings[0]
            # L2-normalize
            norm = sum(v * v for v in vec) ** 0.5
            if norm > 1e-9:
                vec = [v / norm for v in vec]
            return vec
        except Exception as e:
            logger.debug("Embedding call failed: %s", e)
            return None

    async def lookup(self, query: str) -> dict | None:
        """Find cached result for a semantically similar query.

        Returns the cached result dict if similarity >= threshold, else None.
        """
        query_vec = await self._embed(query)
        if query_vec is None:
            self.misses += 1
            return None

        now = time.monotonic()
        best_sim = 0.0
        best_key: str | None = None
        best_result: dict | None = None
        expired: list[str] = []

        for key, (vec, result, _score, ts) in self._cache.items():
            if now - ts > self._ttl:
                expired.append(key)
                continue
            sim = _dot_product(query_vec, vec)
            if sim > best_sim:
                best_sim = sim
                best_key = key
                best_result = result

        for key in expired:
            del self._cache[key]

        if best_key and best_sim >= self._threshold:
            self._cache.move_to_end(best_key)
            self.hits += 1
            logger.info("Semantic cache hit (similarity=%.3f)", best_sim)
            return best_result

        self.misses += 1
        return None

    async def store(self, query: str, result: dict, score: int) -> bool:
        """Store a high-quality result in cache.

        Only stores if score >= min_score. Returns True if stored.
        """
        if score < self._min_score:
            return False

        query_vec = await self._embed(query)
        if query_vec is None:
            return False

        key = hashlib.sha256(query.encode()).hexdigest()[:16]
        self._cache[key] = (query_vec, result, score, time.monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

        logger.info("Cached research result (score=%d, entries=%d)", score, len(self._cache))
        return True
