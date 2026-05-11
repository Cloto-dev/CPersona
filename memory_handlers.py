"""Memory read-write path handlers for CPersona.

Tools: do_store, do_recall, do_recall_with_context, do_archive_episode.

Profile handlers (do_get_profile / do_update_profile) live in admin_handlers.py;
this module re-exports do_update_profile reference only through tasks.py's
lazy queue dispatch.

Accesses `vector._embedding_client` as a module attribute (set by server.main()).
"""

import json
import logging
import math
import re
from datetime import datetime, timezone

import aiosqlite
import httpx

import vector
from config import (
    AUTOCUT_ENABLED,
    CONFIDENCE_ENABLED,
    FTS_ENABLED,
    MAX_CONTENT_LENGTH,
    MAX_MEMORIES,
    RECALL_MODE,
    RRF_K,
    RRF_THRESHOLD_FACTOR,
    STORE_BLOB,
    VECTOR_SEARCH_MODE,
)
import config  # for runtime-mutable VECTOR_MIN_SIMILARITY access
from database import get_db
from utils import (
    _compute_confidence,
    _content_excluded,
    _parse_timestamp_utc,
    _sanitize_content,
    _try_parse_json,
)
from vector import EmbeddingClient, _search_vector

logger = logging.getLogger(__name__)


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

    content = _sanitize_content(raw_content)
    truncated = len(raw_content) > MAX_CONTENT_LENGTH

    if not content:
        return {"ok": True, "skipped": True, "reason": "empty after sanitization"}

    if msg_id:
        row = await db.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = ? AND msg_id = ? LIMIT 1",
            (agent_id, msg_id),
        )
        if row:
            return {"ok": True, "skipped": True, "reason": "duplicate msg_id"}

    existing = await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND channel = ? AND content = ? LIMIT 1",
        (agent_id, channel, content),
    )
    if existing:
        return {"ok": True, "skipped": True, "reason": "duplicate content"}

    embedding_blob = None
    if vector._embedding_client and (VECTOR_SEARCH_MODE == "local" or STORE_BLOB):
        try:
            embeddings = await vector._embedding_client.embed([content])
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

    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        try:
            row = await db.execute_fetchall(
                "SELECT id FROM memories WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
                (agent_id,),
            )
            if row:
                mem_id = row[0][0]
                base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
                await vector._embedding_client._client.post(
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
    exclude_set: set[str] | None = None,
) -> list[dict]:
    """Original cascading recall: stages fill remaining slots sequentially."""
    results: list[dict] = []
    seen_ids: set = set()
    _excl = exclude_set or set()

    if vector._embedding_client and query.strip():
        vector_results = await _search_vector(db, agent_id, query, limit, channel=channel)
        for row in vector_results:
            rid = row.get("_rid", row["id"])
            if rid not in seen_ids and not _content_excluded(row["content"], _excl):
                results.append(row)
                seen_ids.add(rid)

    if FTS_ENABLED and query.strip():
        fts_results = await _search_episodes_fts(db, agent_id, query, limit)
        for row in fts_results:
            rid = ("ep", row["id"])
            if rid not in seen_ids:
                results.append(row)
                seen_ids.add(rid)

    profile_rows = await db.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ? AND user_id = '' ORDER BY updated_at DESC LIMIT 3",
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

    remaining = max(0, limit - len(results))
    if remaining > 0:
        memory_rows = await _search_memories_keyword(db, agent_id, query, remaining, channel=channel)
        for row in memory_rows:
            rid = ("mem", row["id"])
            if rid not in seen_ids and not _content_excluded(row["content"], _excl):
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
    exclude_set: set[str] | None = None,
) -> list[dict]:
    """v2.4 RRF recall: run vector and FTS5 independently, merge with
    Reciprocal Rank Fusion. Avoids cascade's positional bias.
    """
    k = RRF_K
    doc_map: dict[tuple, dict] = {}
    rrf_scores: dict[tuple, float] = {}
    _excl = exclude_set or set()

    rrf_min_sim = config.VECTOR_MIN_SIMILARITY * RRF_THRESHOLD_FACTOR
    if vector._embedding_client:
        vector_results = await _search_vector(db, agent_id, query, limit, min_similarity=rrf_min_sim, channel=channel)
        for rank, row in enumerate(vector_results):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = row.get("_rid", ("mem", row["id"]))
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    if FTS_ENABLED:
        fts_ep_results = await _search_episodes_fts(db, agent_id, query, limit)
        for rank, row in enumerate(fts_ep_results):
            rid = ("ep", row["id"])
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    if FTS_ENABLED:
        fts_mem_results = await _search_memories_keyword(db, agent_id, query, limit, channel=channel)
        for rank, row in enumerate(fts_mem_results):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = ("mem", row["id"])
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    sorted_rids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)
    results = []
    for rid in sorted_rids:
        row = doc_map[rid]
        row["_rrf_score"] = rrf_scores[rid]
        results.append(row)

    profile_rows = await db.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ? AND user_id = '' ORDER BY updated_at DESC LIMIT 3",
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
    """Detect the largest score gap in results and cut below it (Weaviate autocut)."""
    if len(results) < 2:
        return results
    scores = [r.get("_rrf_score") or r.get("_cosine") or 0 for r in results]
    gaps = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]
    if not gaps or max(gaps) <= 0:
        return results
    cut_idx = max(range(len(gaps)), key=lambda i: gaps[i]) + 1
    return results[:cut_idx]


def _adaptive_min_score(memory_count: int) -> float:
    """Compute adaptive quality threshold based on memory pool size."""
    if memory_count <= 0:
        return 1.0
    t = min(1.0, math.log(memory_count + 1) / math.log(500))
    return round(0.5 - t * 0.3, 4)


def _apply_quality_gate(
    results: list[dict],
    min_score: float,
    memory_count: int,
) -> list[dict]:
    """Adaptive quality gate — removes results below dynamic threshold."""
    if not results:
        return results

    filtered = []
    for r in results:
        if r.get("id") == -1:
            if memory_count >= 50:
                filtered.append(r)
            continue

        score = r.get("_confidence_score") or r.get("_rrf_score") or r.get("_cosine")

        if score is not None:
            if score >= min_score:
                filtered.append(r)
        else:
            if memory_count >= 100:
                filtered.append(r)

    return filtered


async def do_recall(
    agent_id: str,
    query: str,
    limit: int,
    deep: bool = False,
    channel: str = "",
    exclude_contents: list | None = None,
) -> dict:
    """Recall relevant memories using multi-strategy search."""
    db = await get_db()

    exclude_set: set[str] = set()
    if exclude_contents:
        exclude_set = {c.strip().lower() for c in exclude_contents if c.strip()}

    if RECALL_MODE == "rrf" and query.strip():
        results = await _recall_rrf(db, agent_id, query, limit, deep, channel, exclude_set)
    else:
        results = await _recall_cascade(db, agent_id, query, limit, deep, channel, exclude_set)

    time_range_hours = 0.0
    recall_counts: dict[int, tuple[int, str]] = {}
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

        mem_ids = [r["id"] for r in results if isinstance(r.get("id"), int) and r["id"] > 0]
        if mem_ids:
            placeholders = ",".join("?" * len(mem_ids))
            rc_rows = await db.execute_fetchall(
                f"SELECT id, recall_count, last_recalled_at FROM memories WHERE id IN ({placeholders})",
                mem_ids,
            )
            recall_counts = {r[0]: (r[1], r[2] or "") for r in rc_rows}

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

    memory_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (agent_id,)))[0][0]
    min_score = _adaptive_min_score(memory_count)
    effective_min = min_score * 0.5 if deep else min_score
    results = _apply_quality_gate(results, effective_min, memory_count)

    if AUTOCUT_ENABLED:
        results = _autocut(results)

    results = results[:limit]
    results.reverse()

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
        r.pop("_rid", None)
        r.pop("_cosine", None)
        r.pop("_confidence_score", None)
        r.pop("_rrf_score", None)
        r.pop("_resolved", None)
        messages.append(msg)

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


async def do_recall_with_context(
    agent_id: str,
    query: str,
    external_context: list | None = None,
    limit: int = 10,
    channel: str = "",
    deep: bool = False,
) -> dict:
    """Recall memories and merge with external conversation context."""
    ctx = external_context or []

    exclude_list = [e["content"].strip().lower() for e in ctx if e.get("content", "").strip()]

    recall_result = await do_recall(agent_id, query, limit, deep=deep, channel=channel, exclude_contents=exclude_list)
    messages = recall_result.get("messages", [])

    for entry in ctx:
        role = entry.get("role", "")
        content = entry.get("content", "").strip()
        if not content:
            continue

        if role == "assistant":
            source = {"type": "Agent", "id": "self"}
        elif role == "user":
            name = entry.get("name", "User")
            user_id = entry.get("user_id", "")
            uid = f"discord:{user_id}" if user_id else f"discord:{name}"
            source = {"type": "User", "id": uid, "name": name}
        else:
            continue

        messages.append(
            {
                "content": content,
                "source": source,
                "timestamp": entry.get("timestamp", ""),
                "context_type": "conversation",
            }
        )

    def _ts_sort_key(m: dict) -> str:
        return m.get("timestamp", "") or ""

    messages.sort(key=_ts_sort_key)

    return {"messages": messages}


async def _search_episodes_fts(db: aiosqlite.Connection, agent_id: str, query: str, limit: int) -> list[dict]:
    """Search episodes using FTS5."""
    sanitized = re.sub(r"[^\w\s]", "", query, flags=re.UNICODE)
    words = sanitized.split()
    if not words:
        return []

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
        rows = await db.execute_fetchall(
            f"""SELECT id, msg_id, content, source, timestamp
               FROM memories
               WHERE agent_id = ?{channel_clause}
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, *channel_params, limit),
        )
        return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4]} for r in rows]

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


async def do_archive_episode(
    agent_id: str,
    history: list[dict],
    summary: str = "",
    keywords: str = "",
    resolved: bool | None = None,
) -> dict:
    """Archive a conversation episode with pre-computed summary, keywords, and resolved status."""
    db = await get_db()

    if not summary:
        return {"ok": True, "episode_id": None}

    resolved = bool(resolved)

    timestamps = [msg.get("timestamp", "") for msg in history if msg.get("timestamp")]
    start_time = min(timestamps) if timestamps else None
    end_time = max(timestamps) if timestamps else None

    embedding_blob = None
    if vector._embedding_client and summary:
        try:
            embeddings = await vector._embedding_client.embed([summary])
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
