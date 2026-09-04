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
import math
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

# Limits on what an embedding backend is allowed to hand back. The backend sits
# outside this process's authentication boundary, so its response is parsed as
# untrusted input: a malformed one must fail the call, never travel far enough to
# be packed into a caller's storage.
#
# The values are chosen to sit far above real traffic so that no legitimate
# response is refused. The largest batch any known caller issues is 32 texts and
# the widest model in use is 1024-dimensional, which is ~2.6 MB of JSON; the
# budgets below leave more than an order of magnitude of headroom above that.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # 64 MiB
DEFAULT_MAX_DIMENSION = 16384
DEFAULT_MAX_BATCH_SIZE = 512


class EmbeddingResponseError(ValueError):
    """An embedding backend returned something that must not be used.

    Deliberately a :class:`ValueError`. Every caller of :meth:`EmbeddingClient.embed`
    already treats ``ValueError`` as "this call produced nothing", so a rejection
    here reaches them through the failure path they already have — as ``None`` plus
    an :class:`EmbedOutcome` carrying the reason — rather than as a new exception
    type they would have to learn to catch.
    """


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


def _describe(value: object) -> str:
    """Name a rejected value by type without quoting it back into the message."""
    return type(value).__name__


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
        expected_dimension: int = 0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_dimension: int = DEFAULT_MAX_DIMENSION,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
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
        self._max_response_bytes = max_response_bytes
        self._max_dimension = max_dimension
        self._max_batch_size = max_batch_size
        # The width this client will accept. A caller that knows the model states it
        # here; otherwise the first valid response fixes it for the life of the
        # instance, so a backend that silently changes model mid-process is caught
        # rather than writing two incompatible vector widths into one store. A
        # restart re-learns it, which is the intended way to change models.
        self._expected_dimension = expected_dimension

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

    # ------------------------------------------------------------------
    # Response boundary
    #
    # The backend is outside this process's authentication boundary, so its
    # response is validated in a fixed order before any of it reaches a caller:
    # byte budget, then parse, then shape, then batch cardinality, then
    # dimension, then finite numbers, then dimension consistency. The order
    # matters — each step is what makes the next one safe to attempt.
    # ------------------------------------------------------------------

    def _parse_within_budget(self, response) -> object:
        """Steps 1-2: refuse an oversized body, then parse it.

        The declared length is checked first, so a backend that announces a huge
        body is refused on its own word. The received length is checked too, since
        a declaration can be absent or false.

        Known limit: httpx has already buffered the body by the time this runs, so
        this bounds what gets *parsed* (where a JSON document becomes a much larger
        Python object graph), not what gets *received*. Bounding the receive side
        means streaming the response, which changes the request path; it is not
        done here.
        """
        limit = self._max_response_bytes
        if limit <= 0:
            return response.json()

        declared = None
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                raw_declared = headers.get("Content-Length")
            except AttributeError:
                raw_declared = None
            if raw_declared is not None:
                try:
                    declared = int(raw_declared)
                except (TypeError, ValueError):
                    declared = None
        if declared is not None and declared > limit:
            raise EmbeddingResponseError(
                f"embedding response declares {declared} bytes, over the {limit}-byte budget"
            )

        body = getattr(response, "content", None)
        if isinstance(body, (bytes, bytearray)) and len(body) > limit:
            raise EmbeddingResponseError(
                f"embedding response is {len(body)} bytes, over the {limit}-byte budget"
            )

        return response.json()

    def _validate_batch(self, raw: object, expected_count: int) -> list[list[float]]:
        """Steps 3-7: shape, cardinality, dimension, finite numbers, consistency.

        Returns the vectors as plain lists of floats. Raises
        :class:`EmbeddingResponseError` — a ``ValueError`` — for anything a caller
        must not store.

        An empty batch is not a rejection: it is the "a 2xx carried no embeddings"
        case that ``embed_with_outcome`` already reports as a failure with its own
        wording, and re-reporting it here would change which message a user reads.
        """
        if raw is None or raw == []:
            return raw  # type: ignore[return-value]

        if not isinstance(raw, list):
            raise EmbeddingResponseError(
                f"embedding response is {_describe(raw)}, expected a list of vectors"
            )

        if len(raw) > self._max_batch_size > 0:
            raise EmbeddingResponseError(
                f"embedding response carries {len(raw)} vectors, over the "
                f"{self._max_batch_size}-vector cap"
            )

        # One vector per input text. A short list is the dangerous case: zip() pairs
        # it silently against the inputs, so the wrong text keeps the wrong vector.
        if expected_count and len(raw) != expected_count:
            raise EmbeddingResponseError(
                f"embedding response carries {len(raw)} vectors for {expected_count} texts"
            )

        validated: list[list[float]] = []
        for position, vector in enumerate(raw):
            validated.append(self._validate_vector(vector, position))

        return validated

    def _validate_vector(self, vector: object, position: int) -> list[float]:
        """One vector: shape, width, and every element finite and numeric."""
        if not isinstance(vector, list):
            raise EmbeddingResponseError(
                f"embedding {position} is {_describe(vector)}, expected a list of numbers"
            )

        width = len(vector)
        if width == 0:
            raise EmbeddingResponseError(f"embedding {position} is empty")
        if width > self._max_dimension > 0:
            raise EmbeddingResponseError(
                f"embedding {position} has {width} dimensions, over the "
                f"{self._max_dimension}-dimension cap"
            )

        expected = self._expected_dimension
        if expected and width != expected:
            raise EmbeddingResponseError(
                f"embedding {position} has {width} dimensions, expected {expected}"
            )

        out: list[float] = []
        for index, value in enumerate(vector):
            # bool is a subclass of int, so JSON `true` would pass a bare numeric
            # check and pack as 1.0. Refuse it as the non-number it is.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingResponseError(
                    f"embedding {position}[{index}] is {_describe(value)}, expected a number"
                )
            number = float(value)
            if not math.isfinite(number):
                raise EmbeddingResponseError(
                    f"embedding {position}[{index}] is not finite"
                )
            # Finite in float64 is not enough: these are stored as float32, where
            # 1e300 becomes inf. `pack_embedding` is the packer that will run, so
            # ask it rather than a constant — and it raises OverflowError, which is
            # not a ValueError and would otherwise escape every caller's except.
            try:
                struct.pack("<f", number)
            except OverflowError:
                raise EmbeddingResponseError(
                    f"embedding {position}[{index}] does not fit in float32"
                ) from None
            out.append(number)

        # Learn the width from the first response this client accepts, so a backend
        # that changes model mid-process is refused rather than mixing two widths
        # into one store.
        if not self._expected_dimension:
            self._expected_dimension = width

        return out

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
        data = self._parse_within_budget(response)
        if not isinstance(data, dict):
            raise EmbeddingResponseError(
                f"embedding response is {_describe(data)}, expected an object"
            )
        return self._validate_batch(data.get("embeddings"), len(texts))

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
        data = self._parse_within_budget(response)
        if not isinstance(data, dict):
            raise EmbeddingResponseError(
                f"embedding response is {_describe(data)}, expected an object"
            )
        items = data["data"]
        if not isinstance(items, list):
            raise EmbeddingResponseError(
                f"embedding response `data` is {_describe(items)}, expected a list"
            )
        raw = []
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                raise EmbeddingResponseError(
                    f"embedding {position} is {_describe(item)}, expected an object"
                )
            raw.append(item["embedding"])
        embeddings = self._validate_batch(raw, len(texts))

        # L2-normalize for consistent cosine similarity via dot product. Every
        # element is already known to be a finite number, so the norm cannot come
        # back as NaN and quietly turn a whole vector into NaN by division.
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
