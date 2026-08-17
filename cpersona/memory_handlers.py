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
from cpersona._vendored_mcp_common import no_persist
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona._vendored_mcp_common.isolation import coerce_for_write
from cpersona.isolation import isolation_where

from cpersona import health
from cpersona import vector
from cpersona.config import (
    AUTOCUT_ENABLED,
    AUTOCUT_MIN_GAP_RATIO,
    AUTOCUT_MIN_RESULTS,
    CONFIDENCE_ENABLED,
    EPISODE_DECAY_FLOOR,
    EPISODE_DECAY_RATE,
    EPISODE_PENALTY_ENABLED,
    FTS_ENABLED,
    MAX_MEMORIES,
    MAX_METADATA_LENGTH,
    RECALL_MODE,
    REMOTE_INDEX_TIMEOUT_SECS,
    RRF_K,
    RRF_MAX_SCALE,
    RRF_THRESHOLD_FACTOR,
    STORE_BLOB,
    local_blobs_stored,
    VECTOR_SEARCH_MODE,
)
from cpersona import config # for runtime-mutable VECTOR_MIN_SIMILARITY access
from cpersona.database import connection, transaction
from cpersona.utils import (
    _clamp_limit,
    _compute_confidence,
    _content_excluded,
    _parse_timestamp_utc,
    episode_timestamp,
    _sanitize_content,
    sanitize_content_with_flag,
    _try_parse_json,
    error_response,
    normalize_source,
)
from cpersona.vector import _search_vector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# store outcome contract (2.5.2b1, an earlier decision item b1-1)
#
# Every do_store return carries ``result`` — the discriminator that answers the
# only question a caller actually has ("is my memory in the database?"):
#
#   stored    a new row was written; ``id`` and ``embedded`` describe it
#   skipped   nothing was written and nothing is wrong — an equivalent row
#             already exists (dedup), or persistence is paused
#   rejected  nothing was written because the request did not satisfy the
#             contract (empty content, or content that sanitizes to empty)
#
# ``ok`` now tracks that verdict instead of being unconditionally True: a
# rejection reports ok=False. Until 2.5.2b1 every branch returned ok=True and
# the only signal was an easily-missed ``skipped: true``, so a caller that
# checked ``ok`` — the obvious thing to check — read "stored" for a write that
# never happened. That is a contract break, deliberately taken on the 2.5.2
# pre-release ladder (charter §3: RELEASE_LIFECYCLE_STANDARD §2.1 makes the
# ladder mandatory, which is what we are on).
#
# ``reason`` is human-readable and NOT a stable machine token; branch on
# ``result`` (and on ``persisted``, which the no-persist path adds).
# ---------------------------------------------------------------------------


def _store_rejected(reason: str) -> dict:
    """The request was understood and refused. Nothing was written."""
    return {"ok": False, "result": "rejected", "reason": reason}


def _store_skipped(reason: str, mem_id: int | None = None) -> dict:
    """Nothing was written, and that is the correct outcome for this request."""
    body = {"ok": True, "result": "skipped", "reason": reason}
    if mem_id is not None:
        body["id"] = mem_id
    return body


async def do_store(agent_id: str, message: dict, channel: str = "", project_id: str = "") -> dict:
    """Store a message in agent memory.

    project_id (v2.4.17): isolation axis. Defaults to '' (= global pool).
    Dedup checks the γ-visible scope (bug-106): a bucket write collides with an
    identical global-pool row (a recall in that bucket would surface both), while
    sibling buckets stay distinct; reads use the same γ semantics (see
    cpersona.isolation.isolation_where).
    """
    if no_persist.is_paused():
        # bug-141: keep the no-persist shape aligned with the success contract
        # ({ok, id, embedded}) — nothing was persisted, so embedded is False.
        # b1-1: it is a `skipped` outcome (deliberately not written, nothing
        # wrong); the helper overwrites `reason` with its TTL message and adds
        # persisted=False, which is the key to branch on for this branch alone.
        return no_persist.make_skipped_response(
            {"ok": True, "result": "skipped", "id": 0, "embedded": False}, "store"
        )

    msg_id = message.get("id", "")
    raw_content = message.get("content", "")
    # 2.5.2 (an earlier decision): normalize known legacy source shapes at the write seam.
    # Unknown shapes are stored verbatim so the health check still surfaces them
    # for human-reviewed migration — never fabricate a discriminator we can't
    # justify (would corrupt attribution and defeat anonymous_source).
    raw_source = message.get("source", {})
    normalized_source, _mapped = normalize_source(raw_source)
    source = json.dumps(normalized_source if normalized_source is not None else {})
    timestamp = message.get("timestamp", datetime.now(timezone.utc).isoformat())
    metadata = json.dumps(message.get("metadata", {}))
    project_id = coerce_for_write(project_id)

    if not raw_content:
        return _store_rejected("empty content")

    # audit C12: content has been capped since 2.1, its JSON sidecars never were.
    # A field that cannot be truncated (valid JSON has no valid prefix) and is not
    # the payload gets refused rather than silently dropped — dropping would lose
    # attribution / producer context while reporting success.
    for field_name, serialised in (("source", source), ("metadata", metadata)):
        if len(serialised) > MAX_METADATA_LENGTH:
            return _store_rejected(
                f"{field_name} too large ({len(serialised)} chars, max {MAX_METADATA_LENGTH})"
            )

    # bug-175: the flag comes back from the seam that does the cutting, so it
    # cannot disagree with what was stored.
    content, truncated = sanitize_content_with_flag(raw_content)

    if not content:
        return _store_rejected("empty after sanitization")

    # bug-106: the dedup probes check the γ-VISIBLE scope, matching read semantics.
    # A bucket write ('X') collides with an identical row in the global pool —
    # recall('X') surfaces X ∪ '' and would return both copies — while a global
    # write probes the global pool only (a bucket copy must not hide the row from
    # every other bucket). Same shape on the channel axis. Import/merge keep
    # exact-bucket probes deliberately: a restore/merge must reconstruct legacy
    # corpora faithfully across buckets (bug-044/076 precedent). The v12 UNIQUE
    # indexes stay exact-bucket as the TOCTOU backstop.
    proj_scope = (project_id, "") if project_id else ("",)
    chan_scope = (channel, "") if channel else ("",)
    proj_in = ",".join("?" * len(proj_scope))
    chan_in = ",".join("?" * len(chan_scope))
    async with connection() as db:
        # Deduplicate by msg_id if provided (γ-project-scoped — the same msg_id in
        # two sibling buckets stays legitimately distinct, bug-044).
        if msg_id:
            row = await db.execute_fetchall(
                f"SELECT id FROM memories WHERE agent_id = ? AND project_id IN ({proj_in}) AND msg_id = ? LIMIT 1",
                (agent_id, *proj_scope, msg_id),
            )
            if row:
                # v2.5.2 additive: echo the existing row's id so callers can chain
                # (e.g. update_memory) without a second lookup.
                return _store_skipped("duplicate msg_id", row[0][0])

        # Deduplicate by exact content match (γ-visible scope).
        existing = await db.execute_fetchall(
            f"SELECT id FROM memories WHERE agent_id = ? AND project_id IN ({proj_in})"
            f" AND channel IN ({chan_in}) AND content = ? LIMIT 1",
            (agent_id, *proj_scope, *chan_scope, content),
        )
        if existing:
            # v2.5.2 additive: same id echo as the msg_id branch above.
            return _store_skipped("duplicate content", existing[0][0])

    embedding_blob = None
    if vector._embedding_client and local_blobs_stored(VECTOR_SEARCH_MODE, STORE_BLOB):
        try:
            embeddings = await vector._embedding_client.embed([content])
            if embeddings and embeddings[0]:
                embedding_blob = EmbeddingClient.pack_embedding(embeddings[0])
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, TypeError) as e:
            logger.warning("Embedding failed during store: %s", e)

    # OR IGNORE lets the v12 UNIQUE dedup indexes absorb a concurrent writer
    # that slipped in between the SELECT-based dedup probes above and this
    # INSERT (bug-010 TOCTOU); rowcount 0 means the row already exists.
    # bug-042/043: transaction() serialises INSERT+commit behind the shared write
    # lock so this commit cannot flush a concurrent import/merge's partial
    # transaction (and vice versa). The remote-index push below stays outside the
    # seam (network I/O, not a DB commit).
    async with transaction() as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO memories (agent_id, project_id, msg_id, content, source, timestamp, metadata, embedding, channel)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, project_id, msg_id, content, source, timestamp, metadata, embedding_blob, channel),
        )
    if cursor.rowcount == 0:
        # v2.5.2 additive asymmetry: the msg_id / content branches echo the
        # existing row's id from their SELECT probe. The OR IGNORE fallback fires
        # only when a concurrent writer slipped between those probes and this
        # INSERT (bug-010 TOCTOU), and a fresh SELECT to recover the id would
        # re-enter the same TOCTOU seam we deliberately closed — so this branch
        # stays id-less by design. Callers keying on `id` MUST treat it as
        # optional under `result="skipped"`.
        return _store_skipped("duplicate (unique index)")
    # lastrowid comes from this cursor's INSERT, so a store interleaved on the
    # shared connection cannot shift it (bug-010: the previous max-id re-SELECT
    # could bind a different row's id to the remote vector entry, making recall
    # return another memory's content).
    mem_id = cursor.lastrowid

    # v2.5.2 additive: `embedded` reports whether any embedding surface was
    # actually populated for this row — the local blob (persisted with the
    # INSERT above) or the remote index push below. False under EMBEDDING_MODE
    # =none, or when the embedding call raised and we degraded to the SQL-only
    # path.
    local_embedded = embedding_blob is not None
    remote_embedded = False

    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        try:
            base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
            resp = await vector._embedding_client._client.post(
                f"{base_url}/index",
                json={
                    "namespace": f"cpersona:{agent_id}",
                    "items": [{"id": f"mem:{mem_id}", "text": content}],
                },
                # #361 (6): state the deadline instead of inheriting the embed
                # client's 30s default — this is the write hot path, and every
                # sibling remote call (probe 3s, search 5s) names its own.
                timeout=REMOTE_INDEX_TIMEOUT_SECS,
            )
            # bug-146: httpx does NOT raise on 4xx/5xx, so the discarded
            # response let a backend failure (bad namespace, expired auth, 500)
            # still report embedded=True while the vector never landed —
            # contradicting the store tool contract ("embedded is true iff ...
            # the remote index push succeeded"). raise_for_status() routes a
            # non-2xx into the same except as a transport error (remote_embedded
            # stays False, logged at the same debug level), matching every
            # sibling remote call (search.py / vector.py / embedding_client.py).
            resp.raise_for_status()
            remote_embedded = True
        except Exception as e:
            logger.debug("Remote index failed (non-fatal): %s", e)

    result = {
        "ok": True,
        "result": "stored",
        "id": mem_id,
        "embedded": local_embedded or remote_embedded,
    }
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


def _like_escape_contains(s: str) -> str:
    """Escape SQL LIKE specials and wrap in ``%...%`` for a literal contains match.

    Used with ``ESCAPE '\\'``. Unlike a raw ``f"%{s}%"``, ``%`` and ``_`` in the
    user query are matched literally instead of acting as wildcards (bug-034).
    """
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _append_profile_rows(db, agent_id: str, results: list[dict]) -> None:
    """Append the agent's profile rows as id=-1 sentinel injection rows.

    bug-136: single source of truth for profile injection, previously copy-pasted
    verbatim into _recall_cascade / _recall_rrf / _recall_rsf. Profiles are global
    per agent (not project-tagged, v2.4.17) -- the UNIQUE constraint stays
    agent_id x user_id. Mutates `results` in place.
    """
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

    # Episodes are agent-level aggregates without per-user source tagging, so
    # a per-user source_id filter normally suppresses them. A channel filter
    # (v2.4.22) scopes episodes to one conversation channel — the session-start
    # grounding path — so channel-scoped episode recall is allowed even with
    # source_id set.
    if FTS_ENABLED and query.strip() and (not source_id or channel):
        fts_results = await _search_episodes_fts(
            db, agent_id, query, limit, channel=channel, project_id=project_id
        )
        for row in fts_results:
            rid = ("ep", row["id"])
            if rid not in seen_ids:
                results.append(row)
                seen_ids.add(rid)

    await _append_profile_rows(db, agent_id, results)

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

    # Episodes lack per-user source tagging, so a per-user source_id filter
    # normally suppresses them; a channel filter (v2.4.22) scopes episodes to
    # one channel and is allowed even with source_id set (grounding path).
    if FTS_ENABLED and (not source_id or channel):
        fts_ep_results = await _search_episodes_fts(
            db, agent_id, query, limit, channel=channel, project_id=project_id
        )
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

    await _append_profile_rows(db, agent_id, results)

    return results


def _minmax_norm(raw: dict) -> dict:
    """Min-max normalize a channel's raw scores to [0, 1] (higher = better).

    All-None (e.g. the LIKE fallback, which has no bm25) → uniform 1.0, so an
    exact substring match still casts a full keyword vote. Degenerate input
    (single row or all-equal) → 1.0 each. Mixed None gets the 0.0 floor.
    """
    vals = {rid: s for rid, s in raw.items() if s is not None}
    if not vals:
        return {rid: 1.0 for rid in raw}
    lo, hi = min(vals.values()), max(vals.values())
    if hi <= lo:
        return {rid: 1.0 for rid in raw}
    out = {rid: (s - lo) / (hi - lo) for rid, s in vals.items()}
    for rid in raw:
        out.setdefault(rid, 0.0)
    return out


async def _recall_rsf(
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
    """Relative-Score-Fusion recall: like RRF but fuse the per-query min-max
    normalized *raw* score of each channel (cosine for vector, -bm25 for FTS)
    instead of rank.

    RRF's rank-only fusion crushes large score margins — a rank-1 vs rank-4
    bm25 gap collapses to ~5% at K=60 — so a near-tie vector channel can
    reintroduce a topically distinct contaminant the keyword channel had
    correctly down-ranked. RSF keeps the margin, letting the keyword channel
    separate them. The fused score is divided by the number of active channels
    so it stays on the cosine [0, 1] scale (and rewards multi-channel agreement)
    for the quality gate. See ClotoCore/docs/RECALL_CONTAMINATION_AB_2026-06-14.md.
    """
    doc_map: dict[tuple, dict] = {}
    vec_raw: dict[tuple, float | None] = {}
    ep_raw: dict[tuple, float | None] = {}
    mem_raw: dict[tuple, float | None] = {}
    _excl = exclude_set or set()

    rsf_min_sim = vector._get_vector_threshold(agent_id) * RRF_THRESHOLD_FACTOR
    if vector._embedding_client:
        for row in await _search_vector(
            db, agent_id, query, limit, min_similarity=rsf_min_sim,
            channel=channel, project_id=project_id, source_id=source_id,
        ):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = row.get("_rid", ("mem", row["id"]))
            doc_map.setdefault(rid, row)
            vec_raw[rid] = row.get("_cosine", 0.0)

    # Episodes lack per-user source tagging (mirrors _recall_rrf gating).
    if FTS_ENABLED and (not source_id or channel):
        for row in await _search_episodes_fts(
            db, agent_id, query, limit, channel=channel, project_id=project_id
        ):
            rid = ("ep", row["id"])
            doc_map.setdefault(rid, row)
            bm = row.get("_bm25")
            ep_raw[rid] = -bm if bm is not None else None

    if FTS_ENABLED:
        for row in await _search_memories_keyword(
            db, agent_id, query, limit, channel=channel, project_id=project_id, source_id=source_id
        ):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = ("mem", row["id"])
            doc_map.setdefault(rid, row)
            bm = row.get("_bm25")
            mem_raw[rid] = -bm if bm is not None else None

    active = [ch for ch in (vec_raw, ep_raw, mem_raw) if ch]
    n_active = len(active) or 1
    fused: dict[tuple, float] = {}
    for ch in active:
        for rid, w in _minmax_norm(ch).items():
            fused[rid] = fused.get(rid, 0.0) + w

    results = []
    for rid in sorted(fused, key=fused.get, reverse=True):
        row = doc_map[rid]
        row["_rsf_score"] = fused[rid] / n_active
        results.append(row)

    await _append_profile_rows(db, agent_id, results)

    return results


def _autocut(results: list[dict]) -> list[dict]:
    """Detect the largest score gap in results and cut below it (Weaviate autocut).

    v2.4.13: Uses relative gap ratio (gap / max_score) instead of absolute gap
    to work correctly across both RRF (~0-0.05) and cosine (0-1.0) score scales.
    Gaps below AUTOCUT_MIN_GAP_RATIO of the top score are treated as uniform
    noise and ignored to prevent over-truncation on evenly-distributed results.

    v2.4.25: a small result set (< AUTOCUT_MIN_RESULTS) is returned whole. Under
    RSF, _minmax_norm pins the lowest row to 0.0, so a 2-item set always shows a
    full-scale gap that this would otherwise cut to a single row — discarding a
    still-relevant second hit. Below the floor there are too few rows for a gap to
    be meaningful, so keep them all.
    """
    if len(results) < AUTOCUT_MIN_RESULTS:
        return results
    # bug-013: gap detection is only meaningful on similarity-scale signals
    # (confidence / cosine). Rank-fusion scores (rrf / rsf) decay
    # hyperbolically by construction — their "gaps" encode retriever overlap
    # (hit by both retrievers vs one), not relevance breaks, so on homogeneous
    # corpora autocut sliced a 17k-hit recall down to 2 rows. Fusion-ordered
    # results rely on the fused quality gate for contamination control; skip
    # the cut unless the ordering signal is similarity-scale.
    first = results[0]
    if first.get("_confidence_score") is not None:
        key = "_confidence_score"
    elif first.get("_rsf_score") is not None or first.get("_rrf_score") is not None:
        return results
    else:
        # Fallback ordering signal is raw cosine. bug-018: cascade recall
        # concatenates stages (vector, then episodes / profiles / keyword) in
        # stage order rather than sorting by cosine, and the non-vector stages
        # carry no _cosine at all. Scoring a missing signal as 0 (below) would
        # fabricate a full-scale gap at the vector->non-vector boundary, so
        # autocut would truncate every non-vector hit whenever a single vector
        # hit exists — silently collapsing multi-strategy cascade recall to
        # vector-only (drops profile injection + keyword hits). Same category
        # error bug-013 fixed for rrf/rsf. Only gap-cut a homogeneous
        # cosine-scored list where every row actually carries the signal.
        if any(r.get("_cosine") is None for r in results):
            return results
        key = "_cosine"
    scores = [r.get(key) or 0 for r in results]
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


def _gate_score(row: dict) -> tuple[float | None, str | None]:
    """The (score, signal) the quality gate keys on, by the SAME branch precedence as
    ``_apply_quality_gate``: confidence > rsf > cosine > rrf. Returns (None, None) for an
    unscored row. Used by both the runtime gate and the gate calibration so the
    calibrated operating point is computed on exactly the value the gate compares
    (v2.4.27, an earlier decision)."""
    confidence = row.get("_confidence_score")
    if confidence is not None:
        return confidence, "confidence"
    rsf = row.get("_rsf_score")
    if rsf is not None:
        return rsf, "rsf"
    cosine = row.get("_cosine")
    if cosine is not None:
        return cosine, "cosine"
    rrf = row.get("_rrf_score")
    if rrf is not None:
        return rrf, "rrf"
    return None, None


def _apply_quality_gate(
    results: list[dict],
    min_score: float,
    memory_count: int,
    gate: float | None = None,
    gate_signal: str | None = None,
    pure_recency: bool = False,
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

    v2.4.26/27 (an earlier decision): ``gate`` is the calibrated operating point and ``gate_signal``
    is the branch it was calibrated for (confidence / rsf / cosine / rrf — see
    ``_gate_score``). It replaces the pool-size heuristic ``min_score`` only in the
    matching branch, so the scales always agree and a stale gate from a different config
    (e.g. calibrated under confidence-on, now confidence-off) is simply never applied
    because that branch isn't the active one. rrf is compared directly (the gate is
    calibrated on raw rrf scores — no ``RRF_MAX_SCALE`` rescale). gate=None preserves the
    legacy heuristic. v2.4.27 extends this to the confidence branch: when
    CONFIDENCE_ENABLED, confidence is the active gate signal (it takes precedence over
    rsf/rrf), so the calibrated gate must live there for #132 to bite in production.
    """
    if not results:
        return results

    filtered = []
    stats = {"confidence": 0, "rsf": 0, "cosine": 0, "rrf": 0, "unscored": 0, "profile": 0, "blocked": 0}

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
        rsf = r.get("_rsf_score")
        cosine = r.get("_cosine")
        rrf = r.get("_rrf_score")

        if confidence is not None:
            # Confidence is on the [0, 1] scale; use the calibrated gate when it was
            # calibrated for this branch, else the pool-size heuristic.
            conf_threshold = gate if (gate is not None and gate_signal == "confidence") else min_score
            if confidence >= conf_threshold:
                filtered.append(r)
                stats["confidence"] += 1
            else:
                stats["blocked"] += 1
        elif rsf is not None:
            # RSF fused score is on the cosine [0, 1] scale.
            rsf_threshold = gate if (gate is not None and gate_signal == "rsf") else min_score
            if rsf >= rsf_threshold:
                filtered.append(r)
                stats["rsf"] += 1
            else:
                stats["blocked"] += 1
        elif cosine is not None:
            cos_threshold = gate if (gate is not None and gate_signal == "cosine") else min_score
            if cosine >= cos_threshold:
                filtered.append(r)
                stats["cosine"] += 1
            else:
                stats["blocked"] += 1
        elif rrf is not None:
            # Calibrated gate is on the raw RRF scale (calibrated on raw _rrf_score), so
            # compare directly; otherwise rescale the cosine-scale heuristic min_score.
            rrf_threshold = gate if (gate is not None and gate_signal == "rrf") else min_score * RRF_MAX_SCALE
            if rrf >= rrf_threshold:
                filtered.append(r)
                stats["rrf"] += 1
            else:
                stats["blocked"] += 1
        else:
            # Unscored (cascade FTS/keyword without confidence) — volume rule
            # bug-125: an empty query is a pure-recency listing with no relevance
            # signal, so bypass the volume rule; otherwise session-start recall
            # returns empty for every agent with fewer than 100 memories.
            if pure_recency or memory_count >= 100:
                filtered.append(r)
                stats["unscored"] += 1
            else:
                stats["blocked"] += 1

    logger.debug(
        "quality_gate: in=%d out=%d (conf=%d rsf=%d cos=%d rrf=%d uns=%d prof=%d) min_score=%.3f count=%d",
        len(results),
        len(filtered),
        stats["confidence"],
        stats["rsf"],
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


async def _get_episode_boundary_ts(
    db: aiosqlite.Connection,
    agent_id: str,
    project_id: str | None = None,
    channel: str = "",
) -> datetime | None:
    """Return the latest episode's created_at as the current-session boundary.

    Used by the episode boundary penalty to distinguish current-session
    memories (no penalty) from prior-session memories (decayed score).

    bug-147: the boundary is scoped to the SAME isolation axes as the
    recall (project_id/channel) via isolation_where, matching the sibling
    confidence-span scoping (the bug-107 fix). An agent-wide MAX(created_at) let
    an unrelated bucket's most-recent episode set the boundary for a
    tightly-scoped recall, penalising in-scope current-session memories against
    another project/channel. project_id=None / channel='' keep the agent-wide
    read (corpus-wide callers, e.g. gate calibration).
    """
    scope = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    rows = await db.execute_fetchall(
        f"SELECT created_at FROM episodes{scope.where} ORDER BY created_at DESC LIMIT 1",
        scope.params,
    )
    if not rows or not rows[0][0]:
        return None
    return _parse_timestamp_utc(rows[0][0])


def _is_episode_result(r: dict) -> bool:
    """True if a fused recall row is an episode (not a memory).

    bug-040/041: memories and episodes share one AUTOINCREMENT id space, so an
    episode id must never key into a ``memories`` query — otherwise recalling
    episode #3 reads/bumps the recall_count of the unrelated memory #3. Episode
    rows carry structural markers: ``_rid=('ep', id)`` and/or
    ``source={'System':'episode'}``. A memory's source is a JSON string (never a
    dict), so the dict-source check cannot false-positive on a memory.
    """
    rid = r.get("_rid")
    if isinstance(rid, tuple) and len(rid) == 2 and rid[0] == "ep":
        return True
    src = r.get("source")
    return isinstance(src, dict) and src.get("System") == "episode"


async def _backfill_cosines(
    db,
    results: list[dict],
    query: str,
    project_id: str | None,
    channel: str,
) -> None:
    """bug-155: backfill ``_cosine`` on rows that reached scoring cosine-less.

    In the fusion recall paths (``_recall_rrf`` / ``_recall_rsf``) only the vector
    channel populates ``_cosine`` — a row that hit only via the FTS / keyword
    channels reaches ``_apply_recall_scoring`` with ``_cosine=None`` even when its
    embedding blob sits in the DB (a ranking-window drop, not a coverage one).
    ``_compute_confidence`` then takes its ``raw_cosine is None`` branch —
    ``sqrt(time_decay) * completion_factor * recency_penalty`` — which is always
    ≥ the cosine branch's ``sqrt(norm_cos * time_decay) * ...`` (``norm_cos <= 1``
    by construction), so under CONFIDENCE_ENABLED the vector-less rows are
    structurally promoted above rows that DO carry a real similarity signal.
    That inverts the fusion intent (the docstring at ``_recall_rsf`` states the
    keyword channel is supposed to help DOWN-rank lexical contaminants).

    Fix: compute the real cosine from the row's stored blob so downstream scoring
    sees a proper signal. Rows we CANNOT backfill (no blob, foreign width,
    profile sentinel id=-1, non-integer id) are left at ``_cosine=None`` — they
    keep today's elevated-branch behaviour, which the 2.6.0 scoring redesign owns.

    Mutates rows in-place; caller sequences this BEFORE the episode-boundary
    penalty so the penalty scales the backfilled cosines uniformly with the
    native ones. Gate on ``CONFIDENCE_ENABLED`` at the call site: under
    confidence-off nothing downstream reads ``_cosine`` in a way that would
    change ordering, and materialising one here would (a) add a stray
    ``match_reason.cosine`` on rows that never had one, (b) flip ``_gate_score``
    on those rows from ``rrf``/``None`` to ``cosine`` — both silent perturbations.
    """
    if not query.strip():
        return
    client = vector._embedding_client
    if client is None:
        return

    needy: list[tuple[dict, int, bool]] = []  # (row, id, is_episode)
    for r in results:
        if r.get("_cosine") is not None:
            continue
        rid = r.get("id")
        if not isinstance(rid, int) or rid <= 0:
            # Profile sentinel (id=-1) and any other non-positive/non-int id.
            continue
        needy.append((r, rid, _is_episode_result(r)))
    if not needy:
        return

    embeddings = await client.embed([query])
    if not embeddings or not embeddings[0]:
        # Same failure semantics _search_vector applies: an empty embed of the
        # query text is a genuine embed failure, and the vector channel handles
        # its own health probe. Here we simply decline to backfill; the rows
        # keep their None cosine and today's elevated-branch score.
        return

    import numpy as np

    query_vec = np.array(embeddings[0], dtype=np.float32)
    query_dim = int(query_vec.shape[0])
    if query_dim <= 0:
        return

    # bug-040/041: memories and episodes id spaces AUTOINCREMENT independently, so
    # an episode id N MUST NOT read memory id N's blob (and vice versa) — one
    # IN(...) batch per table, embedding column only.
    mem_ids = [rid for (_, rid, is_ep) in needy if not is_ep]
    ep_ids = [rid for (_, rid, is_ep) in needy if is_ep]

    async def _fetch_blobs(table: str, ids: list[int]) -> dict[int, bytes | None]:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = await db.execute_fetchall(
            f"SELECT id, embedding FROM {table} WHERE id IN ({placeholders})",
            ids,
        )
        return {row[0]: row[1] for row in rows}

    mem_blobs = await _fetch_blobs("memories", mem_ids)
    ep_blobs = await _fetch_blobs("episodes", ep_ids)

    # Filter by matching width (bug-085 tolerance: a mid-flight model swap leaves
    # ragged-dim rows behind; skip them rather than crashing the reshape). Rows
    # with no blob at all are also skipped -- they keep today's None-cosine score.
    batch_rows: list[dict] = []
    batch_blobs: list[bytes] = []
    for row, rid, is_ep in needy:
        blob = (ep_blobs if is_ep else mem_blobs).get(rid)
        if not blob or len(blob) != query_dim * 4:
            continue
        batch_rows.append(row)
        batch_blobs.append(blob)
    if not batch_rows:
        return

    try:
        sims = vector._cosine_batch(query_vec, query_dim, batch_blobs)
    except (ValueError, TypeError):
        # Defensive: the width filter above should make this unreachable, but a
        # bad blob still leaves every needy row at None (today's behaviour) rather
        # than crashing the recall hot path.
        return

    for row, sim in zip(batch_rows, sims):
        row["_cosine"] = float(sim)
        # bug-183: mark the rows whose gate verdict this backfill can change. Before
        # b2 these reached the gate cosine-less and passed by construction (the
        # `raw_cosine is None` branch is an upper bound on the cosine branch); now they
        # are ordinary gate candidates. do_recall's empty-result rescue keys on this
        # marker so it restores ONLY the membership b2 removed — a row that always
        # carried a native cosine was gated on unchanged grounds and stays gated.
        row["_cosine_backfilled"] = True


async def _apply_recall_scoring(
    db,
    agent_id: str,
    results: list[dict],
    deep: bool,
    project_id: str | None = None,
    channel: str = "",
    query: str = "",
) -> tuple[list[dict], float, dict, float | None]:
    """Post-recall scoring run before the quality gate: the episode-boundary penalty
    (L3, v2.4.14) and, when CONFIDENCE_ENABLED, the confidence score (which also
    re-sorts by it). Factored out of do_recall (v2.4.27) so the gate calibration
    (an earlier decision) computes the operating point on exactly the per-row score the runtime
    gate keys on — including confidence, which takes precedence over the fused score and
    so owns the gate in confidence-enabled deployments. Mutates ``results``.

    Returns ``(results, time_range_hours, recall_counts, newest_age_hours)`` — do_recall
    reuses the latter three for the response confidence metadata and the recall-count
    update, so they are computed once here. ``newest_age_hours`` (bug-207) is how old the
    scope's newest timestamp is right now; it anchors the imputed age of a row whose own
    timestamp will not parse, which half the corpus width alone cannot do once the newest
    row is itself old. ``None`` means no span was computable (empty scope) and
    ``_compute_confidence`` falls back to its unanchored form. Order matters: the episode
    penalty scales ``_cosine`` before
    ``_compute_confidence`` reads it, so the confidence score reflects the penalised
    cosine (as in do_recall).

    ``query`` (bug-155): the original query text, forwarded to ``_backfill_cosines``
    so an FTS-only hit can be scored on its stored embedding rather than fall
    into ``_compute_confidence``'s cosine-less branch. Empty string (the default,
    preserved for existing call sites that predate the fix) disables backfill.
    """
    time_range_hours = 0.0
    newest_age_hours: float | None = None
    recall_counts: dict[int, tuple[int, str]] = {}
    if not results:
        return results, time_range_hours, recall_counts, newest_age_hours

    # bug-155: rows the fusion path admitted via FTS / keyword only arrive with
    # `_cosine=None`. Backfill the true cosine BEFORE the episode-boundary
    # penalty so the penalty scales backfilled and native cosines uniformly, and
    # so the CONFIDENCE_ENABLED block below reads a real signal. Under
    # confidence-off the backfill is a no-op — nothing downstream reads _cosine
    # in a way that would change ordering, and materialising one would perturb
    # `match_reason.cosine` and `_gate_score`.
    if CONFIDENCE_ENABLED:
        await _backfill_cosines(db, results, query, project_id, channel)

    if CONFIDENCE_ENABLED:
        # #361 item (7): `recall_counts` is populated ONLY under this flag, and the
        # recall-count bump in do_recall gates on that same dict being non-empty.
        # So with CPERSONA_CONFIDENCE_ENABLED=false (the shipped default) the read
        # path never records recall_count / last_recalled_at, and the recall-boost
        # feedback loop in _compute_confidence is inert — an install running the
        # default ranks on a different signal set than one with confidence on.
        # Documented rather than changed: making the bump unconditional would move
        # ranking state, which 2.5.2 deliberately holds still (charter §5).
        # bug-107: the temporal span is computed over the SAME isolation scope as
        # the recall — an agent-wide MIN/MAX let timestamps from unrelated
        # projects/channels scale a tightly-scoped recall's confidence curve.
        # Callers that score corpus-wide (gate calibration) keep the defaults.
        range_iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
        range_row = await db.execute_fetchall(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM memories{range_iso.where}",
            range_iso.params,
        )
        if range_row and range_row[0][0] and range_row[0][1]:
            oldest = _parse_timestamp_utc(range_row[0][0])
            newest = _parse_timestamp_utc(range_row[0][1])
            if oldest and newest:
                time_range_hours = max(0.0, (newest - oldest).total_seconds() / 3600)
                # bug-207: the span above is the corpus's internal width; this is where it
                # sits relative to now. Both are needed to place a row of unknown age
                # inside the dated range instead of ahead of it.
                newest_age_hours = max(
                    0.0, (datetime.now(timezone.utc) - newest).total_seconds() / 3600
                )

        # bug-041: exclude episode rows — their id collides with a memory id and
        # would otherwise pull that unrelated memory's recall_count/last_recalled_at
        # into the episode's confidence score.
        mem_ids = [
            r["id"]
            for r in results
            if isinstance(r.get("id"), int) and r["id"] > 0 and not _is_episode_result(r)
        ]
        if mem_ids:
            placeholders = ",".join("?" * len(mem_ids))
            rc_rows = await db.execute_fetchall(
                f"SELECT id, recall_count, last_recalled_at FROM memories WHERE id IN ({placeholders})",
                mem_ids,
            )
            recall_counts = {r[0]: (r[1], r[2] or "") for r in rc_rows}

    # v2.4.14: Episode boundary soft penalty (L3) — weaken cross-session memories
    # before quality gate so current-session signals take precedence.
    if EPISODE_PENALTY_ENABLED:
        episode_boundary_ts = await _get_episode_boundary_ts(
            db, agent_id, project_id=project_id, channel=channel
        )
        if episode_boundary_ts is not None:
            penalized = False
            for r in results:
                factor = _episode_boundary_factor(r.get("timestamp"), episode_boundary_ts)
                if factor < 1.0:
                    penalized = True
                    if "_cosine" in r:
                        r["_cosine"] = r["_cosine"] * factor
                    if "_rrf_score" in r:
                        r["_rrf_score"] = r["_rrf_score"] * factor
                    if "_rsf_score" in r:
                        r["_rsf_score"] = r["_rsf_score"] * factor
            # bug-115: with confidence off (the default), the penalised scores never
            # re-ordered anything — the confidence block below owns the only re-sort,
            # so under default config the penalty was a ranking no-op (computed, then
            # ignored by output order and downstream truncation). Re-sort here for
            # homogeneous fusion-ordered lists. Cascade results (no fusion score on
            # every row) intentionally keep stage order — bug-018 doctrine.
            if penalized and not CONFIDENCE_ENABLED:
                # bug-126: a profile injection row (id == -1) carries no fusion score, so the
                # bare all(...) below saw None and skipped the re-sort whenever a profile was
                # present — silently defeating the bug-115 penalty re-order under default config.
                # Check homogeneity over the SCORED rows only; profile rows sink to the bottom
                # via the sentinel (matching their append-at-end injection); stable-sort keeps ties.
                scored = [r for r in results if r.get("id") != -1]
                for score_key in ("_rrf_score", "_rsf_score"):
                    if scored and all(r.get(score_key) is not None for r in scored):
                        results.sort(key=lambda r, k=score_key: r.get(k, float("-inf")), reverse=True)
                        break

    if CONFIDENCE_ENABLED:
        for r in results:
            ts = r.get("timestamp", "")
            raw_cos = r.get("_cosine")
            is_resolved = r.get("_resolved", False)
            # bug-084: episode rows must not key into recall_counts — episodes and
            # memories AUTOINCREMENT independently, so episode #N would inherit the
            # unrelated memory #N's (recall_count, last_recalled_at) and get a spurious
            # confidence boost. bug-041 excluded episodes from the dict's CONSTRUCTION;
            # this closes the lookup side of the same invariant.
            rc_data = (0, "") if _is_episode_result(r) else recall_counts.get(r.get("id", -1), (0, ""))
            r["_confidence_score"] = _compute_confidence(
                raw_cos,
                ts,
                resolved=is_resolved,
                deep=deep,
                time_range_hours=time_range_hours,
                newest_age_hours=newest_age_hours,
                recall_count=rc_data[0],
                last_recalled_at_str=rc_data[1],
            )["score"]
        results.sort(key=lambda r: r.get("_confidence_score", 0), reverse=True)

    return results, time_range_hours, recall_counts, newest_age_hours


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
    ``source_id`` is non-empty — unless a ``channel`` filter (v2.4.22) is also
    set, in which case channel-scoped episodes are still recalled (the
    session-start grounding path).
    """
    # bug-032: clamp the caller-supplied limit like the list handlers do. A
    # negative limit otherwise flows to SQLite as `LIMIT -1` (unbounded full-corpus
    # scan + O(N) scoring on the hot path) and to `results[:limit]` as a silent
    # tail-drop. do_recall_with_context delegates here, so this covers both entries.
    # 2.5.0 (an earlier decision): the ceiling is the vector scan window (MAX_MEMORIES), not
    # 100 — the library layer bounds resource use only. The context-explosion cap
    # for agents lives at the MCP boundary (the recall tools' JSON Schema declares
    # `maximum: 100`); library callers (bench full-ranking, bulk export, future
    # rerank) may legitimately request full depth. In rrf mode the fusion-list
    # depth tracks `limit`, so the old in-library 100 cap silently collapsed
    # deep-ranking quality (bge-m3 LongMemEval 81.17 -> 48.98).
    limit = _clamp_limit(limit, MAX_MEMORIES)

    # Detect the static degraded case (mode=none) before dispatch; the runtime fault case
    # is observed at the embedding boundary in vector._search_vector. See health.py.
    health.observe_config()

    exclude_set: set[str] = set()
    if exclude_contents:
        exclude_set = {c.strip().lower() for c in exclude_contents if c.strip()}

    async with connection() as db:
        if RECALL_MODE == "rrf" and query.strip():
            results = await _recall_rrf(
                db, agent_id, query, limit, deep, channel, exclude_set,
                project_id=project_id, source_id=source_id,
            )
        elif RECALL_MODE == "rsf" and query.strip():
            results = await _recall_rsf(
                db, agent_id, query, limit, deep, channel, exclude_set,
                project_id=project_id, source_id=source_id,
            )
        else:
            results = await _recall_cascade(
                db, agent_id, query, limit, deep, channel, exclude_set,
                project_id=project_id, source_id=source_id,
            )

        # Episode-boundary penalty + confidence scoring (factored so the gate calibration
        # produces the exact same per-row gate score the runtime gate keys on — an earlier decision).
        # time_range_hours / recall_counts are reused below for the response metadata + the
        # recall-count update, so they are returned rather than recomputed.
        results, time_range_hours, recall_counts, newest_age_hours = await _apply_recall_scoring(
            db, agent_id, results, deep, project_id=project_id, channel=channel, query=query
        )

        memory_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (agent_id,)))[0][0]
    min_score = _adaptive_min_score(memory_count)
    effective_min = min_score * 0.5 if deep else min_score
    # v2.4.26/27 (an earlier decision): use the calibrated gate for whichever branch is active.
    # The gate carries the signal it was calibrated for; _apply_quality_gate applies it
    # only to the matching branch, so a gate from a different config is inert (no scale
    # mismatch). Under CONFIDENCE_ENABLED the active signal is "confidence".
    pure_recency = not query.strip()
    gate = None
    gate_signal = vector._fused_gate_signal
    if config.FUSED_GATE_ENABLED:
        gate = vector._get_fused_gate(agent_id)
        if gate is not None and deep:
            gate = gate * 0.5  # mirror the deep relaxation of min_score
    pre_gate = results
    results = _apply_quality_gate(
        results,
        effective_min,
        memory_count,
        gate=gate,
        gate_signal=gate_signal,
        pure_recency=pure_recency,
    )

    # bug-183 (2.5.2): the gate is a filter with no floor, so a query whose every hit is
    # lexical-but-semantically-distant (identifier/hash lookup, cross-lingual, a needle in
    # a long note) can lose ALL of them and return nothing — the caller cannot tell "no
    # such memory" from "the gate rejected the exact match". The b2 cosine backfill made
    # this reachable: those rows used to reach the gate cosine-less and pass by
    # construction, and now carry a real (low) cosine.
    #
    # Two bounds, both deliberate:
    #
    # (1) Only the EMPTY case. A mixed result is left exactly as the gate decided —
    #     demoting instead of dropping there would reopen the bug-155 inversion (weak
    #     lexical rows re-entering every ranked set) and would perturb the b2 soak on
    #     every query, where this perturbs only queries that today return nothing.
    # (2) Only the rows the backfill MOVED (`_cosine_backfilled`). A row that always
    #     carried a native cosine was gated on grounds b2 did not change, and returning
    #     it here would overturn a standing decision that a below-gate single-channel
    #     vector candidate is not an answer (tests/test_audit_2500b3.py's
    #     `test_empty_query_recall_bypasses_unscored_volume_gate`, and the
    #     `recall-no-hits` golden).
    #
    # What (2) is NOT: an exact reconstruction of pre-b2 membership. Under the pool-size
    # heuristic gate (the common case) the two coincide — a cosine-less row scored
    # sqrt(time_decay) and cleared it. Under a HIGH calibrated gate (say 0.80) that same
    # row scored ~0.55 and was blocked pre-b2 too, so this rescue can return a row b2 did
    # not remove. Considered and accepted: the alternative — gate the rescue on a
    # recomputed pre-b2 score — would switch the rescue OFF precisely where the gate is
    # strictest, which is the identifier/hash lookup this exists for, and would make
    # recall's membership depend on a second, shadow scoring function nothing else uses.
    # The property being defended is not "b2 parity" but "an exact lexical match is never
    # silently invisible"; `gate_fallback` is what keeps that honest by marking the rows
    # as below-gate rather than passing them off as hits.
    #
    # Reachability: `_cosine_backfilled` is only ever set under CONFIDENCE_ENABLED (any
    # fusion mode — cascade included — since the backfill is gated at the call site, not
    # by mode), so the DEFAULT config can never set gate_fallback; and an empty-query
    # pure-recency listing never reaches it either, because the backfill returns early on
    # a blank query and marks nothing.
    #
    # The rescued rows keep _apply_recall_scoring's confidence order; `gate_fallback`
    # tells the caller these are below-gate rows rather than ordinary hits. The profile
    # sentinel keeps the gate's own verdict — its rule is corpus size (memory_count >= 50),
    # not relevance — and is never backfilled (the backfill skips id <= 0).
    gate_fallback = False
    if not any(r.get("id") != -1 for r in results) and any(
        r.get("_cosine_backfilled") for r in pre_gate
    ):
        gate_fallback = True
        profile_passed = any(r.get("id") == -1 for r in results)
        results = [
            r
            for r in pre_gate
            if r.get("_cosine_backfilled") or (r.get("id") == -1 and profile_passed)
        ]
        logger.debug(
            "quality_gate: every non-profile row blocked (in=%d); restoring %d "
            "backfilled row(s) with gate_fallback=true (bug-183)",
            len(pre_gate),
            len(results),
        )

    # bug-183: autocut is a RELEVANCE-gap heuristic — it assumes the list is ordered by a
    # meaningful score and cuts at the largest break. The rescued set is deliberately made
    # of below-gate rows, so that assumption does not hold, and with a profile row present
    # (confidence 1.0) the gap between it and the rescued rows is the whole scale: autocut
    # cuts at index 1 and the response says gate_fallback=true while containing nothing but
    # the profile row. Skip it whenever the rescue fired; the gate already did the cutting.
    if AUTOCUT_ENABLED and not gate_fallback:
        results = _autocut(results)

    results = results[:limit]
    results.reverse()

    messages = []
    for r in results:
        content = r["content"]

        msg: dict = {"content": content}
        # an earlier decision: a stable full-fetch handle. `id` below is the caller-supplied
        # msg_id (absent on episodes), so previews need their own reference — this
        # is what get_contents(refs) resolves. Episode/memory kinds share one
        # AUTOINCREMENT id space (bug-040/041), hence the kind prefix.
        row_id = r.get("id")
        if isinstance(row_id, int) and row_id > 0:
            msg["ref"] = f"ep:{row_id}" if _is_episode_result(r) else f"mem:{row_id}"
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
            # bug-084: same episode guard as the ranking loop — see _apply_recall_scoring.
            rc_data = (0, "") if _is_episode_result(r) else recall_counts.get(r.get("id", -1), (0, ""))
            msg["confidence"] = _compute_confidence(
                raw_cosine,
                ts,
                resolved=is_resolved,
                deep=deep,
                time_range_hours=time_range_hours,
                newest_age_hours=newest_age_hours,
                recall_count=rc_data[0],
                last_recalled_at_str=rc_data[1],
            )
        # v2.5.2 additive: expose the score the ranking / gate keyed on so agents can
        # tell WHY a row surfaced (confidence vs rsf vs cosine vs rrf) instead of
        # guessing from opaque `confidence`. `signal` matches _gate_score's branch
        # precedence (confidence > rsf > cosine > rrf); the breakdown carries the
        # internal per-retriever contributions actually present on this row. Unscored
        # rows (cascade FTS/keyword stages without any signal) omit the key entirely
        # so consumers can distinguish "no signal at all" from "signal was zero".
        # Scoring reshape lives in 2.6.0 (charter §5 soak isolation); this exposes
        # only what the existing scoring layer already computed.
        gate_score, gate_signal = _gate_score(r)
        if gate_signal is not None:
            match_reason: dict = {"signal": gate_signal, "score": gate_score}
            if r.get("_cosine") is not None:
                match_reason["cosine"] = r["_cosine"]
            if r.get("_rrf_score") is not None:
                match_reason["rrf"] = r["_rrf_score"]
            if r.get("_rsf_score") is not None:
                match_reason["rsf"] = r["_rsf_score"]
            msg["match_reason"] = match_reason
        # b1-4 residual: the response is built by allowlist above (`msg`), so these
        # pops are hygiene on the internal row, not the thing that keeps private
        # keys out of the payload. _rsf_score was missing from the list — harmless
        # for that reason, and completed here so the set matches the keys the
        # scoring layer actually attaches.
        r.pop("_rid", None)
        r.pop("_cosine", None)
        r.pop("_cosine_backfilled", None)  # bug-183 marker — same hygiene rule
        r.pop("_confidence_score", None)
        r.pop("_rrf_score", None)
        r.pop("_rsf_score", None)
        r.pop("_resolved", None)
        messages.append(msg)

    # bug-038: the recall_count/last_recalled_at bump is a write that feeds
    # _compute_confidence ranking, so it must honor no-persist even though recall
    # is readOnlyHint=true and deliberately not one of the write-gated tools —
    # otherwise a benchmark/AB session in no-persist mode still mutates ranking
    # state, the exact contamination no-persist exists to prevent.
    #
    # bug-183: rescued rows are excluded for the same reason. recall_count raises the
    # decay floor in _compute_confidence, so crediting a below-gate row would let a row
    # that keeps being returned BECAUSE nothing else passed drift upward until it starts
    # passing the gate on unrelated queries — a feedback loop straight back into the
    # lexical contamination bug-155 closed. A rescue is a disclosure ("this is all there
    # was, and it is weak"), not a confirmed hit, so it earns no ranking credit.
    if not deep and not gate_fallback and recall_counts and not no_persist.is_paused():
        # bug-040: exclude episode rows — their id collides with a memory id, so
        # bumping `WHERE id IN (...)` on the memories table would falsely increment
        # an unrelated memory's recall_count and falsify its last_recalled_at.
        returned_ids = [
            r.get("id", -1)
            for r in results
            if isinstance(r.get("id"), int) and r["id"] > 0 and not _is_episode_result(r)
        ]
        if returned_ids:
            # bug-052: this ranking-bookkeeping write is non-essential — recall is
            # readOnlyHint=true and degrades gracefully. A failure here (e.g. a
            # transient 'database is locked' from a co-resident writer under WAL)
            # must not discard the already-computed recall result, so it is
            # non-fatal. bug-042/043: serialise the write behind the shared lock so
            # its commit cannot flush another handler's partial transaction.
            try:
                placeholders = ",".join("?" * len(returned_ids))
                async with transaction() as db:
                    await db.execute(
                        f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = datetime('now') WHERE id IN ({placeholders})",
                        returned_ids,
                    )
            except Exception as e:
                logger.warning("recall_count bump failed (non-fatal): %s", e)

    result: dict = {"messages": messages}
    # bug-183: present ONLY when the rescue fired. A `false` on every other recall would
    # change the payload of the whole surface (and every recorded golden) to say nothing.
    if gate_fallback:
        result["gate_fallback"] = True
    advisory = health.maybe_advisory()
    if advisory is not None:
        result["advisory"] = advisory
    return result


def _ctx_content(entry: object) -> str:
    """Null/type-safe ``content`` extraction for external_context entries.

    bug-035: the tool schema puts no type constraint on ``content``, so an entry
    with an explicit JSON null (-> None) — or any non-string value — made the old
    ``entry.get("content", "").strip()`` raise ``AttributeError`` (the '' default
    only applies when the key is *absent*, not when it is present-but-null),
    aborting the whole ``recall_with_context`` into an opaque {error}. Skip such a
    malformed entry instead: return '' so the caller's truthiness guard drops it.
    """
    if not isinstance(entry, dict):
        return ""
    val = entry.get("content")
    return val.strip() if isinstance(val, str) else ""


def _ctx_role(entry: object) -> str:
    """Null/type-safe ``role`` extraction for external_context entries.

    bug-163: the same schema gap bug-035 exposed on ``content`` also covers
    ``role``. The C13 disclosure collects the roles into a set and sorts it, so a
    non-string role was two separate faults: an unhashable value (dict/list) blew
    up building the set, and a mixed int/str set blew up in ``sorted()`` — either
    way aborting the whole ``recall_with_context`` into an opaque error. Coerce
    here so the disclosure reports the malformed role instead of dying on it.
    """
    if not isinstance(entry, dict):
        return ""
    val = entry.get("role", "")
    return val if isinstance(val, str) else str(val)


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

    exclude_list = [c.lower() for e in ctx if (c := _ctx_content(e))]

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
        content = _ctx_content(entry)  # bug-035: null/type-safe, skips malformed entries
        if not content:
            continue
        role = entry.get("role", "")

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

    result: dict = {"messages": messages}
    # audit C13: every context entry's content filters recall (exclude_list above
    # is role-agnostic — correct, the caller already holds that text), but only
    # user / assistant entries are merged into `messages`. So a system or tool
    # entry can suppress a memory while being invisible in the response, and the
    # caller sees a memory vanish with nothing explaining it. The filtering stays
    # (it is the right semantics); the silence does not. Reported only when such
    # entries exist, so the common case pays no payload.
    filter_only_roles = sorted(
        {
            _ctx_role(e) or "(unset)"
            for e in ctx
            if _ctx_content(e) and _ctx_role(e) not in ("assistant", "user")
        }
    )
    if filter_only_roles:
        result["context_filter_only"] = {
            "roles": filter_only_roles,
            "note": "entries with these roles filtered recall but are not shown in messages",
        }
    # bug-183: this entry point delegates the retrieval to do_recall but builds its own
    # response dict, so the rescue flag has to be forwarded like `advisory` below —
    # otherwise the merged-context caller is the one caller that cannot tell a
    # gate-rescued result from an ordinary one. Absent unless it fired (same contract).
    if recall_result.get("gate_fallback"):
        result["gate_fallback"] = True
    # Forward the advisory do_recall already produced — do NOT call maybe_advisory() again
    # here (that would flip the full template to the short one within one logical recall).
    advisory = recall_result.get("advisory")
    if advisory is not None:
        result["advisory"] = advisory
    return result


# an earlier decision: get_contents batch size. A full row is worth ~800 tokens (the
# token-inventory measurement that motivated the preview tier), so 20 full rows
# already approaches a whole recall's pre-diet payload — a larger batch would
# reopen the context-explosion hole the preview exists to close.
GET_CONTENTS_MAX_REFS = 20

# an earlier decision: the ref count alone stopped bounding this response. When the write
# cap was 2000 characters, 20 refs could not exceed ~40,000; raising the cap to
# 16,000 would have carried the same call to ~320,000 without a line of this
# file changing. A relaxation of the WRITE bound must not enlarge the READ blast
# radius, so the budget is stated here in characters and pinned at what the
# worst case used to be. It is deliberately not derived from MAX_CONTENT_LENGTH:
# raise that cap to any value and one response stays the size it is today.
GET_CONTENTS_MAX_CHARS = 40000


async def do_get_contents(agent_id: str, refs: list) -> dict:
    """Resolve recall preview refs back to full, untrimmed rows (2.5.0, an earlier decision).

    The recall tools' MCP boundary returns ``content`` as a preview
    (RECALL_PREVIEW_CHARS); every returned message carries a ``ref``
    (``mem:<id>`` / ``ep:<id>``) that this fetches in full. Reads are id-keyed
    (the ids came from an agent-scoped recall — the same provenance argument as
    the other id-keyed handlers) with the agent_id ownership predicate enforced,
    so a ref belonging to another agent lands in ``missing``, never in a leak.
    Malformed refs also land in ``missing`` (fail-soft: one bad ref must not
    abort the batch).
    """
    if not agent_id:
        return error_response("agent_id is required")
    if not isinstance(refs, list) or not refs:
        return error_response("refs must be a non-empty list of 'mem:<id>' / 'ep:<id>' strings")
    if len(refs) > GET_CONTENTS_MAX_REFS:
        return error_response(f"too many refs ({len(refs)}; max {GET_CONTENTS_MAX_REFS}) — split the fetch")

    items: list[dict] = []
    missing: list[str] = []
    deferred: list[str] = []
    used = 0
    async with connection() as db:
        for position, ref in enumerate(refs):
            kind, _, raw = str(ref).partition(":")
            try:
                row_id = int(raw)
            except (TypeError, ValueError):
                row_id = -1
            if kind not in ("mem", "ep") or row_id <= 0:
                missing.append(str(ref))
                continue
            if kind == "mem":
                rows = await db.execute_fetchall(
                    "SELECT msg_id, content, source, timestamp FROM memories WHERE id = ? AND agent_id = ?",
                    (row_id, agent_id),
                )
                if not rows:
                    missing.append(ref)
                    continue
                msg_id, content, source, timestamp = rows[0]
                # Mirror the recall message shape so callers can splice items in.
                item: dict = {"ref": ref, "content": content}
                if source:
                    item["source"] = source if isinstance(source, dict) else _try_parse_json(source)
                if timestamp:
                    item["timestamp"] = timestamp
                if msg_id:
                    item["id"] = msg_id
            else:
                rows = await db.execute_fetchall(
                    "SELECT summary, start_time, resolved, created_at FROM episodes "
                    "WHERE id = ? AND agent_id = ?",
                    (row_id, agent_id),
                )
                if not rows:
                    missing.append(ref)
                    continue
                summary, start_time, resolved, created_at = rows[0]
                item = {
                    "ref": ref,
                    "content": f"[Episode] {summary}",
                    "source": {"System": "episode"},
                    # bug-213: the same fallback the retrievers score by. get_contents is
                    # the expansion of a row recall already returned, so a timestamp that
                    # disagreed with the one recall showed would read as two different rows.
                    "timestamp": episode_timestamp(start_time, created_at),
                    "resolved": bool(resolved),
                }
            # an earlier decision: whole rows only — the budget never cuts a content
            # string. get_contents is the ONLY path back to full text, so a
            # trimmed answer here would be indistinguishable from the preview it
            # was called to escape (the bug-117 failure mode: content with no
            # remaining handle). The first row is therefore admitted whatever
            # its size — one row must never become unreachable — and once the
            # budget is spent the REST of the batch is deferred rather than
            # partially served, so the caller re-fetches on a boundary it can
            # see instead of guessing which refs were dropped.
            if items and used + len(item["content"]) > GET_CONTENTS_MAX_CHARS:
                deferred = [str(r) for r in refs[position:]]
                break
            used += len(item["content"])
            items.append(item)
    result: dict = {"items": items, "missing": missing, "count": len(items)}
    if deferred:
        # Absent unless the budget actually stopped the batch: a caller that
        # never meets it sees the same response shape as before.
        result["deferred"] = deferred
        result["budget_chars"] = GET_CONTENTS_MAX_CHARS
    return result


# CJK codepoint ranges: hiragana, katakana, CJK unified + ext-A, halfwidth katakana.
# Scripts written without inter-word spaces ("scriptio continua") need trigram
# decomposition rather than whitespace tokenisation.
_CJK_CLASS = r"぀-ヿ㐀-䶿一-鿿ｦ-ﾟ"
_CJK_RE = re.compile(f"[{_CJK_CLASS}]")
_TOKEN_RE = re.compile(f"[{_CJK_CLASS}]+|[^\\s{_CJK_CLASS}]+")


def _build_fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression for a (possibly CJK) query.

    Both FTS tables are trigram-tokenised. Whitespace tokenisation
    (``query.split()``) breaks for Japanese/Chinese, which have no inter-word
    spaces: the whole sentence collapses into one phrase that no document
    contains verbatim, so the keyword retriever returns nothing and recall
    falls back to vector-only (the recall-contamination root cause).

    Since the index is trigram-tokenised, we decompose each CJK run into its
    overlapping 3-grams and OR every term together, so a document is retrieved
    whenever it shares any 3-gram with the query (e.g. 'のパン'). ASCII runs are
    kept whole. Terms shorter than 3 codepoints can never match a trigram index,
    so they are dropped here and left to the caller's LIKE fallback. Returns ""
    when no usable term can be formed.
    """
    sanitized = re.sub(r"[^\w\s]", "", query, flags=re.UNICODE)
    terms: list[str] = []
    for tok in _TOKEN_RE.findall(sanitized):
        if _CJK_RE.match(tok):
            if len(tok) >= 3:
                terms.extend(tok[i : i + 3] for i in range(len(tok) - 2))
            # shorter CJK runs (e.g. 'パン') can't match a trigram index -> LIKE
        elif len(tok) >= 3:
            terms.append(tok)
        # ASCII tokens < 3 chars also can't match a trigram index -> dropped
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


async def _search_episodes_fts(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    channel: str = "",
    project_id: str | None = None,
) -> list[dict]:
    """Search episodes using FTS5.

    project_id (v2.4.17) applies the γ filter. channel (v2.4.22) applies an
    exact-match filter on the episode's channel — empty means no channel
    filter (all channels), mirroring the memory search paths.
    """
    fts_query = _build_fts_query(query)
    if not fts_query:
        return []
    # isolation_where composes all three axes (an earlier decision): exact agent, γ project,
    # and the knob2 v2 channel contract — an episode stored under channel '' is
    # global and surfaces in every channel-scoped recall, so old (pre-per-channel)
    # episodes are never orphaned once recall starts filtering by concrete channel.
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")

    rows = await db.execute_fetchall(
        f"""SELECT e.id, e.summary, e.start_time, e.resolved, bm25(episodes_fts), e.created_at
           FROM episodes_fts f
           JOIN episodes e ON f.rowid = e.id
           WHERE episodes_fts MATCH ?
           AND {iso.clause}
           ORDER BY rank
           LIMIT ?""",
        (fts_query, *iso.params, limit),
    )

    return [
        {
            "id": row[0],
            "content": f"[Episode] {row[1]}",
            "source": {"System": "episode"},
            # bug-213: start_time is nullable; created_at is not. This is the path
            # bug-207 measured — it is what passed "" for two thirds of the episodes.
            "timestamp": episode_timestamp(row[2], row[5]),
            "_rid": ("ep", row[0]),
            "_resolved": bool(row[3]),
            "_bm25": row[4],
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
    # isolation_where composes all three axes (an earlier decision): exact agent, γ project,
    # and the knob2 v2 channel contract (stored channel '' matches every
    # channel-scoped recall). The alias="m" variant serves the FTS-join path —
    # this replaces the old .replace("channel", "m.channel") rewrite hack.
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    iso_m = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="m")

    src_like = _like_escape_prefix(source_id)
    src_clause_bare = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params_bare = (src_like,) if src_like else ()
    src_clause_m = " AND json_extract(m.source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params_m = (src_like,) if src_like else ()

    if not query.strip():
        rows = await db.execute_fetchall(
            f"""SELECT id, msg_id, content, source, timestamp
               FROM memories
               WHERE {iso.clause}{src_clause_bare}
               ORDER BY created_at DESC
               LIMIT ?""",
            (*iso.params, *src_params_bare, limit),
        )
        return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4], "_bm25": None} for r in rows]

    if FTS_ENABLED:
        fts_query = _build_fts_query(query)
        if fts_query:
            rows = await db.execute_fetchall(
                f"""SELECT m.id, m.msg_id, m.content, m.source, m.timestamp, bm25(memories_fts)
                   FROM memories_fts f
                   JOIN memories m ON f.rowid = m.id
                   WHERE memories_fts MATCH ?
                   AND {iso_m.clause}{src_clause_m}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, *iso_m.params, *src_params_m, limit),
            )
            if rows:
                return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4], "_bm25": r[5]} for r in rows]

    # Note (bug-085 analysis): unlike vector._search_vector, this LIMIT is NOT a
    # recency scan window — the LIKE predicate filters BEFORE the LIMIT applies,
    # so it caps how many *matching* rows are fetched (always >= limit; the
    # return slices to limit). Old rows stay reachable; no decoupling needed.
    scan_limit = min(MAX_MEMORIES, max(limit * 5, 50))
    rows = await db.execute_fetchall(
        f"""SELECT id, msg_id, content, source, timestamp
           FROM memories
           WHERE {iso.clause}{src_clause_bare}
           AND content LIKE ? ESCAPE '\\'
           ORDER BY created_at DESC
           LIMIT ?""",
        (*iso.params, *src_params_bare, _like_escape_contains(query), scan_limit),
    )
    return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4], "_bm25": None} for r in rows[:limit]]


async def do_archive_episode(
    agent_id: str,
    history: list[dict],
    summary: str = "",
    keywords: str = "",
    resolved: bool | None = None,
    project_id: str = "",
    channel: str = "",
) -> dict:
    """Archive a conversation episode with pre-computed summary, keywords, and resolved status.

    project_id (v2.4.17): isolation axis. Defaults to '' (= global pool).
    channel (v2.4.22): conversation-channel tag for the episodic loop. Defaults
    to '' (= unscoped / shared). A channel-scoped recall returns episodes whose
    channel matches; unfiltered recall returns all of them.
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {"ok": True, "episode_id": None, "id": 0}, "archive_episode"
        )

    # bug-162: judge the text that would actually be STORED. _sanitize_content
    # strips [Memory from ...] annotations and whitespace, so an annotation-only
    # summary is truthy here while being empty in the row — it cleared this guard
    # and produced an empty-summary episode answering ok:true, the very input
    # do_store refuses with result:'rejected'. Refusing here (rather than letting
    # _prepare_episode_row raise) keeps the bug-006 response shape: the drain in
    # tasks.py still depends on that ValueError, so the guard lives in both.
    if not (_sanitize_content(summary) if isinstance(summary, str) else ""):
        # No server-side synthesis exists to fill this in, so an empty summary
        # cannot produce a stored episode. Return an explicit failure rather
        # than {ok:true, episode_id:None}, which read as success while writing
        # nothing (bug-006).
        return {
            "ok": False,
            "episode_id": None,
            "error": "summary is required to archive an episode",
        }

    row = await _prepare_episode_row(
        agent_id, history, summary, keywords, resolved, project_id, channel
    )

    # bug-042/043: transaction() serialises INSERT+commit behind the shared write
    # lock so the background queue drain's episode commit cannot flush — or be
    # flushed by — a concurrent import/merge's partial transaction on the shared
    # connection.
    async with transaction() as db:
        episode_id = await _insert_episode_row(db, row)
    # row[2] is the stored (capped) summary — index the text that actually landed,
    # not the caller's original, so the remote vector and the row agree (C12).
    await vector.remote_index_upsert(
        agent_id, [{"id": f"ep:{episode_id}", "text": row[2]}]
    )
    result = {"ok": True, "episode_id": episode_id}
    # Same signal do_store gives for capped content — and, since bug-175, the same
    # definition: the flag reports whether the cap CUT, not whether the caller's
    # raw string (annotation included) happened to exceed it.
    #
    # bug-195: the isinstance guard has to exist HERE as well as in
    # _prepare_episode_row. This line runs in do_archive_episode's own scope, so
    # it still holds the CALLER's original keywords — the prepare's coercion is
    # local to the prepare. Guarding only there would move the opaque TypeError
    # from the row build to this response line, after the episode was committed.
    keywords_truncated = isinstance(keywords, str) and sanitize_content_with_flag(keywords)[1]
    if sanitize_content_with_flag(summary)[1] or keywords_truncated:
        result["truncated"] = True
    return result


async def _prepare_episode_row(
    agent_id: str,
    history: list[dict],
    summary: str,
    keywords: str = "",
    resolved: bool | None = None,
    project_id: str = "",
    channel: str = "",
) -> tuple:
    """Prepare an episode row: validation + embedding (network I/O, NO lock held).

    Split from the INSERT (bug-089) so the queue drain can run the insert and
    its task-row delete in ONE transaction — the prepare half must stay outside
    because it performs the embedding HTTP round-trip (bug-072 class).
    Raises ValueError when the episode cannot be stored (empty summary)."""
    # audit C12: the episode's text fields were the last unbounded write path.
    # They are prose, so the memory rule applies verbatim — truncate to the same
    # cap rather than refuse, and do it HERE (the shared prepare seam) so the
    # queue drain is bounded by the same rule as the direct call. Capping before
    # the embed below also keeps the oversized string out of the backend request.
    #
    # bug-162: sanitize BEFORE the emptiness guard reads it. The guard used to
    # see the RAW value, so a summary that sanitizes to empty (an annotation-only
    # string, or pure whitespace) cleared it and an empty-summary row was written
    # with ok:true — the very input do_store refuses with result:'rejected'.
    # Refuse on what would actually be stored, not on what was handed in. The
    # isinstance guard keeps a non-string summary on the same ValueError path
    # instead of letting _sanitize_content raise an opaque TypeError.
    summary = _sanitize_content(summary) if isinstance(summary, str) else ""
    if not summary:
        raise ValueError("summary is required to archive an episode")
    # bug-195: keywords gets the same isinstance guard as summary above. A caller
    # that hands a list (the natural mistake — the tool's own description talks
    # about keywords in the plural) hit an opaque TypeError from the regex inside
    # _sanitize_content instead of the structured refusal path. Coerce rather than
    # raise: unlike summary, keywords is optional, so an unusable value is simply
    # no keywords. The sibling guard on the response line in do_archive_episode is
    # required too — that scope never sees this local rebinding.
    keywords = _sanitize_content(keywords) if isinstance(keywords, str) else ""
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

    return (agent_id, project_id, summary, keywords, start_time, end_time, embedding_blob, int(resolved), channel)


async def _insert_episode_row(db, row: tuple) -> int:
    """Leaf: INSERT a prepared episode row inside the caller's open transaction."""
    cursor = await db.execute(
        """INSERT INTO episodes (agent_id, project_id, summary, keywords, start_time, end_time, embedding, resolved, channel)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        row,
    )
    return cursor.lastrowid
