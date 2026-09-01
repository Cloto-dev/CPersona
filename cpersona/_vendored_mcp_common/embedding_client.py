"""Shared embedding client for Cloto MCP servers.

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
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SIZE = 256
DEFAULT_CACHE_TTL = 300  # seconds
DEFAULT_TIMEOUT_SECS = 30


@dataclass(frozen=True)
class EmbedOutcome:
    """What one :meth:`EmbeddingClient.embed_with_outcome` call did.

    ``embed()`` collapses "no backend configured" and "the backend was there and
    failed" into the same ``None``, which is enough to fall back on and not enough
    to tell a user what to fix. A caller that wants to report the difference reads
    this instead; the return value of ``embed()`` is unchanged, so no existing
    caller has to.

    - ``attempted`` — a request was actually issued. False for an unconfigured
      client and for a cache hit, so a caller can tell a served-from-cache success
      apart from a round trip.
    - ``ok`` — usable embeddings came back (a cache hit is ``ok`` without being
      ``attempted``).
    - ``error`` — safe evidence for why this call produced nothing, when that is
      knowable. Present for a failed request and for a misconfigured mode; absent
      when there is simply no backend to call, because that is not a failure.

    The evidence is built from the client's own configuration and the exception
    type, never from request headers, so an API key cannot travel in it. The
    endpoint keeps its scheme, host and path and loses any userinfo and query
    string, which is where a credential would be if one were in a URL at all.
    """

    attempted: bool
    ok: bool
    error: str | None = None


def _safe_endpoint(url: str) -> str:
    """Strip credentials and query parameters from a URL before it is reported."""
    if not url:
        return "<unset>"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparsable>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", "")) or "<unset>"


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

        Returns ``None`` for every unsuccessful case, as it always has. A caller
        that needs to know *which* unsuccessful case it was calls
        :meth:`embed_with_outcome` instead — this method delegates to it and
        discards the second value.
        """
        result, _ = await self.embed_with_outcome(texts)
        return result

    async def embed_with_outcome(
        self, texts: list[str]
    ) -> tuple[list[list[float]] | None, EmbedOutcome]:
        """:meth:`embed`, plus what happened — see :class:`EmbedOutcome`.

        The outcome describes *this* call and is returned to *this* caller rather
        than stored on the client, so concurrent embeds cannot read each other's
        result, and a caller never has to re-issue a request to find out why the
        first one failed.
        """
        if self.mode == "none" or not self._client:
            return None, EmbedOutcome(attempted=False, ok=False)

        # Single-text cache path
        if len(texts) == 1:
            cached = self._cache_get(texts[0])
            if cached is not None:
                self.cache_hits += 1
                return [cached], EmbedOutcome(attempted=False, ok=True)
            self.cache_misses += 1

        try:
            if self.mode == "http":
                result = await self._embed_via_http(texts)
            elif self.mode == "api":
                result = await self._embed_via_api(texts)
            else:
                logger.warning("Unknown embedding mode: %s", self.mode)
                return None, EmbedOutcome(
                    attempted=False,
                    ok=False,
                    error=f"mode={self.mode} is not a supported embedding mode",
                )
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError) as e:
            logger.warning("Embedding request failed: %s", e)
            return None, EmbedOutcome(attempted=True, ok=False, error=self._failure_evidence(e))

        if not result:
            # A 2xx that carried no usable embeddings. Reported as a failure because
            # that is what it is for the caller, and because a health check that
            # re-probed the endpoint would see the same 2xx and call it healthy.
            #
            # `result` is returned unchanged rather than normalized to None: an empty
            # `embeddings` list used to reach the caller as `[]` and a missing key as
            # `None`, and both are falsy, so no caller can distinguish them — but
            # substituting one for the other would still be a behaviour change, and
            # this method's whole claim is that it makes none.
            return result, EmbedOutcome(
                attempted=True,
                ok=False,
                error=(
                    f"mode={self.mode} / POST {_safe_endpoint(self._endpoint())} "
                    f"returned no embeddings"
                ),
            )

        # Cache single-text results
        if len(texts) == 1 and len(result) == 1:
            self._cache_put(texts[0], result[0])

        return result, EmbedOutcome(attempted=True, ok=True)

    def _endpoint(self) -> str:
        """The URL this client posts to under its current mode."""
        return self._api_url if self.mode == "api" else self._http_url

    def _failure_evidence(self, exc: Exception) -> str:
        """One safe line naming the mode, the endpoint and the failure.

        The exception text is included because it is what distinguishes a refused
        connection from a timeout from a 500, which is the whole value of the
        evidence. Any occurrence of the raw endpoint inside it is replaced by the
        stripped form first, so a credential embedded in a configured URL does not
        re-enter through the message.
        """
        raw = self._endpoint()
        safe = _safe_endpoint(raw)
        detail = str(exc)
        if raw:
            detail = detail.replace(raw, safe)
        return f"mode={self.mode} / POST {safe} failed: {type(exc).__name__}: {detail}"

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
