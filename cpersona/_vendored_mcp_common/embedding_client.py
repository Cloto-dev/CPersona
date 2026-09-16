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


@dataclass(frozen=True)
class TokenInfo:
    """Where the backend's embedding window closes on one text.

    - ``count`` — tokens in the whole text, special tokens included.
    - ``window`` — tokens the backend embeds, special tokens included.
    - ``truncated`` — the text runs past the window.
    - ``window_end_char`` — ``text[:window_end_char]`` is what the vector
      represents; equal to ``len(text)`` when nothing was cut.
    """

    count: int
    window: int
    truncated: bool
    window_end_char: int


def _count_tokens_url(embed_url: str) -> str | None:
    """The ``/count_tokens`` route beside a configured ``/embed`` URL, keeping its query."""
    try:
        parts = urlsplit(embed_url)
    except ValueError:
        return None
    path = parts.path.rstrip("/")
    if not path.endswith("/embed"):
        return None
    return urlunsplit(parts._replace(path=path[: -len("embed")] + "count_tokens"))


def _is_int(value: object) -> bool:
    # bool is a subclass of int; JSON `true` is not a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_token_info(raw: object, texts: list[str]) -> list[TokenInfo]:
    """Refuse a report that is malformed or contradicts itself.

    The report decides where a caller splits stored text, so a wrong offset does not
    fail loudly — it silently places the tail of a record outside every node. Each
    entry therefore has to agree with its own text and with itself.
    """
    if not isinstance(raw, list):
        raise EmbeddingResponseError(f"token_info is {_describe(raw)}, expected a list")
    if len(raw) != len(texts):
        raise EmbeddingResponseError(f"token_info has {len(raw)} entries for {len(texts)} texts")
    out = []
    for position, (entry, text) in enumerate(zip(raw, texts)):
        if not isinstance(entry, dict):
            raise EmbeddingResponseError(
                f"token_info[{position}] is {_describe(entry)}, expected an object"
            )
        count, window, truncated, end = (
            entry.get("count"),
            entry.get("window"),
            entry.get("truncated"),
            entry.get("window_end_char"),
        )
        typed = _is_int(count) and _is_int(window) and _is_int(end)
        if not (typed and isinstance(truncated, bool)):
            raise EmbeddingResponseError(f"token_info[{position}] has a missing or mistyped field")
        if count < 0 or window < 1 or not 0 <= end <= len(text):
            raise EmbeddingResponseError(f"token_info[{position}] is out of range")
        if truncated != (count > window) or (not truncated and end != len(text)):
            raise EmbeddingResponseError(f"token_info[{position}] contradicts itself")
        out.append(TokenInfo(count=count, window=window, truncated=truncated, window_end_char=end))
    return out


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

    async def count_tokens(self, texts: list[str]) -> list[TokenInfo] | None:
        """How much of each text the backend's embedding window covers.

        Returns one :class:`TokenInfo` per text, or ``None`` when that is not known:
        no backend, a backend that cannot see its own tokens, a backend that predates
        the report, or a failed request. ``None`` means unknown and must never be read
        as "the text fits". :meth:`count_tokens_with_outcome` says which case it was.
        """
        result, _ = await self.count_tokens_with_outcome(texts)
        return result

    async def count_tokens_with_outcome(
        self, texts: list[str]
    ) -> tuple[list[TokenInfo] | None, EmbedOutcome]:
        """:meth:`count_tokens`, plus what happened.

        ``ok`` is true only when a report came back. The report comes from the
        server's ``/count_tokens`` route, next to ``/embed`` on the same host; only
        ``mode=http`` has one. It runs no model, so a caller can ask before deciding
        how to store a long text without paying for an embedding.
        """
        if self.mode != "http" or not self._client:
            unsupported = self.mode != "none" and self._client
            error = f"mode={self.mode} has no token report" if unsupported else None
            return None, EmbedOutcome(attempted=False, ok=False, error=error)
        url = _count_tokens_url(self._http_url)
        if url is None:
            return None, EmbedOutcome(
                attempted=False,
                ok=False,
                error=f"mode=http / {_safe_endpoint(self._http_url)} does not end in /embed, "
                "so the token report route cannot be derived from it",
            )
        try:
            response = await self._client.post(url, json={"texts": texts})
            response.raise_for_status()
            data = self._parse_within_budget(response)
            if not isinstance(data, dict):
                raise EmbeddingResponseError(
                    f"token report is {_describe(data)}, expected an object"
                )
            raw = data.get("token_info")
            if raw is None:
                return None, EmbedOutcome(
                    attempted=True,
                    ok=False,
                    error=(
                        f"mode=http / POST {_safe_endpoint(url)} reports that its provider "
                        "cannot see its tokens"
                    ),
                )
            return _validate_token_info(raw, texts), EmbedOutcome(attempted=True, ok=True)
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError) as e:
            safe = _safe_endpoint(url)
            # Either configured form of the URL can appear in the exception text, and
            # both carry the same userinfo and query string. The log gets the cleaned
            # line too: httpx puts the request URL in its own message.
            detail = (
                str(e).replace(url, safe).replace(self._http_url, _safe_endpoint(self._http_url))
            )
            hint = ""
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                hint = " (the server predates the token report)"
            evidence = f"mode=http / POST {safe} failed: {type(e).__name__}: {detail}{hint}"
            logger.warning("Token count request failed: %s", evidence)
            return None, EmbedOutcome(attempted=True, ok=False, error=evidence)

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
            # bug-425: build the evidence first and log that, rather than logging the
            # exception and sanitising only the value handed back. httpx puts the
            # request URL inside its own message, so an endpoint configured with
            # credentials in the userinfo or the query string reached the log verbatim
            # while the returned error was already clean -- the sanitiser existed and
            # this one line went around it.
            evidence = self._failure_evidence(e)
            logger.warning("Embedding request failed: %s", evidence)
            return None, EmbedOutcome(attempted=True, ok=False, error=evidence)

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
            # JSON has no integer ceiling, so a body may carry an int with more
            # digits than float64 can hold. `float()` answers that with
            # OverflowError, an ArithmeticError, which is not in the failure
            # boundary's except tuple and so escaped it — the caller's store path
            # aborted instead of storing the row without a vector. The verdict is
            # the same one a non-finite element gets: not a usable number.
            try:
                number = float(value)
            except OverflowError:
                raise EmbeddingResponseError(
                    f"embedding {position}[{index}] does not fit in float64"
                ) from None
            if not math.isfinite(number):
                raise EmbeddingResponseError(f"embedding {position}[{index}] is not finite")
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
        # Each item carries `index`, documented as its position in the input list;
        # arrival order is not promised. Appending in arrival order therefore hands
        # one text another text's vector whenever a backend answers out of order,
        # and nothing downstream can see it: the count still matches, every vector
        # is still well-formed, and the wrong vector is stored and searched for that
        # text with no signal. Place by the index instead, and refuse a response
        # whose indices are not exactly 0..n-1 — without them the correspondence
        # cannot be established at all, and guessing it is what caused this.
        raw: list[object] = [None] * len(items)
        claimed: set[int] = set()
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                raise EmbeddingResponseError(
                    f"embedding {position} is {_describe(item)}, expected an object"
                )
            slot = item.get("index")
            # bool is an int subclass, so JSON `true` would otherwise index slot 1.
            if isinstance(slot, bool) or not isinstance(slot, int):
                raise EmbeddingResponseError(
                    f"embedding {position} has index {_describe(slot)}, expected an integer"
                )
            if not 0 <= slot < len(items) or slot in claimed:
                raise EmbeddingResponseError(
                    f"embedding indices are not exactly 0..{len(items) - 1}: "
                    f"item at position {position} carries index {slot}"
                )
            claimed.add(slot)
            raw[slot] = item["embedding"]
        # Every index is unique and inside the range, and there are as many items as
        # slots, so no slot is left unfilled and no `None` reaches the validator.
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
