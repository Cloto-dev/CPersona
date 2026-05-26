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
from _vendored_mcp_common import no_persist
from _vendored_mcp_common.embedding_client import EmbeddingClient
from _vendored_mcp_common.isolation import coerce_for_write, gamma_clause

import vector
from config import (
    AUTOCUT_ENABLED,
    AUTOCUT_MIN_GAP_RATIO,
    CONFIDENCE_ENABLED,
    EPISODE_DECAY_FLOOR,
    EPISODE_DECAY_RATE,
    EPISODE_PENALTY_ENABLED,
    FTS_ENABLED,
    MAX_CONTENT_LENGTH,
    MAX_MEMORIES,
    RECALL_MODE,
    RRF_K,
    RRF_MAX_SCALE,
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
from vector import _search_vector

logger = logging.getLogger(__name__)


async def do_store(agent_id: str, message: dict, channel: str = "", project_id: str = "") -> dict:
    """Store a message in agent memory.

    project_id (v2.4.17): isolation axis. Defaults to '' (= global pool).
    Dedup is project-scoped so the same msg_id under different projects can
    coexist; reads use γ semantics (see _vendored_mcp_common.isolation.gamma_clause).
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "id": 0}, "store")
    db = await get_db()

    msg_id = message.get("id", "")
    raw_content = message.get("content", "")
    source = json.dumps(message.get("source", {}))
    timestamp = message.get("timestamp", datetime.now(timezone.utc).isoformat())
    metadata = json.dumps(message.get("metadata", {}))
    project_id = coerce_for_write(project_id)

    if not raw_content:
        return {"ok": True, "skipped": True, "reason": "empty content"}

    content = _sanitize_content(raw_content)
    truncated = len(raw_content) > MAX_CONTENT_LENGTH

    if not content:
        return {"ok": True, "skipped": True, "reason": "empty after sanitization"}

    # Deduplicate by msg_id if provided (project-scoped — the same msg_id can
    # legitimately appear in different projects).
    if msg_id:
        row = await db.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = ? AND project_id = ? AND msg_id = ? LIMIT 1",
            (agent_id, project_id, msg_id),
        )
        if row:
            return {"ok": True, "skipped": True, "reason": "duplicate msg_id"}

    # Deduplicate by exact content match (project-scoped).
    existing = await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND project_id = ? AND channel = ? AND content = ? LIMIT 1",
        (agent_id, project_id, channel, content),
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
        """INSERT INTO memories (agent_id, project_id, msg_id, content, source, timestamp, metadata, embedding, channel)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, project_id, msg_id, content, source, timestamp, metadata, embedding_blob, channel),
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


def _like_escape_prefix(s: str) -> str:
    """Escape SQL LIKE special characters and append '%' for prefix match.

    Returns the empty string for empty input so the caller can branch on it.
    Used with ``ESCAPE '\\'`` in the SQL clause.
    """
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _recall_cascade(
    db,
    agent_id: str,
    query: str,
    limit: int,
    deep: bool,
    channel: str = "",
    exclude_set: set[str] | None = None,
    project_id: str | None = None,
    source_id: str = "",
) -> list[dict]:
    """Original cascading recall: stages fill remaining slots sequentially."""
    results: list[dict] = []
    seen_ids: set = set()
    _excl = exclude_set or set()

    if vector._embedding_client and query.strip():
        vector_results = await _search_vector(
            db, agent_id, query, limit, channel=channel, project_id=project_id, source_id=source_id
        )
        for row in vector_results:
            rid = row.get("_rid", row["id"])
            if rid not in seen_ids and not _content_excluded(row["content"], _excl):
                results.append(row)
                seen_ids.add(rid)

    # Episodes are agent-level aggregates without per-user source tagging,
    # so they are not filtered by source_id (would lose all episode recall).
    if FTS_ENABLED and query.strip() and not source_id:
        fts_results = await _search_episodes_fts(db, agent_id, query, limit, project_id=project_id)
        for row in fts_results:
            rid = ("ep", row["id"])
            if rid not in seen_ids:
                results.append(row)
                seen_ids.add(rid)

    # Profiles are not project-tagged in v2.4.17 (the UNIQUE constraint stays
    # agent_id × user_id), so profile injection is global per agent.
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
        memory_rows = await _search_memories_keyword(
            db, agent_id, query, remaining, channel=channel, project_id=project_id, source_id=source_id
        )
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
    project_id: str | None = None,
    source_id: str = "",
) -> list[dict]:
    """v2.4 RRF recall: run vector and FTS5 independently, merge with
    Reciprocal Rank Fusion. Avoids cascade's positional bias.
    """
    k = RRF_K
    doc_map: dict[tuple, dict] = {}
    rrf_scores: dict[tuple, float] = {}
    _excl = exclude_set or set()

    rrf_min_sim = vector._get_vector_threshold(agent_id) * RRF_THRESHOLD_FACTOR
    if vector._embedding_client:
        vector_results = await _search_vector(
            db, agent_id, query, limit, min_similarity=rrf_min_sim,
            channel=channel, project_id=project_id, source_id=source_id,
        )
        for rank, row in enumerate(vector_results):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = row.get("_rid", ("mem", row["id"]))
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    # Episodes skipped when source_id is set — episodes lack per-user source tagging.
    if FTS_ENABLED and not source_id:
        fts_ep_results = await _search_episodes_fts(db, agent_id, query, limit, project_id=project_id)
        for rank, row in enumerate(fts_ep_results):
            rid = ("ep", row["id"])
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    if FTS_ENABLED:
        fts_mem_results = await _search_memories_keyword(
            db, agent_id, query, limit, channel=channel, project_id=project_id, source_id=source_id
        )
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
    """Detect the largest score gap in results and cut below it (Weaviate autocut).

    v2.4.13: Uses relative gap ratio (gap / max_score) instead of absolute gap
    to work correctly across both RRF (~0-0.05) and cosine (0-1.0) score scales.
    Gaps below AUTOCUT_MIN_GAP_RATIO of the top score are treated as uniform
    noise and ignored to prevent over-truncation on evenly-distributed results.
    """
    if len(results) < 2:
        return results
    scores = [r.get("_rrf_score") or r.get("_cosine") or 0 for r in results]
    max_score = scores[0]
    if max_score <= 0:
        return results
    gaps = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]
    max_gap = max(gaps)
    if max_gap / max_score < AUTOCUT_MIN_GAP_RATIO:
        return results  # no meaningful breakpoint
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
    """Adaptive quality gate — remove results below a dynamic threshold.

    Score priority (v2.4.12):
    1. ``_confidence_score`` — 0–1, normalized by ``_compute_confidence``
    2. ``_cosine`` — 0–1, raw cosine similarity from vector search
    3. ``_rrf_score`` — ~0–0.05 scale; threshold is scaled by ``RRF_MAX_SCALE``
       to align with the cosine-scale ``min_score``
    4. Unscored (no score at all) → volume rule (``memory_count >= 100``)

    Rules:
    1. Scored results excluded if score < ``min_score``
       (RRF uses the scaled threshold ``min_score * RRF_MAX_SCALE``)
    2. Profile injection (``id == -1``): skip if ``memory_count < 50``
    3. Unscored results kept only if ``memory_count >= 100``

    v2.4.12 fix: previously ``_rrf_score`` was selected via falsy-chain before
    ``_cosine``, causing the RRF-scale value (0.01–0.05) to be compared against
    the cosine-scale ``min_score`` (0.2–1.0) → every RRF-mode result rejected.
    Cascade mode (no ``_rrf_score`` on rows) is unaffected.
    """
    if not results:
        return results

    filtered = []
    stats = {"confidence": 0, "cosine": 0, "rrf": 0, "unscored": 0, "profile": 0, "blocked": 0}

    for r in results:
        # Profile — gate by memory count (unchanged)
        if r.get("id") == -1:  # profile sentinel
            if memory_count >= 50:
                filtered.append(r)
                stats["profile"] += 1
            else:
                stats["blocked"] += 1
            continue

        confidence = r.get("_confidence_score")
        cosine = r.get("_cosine")
        rrf = r.get("_rrf_score")

        if confidence is not None:
            if confidence >= min_score:
                filtered.append(r)
                stats["confidence"] += 1
            else:
                stats["blocked"] += 1
        elif cosine is not None:
            if cosine >= min_score:
                filtered.append(r)
                stats["cosine"] += 1
            else:
                stats["blocked"] += 1
        elif rrf is not None:
            # RRF scale (~0–0.05) does not match cosine-scale min_score; rescale.
            if rrf >= min_score * RRF_MAX_SCALE:
                filtered.append(r)
                stats["rrf"] += 1
            else:
                stats["blocked"] += 1
        else:
            # Unscored (cascade FTS/keyword without confidence) — volume rule
            if memory_count >= 100:
                filtered.append(r)
                stats["unscored"] += 1
            else:
                stats["blocked"] += 1

    logger.debug(
        "quality_gate: in=%d out=%d (conf=%d cos=%d rrf=%d uns=%d prof=%d) min_score=%.3f count=%d",
        len(results),
        len(filtered),
        stats["confidence"],
        stats["cosine"],
        stats["rrf"],
        stats["unscored"],
        stats["profile"],
        min_score,
        memory_count,
    )

    return filtered


def _episode_boundary_factor(
    memory_ts_str: str | None,
    episode_boundary_ts: datetime | None,
) -> float:
    """Multiplicative decay for memories preceding the latest episode boundary.

    Returns 1.0 for memories within or after the boundary (current session).
    Returns exponential decay in [EPISODE_DECAY_FLOOR, 1.0) for older memories,
    so cross-session noise is weakened relative to current-session memories.
    """
    if not memory_ts_str or episode_boundary_ts is None:
        return 1.0
    mem_dt = _parse_timestamp_utc(memory_ts_str)
    if mem_dt is None or mem_dt >= episode_boundary_ts:
        return 1.0
    hours_before = (episode_boundary_ts - mem_dt).total_seconds() / 3600
    return max(EPISODE_DECAY_FLOOR, math.exp(-EPISODE_DECAY_RATE * hours_before))


async def _get_episode_boundary_ts(db: aiosqlite.Connection, agent_id: str) -> datetime | None:
    """Return the latest episode's created_at as the current-session boundary.

    Used by the episode boundary penalty to distinguish current-session
    memories (no penalty) from prior-session memories (decayed score).
    """
    rows = await db.execute_fetchall(
        "SELECT created_at FROM episodes WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
        (agent_id,),
    )
    if not rows or not rows[0][0]:
        return None
    return _parse_timestamp_utc(rows[0][0])


async def do_recall(
    agent_id: str,
    query: str,
    limit: int,
    deep: bool = False,
    channel: str = "",
    exclude_contents: list | None = None,
    project_id: str | None = None,
    source_id: str = "",
) -> dict:
    """Recall relevant memories using multi-strategy search.

    project_id (v2.4.17): γ filter — None = no project filter, '' = global
    pool only, 'X' = bucket 'X' ∪ global pool. Threaded through the cascade /
    RRF / vector / FTS / keyword paths. The vector top-K is post-filtered, so
    a tightly-tagged query may receive fewer than `limit` results — namespace
    partitioning is a follow-up.

    source_id (v2.4.20): optional prefix filter applied to ``json_extract(source, '$.id')``.
    Empty string disables the filter (default). Used by Discord multi-user
    sessions to prevent cross-user memory contamination: pass e.g.
    ``source_id="discord:12345"`` to restrict to one user, or
    ``source_id="discord:"`` to scope to all Discord-sourced memories.
    Episodes are not source-tagged, so episode recall is skipped when
    ``source_id`` is non-empty.
    """
    db = await get_db()

    exclude_set: set[str] = set()
    if exclude_contents:
        exclude_set = {c.strip().lower() for c in exclude_contents if c.strip()}

    if RECALL_MODE == "rrf" and query.strip():
        results = await _recall_rrf(
            db, agent_id, query, limit, deep, channel, exclude_set,
            project_id=project_id, source_id=source_id,
        )
    else:
        results = await _recall_cascade(
            db, agent_id, query, limit, deep, channel, exclude_set,
            project_id=project_id, source_id=source_id,
        )

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

    # v2.4.14: Episode boundary soft penalty (L3) — weaken cross-session memories
    # before quality gate so current-session signals take precedence.
    if EPISODE_PENALTY_ENABLED and results:
        episode_boundary_ts = await _get_episode_boundary_ts(db, agent_id)
        if episode_boundary_ts is not None:
            for r in results:
                factor = _episode_boundary_factor(r.get("timestamp"), episode_boundary_ts)
                if factor < 1.0:
                    if "_cosine" in r:
                        r["_cosine"] = r["_cosine"] * factor
                    if "_rrf_score" in r:
                        r["_rrf_score"] = r["_rrf_score"] * factor

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
    project_id: str | None = None,
    source_id: str = "",
) -> dict:
    """Recall memories and merge with external conversation context.

    project_id (v2.4.17): γ filter — passed through to do_recall.
    source_id (v2.4.20): per-user source prefix filter — passed through to do_recall.
    """
    ctx = external_context or []

    exclude_list = [e["content"].strip().lower() for e in ctx if e.get("content", "").strip()]

    recall_result = await do_recall(
        agent_id,
        query,
        limit,
        deep=deep,
        channel=channel,
        exclude_contents=exclude_list,
        project_id=project_id,
        source_id=source_id,
    )
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


async def _search_episodes_fts(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    project_id: str | None = None,
) -> list[dict]:
    """Search episodes using FTS5. project_id (v2.4.17) applies the γ filter."""
    sanitized = re.sub(r"[^\w\s]", "", query, flags=re.UNICODE)
    words = sanitized.split()
    if not words:
        return []

    fts_query = " ".join(f'"{w}"' for w in words)
    proj_frag, proj_params = gamma_clause("e.project_id", project_id)
    proj_extra = (" AND " + proj_frag) if proj_frag else ""

    rows = await db.execute_fetchall(
        f"""SELECT e.id, e.summary, e.start_time, e.resolved
           FROM episodes_fts f
           JOIN episodes e ON f.rowid = e.id
           WHERE episodes_fts MATCH ?
           AND e.agent_id = ?{proj_extra}
           ORDER BY rank
           LIMIT ?""",
        (fts_query, agent_id, *proj_params, limit),
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
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    channel: str = "",
    project_id: str | None = None,
    source_id: str = "",
) -> list[dict]:
    """Search memories using FTS5 (preferred) or LIKE fallback.

    project_id (v2.4.17) applies the γ filter on both the bare and joined paths.
    source_id (v2.4.20) applies a prefix filter against ``json_extract(source, '$.id')``.
    """
    channel_clause = " AND channel = ?" if channel else ""
    channel_params = (channel,) if channel else ()
    proj_frag_bare, proj_params_bare = gamma_clause("project_id", project_id)
    proj_extra_bare = (" AND " + proj_frag_bare) if proj_frag_bare else ""
    proj_frag_m, proj_params_m = gamma_clause("m.project_id", project_id)
    proj_extra_m = (" AND " + proj_frag_m) if proj_frag_m else ""

    src_like = _like_escape_prefix(source_id)
    src_clause_bare = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params_bare = (src_like,) if src_like else ()
    src_clause_m = " AND json_extract(m.source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params_m = (src_like,) if src_like else ()

    if not query.strip():
        rows = await db.execute_fetchall(
            f"""SELECT id, msg_id, content, source, timestamp
               FROM memories
               WHERE agent_id = ?{channel_clause}{proj_extra_bare}{src_clause_bare}
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, *channel_params, *proj_params_bare, *src_params_bare, limit),
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
                   AND m.agent_id = ?{channel_clause.replace("channel", "m.channel")}{proj_extra_m}{src_clause_m}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, agent_id, *channel_params, *proj_params_m, *src_params_m, limit),
            )
            if rows:
                return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4]} for r in rows]

    scan_limit = min(MAX_MEMORIES, max(limit * 5, 50))
    rows = await db.execute_fetchall(
        f"""SELECT id, msg_id, content, source, timestamp
           FROM memories
           WHERE agent_id = ?{channel_clause}{proj_extra_bare}{src_clause_bare}
           AND content LIKE ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (agent_id, *channel_params, *proj_params_bare, *src_params_bare, f"%{query}%", scan_limit),
    )
    return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4]} for r in rows[:limit]]


async def do_archive_episode(
    agent_id: str,
    history: list[dict],
    summary: str = "",
    keywords: str = "",
    resolved: bool | None = None,
    project_id: str = "",
) -> dict:
    """Archive a conversation episode with pre-computed summary, keywords, and resolved status.

    project_id (v2.4.17): isolation axis. Defaults to '' (= global pool).
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {"ok": True, "episode_id": None, "id": 0}, "archive_episode"
        )
    db = await get_db()

    if not summary:
        return {"ok": True, "episode_id": None}

    resolved = bool(resolved)
    project_id = coerce_for_write(project_id)

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
        """INSERT INTO episodes (agent_id, project_id, summary, keywords, start_time, end_time, embedding, resolved)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, project_id, summary, keywords, start_time, end_time, embedding_blob, int(resolved)),
    )
    await db.commit()
    return {"ok": True, "episode_id": cursor.lastrowid}
