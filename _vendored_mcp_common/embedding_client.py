"""Shared embedding client for cloto-mcp-servers.

Extracted from cpersona/server.py:146-301 in CScheduler v0.2 to allow
multiple MCP servers (CPersona, CScheduler, ...) to share a single
embedding implementation while talking to the same embedding HTTP server
(default port 8401) or an OpenAI-compatible API.

Each server owns its own EmbeddingClient instance; configuration is
injected via constructor arguments — env-var reading is the caller's
responsibility so that BC fallbacks (e.g. CPERSONA_EMBEDDING_*) live in
the relevant server's startup code.
"""

import hashlib
import logging
import struct
import time
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SIZE = 256
DEFAULT_CACHE_TTL = 300  # seconds
DEFAULT_TIMEOUT_SECS = 30


class EmbeddingClient:
    """Client for computing vector embeddings via HTTP or OpenAI-compatible API.

    Includes a TTL-based LRU cache for single-text queries (recall dedup).
    """

    def __init__(
        self,
        mode: str,
        http_url: str = "",
        api_key: str = "",
        api_url: str = "",
        model: str = "",
        cache_size: int = DEFAULT_CACHE_SIZE,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        timeout: int = DEFAULT_TIMEOUT_SECS,
    ):
        self.mode = mode
        self._http_url = http_url
        self._api_key = api_key
        self._api_url = api_url
        self._model = model
        self._client = None
        # LRU cache: key=text_hash, value=(embedding, timestamp)
        self._cache: OrderedDict[str, tuple[list[float], float]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl
        self._timeout = timeout
        self.cache_hits = 0
        self.cache_misses = 0

    async def initialize(self):
        """Create persistent HTTP client."""
        self._client = httpx.AsyncClient(timeout=self._timeout)
        logger.info(
            "EmbeddingClient initialized (mode=%s, cache=%d, ttl=%ds, timeout=%ds)",
            self.mode,
            self._cache_size,
            self._cache_ttl,
            self._timeout,
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
        # Move to end (most recently used)
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

        # Single-text cache path
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

        # Cache single-text results
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

        # L2-normalize for consistent cosine similarity via dot product
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
