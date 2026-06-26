"""Environment variable configuration for CPersona."""

import os

DB_PATH = os.environ.get("CPERSONA_DB_PATH", "data/cpersona.db")
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
STORE_BLOB = os.environ.get("CPERSONA_STORE_BLOB", "true").lower() == "true"

AUTO_CALIBRATE = os.environ.get("CPERSONA_AUTO_CALIBRATE", "false").lower() == "true"
CALIBRATE_SAMPLE_SIZE = int(os.environ.get("CPERSONA_CALIBRATE_SAMPLE_SIZE", "200"))
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

# Autocut (v2.4 / v2.4.13: relative gap ratio, enabled by default)
AUTOCUT_ENABLED = os.environ.get("CPERSONA_AUTOCUT_ENABLED", "true").lower() == "true"
AUTOCUT_MIN_GAP_RATIO = float(os.environ.get("CPERSONA_AUTOCUT_MIN_GAP_RATIO", "0.15"))

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
