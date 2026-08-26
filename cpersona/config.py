"""Environment variable configuration for CPersona."""

import logging
import os

logger = logging.getLogger(__name__)


# bug-133: malformed numeric settings fall back independently instead of
# preventing the server from importing.
def _parse_int(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("bug-133: invalid int for %s=%r; using default %r", env_key, raw, default)
        return default


def _parse_float(env_key: str, default: float) -> float:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning("bug-133: invalid float for %s=%r; using default %r", env_key, raw, default)
        return default


# C10: public wrappers so entrypoint env reads that live outside config.py
# (server.py's HTTP port and embedding timeout) can route through the same
# bug-133 warn-and-fall-back-to-default path instead of a bare int()/float()
# that would raise an opaque ValueError and abort startup before listening.
def parse_int(env_key: str, default: int) -> int:
    return _parse_int(env_key, default)


def parse_float(env_key: str, default: float) -> float:
    return _parse_float(env_key, default)


DB_PATH = os.environ.get("CPERSONA_DB_PATH", "data/cpersona.db")
# bug-054: optional confinement root for export_memories' caller-supplied
# output_path. When set, an export's resolved realpath MUST stay within this
# directory. When unset (default), export still rejects '..' traversal but allows
# an absolute/relative path — the readOnlyHint=False/destructiveHint=True tool
# annotation makes the host confirm the write. Set this for a hardened deployment.
EXPORT_DIR = os.environ.get("CPERSONA_EXPORT_DIR", "")
MAX_IMPORT_BYTES = _parse_int("CPERSONA_MAX_IMPORT_BYTES", 104857600)
# bug-085: MAX_MEMORIES is the vector retriever's SCAN WINDOW — how many of the
# newest rows it fetches and cosine-ranks per recall — not a response size (that
# is the per-call `limit`, clamped in the handlers). It is the single knob that
# bounds vector-recall reach: rows older than the window are invisible to the
# vector retriever, so the default must comfortably exceed a real corpus.
# Benchmarks on larger corpora raise it via the env var instead of patching code.
MAX_MEMORIES = _parse_int("CPERSONA_MAX_MEMORIES", 10000)
# 16000, raised from 2000, because the old cap was destroying the part
# of a memory that is worth the most. Long records put the conclusion first and
# the hard-won detail last, so cutting the tail on every write removed exactly
# what nothing else records — and no later line can restore it (the 2.6 tree
# splits what is stored; it cannot recover what was never stored). A measurement
# of the live corpus found 124 of 1625 rows (7.6%) sitting exactly at 2000
# characters: scars, not coincidences.
#
# The number is derived, not picked: 1.5x the longest record that ever existed
# (a 10,432-character episode) and 8x the old cap, while no row in the corpus
# exceeds 8000 — so it refuses no honest write, and still stops a runaway blob
# with 60x of headroom. The read side does NOT follow it up: each full-text read
# path keeps its own fixed character budget pinned at what its worst case used
# to be — get_contents at GET_CONTENTS_MAX_CHARS (40000 = 20 refs x 2000),
# recall(full_content=true) at server.RECALL_FULL_CONTENT_MAX_CHARS (200000 =
# limit 100 x 2000; bug-211 — this path shipped unbudgeted at first, so the
# claim here was briefly true only of get_contents), and the two list tools at
# admin_handlers.LIST_MEMORIES_MAX_CHARS / LIST_EPISODES_MAX_CHARS (1,000,000
# and 800,000; bug-255 — the second wave of the same omission). None of them is
# derived from this constant, so a batch cannot grow just because this number did. The one
# documented exception is a single row that alone exceeds the budget, which is
# still returned whole rather than made unreachable; at this default no such
# row can exist.
#
# The embedding window is unchanged (512 tokens), so text past the window is
# still invisible to the vector retriever. It is NOT invisible to search: the
# FTS triggers index the stored content in full, so the tail becomes reachable
# through the lexical channel the moment it is stored. Closing the vector gap is
# the 2.6 tree's job.
MAX_CONTENT_LENGTH = _parse_int("CPERSONA_MAX_CONTENT_LENGTH", 16000)
# The profile owns its ceiling, because the two rows are bounded for
# different reasons and only one of them is bounded by its cap alone.
#
# A memory is preview-trimmed on the way out (RECALL_PREVIEW_CHARS) and its full
# text stays reachable through `ref` + get_contents, so its cap governs what is
# stored, not what a recall response costs. The profile is injected as the id=-1
# sentinel row, and _apply_preview deliberately skips rows with no `ref` —
# trimming them would make their full content permanently unreachable (bug-117).
# So the profile's cap is the ONLY thing bounding it, and it is paid in EVERY
# recall response.
#
# Sharing one constant meant a future relaxation of the memory cap (the 2.6
# tree) would silently unbound the profile too. The number is unchanged —
# 2000 is right for the profile, and small is the point — only its ownership is.
MAX_PROFILE_LENGTH = _parse_int("CPERSONA_MAX_PROFILE_LENGTH", 2000)
# 2.5.2b1 (audit C12): the JSON sidecar fields — source and metadata — reach the
# row with no bound at all, so one call could park an arbitrarily large blob per
# memory (content has been capped since 2.1; these never were). They are cheap
# structured annotations, not payload: the cap is generous enough that no honest
# producer meets it, and truncation is not an option because half a JSON document
# is not a JSON document — the write is refused instead (result='rejected').
#
# bug-176: "the write path" here means the seams that accept content from OUTSIDE
# this database — store, update_memory, the episode prepare, and (bug-221) the
# import, whose file may have been produced by another DB, an older version or a
# hand edit. merge_memories stays outside it: it moves rows that this database
# already accepted under some earlier cap, and re-sanitising an intra-DB move would
# silently rewrite content the operator never edited. check_health's
# oversized_content check reports those after the fact, which is the right division
# — bound what arrives from outside, detect what is being moved within.
MAX_METADATA_LENGTH = _parse_int("CPERSONA_MAX_METADATA_LENGTH", 8000)
FTS_ENABLED = os.environ.get("CPERSONA_FTS_ENABLED", "true").lower() == "true"

# Embedding env: the server-specific CPERSONA_* key takes precedence, then the
# generic key shared across Cloto MCP servers (matches the CScheduler convention
# and the marketplace catalog, which sets EMBEDDING_MODE / EMBEDDING_HTTP_URL).
# Without the generic fallback a catalog-installed cpersona ran with embeddings
# silently off (recall degraded to FTS-only) — bug-001.
EMBEDDING_MODE = os.environ.get("CPERSONA_EMBEDDING_MODE") or os.environ.get("EMBEDDING_MODE", "none")
EMBEDDING_URL = os.environ.get("CPERSONA_EMBEDDING_URL") or os.environ.get("EMBEDDING_HTTP_URL", "")
EMBEDDING_API_KEY = os.environ.get("CPERSONA_EMBEDDING_API_KEY") or os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_API_URL = os.environ.get("CPERSONA_EMBEDDING_API_URL") or os.environ.get("EMBEDDING_API_URL", "https://api.openai.com/v1/embeddings")
EMBEDDING_MODEL = os.environ.get("CPERSONA_EMBEDDING_MODEL") or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")

VECTOR_MIN_SIMILARITY = _parse_float("CPERSONA_VECTOR_MIN_SIMILARITY", 0.3)

EMBEDDING_CACHE_SIZE = _parse_int("CPERSONA_EMBEDDING_CACHE_SIZE", 256)
EMBEDDING_CACHE_TTL = _parse_int("CPERSONA_EMBEDDING_CACHE_TTL", 300)

# Degraded-advisory runtime guard (v2.4.33): when embeddings are unavailable at runtime
# (mode=none, or a configured http endpoint is unreachable) do_recall attaches an
# `advisory` to its response so the calling agent can self-report the degradation
# instead of silently serving keyword/FTS-only recall. On by default; opt out for a
# deliberate FTS-only deployment. See health.py + docs/DEGRADED_ADVISORY_DESIGN.md.
DEGRADED_ADVISORY_ENABLED = os.environ.get("CPERSONA_DEGRADED_ADVISORY", "true").lower() == "true"

TASK_QUEUE_ENABLED = os.environ.get("CPERSONA_TASK_QUEUE_ENABLED", "true").lower() == "true"

CONFIDENCE_ENABLED = os.environ.get("CPERSONA_CONFIDENCE_ENABLED", "false").lower() == "true"
COSINE_FLOOR = _parse_float("CPERSONA_COSINE_FLOOR", 0.20)
COSINE_CEIL = _parse_float("CPERSONA_COSINE_CEIL", 0.75)
DECAY_RATE = _parse_float("CPERSONA_DECAY_RATE", 0.005)
DECAY_FLOOR = _parse_float("CPERSONA_DECAY_FLOOR", 0.3)
DECAY_CEIL = _parse_float("CPERSONA_DECAY_CEIL", 0.5)
RECALL_BOOST = _parse_float("CPERSONA_RECALL_BOOST", 0.02)
BOOST_DECAY_RATE = _parse_float("CPERSONA_BOOST_DECAY_RATE", 0.002)
MIN_TIME_RANGE_HOURS = _parse_float("CPERSONA_MIN_TIME_RANGE_HOURS", 24.0)
REFERENCE_HOURS = _parse_float("CPERSONA_REFERENCE_HOURS", 168.0)
RESOLVED_DECAY_FACTOR = _parse_float("CPERSONA_RESOLVED_DECAY_FACTOR", 0.3)
RECENT_RECALL_PENALTY = _parse_float("CPERSONA_RECENT_RECALL_PENALTY", 0.7)
RECENT_RECALL_WINDOW_MIN = _parse_float("CPERSONA_RECENT_RECALL_WINDOW_MIN", 5.0)
TASK_MAX_RETRIES = _parse_int("CPERSONA_TASK_MAX_RETRIES", 3)
TASK_RETRY_DELAY = _parse_int("CPERSONA_TASK_RETRY_DELAY", 30)

VECTOR_SEARCH_MODE = os.environ.get("CPERSONA_VECTOR_SEARCH_MODE", "local")
# bug-033: dedicated per-call timeout for the remote /search POST on the recall
# hot path. Without it the POST inherits the embed client's 30s DEFAULT_TIMEOUT_SECS,
# so a hung/flapping endpoint blocks every recall ~30s before falling back to local.
# Short enough to fail over fast, long enough for a healthy remote search.
REMOTE_SEARCH_TIMEOUT_SECS = _parse_float("CPERSONA_REMOTE_SEARCH_TIMEOUT_SECS", 5.0)
# #361 item (6): the sibling of the above for the /index POST on the WRITE hot
# path. Every other remote call states its own short deadline (probe 3s, search
# 5s); the index push was the one that silently inherited the embed client's 30s
# default, so the read path was better protected than the write path against the
# same hung endpoint. Slightly longer than search: indexing does the embedding
# work server-side, and a failure here is non-fatal (embedded=false) rather than
# a fallback to another retriever.
REMOTE_INDEX_TIMEOUT_SECS = _parse_float("CPERSONA_REMOTE_INDEX_TIMEOUT_SECS", 10.0)
STORE_BLOB = os.environ.get("CPERSONA_STORE_BLOB", "true").lower() == "true"


def local_blobs_stored(vector_search_mode: str, store_blob: bool) -> bool:
    """Whether a write leaves an embedding BLOB in the local row.

    bug-182: this rule used to exist only as an inline condition on the write
    paths, so the maintenance layer had no way to know that a NULL embedding was
    the *configured* steady state rather than a failed pipeline — it read the
    NULLs as a broken embedding pipeline and re-embedded the corpus on every
    fix run. One definition now, called by the write gate and by the checks that
    interpret its result.

    Takes the two values as arguments rather than reading the module globals so
    each caller passes the copy it actually stores by (and a test that patches
    one module's copy still steers that module alone).
    """
    return vector_search_mode == "local" or store_blob

AUTO_CALIBRATE = os.environ.get("CPERSONA_AUTO_CALIBRATE", "false").lower() == "true"
CALIBRATE_SAMPLE_SIZE = _parse_int("CPERSONA_CALIBRATE_SAMPLE_SIZE", 200)
# bug-053: hard upper bound on the calibration sample. sample_size is a
# caller-supplied MCP tool parameter that feeds both a LIMIT scan and an O(n^2)
# dense cosine matrix (vecs @ vecs.T) plus np.triu_indices — an unclamped large
# value (e.g. 20000) allocates multi-GB transient arrays and OOM-kills the whole
# server process, taking recall down for every agent on the shared connection.
# Mirrors the _clamp_limit discipline already applied to the recall/list handlers.
CALIBRATE_MAX_SAMPLE = max(1, _parse_int("CPERSONA_CALIBRATE_MAX_SAMPLE", 2000))
CALIBRATE_Z_FACTOR = _parse_float("CPERSONA_CALIBRATE_Z_FACTOR", 1.0)
CALIBRATE_FLOOR = _parse_float("CPERSONA_CALIBRATE_FLOOR", 0.05)
# v2.4.24 — calibration method. "percentile" sets the threshold at a quantile of
# the random-pair (null) similarity distribution; "zscore" uses mean + z*std.
# Both place the floor ABOVE the null mean so unrelated pairs are rejected — the
# pre-2.4.24 zscore formula subtracted (mean - z*std), placing the floor below
# the null mean and admitting the majority of unrelated pairs (topic drift).
# bug-231: one definition of the method enum. The handler validates against it and the
# calibrate_threshold JSON Schema publishes it, so a spelling the schema advertises and
# the handler rejects (or the reverse) cannot drift into existence — the same
# single-definition treatment CANONICAL_SOURCE_TYPES got for the store schema.
CALIBRATE_METHODS = ("separation", "percentile", "zscore")
CALIBRATE_METHOD = os.environ.get("CPERSONA_CALIBRATE_METHOD", "separation")
CALIBRATE_PERCENTILE = _parse_float("CPERSONA_CALIBRATE_PERCENTILE", 0.95)
# v2.4.24 — method="separation" positive proxy: memories stored within this window
# (minutes) are treated as same-session ≈ related, a representative (non-extreme)
# proxy for the two-population operating-point search. Falls back to nearest-neighbour
# when too few temporally-adjacent pairs exist.
CALIBRATE_TEMPORAL_WINDOW_MIN = _parse_float("CPERSONA_CALIBRATE_TEMPORAL_WINDOW_MIN", 30.0)
# v2.4.24 — recalibrate on embedding-model change. The calibration is fingerprinted
# by embedding dimension (robust to a missing/stale EMBEDDING_MODEL label); when the
# live corpus dimension differs from the persisted one, the threshold is recomputed
# at startup even if AUTO_CALIBRATE is off. Catches silent jina(768d)->bge-m3(1024d)
# style swaps that would otherwise leave a stale, mis-scaled threshold in place.
CALIBRATE_ON_MODEL_CHANGE = os.environ.get("CPERSONA_CALIBRATE_ON_MODEL_CHANGE", "true").lower() == "true"

# v2.4.26 — post-fusion quality-gate calibration. The fused-score
# (RSF/RRF) quality gate is calibrated by simulate-query separation: sample stored
# memories as pseudo-queries, run the active fusion pipeline, and separate the fused
# scores of temporally-adjacent (same-session ≈ related) rows from unrelated rows.
# This replaces the pool-size heuristic _adaptive_min_score, which never used the
# calibrated distribution and so left rsf/rrf precision uncalibrated. Falls back to
# the heuristic when disabled or when too few samples exist.
FUSED_GATE_ENABLED = os.environ.get("CPERSONA_FUSED_GATE_ENABLED", "true").lower() == "true"
# Number of pseudo-queries sampled at calibration time (each runs one fusion recall,
# so this bounds calibration cost — an offline / startup event, not per-recall).
FUSED_GATE_SAMPLE_QUERIES = max(1, _parse_int("CPERSONA_FUSED_GATE_SAMPLE_QUERIES", 40))
# Independent calibration draws per gate; the applied threshold is their median
# of several. The separation objective is multimodal over a real corpus, so a
# single draw's argmax can hand the gate to a minor mode — production shipped a
# 0.1544 gate that 21 probe draws never reproduced. Total calibration cost is
# DRAWS * SAMPLE_QUERIES recalls, still an offline / startup event.
FUSED_GATE_CALIBRATION_DRAWS = max(1, _parse_int("CPERSONA_FUSED_GATE_CALIBRATION_DRAWS", 5))
# knob 3 — the precision point. The calibrated separation curve is data-derived; this
# is the single policy choice of where to sit on it. strict / balanced / lenient map to
# a specificity weight beta in _separation_threshold (maximise sensitivity +
# beta*specificity): strict=2.0 (fewer contaminants, more misses), balanced=1.0
# (Youden's J), lenient=0.5 (fewer misses, more contaminants). A raw
# CPERSONA_FUSED_GATE_BETA overrides the named level.
RECALL_PRECISION = os.environ.get("CPERSONA_RECALL_PRECISION", "balanced").lower()
_PRECISION_BETA = {"strict": 2.0, "balanced": 1.0, "lenient": 0.5}
FUSED_GATE_BETA = _parse_float(
    "CPERSONA_FUSED_GATE_BETA", _PRECISION_BETA.get(RECALL_PRECISION, 1.0)
)

# Autocut (v2.4 / v2.4.13: relative gap ratio, enabled by default)
AUTOCUT_ENABLED = os.environ.get("CPERSONA_AUTOCUT_ENABLED", "true").lower() == "true"
AUTOCUT_MIN_GAP_RATIO = _parse_float("CPERSONA_AUTOCUT_MIN_GAP_RATIO", 0.15)
# v2.4.25: minimum result count before autocut engages. RSF min-max normalization
# forces the lowest-scoring row to 0.0, so any small result set carries an
# artificial full-scale gap that autocut would cut to a single row (the 2-item
# over-cut that blocked making rsf the default). Below this floor, recall is too
# small for a "gap" to be meaningful — keep every row. Hard floor of 2 keeps the
# gap computation well-defined.
AUTOCUT_MIN_RESULTS = max(2, _parse_int("CPERSONA_AUTOCUT_MIN_RESULTS", 3))

# Episode boundary soft penalty (L3 — v2.4.14)
# Memories created before the latest archived episode are penalised by a
# multiplicative factor so cross-session noise is filtered by the quality gate.
EPISODE_PENALTY_ENABLED = os.environ.get("CPERSONA_EPISODE_PENALTY_ENABLED", "true").lower() == "true"
EPISODE_DECAY_RATE = _parse_float("CPERSONA_EPISODE_DECAY_RATE", 0.01)
EPISODE_DECAY_FLOOR = _parse_float("CPERSONA_EPISODE_DECAY_FLOOR", 0.5)

RECALL_MODE = os.environ.get("CPERSONA_RECALL_MODE", "rrf")
# 2.5.0: MCP-boundary preview tier for recall responses. Message
# content longer than this many characters is returned as a pure prefix (plus
# content_truncated/content_len markers) unless the caller opts out with
# full_content=true; full text is re-fetchable via get_contents(refs). 0
# disables trimming. Boundary-layer only — library callers (do_recall) always
# receive full content, same layering as the limit cap.
RECALL_PREVIEW_CHARS = _parse_int("CPERSONA_RECALL_PREVIEW_CHARS", 500)
RRF_K = max(1, _parse_int("CPERSONA_RRF_K", 60))
RRF_THRESHOLD_FACTOR = _parse_float("CPERSONA_RRF_THRESHOLD_FACTOR", 0.5)
# v2.4.12: Max theoretical _rrf_score ≈ num_retrievers / (RRF_K + 1), with 3
# retrievers (vector, FTS episodes, FTS memories) at rank 0 each. Used by
# _apply_quality_gate to map cosine-scale min_score (0.2–1.0) into the RRF
# score's tight range (0–~0.05).
RRF_MAX_SCALE = 3.0 / (RRF_K + 1)

# OAuth 2.0 protected resource discovery (RFC 9728, docs/OAUTH_DESIGN.md §7).
# Discovery only: nothing here verifies a token. The metadata document and the
# 401's resource_metadata parameter are what let a conformant client find the
# authorization server it should talk to; without them the client finds nothing
# and falls through to asking a human to type in a client id.
#
# The feature is off unless OAUTH_RESOURCE is non-empty AND at least one
# authorization server is listed. Off means byte-identical responses to today.
OAUTH_RESOURCE = os.environ.get("CPERSONA_OAUTH_RESOURCE", "")
# Whitespace- or comma-separated issuer URLs.
OAUTH_AUTHORIZATION_SERVERS = os.environ.get("CPERSONA_OAUTH_AUTHORIZATION_SERVERS", "")
# Advertised on the 401. Measured (docs/OAUTH_DESIGN.md §2): the client sends
# back exactly the scope the resource server asked for, so this value is the
# lever over scope design — an empty one gives it away.
OAUTH_SCOPES = os.environ.get("CPERSONA_OAUTH_SCOPES", "cpersona:read cpersona:write")


# Transport. Unlike everything above this is read at CALL time, not at import:
# `main()` reads it after the ACL file, the preflight and the embedding client
# are already up, and the tests that cover those paths set it with
# `monkeypatch.setenv`. A module constant would freeze the import-time value and
# make every one of them silently test the default.
def transport() -> str:
    """The configured transport name, unvalidated (``main`` rejects unknown ones)."""
    return os.environ.get("CPERSONA_TRANSPORT", "stdio")


def shared_transport() -> bool:
    """True when ONE process serves SEVERAL client sessions.

    The streamable-HTTP transport runs ``stateless=True``: a single process
    answers every connected client and no session survives a request, so any
    process-level "already told them" state is shared by callers that never saw
    each other's responses (bug-251). Under stdio the process IS the session.

    Deliberately not spelled `transport() == "streamable-http"` at the call
    sites: this asks "is my process-level state shared", which happens to
    coincide with "am I serving HTTP" only because the HTTP mode is stateless.
    A sessionful HTTP mode would keep the second answer and change this one.
    """
    return transport() == "streamable-http"
