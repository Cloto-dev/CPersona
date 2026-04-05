"""
Cloto MCP Server: CPersona Memory
Persistent memory with FTS5 full-text search and pluggable vector embedding.
Evolved from CPersona 2.2 (Rust plugin) with 2.1 (ai_karin) architecture enhancements.

Phase 1: store, recall (FTS5 + keyword) — COMPLETE
Phase 2: Vector embedding integration (cosine similarity search) — COMPLETE
Phase 3: LLM-powered memory extraction (profile + episode summarization) — COMPLETE
Phase 4: Anti-contamination — memory boundary markers, timestamp annotations,
         anti-hallucination guardrails — COMPLETE
Phase 5: Background task queue (DB-persisted, crash-recoverable) — COMPLETE
Phase 6: Memory portability — JSONL export/import, pre-computed summary support,
         Claude Code integration — COMPLETE
Phase 7: Memory confidence score — cosine + time decay geometric mean,
         opt-in confidence metadata in recall output — COMPLETE
Phase 8: Scalability — memories FTS5 index, heapq top-K vector search,
         adaptive scan limits — COMPLETE
"""

import asyncio
import hashlib
import heapq
import json
import logging
import math
import os
import re
import struct
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone

import aiosqlite
import httpx
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from common.mcp_utils import ToolRegistry

logger = logging.getLogger(__name__)


def _clamp_limit(limit: int, cap: int) -> int:
    """Clamp a user-supplied limit to [0, cap], preventing negative bypass."""
    return min(max(0, limit), cap)


# ============================================================
# Configuration
# ============================================================

DB_PATH = os.environ.get("CPERSONA_DB_PATH", "data/cpersona.db")
MAX_MEMORIES = int(os.environ.get("CPERSONA_MAX_MEMORIES", "500"))
MAX_CONTENT_LENGTH = int(os.environ.get("CPERSONA_MAX_CONTENT_LENGTH", "2000"))
FTS_ENABLED = os.environ.get("CPERSONA_FTS_ENABLED", "true").lower() == "true"

# Embedding configuration
EMBEDDING_MODE = os.environ.get("CPERSONA_EMBEDDING_MODE", "none")
EMBEDDING_URL = os.environ.get("CPERSONA_EMBEDDING_URL", "")
EMBEDDING_API_KEY = os.environ.get("CPERSONA_EMBEDDING_API_KEY", "")
EMBEDDING_API_URL = os.environ.get("CPERSONA_EMBEDDING_API_URL", "https://api.openai.com/v1/embeddings")
EMBEDDING_MODEL = os.environ.get("CPERSONA_EMBEDDING_MODEL", "text-embedding-3-small")

# Vector search threshold (cosine similarity, 0.0-1.0)
VECTOR_MIN_SIMILARITY = float(os.environ.get("CPERSONA_VECTOR_MIN_SIMILARITY", "0.3"))

# Embedding cache (query deduplication)
EMBEDDING_CACHE_SIZE = int(os.environ.get("CPERSONA_EMBEDDING_CACHE_SIZE", "256"))
EMBEDDING_CACHE_TTL = int(os.environ.get("CPERSONA_EMBEDDING_CACHE_TTL", "300"))  # seconds

# Background task queue (Phase 5: crash-recoverable async processing)
TASK_QUEUE_ENABLED = os.environ.get("CPERSONA_TASK_QUEUE_ENABLED", "true").lower() == "true"

# Confidence scoring (v2.3.2)
CONFIDENCE_ENABLED = os.environ.get("CPERSONA_CONFIDENCE_ENABLED", "false").lower() == "true"
COSINE_FLOOR = float(os.environ.get("CPERSONA_COSINE_FLOOR", "0.20"))
COSINE_CEIL = float(os.environ.get("CPERSONA_COSINE_CEIL", "0.75"))
DECAY_RATE = float(os.environ.get("CPERSONA_DECAY_RATE", "0.005"))
DECAY_FLOOR = float(os.environ.get("CPERSONA_DECAY_FLOOR", "0.3"))
DECAY_CEIL = float(os.environ.get("CPERSONA_DECAY_CEIL", "0.5"))
RECALL_BOOST = float(os.environ.get("CPERSONA_RECALL_BOOST", "0.02"))
BOOST_DECAY_RATE = float(os.environ.get("CPERSONA_BOOST_DECAY_RATE", "0.002"))
MIN_TIME_RANGE_HOURS = float(os.environ.get("CPERSONA_MIN_TIME_RANGE_HOURS", "24"))
REFERENCE_HOURS = float(os.environ.get("CPERSONA_REFERENCE_HOURS", "168"))  # 1 week
RESOLVED_DECAY_FACTOR = float(os.environ.get("CPERSONA_RESOLVED_DECAY_FACTOR", "0.3"))
TASK_MAX_RETRIES = int(os.environ.get("CPERSONA_TASK_MAX_RETRIES", "3"))
TASK_RETRY_DELAY = int(os.environ.get("CPERSONA_TASK_RETRY_DELAY", "30"))  # seconds

# Remote vector search (v2.3.5)
VECTOR_SEARCH_MODE = os.environ.get("CPERSONA_VECTOR_SEARCH_MODE", "local")  # local | remote
STORE_BLOB = os.environ.get("CPERSONA_STORE_BLOB", "true").lower() == "true"

# Auto-calibration (v2.3.7)
AUTO_CALIBRATE = os.environ.get("CPERSONA_AUTO_CALIBRATE", "false").lower() == "true"
CALIBRATE_SAMPLE_SIZE = int(os.environ.get("CPERSONA_CALIBRATE_SAMPLE_SIZE", "200"))
CALIBRATE_Z_FACTOR = float(os.environ.get("CPERSONA_CALIBRATE_Z_FACTOR", "1.0"))
CALIBRATE_FLOOR = float(os.environ.get("CPERSONA_CALIBRATE_FLOOR", "0.05"))

# Autocut (v2.4)
AUTOCUT_ENABLED = os.environ.get("CPERSONA_AUTOCUT_ENABLED", "false").lower() == "true"

# Recall mode (v2.4)
RECALL_MODE = os.environ.get("CPERSONA_RECALL_MODE", "rrf")  # rrf | cascade
RRF_K = max(1, int(os.environ.get("CPERSONA_RRF_K", "60")))
RRF_THRESHOLD_FACTOR = float(os.environ.get("CPERSONA_RRF_THRESHOLD_FACTOR", "0.5"))

# ============================================================
# Embedding Client
# ============================================================


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
        self._client = None
        # LRU cache: key=text_hash, value=(embedding, timestamp)
        self._cache: OrderedDict[str, tuple[list[float], float]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl
        self.cache_hits = 0
        self.cache_misses = 0

    async def initialize(self):
        """Create persistent HTTP client."""
        import httpx

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


_embedding_client: EmbeddingClient | None = None


# ============================================================
# LLM Proxy (Phase 3: Memory Extraction)
# ============================================================


# ============================================================
# Background Task Queue (Phase 5)
# ============================================================


class MemoryTaskQueue:
    """DB-persisted background task queue with crash recovery.

    Tasks (update_profile, archive_episode) are serialized to SQLite on enqueue,
    processed asynchronously in FIFO order, and deleted on success.
    On startup, any pending tasks from a previous crash are automatically recovered.

    Ported from KS2.1 (ai_karin) MemoryWorker — adapted from Rust/tokio to Python/asyncio.
    """

    def __init__(self):
        self._event = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start the background processing loop."""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        # Signal to process any pending tasks from before crash
        self._event.set()
        logger.info("MemoryTaskQueue: started (max_retries=%d, retry_delay=%ds)", TASK_MAX_RETRIES, TASK_RETRY_DELAY)

    async def stop(self):
        """Stop the background loop gracefully."""
        self._running = False
        self._event.set()  # Wake up the loop so it can exit
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                logger.warning("MemoryTaskQueue: forced shutdown after timeout")

    async def enqueue(self, task_type: str, agent_id: str, payload: list[dict]) -> int:
        """Enqueue a task. Returns task ID."""
        db = await get_db()
        cursor = await db.execute(
            "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES (?, ?, ?)",
            (task_type, agent_id, json.dumps(payload)),
        )
        await db.commit()
        task_id = cursor.lastrowid
        logger.info("MemoryTaskQueue: enqueued %s for agent %s (task_id=%d)", task_type, agent_id, task_id)
        self._event.set()
        return task_id

    async def get_status(self) -> dict:
        """Get queue status for monitoring."""
        db = await get_db()
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
        pending = rows[0][0] if rows else 0
        return {
            "enabled": True,
            "pending": pending,
            "max_retries": TASK_MAX_RETRIES,
            "retry_delay": TASK_RETRY_DELAY,
        }

    async def _loop(self):
        """Main processing loop — waits for signal, drains all pending tasks."""
        while self._running:
            await self._event.wait()
            self._event.clear()

            while self._running:
                task = await self._fetch_next()
                if task is None:
                    break

                task_id, task_type, agent_id, payload, retries = task
                logger.info(
                    "MemoryTaskQueue: processing %s (task_id=%d, agent=%s, retry=%d/%d)",
                    task_type,
                    task_id,
                    agent_id,
                    retries,
                    TASK_MAX_RETRIES,
                )
                try:
                    if task_type == "update_profile":
                        await do_update_profile(agent_id, payload)
                    elif task_type == "archive_episode":
                        await do_archive_episode(agent_id, payload)
                    else:
                        logger.error("MemoryTaskQueue: unknown task type %s, discarding", task_type)

                    # Task succeeded — delete it
                    await self._delete_task(task_id)
                    logger.info("MemoryTaskQueue: completed %s (task_id=%d)", task_type, task_id)
                except Exception as e:
                    logger.error("MemoryTaskQueue: task %d (%s) failed: %s", task_id, task_type, e)
                    if retries + 1 >= TASK_MAX_RETRIES:
                        logger.error("MemoryTaskQueue: task %d exceeded max retries, discarding", task_id)
                        await self._delete_task(task_id)
                    else:
                        await self._increment_retry(task_id)
                        # Back off before processing the next task
                        await asyncio.sleep(TASK_RETRY_DELAY)

    async def _fetch_next(self) -> tuple | None:
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT id, task_type, agent_id, payload, retries FROM pending_memory_tasks ORDER BY id ASC LIMIT 1"
        )
        if not rows:
            return None
        task_id, task_type, agent_id, payload_json, retries = rows[0]
        payload = json.loads(payload_json)
        return (task_id, task_type, agent_id, payload, retries)

    async def _delete_task(self, task_id: int):
        db = await get_db()
        await db.execute("DELETE FROM pending_memory_tasks WHERE id = ?", (task_id,))
        await db.commit()

    async def _increment_retry(self, task_id: int):
        db = await get_db()
        await db.execute(
            "UPDATE pending_memory_tasks SET retries = retries + 1 WHERE id = ?",
            (task_id,),
        )
        await db.commit()


_task_queue: MemoryTaskQueue | None = None


# ============================================================
# Database
# ============================================================

SCHEMA_VERSION = 7

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    msg_id     TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '{}',
    timestamp  TEXT NOT NULL,
    metadata   TEXT NOT NULL DEFAULT '{}',
    embedding  BLOB,
    channel    TEXT NOT NULL DEFAULT '',
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_agent
    ON memories(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_msg_id
    ON memories(agent_id, msg_id);

CREATE TABLE IF NOT EXISTS profiles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, user_id)
);

CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    summary    TEXT NOT NULL,
    keywords   TEXT NOT NULL DEFAULT '',
    embedding  BLOB,
    start_time TEXT,
    end_time   TEXT,
    resolved   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_agent
    ON episodes(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pending_memory_tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type  TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    retries    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary,
    keywords,
    content=episodes,
    content_rowid=id,
    tokenize='trigram'
);

-- Sync triggers
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary, keywords)
    VALUES (new.id, new.summary, new.keywords);
END;

CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, keywords)
    VALUES ('delete', old.id, old.summary, old.keywords);
END;

CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, keywords)
    VALUES ('delete', old.id, old.summary, old.keywords);
    INSERT INTO episodes_fts(rowid, summary, keywords)
    VALUES (new.id, new.summary, new.keywords);
END;

-- v2.3.4: FTS5 index on memories for scalable keyword search
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content=memories,
    content_rowid=id,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
"""

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """Get or create the database connection."""
    global _db
    if _db is not None:
        return _db

    # Ensure parent directory exists
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    _db = await aiosqlite.connect(DB_PATH)
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")

    # Apply schema
    await _db.executescript(SCHEMA_SQL)

    # Apply FTS if enabled
    if FTS_ENABLED:
        await _db.executescript(FTS_SQL)

    # Track schema version and apply migrations
    row = await _db.execute_fetchall("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    current = row[0][0] if row else 0

    # v2.3.3: Add resolved column to episodes
    if current < 3:
        try:
            await _db.execute("ALTER TABLE episodes ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # Column already exists (e.g., fresh DB with updated SCHEMA_SQL)

    # v2.3.4: Backfill memories_fts from existing data
    if current < 4 and FTS_ENABLED:
        try:
            await _db.execute("INSERT OR IGNORE INTO memories_fts(rowid, content) SELECT id, content FROM memories")
        except Exception:
            pass  # Table may not exist if FTS disabled, or already populated

    # v2.3.6: Rebuild FTS tables with trigram tokenizer for CJK support
    if current < 5 and FTS_ENABLED:
        try:
            # Drop old FTS tables and triggers, then re-create with trigram
            await _db.executescript(
                """
                DROP TRIGGER IF EXISTS episodes_ai;
                DROP TRIGGER IF EXISTS episodes_ad;
                DROP TRIGGER IF EXISTS episodes_au;
                DROP TRIGGER IF EXISTS memories_fts_ai;
                DROP TRIGGER IF EXISTS memories_fts_ad;
                DROP TABLE IF EXISTS episodes_fts;
                DROP TABLE IF EXISTS memories_fts;
                """
            )
            # Re-create with trigram tokenizer (done by FTS_SQL above, but need to re-run)
            await _db.executescript(FTS_SQL)
            # Backfill
            await _db.execute(
                "INSERT OR IGNORE INTO episodes_fts(rowid, summary, keywords) "
                "SELECT id, summary, keywords FROM episodes"
            )
            await _db.execute("INSERT OR IGNORE INTO memories_fts(rowid, content) SELECT id, content FROM memories")
        except Exception as e:
            logger.warning("FTS trigram migration failed (non-fatal): %s", e)

    # v2.4.1: Add channel column for context separation (chat vs discord)
    if current < 6:
        try:
            await _db.execute("ALTER TABLE memories ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
            # Backfill: existing memories are from chat
            await _db.execute("UPDATE memories SET channel = 'chat' WHERE channel = ''")
        except Exception:
            pass  # Column already exists (fresh DB with updated SCHEMA_SQL)
        await _db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_agent_channel ON memories(agent_id, channel, created_at DESC)"
        )

    # v2.4.4: Add recall_count + last_recalled_at columns for recall boost
    if current < 7:
        try:
            await _db.execute("ALTER TABLE memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # Column already exists
        try:
            await _db.execute("ALTER TABLE memories ADD COLUMN last_recalled_at TEXT")
        except Exception:
            pass  # Column already exists

    if current < SCHEMA_VERSION:
        await _db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        await _db.commit()

    return _db


async def close_db():
    """Close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ============================================================
# Content Sanitization (v2.4.3)
# ============================================================

_MENTION_PATTERN = re.compile(r"<@!?\d+>")
_MEMORY_ANNOTATION_PATTERN = re.compile(r"\[Memory from [^\]]+\]\s*")


def _sanitize_content(content: str) -> str:
    """Sanitize content before storing in memory.

    Removes Discord bot mentions, [Memory from ...] annotations,
    trims whitespace, and enforces length limit.
    """
    content = _MENTION_PATTERN.sub("", content)
    content = _MEMORY_ANNOTATION_PATTERN.sub("", content)
    content = content.strip()
    if len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH]
    return content


# ============================================================
# Memory Operations
# ============================================================


def generate_mem_key(agent_id: str, message: dict) -> str:
    """Generate a unique key for a memory entry (2.1-compatible)."""
    ts = message.get("timestamp", datetime.now(timezone.utc).isoformat())
    content = message.get("content", "")
    hash_input = f"{agent_id}:{ts}:{content}"
    short_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
    return f"mem:{agent_id}:{ts}:{short_hash}"


async def do_store(agent_id: str, message: dict, channel: str = "") -> dict:
    """Store a message in agent memory."""
    db = await get_db()

    msg_id = message.get("id", "")
    raw_content = message.get("content", "")
    source = json.dumps(message.get("source", {}))
    timestamp = message.get("timestamp", datetime.now(timezone.utc).isoformat())
    metadata = json.dumps(message.get("metadata", {}))

    if not raw_content:
        return {"ok": True, "skipped": True, "reason": "empty content"}

    # Sanitize content (v2.4.3)
    content = _sanitize_content(raw_content)
    truncated = len(raw_content) > MAX_CONTENT_LENGTH

    if not content:
        return {"ok": True, "skipped": True, "reason": "empty after sanitization"}

    # Deduplicate by msg_id if provided
    if msg_id:
        row = await db.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = ? AND msg_id = ? LIMIT 1",
            (agent_id, msg_id),
        )
        if row:
            return {"ok": True, "skipped": True, "reason": "duplicate msg_id"}

    # Deduplicate by exact content match (v2.4.3)
    existing = await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND channel = ? AND content = ? LIMIT 1",
        (agent_id, channel, content),
    )
    if existing:
        return {"ok": True, "skipped": True, "reason": "duplicate content"}

    # Compute embedding before insert (so we can include it in the INSERT)
    embedding_blob = None
    if _embedding_client and (VECTOR_SEARCH_MODE == "local" or STORE_BLOB):
        try:
            embeddings = await _embedding_client.embed([content])
            if embeddings and embeddings[0]:
                embedding_blob = EmbeddingClient.pack_embedding(embeddings[0])
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, TypeError) as e:
            logger.warning("Embedding failed during store: %s", e)

    await db.execute(
        """INSERT INTO memories (agent_id, msg_id, content, source, timestamp, metadata, embedding, channel)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, msg_id, content, source, timestamp, metadata, embedding_blob, channel),
    )
    await db.commit()

    # v2.3.5: Remote index for centralized vector search
    if VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url:
        try:
            # Get the inserted row ID
            row = await db.execute_fetchall(
                "SELECT id FROM memories WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
                (agent_id,),
            )
            if row:
                mem_id = row[0][0]
                base_url = _embedding_client._http_url.rsplit("/", 1)[0]  # strip /embed
                await _embedding_client._client.post(
                    f"{base_url}/index",
                    json={
                        "namespace": f"cpersona:{agent_id}",
                        "items": [{"id": f"mem:{mem_id}", "text": content}],
                    },
                )
        except Exception as e:
            logger.debug("Remote index failed (non-fatal): %s", e)

    result = {"ok": True}
    if truncated:
        result["truncated"] = True
    return result


async def _recall_cascade(
    db,
    agent_id: str,
    query: str,
    limit: int,
    deep: bool,
    channel: str = "",
) -> list[dict]:
    """Original cascading recall: stages fill remaining slots sequentially."""
    results: list[dict] = []
    seen_ids: set = set()

    # Strategy 0: Vector search
    if _embedding_client and query.strip():
        vector_results = await _search_vector(db, agent_id, query, limit, channel=channel)
        for row in vector_results:
            rid = row.get("_rid", row["id"])
            if rid not in seen_ids:
                results.append(row)
                seen_ids.add(rid)

    # Strategy 1: FTS5 episode search
    if FTS_ENABLED and query.strip():
        fts_results = await _search_episodes_fts(db, agent_id, query, limit)
        for row in fts_results:
            rid = ("ep", row["id"])
            if rid not in seen_ids:
                results.append(row)
                seen_ids.add(rid)

    # Strategy 2: Profile lookup
    profile_rows = await db.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ? ORDER BY updated_at DESC LIMIT 3",
        (agent_id,),
    )
    for (profile_content,) in profile_rows:
        results.append(
            {
                "id": -1,
                "content": f"[Profile] {profile_content}",
                "source": {"System": "profile"},
                "timestamp": "",
            }
        )

    # Strategy 3: Keyword match on memories
    remaining = max(0, limit - len(results))
    if remaining > 0:
        memory_rows = await _search_memories_keyword(db, agent_id, query, remaining, channel=channel)
        for row in memory_rows:
            rid = ("mem", row["id"])
            if rid not in seen_ids:
                results.append(row)
                seen_ids.add(rid)

    return results


async def _recall_rrf(
    db,
    agent_id: str,
    query: str,
    limit: int,
    deep: bool,
    channel: str = "",
) -> list[dict]:
    """v2.4 RRF recall: run vector and FTS5 independently, merge with
    Reciprocal Rank Fusion. Avoids cascade's positional bias.

    score(doc) = sum( 1 / (k + rank_i) ) for each retriever that found doc
    """
    k = RRF_K
    doc_map: dict[tuple, dict] = {}  # rid → row dict
    rrf_scores: dict[tuple, float] = {}  # rid → accumulated RRF score

    # --- Retriever 1: Vector search (independent, up to limit) ---
    # Phase 3: RRF mode relaxes the similarity threshold for broader coverage
    rrf_min_sim = VECTOR_MIN_SIMILARITY * RRF_THRESHOLD_FACTOR
    if _embedding_client:
        vector_results = await _search_vector(db, agent_id, query, limit, min_similarity=rrf_min_sim, channel=channel)
        for rank, row in enumerate(vector_results):
            rid = row.get("_rid", ("mem", row["id"]))
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    # --- Retriever 2: FTS5 episode search (independent, up to limit) ---
    if FTS_ENABLED:
        fts_ep_results = await _search_episodes_fts(db, agent_id, query, limit)
        for rank, row in enumerate(fts_ep_results):
            rid = ("ep", row["id"])
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    # --- Retriever 3: FTS5 keyword on memories (independent, up to limit) ---
    if FTS_ENABLED:
        fts_mem_results = await _search_memories_keyword(db, agent_id, query, limit, channel=channel)
        for rank, row in enumerate(fts_mem_results):
            rid = ("mem", row["id"])
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    # --- Merge: sort by RRF score descending, attach score for autocut ---
    sorted_rids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)
    results = []
    for rid in sorted_rids:
        row = doc_map[rid]
        row["_rrf_score"] = rrf_scores[rid]
        results.append(row)

    # --- Profile injection (always, not ranked) ---
    profile_rows = await db.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ? ORDER BY updated_at DESC LIMIT 3",
        (agent_id,),
    )
    for (profile_content,) in profile_rows:
        results.append(
            {
                "id": -1,
                "content": f"[Profile] {profile_content}",
                "source": {"System": "profile"},
                "timestamp": "",
            }
        )

    return results


def _autocut(results: list[dict]) -> list[dict]:
    """Detect the largest score gap in results and cut below it (Weaviate autocut).

    Looks at _rrf_score (RRF mode) or _cosine (cascade mode) to find a
    natural breakpoint in the score distribution. Results after the largest
    gap are removed as noise.
    """
    if len(results) < 2:
        return results
    scores = [r.get("_rrf_score") or r.get("_cosine") or 0 for r in results]
    gaps = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]
    if not gaps or max(gaps) <= 0:
        return results
    cut_idx = max(range(len(gaps)), key=lambda i: gaps[i]) + 1
    return results[:cut_idx]


def _adaptive_min_score(memory_count: int) -> float:
    """Compute adaptive quality threshold based on memory pool size.

    Fewer memories → stricter threshold (avoid noise domination).
    More memories → lenient threshold (allow broader reference).

    Returns:
        0.5 (sparse, count~1) → 0.2 (dense, count~500+).
        1.0 when count=0 (nothing should pass).
    """
    if memory_count <= 0:
        return 1.0
    t = min(1.0, math.log(memory_count + 1) / math.log(500))
    return round(0.5 - t * 0.3, 4)


def _apply_quality_gate(
    results: list[dict],
    min_score: float,
    memory_count: int,
) -> list[dict]:
    """Adaptive quality gate — removes results below dynamic threshold.

    Rules:
    1. Scored results: _confidence_score or _rrf_score or _cosine < min_score → exclude
    2. Profile injection: skip if memory_count < 50 (profile dominates with sparse data)
    3. Unscored results (keyword/FTS only): keep only if memory_count >= 100
    """
    if not results:
        return results

    filtered = []
    for r in results:
        # Profile — gate by memory count
        if r.get("id") == -1:  # profile sentinel
            if memory_count >= 50:
                filtered.append(r)
            continue

        # Get the best available score
        score = r.get("_confidence_score") or r.get("_rrf_score") or r.get("_cosine")

        if score is not None:
            if score >= min_score:
                filtered.append(r)
        else:
            # Unscored result (keyword/FTS without cosine)
            if memory_count >= 100:
                filtered.append(r)

    return filtered


async def do_recall(agent_id: str, query: str, limit: int, deep: bool = False, channel: str = "") -> dict:
    """Recall relevant memories using multi-strategy search.

    Supports two modes (CPERSONA_RECALL_MODE):
    - "cascade" (default): Sequential stages, each filling remaining slots.
    - "rrf" (v2.4): Run vector and FTS5 independently, merge with
      Reciprocal Rank Fusion. Avoids the positional disadvantage of
      cascade ordering.
    """
    db = await get_db()

    if RECALL_MODE == "rrf" and query.strip():
        results = await _recall_rrf(db, agent_id, query, limit, deep, channel)
    else:
        results = await _recall_cascade(db, agent_id, query, limit, deep, channel)

    # v2.4.4: Compute time range for dynamic decay + fetch recall counts
    time_range_hours = 0.0
    recall_counts: dict[int, tuple[int, str]] = {}  # id → (count, last_recalled_at)
    if CONFIDENCE_ENABLED and results:
        range_row = await db.execute_fetchall(
            "SELECT MIN(timestamp), MAX(timestamp) FROM memories WHERE agent_id = ?",
            (agent_id,),
        )
        if range_row and range_row[0][0] and range_row[0][1]:
            oldest = _parse_timestamp_utc(range_row[0][0])
            newest = _parse_timestamp_utc(range_row[0][1])
            if oldest and newest:
                time_range_hours = max(0.0, (newest - oldest).total_seconds() / 3600)

        # Fetch recall_count + last_recalled_at for memory results
        mem_ids = [r["id"] for r in results if isinstance(r.get("id"), int) and r["id"] > 0]
        if mem_ids:
            placeholders = ",".join("?" * len(mem_ids))
            rc_rows = await db.execute_fetchall(
                f"SELECT id, recall_count, last_recalled_at FROM memories WHERE id IN ({placeholders})",
                mem_ids,
            )
            recall_counts = {r[0]: (r[1], r[2] or "") for r in rc_rows}

    # v2.3.2+: Re-rank by confidence score before truncation (if enabled)
    if CONFIDENCE_ENABLED:
        for r in results:
            ts = r.get("timestamp", "")
            raw_cos = r.get("_cosine")
            is_resolved = r.get("_resolved", False)
            rc_data = recall_counts.get(r.get("id", -1), (0, ""))
            r["_confidence_score"] = _compute_confidence(
                raw_cos,
                ts,
                resolved=is_resolved,
                deep=deep,
                time_range_hours=time_range_hours,
                recall_count=rc_data[0],
                last_recalled_at_str=rc_data[1],
            )["score"]
        results.sort(key=lambda r: r.get("_confidence_score", 0), reverse=True)

    # v2.4.6: Adaptive quality gate — dynamic threshold based on memory pool size.
    # Fewer memories → stricter filtering to prevent noise domination.
    memory_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (agent_id,)))[0][0]
    min_score = _adaptive_min_score(memory_count)
    effective_min = min_score * 0.5 if deep else min_score
    results = _apply_quality_gate(results, effective_min, memory_count)

    # v2.4: Autocut — detect score gap and remove noise results
    if AUTOCUT_ENABLED:
        results = _autocut(results)

    # Truncate to limit and reverse for chronological order (oldest first for LLM)
    results = results[:limit]
    results.reverse()

    # Convert to ClotoMessage-compatible format
    # Note: timestamp is passed as a separate field (not embedded in content).
    # The mind server uses it for system-level framing so the LLM cannot echo it.
    messages = []
    for r in results:
        content = r["content"]

        msg: dict = {"content": content}
        if r.get("source"):
            msg["source"] = r["source"] if isinstance(r["source"], dict) else _try_parse_json(r["source"])
        if r.get("timestamp"):
            msg["timestamp"] = r["timestamp"]
        if r.get("msg_id"):
            msg["id"] = r["msg_id"]
        # v2.3.2+: Attach confidence metadata
        if CONFIDENCE_ENABLED:
            raw_cosine = r.get("_cosine")
            ts = r.get("timestamp", "")
            is_resolved = r.get("_resolved", False)
            rc_data = recall_counts.get(r.get("id", -1), (0, ""))
            msg["confidence"] = _compute_confidence(
                raw_cosine,
                ts,
                resolved=is_resolved,
                deep=deep,
                time_range_hours=time_range_hours,
                recall_count=rc_data[0],
                last_recalled_at_str=rc_data[1],
            )
        # Remove internal tracking keys
        r.pop("_rid", None)
        r.pop("_cosine", None)
        r.pop("_confidence_score", None)
        r.pop("_rrf_score", None)
        r.pop("_resolved", None)
        messages.append(msg)

    # v2.4.4: Increment recall_count for returned memories (skip deep recall)
    if not deep and recall_counts:
        returned_ids = [r.get("id", -1) for r in results if isinstance(r.get("id"), int) and r["id"] > 0]
        if returned_ids:
            placeholders = ",".join("?" * len(returned_ids))
            await db.execute(
                f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = datetime('now') WHERE id IN ({placeholders})",
                returned_ids,
            )
            await db.commit()

    return {"messages": messages}


async def _search_vector(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_similarity: float | None = None,
    channel: str = "",
) -> list[dict]:
    """Search memories and episodes using vector cosine similarity."""

    # v2.3.5: Remote vector search via embedding server
    if VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url:
        try:
            base_url = _embedding_client._http_url.rsplit("/", 1)[0]
            resp = await _embedding_client._client.post(
                f"{base_url}/search",
                json={
                    "namespace": f"cpersona:{agent_id}",
                    "query": query,
                    "limit": limit,
                    "min_similarity": VECTOR_MIN_SIMILARITY,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for hit in data.get("results", []):
                # Parse "mem:{id}" or "ep:{id}" format
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

    # Local vector search (default or fallback)
    import numpy as np

    # 1. Compute query embedding
    embeddings = await _embedding_client.embed([query])
    if not embeddings or not embeddings[0]:
        return []
    query_vec = np.array(embeddings[0], dtype=np.float32)
    query_dim = len(query_vec)
    effective_min_sim = min_similarity if min_similarity is not None else VECTOR_MIN_SIMILARITY

    candidates: list[tuple[float, dict]] = []
    scan_limit = min(MAX_MEMORIES, max(limit * 10, 100))

    # 2. Search memory embeddings (batch matrix multiplication)
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
        # Batch decode: collect valid embeddings into a matrix for vectorized cosine
        valid_rows = []
        blobs = []
        for row in rows:
            blob = row[5]
            if blob and len(blob) == query_dim * 4:  # float32 = 4 bytes
                valid_rows.append(row)
                blobs.append(blob)

        if valid_rows:
            # Single matrix multiplication: (N, dim) @ (dim,) → (N,)
            mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), query_dim)
            sims = mat @ query_vec  # BLAS-optimized dot product

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

    # 3. Search episode embeddings (batch matrix multiplication)
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

    # 4. Return top-K by similarity (heap selection: O(N log K) vs O(N log N) sort)
    top_k = heapq.nlargest(limit, candidates, key=lambda x: x[0])
    return [c[1] for c in top_k]


async def _search_episodes_fts(db: aiosqlite.Connection, agent_id: str, query: str, limit: int) -> list[dict]:
    """Search episodes using FTS5."""
    # Sanitize: strip everything except alphanumeric, CJK, and whitespace
    # to prevent FTS5 operator injection (AND/OR/NOT/NEAR/*/^/- etc.)
    sanitized = re.sub(r"[^\w\s]", "", query, flags=re.UNICODE)
    words = sanitized.split()
    if not words:
        return []

    # Each word quoted for phrase matching; quotes inside words already stripped
    fts_query = " ".join(f'"{w}"' for w in words)

    rows = await db.execute_fetchall(
        """SELECT e.id, e.summary, e.start_time, e.resolved
           FROM episodes_fts f
           JOIN episodes e ON f.rowid = e.id
           WHERE episodes_fts MATCH ?
           AND e.agent_id = ?
           ORDER BY rank
           LIMIT ?""",
        (fts_query, agent_id, limit),
    )

    return [
        {
            "id": row[0],
            "content": f"[Episode] {row[1]}",
            "source": {"System": "episode"},
            "timestamp": row[2] or "",
            "_resolved": bool(row[3]),
        }
        for row in rows
    ]


async def _search_memories_keyword(
    db: aiosqlite.Connection, agent_id: str, query: str, limit: int, channel: str = ""
) -> list[dict]:
    """Search memories using FTS5 (preferred) or LIKE fallback."""
    channel_clause = " AND channel = ?" if channel else ""
    channel_params = (channel,) if channel else ()

    if not query.strip():
        # No query — return recent memories
        rows = await db.execute_fetchall(
            f"""SELECT id, msg_id, content, source, timestamp
               FROM memories
               WHERE agent_id = ?{channel_clause}
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, *channel_params, limit),
        )
        return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4]} for r in rows]

    # v2.3.4: Use FTS5 on memories when available
    if FTS_ENABLED:
        sanitized = re.sub(r"[^\w\s]", "", query, flags=re.UNICODE)
        words = sanitized.split()
        if words:
            fts_query = " ".join(f'"{w}"' for w in words)
            rows = await db.execute_fetchall(
                f"""SELECT m.id, m.msg_id, m.content, m.source, m.timestamp
                   FROM memories_fts f
                   JOIN memories m ON f.rowid = m.id
                   WHERE memories_fts MATCH ?
                   AND m.agent_id = ?{channel_clause.replace("channel", "m.channel")}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, agent_id, *channel_params, limit),
            )
            if rows:
                return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4]} for r in rows]
            # FTS5 returned nothing — fall through to LIKE

    # LIKE fallback (FTS disabled or FTS returned no results)
    scan_limit = min(MAX_MEMORIES, max(limit * 5, 50))
    rows = await db.execute_fetchall(
        f"""SELECT id, msg_id, content, source, timestamp
           FROM memories
           WHERE agent_id = ?{channel_clause}
           AND content LIKE ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (agent_id, *channel_params, f"%{query}%", scan_limit),
    )
    return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4]} for r in rows[:limit]]


async def do_get_profile(agent_id: str) -> dict:
    """Get the current profile for an agent."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ? AND user_id = '' LIMIT 1",
        (agent_id,),
    )
    return {"profile": rows[0][0] if rows else ""}


async def do_update_profile(agent_id: str, profile: str = "") -> dict:
    """Update agent profile with pre-computed content."""
    db = await get_db()

    if not profile:
        return {"ok": True, "profiles_updated": 0}

    result = profile

    await db.execute(
        """INSERT INTO profiles (agent_id, user_id, content, updated_at)
           VALUES (?, '', ?, datetime('now'))
           ON CONFLICT(agent_id, user_id) DO UPDATE SET
               content = excluded.content,
               updated_at = excluded.updated_at""",
        (agent_id, result),
    )
    await db.commit()
    return {"ok": True, "profiles_updated": 1}


async def do_archive_episode(
    agent_id: str,
    history: list[dict],
    summary: str = "",
    keywords: str = "",
    resolved: bool | None = None,
) -> dict:
    """Archive a conversation episode with pre-computed summary, keywords, and resolved status.

    All LLM processing (summarization, keyword extraction, resolved classification)
    is performed by the caller (kernel via CFR engine, or Claude Code sub-agent).
    CPersona stores the results without making any LLM calls.
    """
    db = await get_db()

    if not summary:
        return {"ok": True, "episode_id": None}

    resolved = bool(resolved)

    # Timestamps
    timestamps = [msg.get("timestamp", "") for msg in history if msg.get("timestamp")]
    start_time = min(timestamps) if timestamps else None
    end_time = max(timestamps) if timestamps else None

    # Compute embedding for episode summary
    embedding_blob = None
    if _embedding_client and summary:
        try:
            embeddings = await _embedding_client.embed([summary])
            if embeddings and embeddings[0]:
                embedding_blob = EmbeddingClient.pack_embedding(embeddings[0])
        except Exception as e:
            logger.warning("Embedding failed for episode: %s", e)

    cursor = await db.execute(
        """INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, embedding, resolved)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, summary, keywords, start_time, end_time, embedding_blob, int(resolved)),
    )
    await db.commit()
    return {"ok": True, "episode_id": cursor.lastrowid}


def _format_memory_timestamp(ts_raw: str) -> str | None:
    """Convert an ISO-8601 timestamp to a human-readable local time annotation.

    Uses the OS-local timezone (no hardcoded TZ). Returns None on parse failure.
    """
    if not ts_raw:
        return None
    try:
        # Parse ISO-8601 (handles +00:00, Z, and offset formats)
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        # Convert to OS-local timezone
        local_dt = dt.astimezone()
        tz_name = local_dt.strftime("%Z")  # e.g. "JST", "EST", "CET"
        return local_dt.strftime(f"%Y-%m-%d %H:%M {tz_name}")
    except (ValueError, OSError):
        return None


def _parse_timestamp_utc(ts_raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a UTC datetime."""
    if not ts_raw:
        return None
    try:
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except (ValueError, OSError):
        return None


def _compute_confidence(
    raw_cosine: float | None,
    timestamp_str: str,
    *,
    resolved: bool = False,
    deep: bool = False,
    time_range_hours: float = 0.0,
    recall_count: int = 0,
    last_recalled_at_str: str = "",
) -> dict:
    """Compute confidence metadata for a recall result (v2.3.2+).

    Returns a dict with 'age_hours', 'score', and optionally 'cosine', 'resolved'.
    Score = sqrt(norm_cos × time_decay) × completion_factor.
    When deep=True, time_decay and completion_factor are both 1.0.

    v2.4.4: Dynamic time decay + recall boost with gradual decay.
    Boost protection fades slowly (BOOST_DECAY_RATE) if memory is
    not recalled again, converging back to DECAY_FLOOR.
    """
    now = datetime.now(timezone.utc)
    age_hours = 0.0

    parsed = _parse_timestamp_utc(timestamp_str)
    if parsed:
        age_hours = max(0.0, (now - parsed).total_seconds() / 3600)

    # Recall boost: raise floor, but boost itself decays since last recall
    raw_boost = math.log(1 + recall_count) * RECALL_BOOST
    if raw_boost > 0 and last_recalled_at_str:
        last_recalled = _parse_timestamp_utc(last_recalled_at_str)
        if last_recalled:
            hours_since = max(0.0, (now - last_recalled).total_seconds() / 3600)
            boost_decay = 1.0 / (1.0 + hours_since * BOOST_DECAY_RATE)
            raw_boost *= boost_decay
    effective_floor = min(DECAY_CEIL, DECAY_FLOOR + raw_boost)

    # v2.3.3: deep recall disables decay
    if deep:
        time_decay = 1.0
    elif time_range_hours > 0:
        # v2.4.4: Dynamic rate — scale DECAY_RATE by time_range
        effective_range = max(MIN_TIME_RANGE_HOURS, time_range_hours)
        effective_rate = DECAY_RATE / max(1.0, effective_range / REFERENCE_HOURS)
        time_decay = max(effective_floor, 1.0 / (1.0 + age_hours * effective_rate))
    else:
        # Fallback: original fixed rate (backward compatible)
        time_decay = max(effective_floor, 1.0 / (1.0 + age_hours * DECAY_RATE))
    completion_factor = 1.0 if (deep or not resolved) else RESOLVED_DECAY_FACTOR

    confidence: dict = {"age_hours": round(age_hours, 1)}
    if resolved:
        confidence["resolved"] = True

    if raw_cosine is not None:
        # Normalize cosine to 0.0–1.0 range
        denom = COSINE_CEIL - COSINE_FLOOR
        norm_cos = max(0.0, min(1.0, (raw_cosine - COSINE_FLOOR) / denom)) if denom > 0 else 0.0
        confidence["cosine"] = round(raw_cosine, 4)
        confidence["score"] = round(math.sqrt(norm_cos * time_decay) * completion_factor, 4)
    else:
        # Non-vector results: score based on time decay only
        confidence["score"] = round(math.sqrt(time_decay) * completion_factor, 4)

    return confidence


def _try_parse_json(s: str) -> dict:
    """Try to parse a string as JSON, return empty dict on failure."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


# ============================================================
# MCP Server
# ============================================================

registry = ToolRegistry("cloto-mcp-cpersona")


# Tool registrations are below all do_* functions (auto_tool evaluates at definition time)


async def do_list_memories(agent_id: str, limit: int) -> dict:
    """List recent memories for dashboard display."""
    db = await get_db()
    if agent_id:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, msg_id, content, source, timestamp, created_at "
            "FROM memories WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, _clamp_limit(limit, 500)),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, msg_id, content, source, timestamp, created_at "
            "FROM memories ORDER BY created_at DESC LIMIT ?",
            (_clamp_limit(limit, 500),),
        )
    memories = []
    for row in rows:
        source = {}
        try:
            source = json.loads(row[4]) if row[4] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        memories.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "content": row[3],
                "source": source,
                "timestamp": row[5],
                "created_at": row[6],
            }
        )
    return {"memories": memories, "count": len(memories)}


async def do_list_episodes(agent_id: str, limit: int) -> dict:
    """List archived episodes for dashboard display."""
    db = await get_db()
    if agent_id:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, summary, keywords, start_time, end_time, created_at "
            "FROM episodes WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, _clamp_limit(limit, 200)),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, summary, keywords, start_time, end_time, created_at "
            "FROM episodes ORDER BY created_at DESC LIMIT ?",
            (_clamp_limit(limit, 200),),
        )
    episodes = []
    for row in rows:
        episodes.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "summary": row[2],
                "keywords": row[3],
                "start_time": row[4],
                "end_time": row[5],
                "created_at": row[6],
            }
        )
    return {"episodes": episodes, "count": len(episodes)}


async def do_delete_memory(memory_id: int, agent_id: str = "") -> dict:
    """Delete a single memory by ID.

    When agent_id is provided (non-empty), enforces ownership: only deletes
    if the memory belongs to that agent. When empty (dashboard/admin calls),
    deletes unconditionally.
    """
    db = await get_db()
    if agent_id:
        cursor = await db.execute(
            "DELETE FROM memories WHERE id = ? AND agent_id = ?",
            (memory_id, agent_id),
        )
    else:
        cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    await db.commit()
    if cursor.rowcount == 0:
        return {"error": f"Memory {memory_id} not found or not owned by agent"}

    # v2.3.5: Remove from remote vector index
    if VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url:
        ns = f"cpersona:{agent_id}" if agent_id else "cpersona:"
        try:
            base_url = _embedding_client._http_url.rsplit("/", 1)[0]
            await _embedding_client._client.post(
                f"{base_url}/remove",
                json={"namespace": ns, "ids": [f"mem:{memory_id}"]},
            )
        except Exception as e:
            logger.debug("Remote remove failed (non-fatal): %s", e)

    return {"ok": True, "deleted_id": memory_id}


async def do_delete_agent_data(agent_id: str) -> dict:
    """Delete ALL data for a specific agent (memories, profiles, episodes).

    Called by the kernel during agent deletion to prevent orphaned data.
    Requires a non-empty agent_id to prevent accidental full-table wipe.
    """
    if not agent_id:
        return {"error": "agent_id is required for bulk deletion"}

    db = await get_db()
    mem_cursor = await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent_id,))
    prof_cursor = await db.execute("DELETE FROM profiles WHERE agent_id = ?", (agent_id,))
    ep_cursor = await db.execute("DELETE FROM episodes WHERE agent_id = ?", (agent_id,))
    await db.commit()

    # v2.3.5: Purge remote vector index for this agent
    if VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url:
        try:
            base_url = _embedding_client._http_url.rsplit("/", 1)[0]
            await _embedding_client._client.post(
                f"{base_url}/purge",
                json={"namespace": f"cpersona:{agent_id}"},
            )
        except Exception as e:
            logger.debug("Remote purge failed (non-fatal): %s", e)

    result = {
        "ok": True,
        "agent_id": agent_id,
        "deleted_memories": mem_cursor.rowcount,
        "deleted_profiles": prof_cursor.rowcount,
        "deleted_episodes": ep_cursor.rowcount,
    }
    logger.info(
        "Deleted agent data for %s: %d memories, %d profiles, %d episodes",
        agent_id,
        mem_cursor.rowcount,
        prof_cursor.rowcount,
        ep_cursor.rowcount,
    )
    return result


async def do_calibrate_threshold(agent_id: str, sample_size: int = 0, z_factor: float = 0) -> dict:
    """Auto-calibrate VECTOR_MIN_SIMILARITY based on embedding distribution.

    Samples random embedding pairs from stored memories and computes their
    cosine similarity distribution as a null distribution (mostly unrelated
    pairs). The threshold is set at mean + z_factor * std, filtering out
    pairs that are not significantly above the noise floor.

    No ground-truth labels are used — calibration is purely statistical,
    adapting to both the embedding model and corpus characteristics.

    Strategy (z-score null distribution, lower tail):
      threshold = mean - z * std
      - z=1.0: filters bottom ~16% of noise (default, permissive)
      - z=2.0: filters bottom ~2% (very permissive)
      - z=0.5: filters bottom ~31% (moderate)
    A floor value prevents the threshold from being uselessly low.

    v2.3.7
    """
    import numpy as np

    global VECTOR_MIN_SIMILARITY

    db = await get_db()
    sample_n = sample_size or CALIBRATE_SAMPLE_SIZE
    z = z_factor or CALIBRATE_Z_FACTOR

    # Sample random embeddings from this agent's memories
    rows = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = ? AND embedding IS NOT NULL ORDER BY RANDOM() LIMIT ?",
        (agent_id, sample_n),
    )

    if len(rows) < 10:
        return {"ok": False, "error": f"Need at least 10 embeddings, found {len(rows)}"}

    # Decode embeddings
    vecs = []
    for (blob,) in rows:
        vec = np.frombuffer(blob, dtype=np.float32).copy()
        vecs.append(vec)
    vecs = np.array(vecs)  # (N, dim)

    # Compute pairwise cosine similarities (dot product on L2-normalized vecs)
    sim_matrix = vecs @ vecs.T  # (N, N)

    # Extract upper triangle (exclude diagonal = self-similarity = 1.0)
    n = len(vecs)
    triu_indices = np.triu_indices(n, k=1)
    pairwise_sims = sim_matrix[triu_indices]

    num_pairs = len(pairwise_sims)
    old_threshold = VECTOR_MIN_SIMILARITY

    # Compute statistics
    sim_mean = float(np.mean(pairwise_sims))
    sim_std = float(np.std(pairwise_sims))
    sim_median = float(np.median(pairwise_sims))

    # z-score based threshold: mean - z * std (lower tail), with floor
    z_threshold = sim_mean - z * sim_std
    new_threshold = max(z_threshold, CALIBRATE_FLOOR)

    # Apply
    VECTOR_MIN_SIMILARITY = round(new_threshold, 4)

    result = {
        "ok": True,
        "agent_id": agent_id,
        "sampled_embeddings": n,
        "num_pairs": num_pairs,
        "z_factor": z,
        "distribution": {
            "mean": round(sim_mean, 4),
            "std": round(sim_std, 4),
            "median": round(sim_median, 4),
        },
        "old_threshold": old_threshold,
        "new_threshold": VECTOR_MIN_SIMILARITY,
    }
    logger.info(
        "Calibrated VECTOR_MIN_SIMILARITY: %.4f → %.4f (z=%.1f of %d pairs, mean=%.4f, std=%.4f)",
        old_threshold,
        VECTOR_MIN_SIMILARITY,
        z,
        num_pairs,
        sim_mean,
        sim_std,
    )
    return result


async def do_delete_episode(episode_id: int, agent_id: str = "") -> dict:
    """Delete a single episode by ID (FTS5 triggers handle index cleanup).

    When agent_id is provided (non-empty), enforces ownership.
    """
    db = await get_db()
    if agent_id:
        cursor = await db.execute(
            "DELETE FROM episodes WHERE id = ? AND agent_id = ?",
            (episode_id, agent_id),
        )
    else:
        cursor = await db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    await db.commit()
    if cursor.rowcount == 0:
        return {"error": f"Episode {episode_id} not found or not owned by agent"}
    return {"ok": True, "deleted_id": episode_id}


async def do_export_memories(agent_id: str, output_path: str, include_embeddings: bool = False) -> dict:
    """Export memories, episodes, and profiles to a JSONL file.

    Each line is a self-contained JSON object with a `_type` field:
    header, memory, episode, or profile.
    Embeddings are excluded by default (model-dependent BLOBs).
    """
    db = await get_db()

    agent_filter = " WHERE agent_id = ?" if agent_id else ""
    agent_params: tuple = (agent_id,) if agent_id else ()

    # Pre-count for header
    mem_count = (await db.execute_fetchall(f"SELECT COUNT(*) FROM memories{agent_filter}", agent_params))[0][0]
    ep_count = (await db.execute_fetchall(f"SELECT COUNT(*) FROM episodes{agent_filter}", agent_params))[0][0]
    prof_count = (await db.execute_fetchall(f"SELECT COUNT(*) FROM profiles{agent_filter}", agent_params))[0][0]

    # Ensure output directory exists
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    exported_memories = 0
    exported_episodes = 0
    exported_profiles = 0

    import base64

    with open(output_path, "w", encoding="utf-8") as f:
        # Header line
        header = {
            "_type": "header",
            "version": "cpersona-export/1.0",
            "agent_id": agent_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "memory_count": mem_count,
            "episode_count": ep_count,
            "has_profile": prof_count > 0,
        }
        f.write(json.dumps(header, ensure_ascii=False) + "\n")

        # Memories
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, msg_id, content, source, timestamp, metadata, embedding, created_at"
            f" FROM memories{agent_filter} ORDER BY id",
            agent_params,
        )
        for row in rows:
            record: dict = {
                "_type": "memory",
                "id": row[0],
                "agent_id": row[1],
                "msg_id": row[2],
                "content": row[3],
                "source": _try_parse_json(row[4]) if row[4] else {},
                "timestamp": row[5],
                "metadata": _try_parse_json(row[6]) if row[6] else {},
                "created_at": row[8],
            }
            if include_embeddings and row[7]:
                record["embedding_b64"] = base64.b64encode(row[7]).decode("ascii")
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported_memories += 1

        # Episodes
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, summary, keywords, start_time, end_time, embedding, created_at, resolved"
            f" FROM episodes{agent_filter} ORDER BY id",
            agent_params,
        )
        for row in rows:
            record = {
                "_type": "episode",
                "id": row[0],
                "agent_id": row[1],
                "summary": row[2],
                "keywords": row[3],
                "start_time": row[4],
                "end_time": row[5],
                "created_at": row[7],
                "resolved": bool(row[8]) if row[8] else False,
            }
            if include_embeddings and row[6]:
                record["embedding_b64"] = base64.b64encode(row[6]).decode("ascii")
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported_episodes += 1

        # Profiles
        rows = await db.execute_fetchall(
            f"SELECT agent_id, user_id, content, updated_at FROM profiles{agent_filter} ORDER BY agent_id",
            agent_params,
        )
        for row in rows:
            record = {
                "_type": "profile",
                "agent_id": row[0],
                "user_id": row[1],
                "content": row[2],
                "updated_at": row[3],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported_profiles += 1

    return {
        "ok": True,
        "path": output_path,
        "memories": exported_memories,
        "episodes": exported_episodes,
        "profiles": exported_profiles,
    }


async def do_import_memories(input_path: str, target_agent_id: str = "", dry_run: bool = False) -> dict:
    """Import memories, episodes, and profiles from a JSONL file.

    - msg_id deduplication: memories with an existing msg_id are skipped (idempotent).
    - Embeddings are NOT imported (re-compute via store or embedding server).
    - Profiles are UPSERTed (overwrite on conflict).
    - Episode FTS5 triggers fire automatically on INSERT.
    """
    if not os.path.exists(input_path):
        return {"error": f"File not found: {input_path}"}

    db = await get_db()

    imported_memories = 0
    skipped_memories = 0
    imported_episodes = 0
    profile_updated = False
    errors: list[str] = []

    with open(input_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"Line {line_num}: invalid JSON: {e}")
                continue

            rtype = record.get("_type", "")

            if rtype == "header":
                continue

            elif rtype == "memory":
                aid = target_agent_id or record.get("agent_id", "")
                if not aid:
                    errors.append(f"Line {line_num}: memory missing agent_id")
                    continue

                content = record.get("content", "")
                if not content:
                    skipped_memories += 1
                    continue

                msg_id = record.get("msg_id", "")

                # Dedup check (even in dry_run, count as skip for accurate preview)
                if msg_id:
                    existing = await db.execute_fetchall(
                        "SELECT id FROM memories WHERE agent_id = ? AND msg_id = ? LIMIT 1",
                        (aid, msg_id),
                    )
                    if existing:
                        skipped_memories += 1
                        continue

                if not dry_run:
                    source = json.dumps(record.get("source", {}))
                    timestamp = record.get("timestamp", "")
                    metadata = json.dumps(record.get("metadata", {}))
                    await db.execute(
                        "INSERT INTO memories (agent_id, msg_id, content, source, timestamp, metadata)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (aid, msg_id, content, source, timestamp, metadata),
                    )
                imported_memories += 1

            elif rtype == "episode":
                aid = target_agent_id or record.get("agent_id", "")
                if not aid:
                    errors.append(f"Line {line_num}: episode missing agent_id")
                    continue

                summary = record.get("summary", "")
                if not summary:
                    continue

                if not dry_run:
                    keywords = record.get("keywords", "")
                    start_time = record.get("start_time")
                    end_time = record.get("end_time")
                    resolved = 1 if record.get("resolved") else 0
                    await db.execute(
                        "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, resolved)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (aid, summary, keywords, start_time, end_time, resolved),
                    )
                imported_episodes += 1

            elif rtype == "profile":
                aid = target_agent_id or record.get("agent_id", "")
                if not aid:
                    errors.append(f"Line {line_num}: profile missing agent_id")
                    continue

                content = record.get("content", "")
                if not content:
                    continue

                if not dry_run:
                    user_id = record.get("user_id", "")
                    await db.execute(
                        "INSERT INTO profiles (agent_id, user_id, content, updated_at)"
                        " VALUES (?, ?, ?, datetime('now'))"
                        " ON CONFLICT(agent_id, user_id) DO UPDATE SET"
                        "   content = excluded.content,"
                        "   updated_at = excluded.updated_at",
                        (aid, user_id, content),
                    )
                profile_updated = True

            else:
                if rtype:
                    errors.append(f"Line {line_num}: unknown type '{rtype}'")

    if not dry_run:
        await db.commit()

    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "imported_memories": imported_memories,
        "skipped_memories": skipped_memories,
        "imported_episodes": imported_episodes,
        "profile_updated": profile_updated,
    }
    if errors:
        result["errors"] = errors
    return result


async def do_merge_memories(
    source_agent_id: str,
    target_agent_id: str,
    strategy: str = "skip",
    mode: str = "copy",
    dry_run: bool = False,
) -> dict:
    """Merge memories, episodes, and profiles from one agent into another.

    Atomic one-shot equivalent of export(source) → import(target).
    No intermediate files; operates directly on the database.

    - strategy="skip": skip records where target already has the same msg_id (default).
    - mode="copy": preserve source data. mode="move": delete source after merge.
    - Embeddings are NOT copied (re-computed on next recall, consistent with import).
    - Profiles are copied only if target has no profile for the same user_id.
    - FTS5 indexes are updated automatically via SQLite triggers.
    """
    if not source_agent_id:
        return {"error": "source_agent_id is required"}
    if not target_agent_id:
        return {"error": "target_agent_id is required"}
    if source_agent_id == target_agent_id:
        return {"error": "source_agent_id and target_agent_id must differ"}
    if strategy != "skip":
        return {"error": f"Unsupported strategy '{strategy}'. Currently supported: 'skip'"}
    if mode not in ("copy", "move"):
        return {"error": f"Invalid mode '{mode}'. Supported: 'copy', 'move'"}

    db = await get_db()

    merged_memories = 0
    skipped_memories = 0
    merged_episodes = 0
    skipped_episodes = 0
    profile_copied = False
    skipped_profile = False

    # --- Memories ---
    rows = await db.execute_fetchall(
        "SELECT msg_id, content, source, timestamp, metadata, channel FROM memories WHERE agent_id = ?",
        (source_agent_id,),
    )
    for msg_id, content, source, timestamp, metadata, channel in rows:
        if not content:
            continue
        # Dedup by msg_id
        if msg_id:
            existing = await db.execute_fetchall(
                "SELECT id FROM memories WHERE agent_id = ? AND msg_id = ? LIMIT 1",
                (target_agent_id, msg_id),
            )
            if existing:
                skipped_memories += 1
                continue
        if not dry_run:
            await db.execute(
                "INSERT INTO memories (agent_id, msg_id, content, source, timestamp, metadata, channel)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (target_agent_id, msg_id, content, source, timestamp, metadata, channel),
            )
        merged_memories += 1

    # --- Episodes ---
    rows = await db.execute_fetchall(
        "SELECT summary, keywords, start_time, end_time, resolved FROM episodes WHERE agent_id = ?",
        (source_agent_id,),
    )
    for summary, keywords, start_time, end_time, resolved in rows:
        if not summary:
            continue
        # Dedup by summary text
        existing = await db.execute_fetchall(
            "SELECT id FROM episodes WHERE agent_id = ? AND summary = ? LIMIT 1",
            (target_agent_id, summary),
        )
        if existing:
            skipped_episodes += 1
            continue
        if not dry_run:
            await db.execute(
                "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, resolved)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (target_agent_id, summary, keywords, start_time, end_time, resolved),
            )
        merged_episodes += 1

    # --- Profiles (copy only if target has no profile for that user_id) ---
    rows = await db.execute_fetchall(
        "SELECT user_id, content FROM profiles WHERE agent_id = ?",
        (source_agent_id,),
    )
    for user_id, content in rows:
        if not content:
            continue
        existing = await db.execute_fetchall(
            "SELECT id FROM profiles WHERE agent_id = ? AND user_id = ? LIMIT 1",
            (target_agent_id, user_id),
        )
        if existing:
            skipped_profile = True
            continue
        if not dry_run:
            await db.execute(
                "INSERT INTO profiles (agent_id, user_id, content, updated_at) VALUES (?, ?, ?, datetime('now'))",
                (target_agent_id, user_id, content),
            )
        profile_copied = True

    if not dry_run:
        await db.commit()

    # --- Move mode: delete source after successful merge ---
    move_result = None
    if mode == "move" and not dry_run:
        move_result = await do_delete_agent_data(source_agent_id)

    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "source_agent_id": source_agent_id,
        "target_agent_id": target_agent_id,
        "strategy": strategy,
        "mode": mode,
        "merged_memories": merged_memories,
        "skipped_memories": skipped_memories,
        "merged_episodes": merged_episodes,
        "skipped_episodes": skipped_episodes,
        "profile_copied": profile_copied,
        "skipped_profile": skipped_profile,
    }
    if move_result:
        result["source_deleted"] = move_result

    logger.info(
        "Merge %s → %s (%s, %s): %d memories (+%d skipped), %d episodes (+%d skipped), profile=%s%s",
        source_agent_id,
        target_agent_id,
        strategy,
        mode,
        merged_memories,
        skipped_memories,
        merged_episodes,
        skipped_episodes,
        "copied" if profile_copied else ("skipped" if skipped_profile else "none"),
        " [DRY RUN]" if dry_run else "",
    )
    return result


# --- Tool registrations (must be after all do_* definitions) ---

registry.auto_tool(
    "store",
    "Store a message in agent memory for future recall.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "message": {
                "type": "object",
                "description": "ClotoMessage to store (id, content, source, timestamp, metadata)",
            },
            "channel": {
                "type": "string",
                "description": "Memory channel for context separation (e.g. 'chat', 'discord'). Default: '' (shared).",
            },
        },
        "required": ["agent_id", "message"],
    },
    do_store,
    [("agent_id", str), ("message", dict), ("channel", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "recall",
    "Recall relevant memories using multi-strategy search (vector + FTS5 + keyword).",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "query": {"type": "string", "description": "Search query (empty returns recent memories)"},
            "limit": {"type": "integer", "description": "Max memories to return", "default": 10},
            "deep": {
                "type": "boolean",
                "description": "Deep recall — disable time and completion decay for exhaustive search",
                "default": False,
            },
            "channel": {
                "type": "string",
                "description": "Filter memories by channel (e.g. 'chat', 'discord'). Default: '' (all channels).",
            },
        },
        "required": ["agent_id", "query"],
    },
    do_recall,
    [("agent_id", str), ("query", str), ("limit", int, 10), ("deep", bool, False), ("channel", str, "")],
    annotations=ToolAnnotations(readOnlyHint=True),
)


async def do_update_profile_or_queue(agent_id: str, profile: str = "") -> dict:
    """Save pre-computed profile. Queue is bypassed since no LLM processing is needed."""
    return await do_update_profile(agent_id, profile=profile)


async def do_archive_episode_or_queue(
    agent_id: str, history: list, summary: str = "", keywords: str = "", resolved: bool | None = None
) -> dict:
    """Enqueue episode archival if task queue is enabled, otherwise run synchronously.

    When summary/keywords are pre-computed, bypass the queue and store directly
    (no LLM call needed, so queuing for retry is unnecessary).
    """
    if summary:
        # Pre-computed: store directly, no LLM needed
        return await do_archive_episode(agent_id, history, summary=summary, keywords=keywords, resolved=resolved)
    if _task_queue and TASK_QUEUE_ENABLED:
        task_id = await _task_queue.enqueue("archive_episode", agent_id, history)
        return {"ok": True, "queued": True, "task_id": task_id}
    return await do_archive_episode(agent_id, history)


async def do_get_queue_status() -> dict:
    """Get the status of the background task queue."""
    if _task_queue and TASK_QUEUE_ENABLED:
        return await _task_queue.get_status()
    return {"enabled": False, "pending": 0}


registry.auto_tool(
    "get_profile",
    "Get the current profile for an agent.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
        },
        "required": ["agent_id"],
    },
    do_get_profile,
    [("agent_id", str)],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "update_profile",
    "Save a pre-computed agent profile to the database.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "profile": {
                "type": "string",
                "description": "Profile text to save (pre-computed by caller)",
            },
        },
        "required": ["agent_id", "profile"],
    },
    do_update_profile_or_queue,
    [("agent_id", str), ("profile", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)

registry.auto_tool(
    "archive_episode",
    "Archive a conversation episode with pre-computed summary, keywords, and resolved status. "
    "All LLM processing is performed by the caller.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "history": {
                "type": "array",
                "description": "Original conversation messages (used for timestamp extraction and embedding)",
                "items": {"type": "object"},
            },
            "summary": {
                "type": "string",
                "description": "Episode summary (pre-computed by caller)",
            },
            "keywords": {
                "type": "string",
                "description": "Space-separated keywords (pre-computed by caller)",
            },
            "resolved": {
                "type": "boolean",
                "description": "Whether the topic was completed/concluded",
            },
        },
        "required": ["agent_id", "summary"],
    },
    do_archive_episode_or_queue,
    [("agent_id", str), ("history", list, []), ("summary", str, ""), ("keywords", str, ""), ("resolved", bool, None)],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "list_memories",
    "List recent memories for an agent (for dashboard display).",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier (empty for all agents)"},
            "limit": {"type": "integer", "description": "Max memories to return", "default": 100},
        },
        "required": [],
    },
    do_list_memories,
    [("agent_id", str), ("limit", int, 100)],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "list_episodes",
    "List archived episodes for an agent (for dashboard display).",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier (empty for all agents)"},
            "limit": {"type": "integer", "description": "Max episodes to return", "default": 50},
        },
        "required": [],
    },
    do_list_episodes,
    [("agent_id", str), ("limit", int, 50)],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "delete_agent_data",
    "Delete ALL data (memories, profiles, episodes) for a specific agent. Used by kernel during agent deletion.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID whose data should be purged"},
        },
        "required": ["agent_id"],
    },
    do_delete_agent_data,
    [("agent_id", str)],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
)

registry.auto_tool(
    "calibrate_threshold",
    "Auto-calibrate vector search threshold using null distribution z-score. "
    "Samples random memory pairs, computes cosine distribution, sets threshold "
    "at mean + z*std. No labels used, purely statistical. Adapts to both "
    "embedding model and corpus characteristics.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID whose memories to sample"},
            "sample_size": {"type": "integer", "description": "Number of embeddings to sample (default: 200)"},
            "z_factor": {"type": "number", "description": "Z-score multiplier (default: 1.0, higher = stricter)"},
        },
        "required": ["agent_id"],
    },
    do_calibrate_threshold,
    [("agent_id", str), ("sample_size", int, 0), ("z_factor", float, 0)],
)

registry.auto_tool(
    "delete_memory",
    "Delete a single memory by ID. Ownership is enforced when agent_id is provided.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID for ownership verification (injected by kernel)"},
            "memory_id": {"type": "integer", "description": "Memory ID to delete"},
        },
        "required": ["memory_id"],
    },
    do_delete_memory,
    [("memory_id", int), ("agent_id", str)],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
)

registry.auto_tool(
    "delete_episode",
    "Delete a single episode by ID. Ownership is enforced when agent_id is provided.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID for ownership verification (injected by kernel)"},
            "episode_id": {"type": "integer", "description": "Episode ID to delete"},
        },
        "required": ["episode_id"],
    },
    do_delete_episode,
    [("episode_id", int), ("agent_id", str)],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
)

registry.auto_tool(
    "get_queue_status",
    "Get the status of the background task queue (pending tasks, retry config).",
    {
        "type": "object",
        "properties": {},
    },
    do_get_queue_status,
    [],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "export_memories",
    "Export memories, episodes, and profiles to a JSONL file for backup or portability.",
    {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Agent identifier (empty string to export all agents)",
            },
            "output_path": {
                "type": "string",
                "description": "File path for the JSONL output",
            },
            "include_embeddings": {
                "type": "boolean",
                "description": "Include embedding BLOBs as base64 (default false, usually not needed)",
                "default": False,
            },
        },
        "required": ["agent_id", "output_path"],
    },
    do_export_memories,
    [("agent_id", str), ("output_path", str), ("include_embeddings", bool, False)],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "import_memories",
    "Import memories, episodes, and profiles from a JSONL file. Idempotent via msg_id deduplication.",
    {
        "type": "object",
        "properties": {
            "input_path": {
                "type": "string",
                "description": "Path to the JSONL file to import",
            },
            "target_agent_id": {
                "type": "string",
                "description": "Remap all records to this agent ID (empty to use original agent_id from file)",
                "default": "",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Count records without writing to DB (preview mode)",
                "default": False,
            },
        },
        "required": ["input_path"],
    },
    do_import_memories,
    [("input_path", str), ("target_agent_id", str, ""), ("dry_run", bool, False)],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "merge_memories",
    "Merge memories, episodes, and profiles from one agent into another. "
    "Atomic one-shot equivalent of export→import without intermediate files. "
    "Strategy 'skip' deduplicates by msg_id (memories) and summary (episodes).",
    {
        "type": "object",
        "properties": {
            "source_agent_id": {
                "type": "string",
                "description": "Agent ID to merge FROM",
            },
            "target_agent_id": {
                "type": "string",
                "description": "Agent ID to merge INTO",
            },
            "strategy": {
                "type": "string",
                "description": "Merge strategy: 'skip' (default) — skip duplicates, keep target's version",
                "default": "skip",
            },
            "mode": {
                "type": "string",
                "description": "Merge mode: 'copy' (preserve source) or 'move' (delete source after merge)",
                "default": "copy",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Preview merge without writing to DB",
                "default": False,
            },
        },
        "required": ["source_agent_id", "target_agent_id"],
    },
    do_merge_memories,
    [
        ("source_agent_id", str),
        ("target_agent_id", str),
        ("strategy", str, "skip"),
        ("mode", str, "copy"),
        ("dry_run", bool, False),
    ],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)


async def do_check_health(agent_id: str = "", fix: bool = False) -> dict:
    """Check and optionally fix memory database health issues."""
    db = await get_db()
    issues = []

    # agent_clause is always either "" or "AND agent_id = ?" (parameterized).
    # It is NOT user-controlled — safe for f-string interpolation.
    agent_clause = "AND agent_id = ?" if agent_id else ""
    agent_params = (agent_id,) if agent_id else ()

    # 1. [Memory from ...] annotations in content
    rows = await db.execute_fetchall(
        f"SELECT id, content FROM memories WHERE content LIKE '%[Memory from%' {agent_clause}",
        agent_params,
    )
    if rows:
        issues.append({"type": "memory_annotation", "count": len(rows)})
        if fix:
            for row_id, content in rows:
                cleaned = _MEMORY_ANNOTATION_PATTERN.sub("", content).strip()
                await db.execute("UPDATE memories SET content = ? WHERE id = ?", (cleaned, row_id))

    # 2. Discord mentions in content
    rows = await db.execute_fetchall(
        f"SELECT id, content FROM memories WHERE content LIKE '%<@%' {agent_clause}",
        agent_params,
    )
    if rows:
        issues.append({"type": "discord_mention", "count": len(rows)})
        if fix:
            for row_id, content in rows:
                cleaned = _MENTION_PATTERN.sub("", content).strip()
                await db.execute("UPDATE memories SET content = ? WHERE id = ?", (cleaned, row_id))

    # 3. Duplicate content
    dup_rows = await db.execute_fetchall(
        f"""SELECT content, COUNT(*) as cnt FROM memories
            WHERE 1=1 {agent_clause}
            GROUP BY agent_id, content HAVING cnt > 1""",
        agent_params,
    )
    if dup_rows:
        total_dupes = sum(r[1] - 1 for r in dup_rows)
        issues.append({"type": "duplicate_content", "groups": len(dup_rows), "total_extra": total_dupes})
        if fix:
            await db.execute(
                "DELETE FROM memories WHERE id NOT IN (SELECT MIN(id) FROM memories GROUP BY agent_id, content)"
            )

    # 4. Oversized content
    rows = await db.execute_fetchall(
        f"SELECT id, length(content) as len FROM memories WHERE length(content) > ? {agent_clause}",
        (MAX_CONTENT_LENGTH, *agent_params),
    )
    if rows:
        issues.append({"type": "oversized_content", "count": len(rows), "max_len": max(r[1] for r in rows)})
        if fix:
            for row_id, _ in rows:
                await db.execute(
                    "UPDATE memories SET content = SUBSTR(content, 1, ?) WHERE id = ?",
                    (MAX_CONTENT_LENGTH, row_id),
                )

    # 5. Empty channel
    count = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE channel = '' {agent_clause}",
            agent_params,
        )
    )[0][0]
    if count > 0:
        issues.append({"type": "empty_channel", "count": count})
        if fix:
            await db.execute(
                f"UPDATE memories SET channel = 'chat' WHERE channel = '' {agent_clause}",
                agent_params,
            )

    # 6. Embedding dimension mismatch
    if _embedding_client:
        try:
            test_emb = await _embedding_client.embed(["test"])
            if test_emb and test_emb[0]:
                expected_bytes = len(test_emb[0]) * 4
                mismatched = (
                    await db.execute_fetchall(
                        f"""SELECT COUNT(*) FROM memories
                        WHERE embedding IS NOT NULL AND length(embedding) != ?
                        {agent_clause}""",
                        (expected_bytes, *agent_params),
                    )
                )[0][0]
                if mismatched > 0:
                    issues.append(
                        {
                            "type": "embedding_dimension_mismatch",
                            "count": mismatched,
                            "expected_dim": len(test_emb[0]),
                        }
                    )
        except Exception as e:
            logger.warning("Embedding dimension check failed: %s", e)

    # 7. Null embeddings
    null_count = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE embedding IS NULL {agent_clause}",
            agent_params,
        )
    )[0][0]
    if null_count > 0:
        issues.append({"type": "null_embedding", "count": null_count})

    # 7b. Null embedding auto-repair (batch limit: 50)
    if null_count > 0 and fix and _embedding_client:
        rows = await db.execute_fetchall(
            f"SELECT id, content FROM memories WHERE embedding IS NULL {agent_clause} LIMIT 50",
            agent_params,
        )
        re_embedded = 0
        for row_id, content in rows:
            try:
                emb = await _embedding_client.embed([content])
                if emb and emb[0]:
                    blob = _embedding_client.pack_embedding(emb[0])
                    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (blob, row_id))
                    re_embedded += 1
            except Exception:
                pass
        if re_embedded > 0:
            # Annotate the null_embedding issue with repair count
            for issue in issues:
                if issue["type"] == "null_embedding":
                    issue["re_embedded"] = re_embedded
                    break

    # 8. FTS5 sync verification
    try:
        mem_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories"))[0][0]
        mem_fts_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories_fts"))[0][0]
        if mem_count != mem_fts_count:
            issues.append({"type": "fts_memories_desync", "memories": mem_count, "fts": mem_fts_count})
            if fix:
                await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

        ep_count = (await db.execute_fetchall("SELECT COUNT(*) FROM episodes"))[0][0]
        ep_fts_count = (await db.execute_fetchall("SELECT COUNT(*) FROM episodes_fts"))[0][0]
        if ep_count != ep_fts_count:
            issues.append({"type": "fts_episodes_desync", "episodes": ep_count, "fts": ep_fts_count})
            if fix:
                await db.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('rebuild')")
    except Exception:
        pass  # FTS tables may not exist in very old DBs

    # 9. Schema version verification
    try:
        db_version = (await db.execute_fetchall("SELECT MAX(version) FROM schema_version"))[0][0]
        if db_version != SCHEMA_VERSION:
            issues.append(
                {
                    "type": "schema_version_mismatch",
                    "db_version": db_version,
                    "expected": SCHEMA_VERSION,
                }
            )
    except Exception:
        pass

    # 10. JSON validity (source, metadata fields)
    try:
        bad_source = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories WHERE json_valid(source) = 0 {agent_clause}",
                agent_params,
            )
        )[0][0]
        bad_metadata = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories WHERE json_valid(metadata) = 0 {agent_clause}",
                agent_params,
            )
        )[0][0]
        if bad_source + bad_metadata > 0:
            issues.append(
                {
                    "type": "invalid_json",
                    "bad_source": bad_source,
                    "bad_metadata": bad_metadata,
                }
            )
            if fix:
                await db.execute(
                    f"UPDATE memories SET source = '{{}}' WHERE json_valid(source) = 0 {agent_clause}",
                    agent_params,
                )
                await db.execute(
                    f"UPDATE memories SET metadata = '{{}}' WHERE json_valid(metadata) = 0 {agent_clause}",
                    agent_params,
                )
    except Exception:
        pass  # json_valid() requires SQLite 3.38+

    # 11. Timestamp consistency
    bad_ts = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE datetime(timestamp) IS NULL AND timestamp != '' {agent_clause}",
            agent_params,
        )
    )[0][0]
    if bad_ts > 0:
        issues.append({"type": "invalid_timestamp", "count": bad_ts})
        if fix:
            await db.execute(
                f"UPDATE memories SET timestamp = created_at WHERE datetime(timestamp) IS NULL AND timestamp != '' {agent_clause}",
                agent_params,
            )

    # 12. Stale pending tasks (older than 1 hour)
    stale_tasks = (
        await db.execute_fetchall(
            "SELECT COUNT(*) FROM pending_memory_tasks WHERE created_at < datetime('now', '-1 hour')"
        )
    )[0][0]
    if stale_tasks > 0:
        issues.append({"type": "stale_pending_tasks", "count": stale_tasks})
        if fix:
            await db.execute("DELETE FROM pending_memory_tasks WHERE created_at < datetime('now', '-1 hour')")

    # 13. Missing profiles (agents with memories but no profile)
    missing = await db.execute_fetchall(
        """SELECT DISTINCT m.agent_id FROM memories m
           LEFT JOIN profiles p ON m.agent_id = p.agent_id
           WHERE p.id IS NULL"""
    )
    if missing:
        agents = [r[0] for r in missing]
        issues.append({"type": "missing_profile", "count": len(agents), "agents": agents})

    if fix:
        await db.commit()

    total = (await db.execute_fetchall(f"SELECT COUNT(*) FROM memories WHERE 1=1 {agent_clause}", agent_params))[0][0]

    # 14. Storage statistics (informational, does not affect healthy status)
    try:
        page_info = await db.execute_fetchall("PRAGMA page_count")
        page_size_info = await db.execute_fetchall("PRAGMA page_size")
        db_size_bytes = page_info[0][0] * page_size_info[0][0]
    except Exception:
        db_size_bytes = 0

    stats = {
        "db_size_bytes": db_size_bytes,
        "memories": total,
        "episodes": (await db.execute_fetchall("SELECT COUNT(*) FROM episodes"))[0][0],
        "profiles": (await db.execute_fetchall("SELECT COUNT(*) FROM profiles"))[0][0],
        "pending_tasks": (await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks"))[0][0],
    }
    if agent_id:
        stats["agent_memories"] = total
        stats["agent_episodes"] = (
            await db.execute_fetchall("SELECT COUNT(*) FROM episodes WHERE agent_id = ?", (agent_id,))
        )[0][0]

    return {
        "total_memories": total,
        "issues": issues,
        "healthy": len(issues) == 0,
        "fixed": fix,
        "stats": stats,
    }


registry.auto_tool(
    "check_health",
    "Check memory database health (15 checks). Detects contamination, duplicates, "
    "oversized content, embedding issues, FTS desync, invalid JSON/timestamps, "
    "stale tasks, missing profiles. Returns storage stats. Set fix=true to auto-repair.",
    {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Agent ID to check (empty = all agents)",
            },
            "fix": {
                "type": "boolean",
                "description": "Auto-fix detected issues",
                "default": False,
            },
        },
    },
    do_check_health,
    [("agent_id", str, ""), ("fix", bool, False)],
    annotations=ToolAnnotations(readOnlyHint=False),
)


async def _run_http_server():
    """Run CPersona as a Streamable HTTP MCP server with Bearer token auth."""
    import contextlib
    from collections.abc import AsyncIterator

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    auth_token = os.environ.get("CPERSONA_AUTH_TOKEN", "")

    session_manager = StreamableHTTPSessionManager(
        app=registry.server,
        stateless=True,
    )

    async def mcp_endpoint(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    class BearerTokenMiddleware:
        """Simple Bearer token authentication middleware."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            request = Request(scope, receive)
            # Allow CORS preflight without auth
            if request.method == "OPTIONS":
                await self.app(scope, receive, send)
                return
            header = request.headers.get("authorization", "")
            if auth_token and header:
                # Validate token if both are present
                if not header.startswith("Bearer ") or header[7:] != auth_token:
                    response = JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await response(scope, receive, send)
                    return
            elif auth_token and not header:
                # Token configured but not provided — allow (authless for Claude web)
                pass
            await self.app(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("CPersona Streamable HTTP server ready")
            yield

    app = Starlette(
        routes=[Mount("/mcp", app=mcp_endpoint), Mount("/", app=mcp_endpoint)],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["https://claude.ai", "https://www.claude.ai"],
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                allow_headers=[
                    "Authorization",
                    "Content-Type",
                    "Mcp-Session-Id",
                    "Mcp-Protocol-Version",
                    "Last-Event-Id",
                ],
                expose_headers=["Mcp-Session-Id"],
            ),
            Middleware(BearerTokenMiddleware),
        ],
        lifespan=lifespan,
    )

    host = os.environ.get("CPERSONA_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("CPERSONA_HTTP_PORT", "8402"))
    logger.info("Starting Streamable HTTP on %s:%d", host, port)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    global _embedding_client, _task_queue

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Initialize embedding client
    if EMBEDDING_MODE != "none":
        _embedding_client = EmbeddingClient(
            mode=EMBEDDING_MODE,
            http_url=EMBEDDING_URL,
            api_key=EMBEDDING_API_KEY,
            api_url=EMBEDDING_API_URL,
            model=EMBEDDING_MODEL,
        )
        await _embedding_client.initialize()
        logger.info("Embedding client ready (mode=%s)", EMBEDDING_MODE)
    else:
        logger.info("Embedding disabled (mode=none), using FTS5 + keyword only")

    # Initialize DB on startup
    await get_db()

    # Start background task queue (Phase 5)
    if TASK_QUEUE_ENABLED:
        _task_queue = MemoryTaskQueue()
        await _task_queue.start()
    else:
        logger.info("Task queue disabled")

    try:
        transport = os.environ.get("CPERSONA_TRANSPORT", "stdio")
        if transport == "stdio":
            async with stdio_server() as (read_stream, write_stream):
                await registry.server.run(read_stream, write_stream, registry.server.create_initialization_options())
        elif transport == "streamable-http":
            await _run_http_server()
        else:
            raise ValueError(f"Unknown transport: {transport}")
    finally:
        if _task_queue:
            await _task_queue.stop()
        await close_db()
        if _embedding_client:
            await _embedding_client.close()


if __name__ == "__main__":
    asyncio.run(main())
