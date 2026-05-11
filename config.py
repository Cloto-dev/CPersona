"""Environment variable configuration for CPersona."""

import os

DB_PATH = os.environ.get("CPERSONA_DB_PATH", "data/cpersona.db")
MAX_MEMORIES = int(os.environ.get("CPERSONA_MAX_MEMORIES", "500"))
MAX_CONTENT_LENGTH = int(os.environ.get("CPERSONA_MAX_CONTENT_LENGTH", "2000"))
FTS_ENABLED = os.environ.get("CPERSONA_FTS_ENABLED", "true").lower() == "true"

EMBEDDING_MODE = os.environ.get("CPERSONA_EMBEDDING_MODE", "none")
EMBEDDING_URL = os.environ.get("CPERSONA_EMBEDDING_URL", "")
EMBEDDING_API_KEY = os.environ.get("CPERSONA_EMBEDDING_API_KEY", "")
EMBEDDING_API_URL = os.environ.get("CPERSONA_EMBEDDING_API_URL", "https://api.openai.com/v1/embeddings")
EMBEDDING_MODEL = os.environ.get("CPERSONA_EMBEDDING_MODEL", "text-embedding-3-small")

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

AUTOCUT_ENABLED = os.environ.get("CPERSONA_AUTOCUT_ENABLED", "false").lower() == "true"

RECALL_MODE = os.environ.get("CPERSONA_RECALL_MODE", "rrf")
RRF_K = max(1, int(os.environ.get("CPERSONA_RRF_K", "60")))
RRF_THRESHOLD_FACTOR = float(os.environ.get("CPERSONA_RRF_THRESHOLD_FACTOR", "0.5"))
