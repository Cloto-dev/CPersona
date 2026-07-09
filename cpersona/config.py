"""Environment variable configuration for CPersona."""

import os

DB_PATH = os.environ.get("CPERSONA_DB_PATH", "data/cpersona.db")
# bug-054: optional confinement root for export_memories' caller-supplied
# output_path. When set, an export's resolved realpath MUST stay within this
# directory. When unset (default), export still rejects '..' traversal but allows
# an absolute/relative path — the readOnlyHint=False/destructiveHint=True tool
# annotation makes the host confirm the write. Set this for a hardened deployment.
EXPORT_DIR = os.environ.get("CPERSONA_EXPORT_DIR", "")
MAX_MEMORIES = int(os.environ.get("CPERSONA_MAX_MEMORIES", "500"))
MAX_CONTENT_LENGTH = int(os.environ.get("CPERSONA_MAX_CONTENT_LENGTH", "2000"))
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

VECTOR_MIN_SIMILARITY = float(os.environ.get("CPERSONA_VECTOR_MIN_SIMILARITY", "0.3"))

EMBEDDING_CACHE_SIZE = int(os.environ.get("CPERSONA_EMBEDDING_CACHE_SIZE", "256"))
EMBEDDING_CACHE_TTL = int(os.environ.get("CPERSONA_EMBEDDING_CACHE_TTL", "300"))

# Degraded-advisory runtime guard (v2.4.33): when embeddings are unavailable at runtime
# (mode=none, or a configured http endpoint is unreachable) do_recall attaches an
# `advisory` to its response so the calling agent can self-report the degradation
# instead of silently serving keyword/FTS-only recall. On by default; opt out for a
# deliberate FTS-only deployment. See health.py + docs/DEGRADED_ADVISORY_DESIGN.md.
DEGRADED_ADVISORY_ENABLED = os.environ.get("CPERSONA_DEGRADED_ADVISORY", "true").lower() == "true"

TASK_QUEUE_ENABLED = os.environ.get("CPERSONA_TASK_QUEUE_ENABLED", "true").lower() == "true"

CONFIDENCE_ENABLED = os.environ.get("CPERSONA_CONFIDENCE_ENABLED", "false").lower() == "true"
COSINE_FLOOR = float(os.environ.get("CPERSONA_COSINE_FLOOR", "0.20"))
COSINE_CEIL = float(os.environ.get("CPERSONA_COSINE_CEIL", "0.75"))
DECAY_RATE = float(os.environ.get("CPERSONA_DECAY_RATE", "0.005"))
DECAY_FLOOR = float(os.environ.get("CPERSONA_DECAY_FLOOR", "0.3"))
DECAY_CEIL = float(os.environ.get("CPERSONA_DECAY_CEIL", "0.5"))
RECALL_BOOST = float(os.environ.get("CPERSONA_RECALL_BOOST", "0.02"))
BOOST_DECAY_RATE = float(os.environ.get("CPERSONA_BOOST_DECAY_RATE", "0.002"))
MIN_TIME_RANGE_HOURS = float(os.environ.get("CPERSONA_MIN_TIME_RANGE_HOURS", "24"))
REFERENCE_HOURS = float(os.environ.get("CPERSONA_REFERENCE_HOURS", "168"))
RESOLVED_DECAY_FACTOR = float(os.environ.get("CPERSONA_RESOLVED_DECAY_FACTOR", "0.3"))
RECENT_RECALL_PENALTY = float(os.environ.get("CPERSONA_RECENT_RECALL_PENALTY", "0.7"))
RECENT_RECALL_WINDOW_MIN = float(os.environ.get("CPERSONA_RECENT_RECALL_WINDOW_MIN", "5"))
TASK_MAX_RETRIES = int(os.environ.get("CPERSONA_TASK_MAX_RETRIES", "3"))
TASK_RETRY_DELAY = int(os.environ.get("CPERSONA_TASK_RETRY_DELAY", "30"))

VECTOR_SEARCH_MODE = os.environ.get("CPERSONA_VECTOR_SEARCH_MODE", "local")
# bug-033: dedicated per-call timeout for the remote /search POST on the recall
# hot path. Without it the POST inherits the embed client's 30s DEFAULT_TIMEOUT_SECS,
# so a hung/flapping endpoint blocks every recall ~30s before falling back to local.
# Short enough to fail over fast, long enough for a healthy remote search.
REMOTE_SEARCH_TIMEOUT_SECS = float(os.environ.get("CPERSONA_REMOTE_SEARCH_TIMEOUT_SECS", "5.0"))
STORE_BLOB = os.environ.get("CPERSONA_STORE_BLOB", "true").lower() == "true"

AUTO_CALIBRATE = os.environ.get("CPERSONA_AUTO_CALIBRATE", "false").lower() == "true"
CALIBRATE_SAMPLE_SIZE = int(os.environ.get("CPERSONA_CALIBRATE_SAMPLE_SIZE", "200"))
# bug-053: hard upper bound on the calibration sample. sample_size is a
# caller-supplied MCP tool parameter that feeds both a LIMIT scan and an O(n^2)
# dense cosine matrix (vecs @ vecs.T) plus np.triu_indices — an unclamped large
# value (e.g. 20000) allocates multi-GB transient arrays and OOM-kills the whole
# server process, taking recall down for every agent on the shared connection.
# Mirrors the _clamp_limit discipline already applied to the recall/list handlers.
CALIBRATE_MAX_SAMPLE = max(1, int(os.environ.get("CPERSONA_CALIBRATE_MAX_SAMPLE", "2000")))
CALIBRATE_Z_FACTOR = float(os.environ.get("CPERSONA_CALIBRATE_Z_FACTOR", "1.0"))
CALIBRATE_FLOOR = float(os.environ.get("CPERSONA_CALIBRATE_FLOOR", "0.05"))
# v2.4.24 — calibration method. "percentile" sets the threshold at a quantile of
# the random-pair (null) similarity distribution; "zscore" uses mean + z*std.
# Both place the floor ABOVE the null mean so unrelated pairs are rejected — the
# pre-2.4.24 zscore formula subtracted (mean - z*std), placing the floor below
# the null mean and admitting the majority of unrelated pairs (topic drift).
CALIBRATE_METHOD = os.environ.get("CPERSONA_CALIBRATE_METHOD", "separation")
CALIBRATE_PERCENTILE = float(os.environ.get("CPERSONA_CALIBRATE_PERCENTILE", "0.95"))
# v2.4.24 — method="separation" positive proxy: memories stored within this window
# (minutes) are treated as same-session ≈ related, a representative (non-extreme)
# proxy for the two-population operating-point search. Falls back to nearest-neighbour
# when too few temporally-adjacent pairs exist.
CALIBRATE_TEMPORAL_WINDOW_MIN = float(os.environ.get("CPERSONA_CALIBRATE_TEMPORAL_WINDOW_MIN", "30"))
# v2.4.24 — recalibrate on embedding-model change. The calibration is fingerprinted
# by embedding dimension (robust to a missing/stale EMBEDDING_MODEL label); when the
# live corpus dimension differs from the persisted one, the threshold is recomputed
# at startup even if AUTO_CALIBRATE is off. Catches silent jina(768d)->bge-m3(1024d)
# style swaps that would otherwise leave a stale, mis-scaled threshold in place.
CALIBRATE_ON_MODEL_CHANGE = os.environ.get("CPERSONA_CALIBRATE_ON_MODEL_CHANGE", "true").lower() == "true"

# v2.4.26 — post-fusion quality-gate calibration (Goal #132). The fused-score
# (RSF/RRF) quality gate is calibrated by simulate-query separation: sample stored
# memories as pseudo-queries, run the active fusion pipeline, and separate the fused
# scores of temporally-adjacent (same-session ≈ related) rows from unrelated rows.
# This replaces the pool-size heuristic _adaptive_min_score, which never used the
# calibrated distribution and so left rsf/rrf precision uncalibrated. Falls back to
# the heuristic when disabled or when too few samples exist.
FUSED_GATE_ENABLED = os.environ.get("CPERSONA_FUSED_GATE_ENABLED", "true").lower() == "true"
# Number of pseudo-queries sampled at calibration time (each runs one fusion recall,
# so this bounds calibration cost — an offline / startup event, not per-recall).
FUSED_GATE_SAMPLE_QUERIES = max(1, int(os.environ.get("CPERSONA_FUSED_GATE_SAMPLE_QUERIES", "40")))
# knob 3 — the precision point. The calibrated separation curve is data-derived; this
# is the single policy choice of where to sit on it. strict / balanced / lenient map to
# a specificity weight beta in _separation_threshold (maximise sensitivity +
# beta*specificity): strict=2.0 (fewer contaminants, more misses), balanced=1.0
# (Youden's J), lenient=0.5 (fewer misses, more contaminants). A raw
# CPERSONA_FUSED_GATE_BETA overrides the named level.
RECALL_PRECISION = os.environ.get("CPERSONA_RECALL_PRECISION", "balanced").lower()
_PRECISION_BETA = {"strict": 2.0, "balanced": 1.0, "lenient": 0.5}
FUSED_GATE_BETA = float(
    os.environ.get("CPERSONA_FUSED_GATE_BETA")
    or _PRECISION_BETA.get(RECALL_PRECISION, 1.0)
)

# Autocut (v2.4 / v2.4.13: relative gap ratio, enabled by default)
AUTOCUT_ENABLED = os.environ.get("CPERSONA_AUTOCUT_ENABLED", "true").lower() == "true"
AUTOCUT_MIN_GAP_RATIO = float(os.environ.get("CPERSONA_AUTOCUT_MIN_GAP_RATIO", "0.15"))
# v2.4.25: minimum result count before autocut engages. RSF min-max normalization
# forces the lowest-scoring row to 0.0, so any small result set carries an
# artificial full-scale gap that autocut would cut to a single row (the 2-item
# over-cut that blocked making rsf the default). Below this floor, recall is too
# small for a "gap" to be meaningful — keep every row. Hard floor of 2 keeps the
# gap computation well-defined.
AUTOCUT_MIN_RESULTS = max(2, int(os.environ.get("CPERSONA_AUTOCUT_MIN_RESULTS", "3")))

# Episode boundary soft penalty (L3 — v2.4.14)
# Memories created before the latest archived episode are penalised by a
# multiplicative factor so cross-session noise is filtered by the quality gate.
EPISODE_PENALTY_ENABLED = os.environ.get("CPERSONA_EPISODE_PENALTY_ENABLED", "true").lower() == "true"
EPISODE_DECAY_RATE = float(os.environ.get("CPERSONA_EPISODE_DECAY_RATE", "0.01"))
EPISODE_DECAY_FLOOR = float(os.environ.get("CPERSONA_EPISODE_DECAY_FLOOR", "0.5"))

RECALL_MODE = os.environ.get("CPERSONA_RECALL_MODE", "rrf")
RRF_K = max(1, int(os.environ.get("CPERSONA_RRF_K", "60")))
RRF_THRESHOLD_FACTOR = float(os.environ.get("CPERSONA_RRF_THRESHOLD_FACTOR", "0.5"))
# v2.4.12: Max theoretical _rrf_score ≈ num_retrievers / (RRF_K + 1), with 3
# retrievers (vector, FTS episodes, FTS memories) at rank 0 each. Used by
# _apply_quality_gate to map cosine-scale min_score (0.2–1.0) into the RRF
# score's tight range (0–~0.05).
RRF_MAX_SCALE = 3.0 / (RRF_K + 1)
