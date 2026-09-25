"""Memory read-write path handlers for CPersona.

Tools: do_store, do_recall, do_recall_with_context, do_archive_episode.

Profile handlers (do_get_profile / do_update_profile) live in admin_handlers.py;
this module re-exports do_update_profile reference only through tasks.py's
lazy queue dispatch.

Accesses `vector._embedding_client` as a module attribute (set by server.main()).
"""

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timezone

import aiosqlite
import httpx
from cpersona._vendored_mcp_common.isolation import coerce_for_write
from cpersona.isolation import isolation_where, source_id_where

from cpersona import blocks
from cpersona import cue
from cpersona import excerpts
from cpersona import health
from cpersona import nodes
from cpersona import providers
from cpersona import scope_stats
from cpersona import recall_trace
from cpersona import session
from cpersona import update_check
from cpersona import vector
from cpersona.session import resolve_session_key
from cpersona.config import (
    AUTOCUT_ENABLED,
    AUTOCUT_MIN_GAP_RATIO,
    AUTOCUT_MIN_RESULTS,
    CONFIDENCE_ENABLED,
    CONFIDENCE_ORDERING,
    EPISODE_DECAY_FLOOR,
    EPISODE_DECAY_RATE,
    EPISODE_PENALTY_ENABLED,
    FTS_ENABLED,
    MAX_MEMORIES,
    MAX_METADATA_LENGTH,
    PRIOR_AGE_ANCHOR,
    PRIOR_AGE_FLOOR,
    PRIOR_AGE_RATE,
    PRIOR_FAR_WEIGHT,
    RECALL_LIBRARY_MAX_LIMIT,
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
    future_timestamp_issue,
    _sanitize_content,
    sanitize_content_with_flag,
    _try_parse_json,
    error_response,
    normalize_source,
)
from cpersona.vector import _search_vector

logger = logging.getLogger(__name__)

# Resolve the provider seams now, so that a selection the registry refuses stops
# the server at import rather than failing its first recall (cpersona/providers.py).
providers.active()

#: Set once the missing-client warning below has been emitted. The condition is a
#: property of the process, not of the row being written: the client is installed
#: once at startup, so a store that finds it absent will find it absent every time.
#: Warning per store would bury the one line that matters under one line per write.
_warned_no_embedding_client = False


def _warn_once_no_embedding_client() -> None:
    """Say that rows are being stored unembedded although the configuration asks
    for embeddings.

    ``vector._embedding_client`` is installed by ``server.main()``. Anything that
    reaches ``do_store`` without going through it — an embedded caller, a script, a
    test harness — stores rows with a NULL embedding and answers ``embedded: false``,
    which is also the honest answer under ``EMBEDDING_MODE=none``. Nothing separated
    the two, so an install that was silently not embedding anything looked exactly
    like one deliberately configured not to, for as long as nobody searched.
    """
    global _warned_no_embedding_client
    if _warned_no_embedding_client or config.EMBEDDING_MODE == "none":
        return
    _warned_no_embedding_client = True
    logger.warning(
        "CPERSONA_EMBEDDING_MODE is %r but no embedding client is installed, so rows "
        "are being stored with no embedding and semantic recall will fall back to "
        "keyword/FTS. The client is created during server startup; a process that "
        "calls the handlers directly has to create it too. This is logged once.",
        config.EMBEDDING_MODE,
    )


# ---------------------------------------------------------------------------
# store outcome contract (2.5.2b1)
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


async def do_store(
    agent_id: str,
    message: dict,
    channel: str = "",
    project_id: str = "",
    session_key: str = "",
) -> dict:
    """Store a message in agent memory.

    project_id (v2.4.17): isolation axis. Defaults to '' (= global pool).
    Dedup checks the γ-visible scope (bug-106): a bucket write collides with an
    identical global-pool row (a recall in that bucket would surface both), while
    sibling buckets stay distinct; reads use the same γ semantics (see
    cpersona.isolation.isolation_where).
    """
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        # bug-141: keep the no-persist shape aligned with the success contract
        # ({ok, id, embedded}) — nothing was persisted, so embedded is False.
        # b1-1: it is a `skipped` outcome (deliberately not written, nothing
        # wrong); the helper overwrites `reason` with its TTL message and adds
        # persisted=False, which is the key to branch on for this branch alone.
        return session.make_skipped_response(
            {"ok": True, "result": "skipped", "id": 0, "embedded": False}, "store", key
        )

    msg_id = message.get("id", "")
    raw_content = message.get("content", "")
    # 2.5.2: normalize known legacy source shapes at the write seam.
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

    # N-03: what a stamp ahead of this clock costs, and why it is answered here.
    #
    # A caller generates its stamp on another host and it reaches us after a
    # network hop, so a correct client can name a moment slightly ahead of the
    # one we read. Past the allowance it is not skew. It is also not harmless:
    # the confidence curve reads `max(0.0, now - timestamp)`, so such a row is
    # scored as one written this instant and cannot decay — tomorrow it is still
    # ahead — and its stamp widens the corpus span that scales the decay rate,
    # flattening the time axis for every OTHER row too. Measured on a four-row
    # corpus, adding one row stamped 2099 took the rows admitted by the quality
    # gate from one to five (`corpus-future-*` in the behaviour golden).
    #
    # The verdict is taken on the value about to be written rather than on
    # `message["timestamp"]`, so an omitted stamp — which is this server's own
    # clock — can never report itself as early.
    #
    # Placed before the dedup probes so `reject` is decisive: a refused write is
    # refused whether or not an identical row happens to exist. `warn` rides back
    # on the stored result below; the log fires either way, which is what an
    # operator watching a misconfigured client actually reads.
    future_timestamp = future_timestamp_issue(timestamp)
    future_mode = config.FUTURE_TIMESTAMP_MODE
    if future_timestamp and future_mode != "off":
        logger.warning(
            "store: timestamp %r is %.0fs ahead of this clock, past the %ss "
            "CPERSONA_FUTURE_TIMESTAMP_SKEW_SECONDS allowance. Raise the allowance if "
            "this client is legitimate, or set CPERSONA_FUTURE_TIMESTAMP_MODE=reject to "
            "refuse it.",
            future_timestamp["timestamp"],
            future_timestamp["ahead_by_seconds"],
            future_timestamp["allowance_seconds"],
        )
        if future_mode == "reject":
            return _store_rejected(
                f"timestamp {timestamp!r} is "
                f"{future_timestamp['ahead_by_seconds']:.0f}s ahead of this clock "
                f"(allowance {future_timestamp['allowance_seconds']}s)"
            )

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

    # Overflow tree (docs/OVERFLOW_TREE_DESIGN.md §3): whether this text runs past
    # the embedding window decides whether its nodes are queued. Asked alongside the
    # embedding rather than after it, so the write waits for the slower of the two
    # requests instead of their sum. The probe never raises, so a result nobody
    # collects (a duplicate below, or a raise out of the insert) leaves nothing to log.
    window_probe = asyncio.ensure_future(nodes.runs_past_window(content)) if nodes.building_enabled() else None

    embedding_blob = None
    if vector._embedding_client and local_blobs_stored(VECTOR_SEARCH_MODE, STORE_BLOB):
        try:
            embeddings = await vector._embedding_client.embed([content])
            if embeddings:
                embedding_blob = vector.pack_for_storage(embeddings[0])
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, TypeError) as e:
            logger.warning("Embedding failed during store: %s", e)
    elif local_blobs_stored(VECTOR_SEARCH_MODE, STORE_BLOB):
        _warn_once_no_embedding_client()

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
            # bug-332: non-fatal is not the same as unreported -- the same
            # correction the bulk sibling took in bug-304, on the single-row path
            # it was deliberately scoped away from. This went to a debug record
            # that is off in any normal deployment, and `embedded` below is the
            # OR of the two surfaces, so a written local blob reported success
            # whatever the index answered. The row is then in the database and
            # not in the index, and a corpus that answers recalls with silence is
            # the one symptom that does not look like a fault. Still non-fatal,
            # still ok:true -- what changes is that the shortfall is said once, at
            # a level that is on, naming the row and the namespace.
            logger.warning(
                "Remote index push failed for mem:%s in namespace cpersona:%s "
                "(non-fatal; the row is stored but will not be found by remote search): %s",
                mem_id,
                agent_id,
                e,
            )

    result = {
        "ok": True,
        "result": "stored",
        "id": mem_id,
        "embedded": local_embedded or remote_embedded,
    }
    if truncated:
        result["truncated"] = True
    if future_timestamp and future_mode == "warn":
        result["timestamp_ahead_of_clock"] = future_timestamp
    if window_probe is not None and await window_probe:
        queued = await nodes.queue_build("mem", mem_id, agent_id, key)
        if queued:
            result["nodes"] = queued
    # Blocks are queued for every record, not only the ones that run past the
    # window: a short record still divides into clauses, and the build declines
    # by itself when the division yields a single block. The gate is checked
    # first, so a deployment that has not opted in does no work here at all.
    if blocks.building_enabled():
        queued = await blocks.queue_build("mem", mem_id, agent_id, key)
        if queued:
            result["blocks"] = queued
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
    lexical_terms: list[str] | None = None,
    query_vec_out: list | None = None,
) -> list[dict]:
    """Original cascading recall: stages fill remaining slots sequentially.

    Unchanged by `CPERSONA_VECTOR_REACH`: the far list is a second ranked list
    for a fusion to weigh against the first, and this recall fuses nothing — it
    concatenates stages in stage order. Adding far rows here would append older,
    lower-ranked candidates behind the vector stage rather than let them compete,
    which is not what the setting is for (docs/SCAN_WINDOW_REACH_DESIGN.md §3).
    """
    results: list[dict] = []
    seen_ids: set = set()
    _excl = exclude_set or set()
    rec = recall_trace.current()
    vector_results: list[dict] = []
    fts_results: list[dict] = []
    memory_rows: list[dict] = []

    if vector._embedding_client and query.strip():
        vector_results = await _search_vector(
            db, agent_id, query, limit, channel=channel, project_id=project_id,
            source_id=source_id, query_vec_out=query_vec_out,
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
            db, agent_id, query, limit, channel=channel, project_id=project_id, extra_terms=lexical_terms
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
            db, agent_id, query, remaining, channel=channel, project_id=project_id, source_id=source_id,
            extra_terms=lexical_terms,
        )
        for row in memory_rows:
            rid = ("mem", row["id"])
            if rid not in seen_ids and not _content_excluded(row["content"], _excl):
                results.append(row)
                seen_ids.add(rid)

    if rec is not None:
        # Cascade fills stage by stage; it computes no fused score.
        rec.arm("vector_near", vector_results, "_cosine")
        rec.arm("episode_fts", fts_results, "_bm25")
        rec.arm("memory_keyword", memory_rows, "_bm25")
        rec.fusion(results, "_none")
    return results


async def _recall_rrf(
    db,
    agent_id: str,
    query: str,
    depth: int,
    deep: bool,
    channel: str = "",
    exclude_set: set[str] | None = None,
    project_id: str | None = None,
    source_id: str = "",
    lexical_terms: list[str] | None = None,
    query_vec_out: list | None = None,
) -> list[dict]:
    """v2.4 RRF recall: run vector and FTS5 independently, merge with
    Reciprocal Rank Fusion. Avoids cascade's positional bias.

    `depth` is the Recall Depth (2.6): the top-K each arm hands to the fusion.
    It is not the response count -- `do_recall` cuts the fused list to `limit`
    afterwards -- so the fusion may consider more rows than the caller receives.
    The fused list is returned whole; nothing here knows the count.
    """
    k = RRF_K
    doc_map: dict[tuple, dict] = {}
    rrf_scores: dict[tuple, float] = {}
    _excl = exclude_set or set()
    # The recall trace (docs/RECALL_PROCESS_DESIGN.md §1): each arm's list and each
    # row's votes, recorded only when the caller asked for a trace.
    rec = recall_trace.current()
    votes: dict[str, dict] | None = {} if rec is not None else None
    fts_ep_results: list[dict] = []
    fts_mem_results: list[dict] = []
    vector_results: list[dict] = []
    far_results: list[dict] = []

    rrf_min_sim = vector._get_vector_threshold(agent_id) * RRF_THRESHOLD_FACTOR
    if vector._embedding_client:
        # One call to the vector retriever, as always. `far_out` collects the
        # second ranked list it produces when CPERSONA_VECTOR_REACH is set above
        # the scan window, and stays empty otherwise.
        vector_results = await _search_vector(
            db, agent_id, query, depth, min_similarity=rrf_min_sim,
            channel=channel, project_id=project_id, source_id=source_id,
            far_out=far_results, query_vec_out=query_vec_out,
        )
        for rank, row in enumerate(vector_results):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = row.get("_rid", ("mem", row["id"]))
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            if votes is not None:
                votes.setdefault(f"{rid[0]}:{rid[1]}", {})["vector_near"] = 1.0 / (k + rank + 1)

        # The far list (CPERSONA_VECTOR_REACH, empty unless it is set above the
        # scan window) is one more ranked list, fused exactly like the others: a
        # row's reciprocal-rank contribution is a function of its rank on its own
        # list, so appending a list leaves every existing row with the vote it
        # already had. The two vector lists are disjoint by scan position, so no
        # row is counted twice and the most a single row can still reach is three
        # votes — which is the per-row maximum the legacy quality gate rescales
        # its threshold by. See docs/SCAN_WINDOW_REACH_DESIGN.md §3.1.
        #
        # 2.6.0a7 (docs/PRIOR_FUNCTION_DESIGN.md §2): a far vote is worth
        # CPERSONA_PRIOR_FAR_WEIGHT of a near one. At the default of 1 this is
        # the unpriced far vote above; at 0 a far row contributes nothing.
        for rank, row in enumerate(far_results):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = row.get("_rid", ("mem", row["id"]))
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + PRIOR_FAR_WEIGHT / (k + rank + 1)
            if votes is not None:
                votes.setdefault(f"{rid[0]}:{rid[1]}", {})["vector_far"] = PRIOR_FAR_WEIGHT / (k + rank + 1)

    # Episodes lack per-user source tagging, so a per-user source_id filter
    # normally suppresses them; a channel filter (v2.4.22) scopes episodes to
    # one channel and is allowed even with source_id set (grounding path).
    if FTS_ENABLED and (not source_id or channel):
        fts_ep_results = await _search_episodes_fts(
            db, agent_id, query, depth, channel=channel, project_id=project_id, extra_terms=lexical_terms
        )
        for rank, row in enumerate(fts_ep_results):
            rid = ("ep", row["id"])
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            if votes is not None:
                votes.setdefault(f"ep:{row['id']}", {})["episode_fts"] = 1.0 / (k + rank + 1)

    if FTS_ENABLED:
        fts_mem_results = await _search_memories_keyword(
            db, agent_id, query, depth, channel=channel, project_id=project_id, source_id=source_id,
            extra_terms=lexical_terms,
        )
        for rank, row in enumerate(fts_mem_results):
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = ("mem", row["id"])
            if rid not in doc_map:
                doc_map[rid] = row
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            if votes is not None:
                votes.setdefault(f"mem:{row['id']}", {})["memory_keyword"] = 1.0 / (k + rank + 1)

    sorted_rids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)
    results = []
    for rid in sorted_rids:
        row = doc_map[rid]
        row["_rrf_score"] = rrf_scores[rid]
        results.append(row)
    if rec is not None:
        rec.arm("vector_near", vector_results, "_cosine")
        rec.arm("vector_far", far_results, "_cosine")
        rec.arm("episode_fts", fts_ep_results, "_bm25")
        rec.arm("memory_keyword", fts_mem_results, "_bm25")
        rec.fusion(results, "_rrf_score", votes)

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
    depth: int,
    deep: bool,
    channel: str = "",
    exclude_set: set[str] | None = None,
    project_id: str | None = None,
    source_id: str = "",
    lexical_terms: list[str] | None = None,
    query_vec_out: list | None = None,
) -> list[dict]:
    """Relative-Score-Fusion recall: like RRF but fuse the per-query min-max
    normalized *raw* score of each channel (cosine for vector, -bm25 for FTS)
    instead of rank. `depth` is the Recall Depth, as in `_recall_rrf`: the
    per-arm top-K, not the response count.

    RRF's rank-only fusion crushes large score margins — a rank-1 vs rank-4
    bm25 gap collapses to ~5% at K=60 — so a near-tie vector channel can
    reintroduce a topically distinct contaminant the keyword channel had
    correctly down-ranked. RSF keeps the margin, letting the keyword channel
    separate them. Dividing the sum by the number of active channels keeps the
    result inside [0, 1] and rewards multi-channel agreement.

    That range is not the cosine scale, and the quality gate is where the
    difference bites. ``_minmax_norm`` rescales each channel against the min and
    max of *this query's* candidates, so a fused score places a row among the
    rows retrieved alongside it rather than measuring its similarity to the
    query: the weakest survivor of a strong set is pinned to 0.0 however similar
    it is, and a lone candidate normalizes to 1.0 however weak. Both then meet a
    cosine-scale ``min_score`` in ``_apply_quality_gate``.
    See ClotoCore/docs/RECALL_CONTAMINATION_AB_2026-06-14.md.
    """
    doc_map: dict[tuple, dict] = {}
    vec_raw: dict[tuple, float | None] = {}
    far_raw: dict[tuple, float | None] = {}
    ep_raw: dict[tuple, float | None] = {}
    mem_raw: dict[tuple, float | None] = {}
    _excl = exclude_set or set()
    rec = recall_trace.current()
    near_rows: list[dict] = []
    far_rows: list[dict] = []
    ep_rows: list[dict] = []
    mem_rows: list[dict] = []

    rsf_min_sim = vector._get_vector_threshold(agent_id) * RRF_THRESHOLD_FACTOR
    if vector._embedding_client:
        near_rows = await _search_vector(
            db, agent_id, query, depth, min_similarity=rsf_min_sim,
            channel=channel, project_id=project_id, source_id=source_id,
            far_out=far_rows, query_vec_out=query_vec_out,
        )
        for row in near_rows:
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = row.get("_rid", ("mem", row["id"]))
            doc_map.setdefault(rid, row)
            vec_raw[rid] = row.get("_cosine", 0.0)

        # The far list is a fourth CHANNEL here, not a fourth rank list, and
        # unlike under rrf that is not free: each channel is min-max normalised
        # within itself and the sum is divided by the number of active channels,
        # so a far list that exists lowers every fused score against the
        # cosine-scale gate. Merging the far rows into the vector channel instead
        # would move the near rows' own min and max. Neither is bit-preserving
        # once the far list exists; the setting's measurement is registered for
        # the shipped rrf mode, and no claim is made about this one until it is
        # measured (docs/SCAN_WINDOW_REACH_DESIGN.md §3.2). With the reach off
        # the list is empty, so the divisor and every score are what they were.
        for row in far_rows:
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = row.get("_rid", ("mem", row["id"]))
            doc_map.setdefault(rid, row)
            far_raw[rid] = row.get("_cosine", 0.0)

    # Episodes lack per-user source tagging (mirrors _recall_rrf gating).
    if FTS_ENABLED and (not source_id or channel):
        ep_rows = await _search_episodes_fts(
            db, agent_id, query, depth, channel=channel, project_id=project_id, extra_terms=lexical_terms
        )
        for row in ep_rows:
            rid = ("ep", row["id"])
            doc_map.setdefault(rid, row)
            bm = row.get("_bm25")
            ep_raw[rid] = -bm if bm is not None else None

    if FTS_ENABLED:
        mem_rows = await _search_memories_keyword(
            db, agent_id, query, depth, channel=channel, project_id=project_id, source_id=source_id,
            extra_terms=lexical_terms,
        )
        for row in mem_rows:
            if _content_excluded(row.get("content", ""), _excl):
                continue
            rid = ("mem", row["id"])
            doc_map.setdefault(rid, row)
            bm = row.get("_bm25")
            mem_raw[rid] = -bm if bm is not None else None

    active = [ch for ch in (vec_raw, far_raw, ep_raw, mem_raw) if ch]
    n_active = len(active) or 1
    fused: dict[tuple, float] = {}
    votes: dict[str, dict] | None = {} if rec is not None else None
    names = {id(vec_raw): "vector_near", id(far_raw): "vector_far", id(ep_raw): "episode_fts", id(mem_raw): "memory_keyword"}
    for ch in active:
        # 2.6.0a7: the far channel is weighted by CPERSONA_PRIOR_FAR_WEIGHT; the
        # divisor stays the channel count (docs/PRIOR_FUNCTION_DESIGN.md §2).
        channel_weight = PRIOR_FAR_WEIGHT if ch is far_raw else 1.0
        for rid, w in _minmax_norm(ch).items():
            fused[rid] = fused.get(rid, 0.0) + w * channel_weight
            if votes is not None:
                votes.setdefault(f"{rid[0]}:{rid[1]}", {})[names[id(ch)]] = w * channel_weight / n_active

    results = []
    for rid in sorted(fused, key=fused.get, reverse=True):
        row = doc_map[rid]
        row["_rsf_score"] = fused[rid] / n_active
        results.append(row)
    if rec is not None:
        rec.arm("vector_near", near_rows, "_cosine")
        rec.arm("vector_far", far_rows, "_cosine")
        rec.arm("episode_fts", ep_rows, "_bm25")
        rec.arm("memory_keyword", mem_rows, "_bm25")
        rec.fusion(results, "_rsf_score", votes)

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
    # bug-335: the injected profile row is not a retrieval result and is not
    # scored like one. It carries no cosine by construction, so under confidence
    # it takes the strictly higher time-decay-only branch, sorts to the top, and
    # the step down to the first real memory is the largest gap in the list --
    # measured, a profile at 0.8385 against memories from 0.52 to 0.45 made this
    # return the profile alone. The cosine branch already refuses a list whose
    # rows do not all carry the signal; the confidence branch had no such guard,
    # and the guard it needs is not homogeneity of the field (the sentinel has
    # one) but that the sentinel is not a member of the comparison at all. It is
    # removed from the measurement and returned regardless of where the cut lands.
    sentinels = [r for r in results if r.get("id") == -1]
    scored = [r for r in results if r.get("id") != -1] if sentinels else results
    if len(scored) < AUTOCUT_MIN_RESULTS:
        return results
    results_for_gap = scored
    # bug-013: gap detection is only meaningful on similarity-scale signals
    # (confidence / cosine). Rank-fusion scores (rrf / rsf) decay
    # hyperbolically by construction — their "gaps" encode retriever overlap
    # (hit by both retrievers vs one), not relevance breaks, so on homogeneous
    # corpora autocut sliced a 17k-hit recall down to 2 rows. Fusion-ordered
    # results rely on the fused quality gate for contamination control; skip
    # the cut unless the ordering signal is similarity-scale.
    first = results_for_gap[0]
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
        if any(r.get("_cosine") is None for r in results_for_gap):
            return results
        key = "_cosine"
    scores = [r.get(key) or 0 for r in results_for_gap]
    max_score = scores[0]
    if max_score <= 0:
        return results
    gaps = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]
    max_gap = max(gaps)
    if max_gap / max_score < AUTOCUT_MIN_GAP_RATIO:
        return results  # no meaningful breakpoint
    cut_idx = max(range(len(gaps)), key=lambda i: gaps[i]) + 1
    if not sentinels:
        return results[:cut_idx]
    # Kept in the order the caller assembled, sentinel included wherever it sat.
    survivors = {id(r) for r in results_for_gap[:cut_idx]} | {id(r) for r in sentinels}
    return [r for r in results if id(r) in survivors]


def _adaptive_min_score(memory_count: int) -> float:
    """Compute adaptive quality threshold based on recall pool size.

    bug-216: an empty pool used to return 1.0, which is at or above the ceiling of
    EVERY gate branch (cosine and confidence cannot reach 1.0 for a real pair, the rrf
    branch's `min_score * RRF_MAX_SCALE` is the rank-1-in-all-three-retrievers maximum).
    So an agent whose pool the counting query happened to miss had every retrieved row
    discarded — recall answered "nothing" while the data was present and had been
    successfully retrieved. The threshold is floored at the small-pool value, which is
    the count -> 0 limit of the curve below (log(1) = 0 -> 0.5): strict, but reachable.
    """
    if memory_count <= 0:
        return 0.5
    t = min(1.0, math.log(memory_count + 1) / math.log(500))
    return round(0.5 - t * 0.3, 4)


def _gate_score(row: dict) -> tuple[float | None, str | None]:
    """The (score, signal) the quality gate keys on, by the SAME branch precedence as
    ``_apply_quality_gate``: confidence > rsf > cosine > rrf. Returns (None, None) for an
    unscored row. Used by both the runtime gate and the gate calibration so the
    calibrated operating point is computed on exactly the value the gate compares
    (v2.4.27)."""
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

    bug-216: ``memory_count`` is the size of the POOL this gate governs — memories +
    episodes over the recall's isolation scope — not a count of the ``memories`` table.
    The gate filters episode rows too, so a count that omitted them made an
    episodes-only agent look empty (see ``_adaptive_min_score``).

    v2.4.12 fix: previously ``_rrf_score`` was selected via falsy-chain before
    ``_cosine``, causing the RRF-scale value (0.01–0.05) to be compared against
    the cosine-scale ``min_score`` (0.2–1.0) → every RRF-mode result rejected.
    Cascade mode (no ``_rrf_score`` on rows) is unaffected.

    v2.4.26/27: ``gate`` is the calibrated operating point and ``gate_signal``
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
    # The recall trace records each decision with its reason; None outside a traced recall.
    rec = recall_trace.current()

    for r in results:
        # Profile — gate by memory count (unchanged)
        if r.get("id") == -1:  # profile sentinel
            if memory_count >= 50:
                filtered.append(r)
                stats["profile"] += 1
            else:
                stats["blocked"] += 1
            if rec is not None:
                rec.gate_decision(r, "profile", None, 50, memory_count >= 50, "profile_small_pool")
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
            if rec is not None:
                rec.gate_decision(r, "confidence", confidence, conf_threshold, confidence >= conf_threshold, "below_gate")
        elif rsf is not None:
            # RSF fused scores lie in [0, 1] but not on the cosine scale: min-max
            # normalization makes them relative to the rest of this query's
            # candidates (weakest survivor pins to 0.0, a lone candidate to 1.0),
            # so this comparison against a cosine-scale threshold is
            # query-dependent. See _recall_rsf.
            rsf_threshold = gate if (gate is not None and gate_signal == "rsf") else min_score
            if rsf >= rsf_threshold:
                filtered.append(r)
                stats["rsf"] += 1
            else:
                stats["blocked"] += 1
            if rec is not None:
                rec.gate_decision(r, "rsf", rsf, rsf_threshold, rsf >= rsf_threshold, "below_gate")
        elif cosine is not None:
            cos_threshold = gate if (gate is not None and gate_signal == "cosine") else min_score
            if cosine >= cos_threshold:
                filtered.append(r)
                stats["cosine"] += 1
            else:
                stats["blocked"] += 1
            if rec is not None:
                rec.gate_decision(r, "cosine", cosine, cos_threshold, cosine >= cos_threshold, "below_gate")
        elif rrf is not None:
            # Calibrated gate is on the raw RRF scale (calibrated on raw _rrf_score), so
            # compare directly; otherwise rescale the cosine-scale heuristic min_score.
            rrf_threshold = gate if (gate is not None and gate_signal == "rrf") else min_score * RRF_MAX_SCALE
            if rrf >= rrf_threshold:
                filtered.append(r)
                stats["rrf"] += 1
            else:
                stats["blocked"] += 1
            if rec is not None:
                rec.gate_decision(r, "rrf", rrf, rrf_threshold, rrf >= rrf_threshold, "below_gate")
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
            if rec is not None:
                rec.gate_decision(r, None, None, None, pure_recency or memory_count >= 100, "unscored_volume")

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


def _confidence_orders() -> bool:
    """Whether the confidence score orders and gates recall (2.6.0a7).

    Only under ``CPERSONA_CONFIDENCE_ORDERING=legacy``. From 2.6.0a7 the default is
    ``fusion``: with confidence enabled, recall keeps the fusion order and the fusion
    gate, and the confidence value is only returned beside each row. Measured on a
    real store, confidence on made the rrf and rsf modes return identical responses
    -- the re-sort discarded the fusion order -- while leaving answer accuracy where
    confidence off had it (docs/PRIOR_FUNCTION_DESIGN.md §1, §5). Read at call time
    so a test can set either global.
    """
    return CONFIDENCE_ENABLED and CONFIDENCE_ORDERING == "legacy"


def _age_weight(age_hours: float) -> float:
    """The age weight ``p_age = max(floor, 1 / (1 + age_hours * rate))``.

    The same family as the time decay inside the confidence score, so an arm with
    confidence's own rate and floor isolates the time term confidence used to apply.
    Exactly 1.0 while CPERSONA_PRIOR_AGE_RATE is 0 (the default).
    """
    if PRIOR_AGE_RATE <= 0:
        return 1.0
    return max(PRIOR_AGE_FLOOR, 1.0 / (1.0 + max(0.0, age_hours) * PRIOR_AGE_RATE))


def _apply_prior(
    results: list[dict],
    span: tuple[datetime | None, datetime | None],
    now: datetime,
) -> list[dict]:
    """Order the admitted rows by fused score x p(row) (docs/PRIOR_FUNCTION_DESIGN.md §2-§4).

    Called after the quality gate and autocut, before the count cut, so it moves rows
    within what was admitted and never admits or removes one (§3). Returns ``results``
    untouched -- the same list, in the same order -- when the age rate is 0, when
    confidence still orders (``legacy``), when the list is not uniformly fusion-scored
    (cascade keeps its stage order, bug-018), or when the scope has no dated row.

    ``span`` is ``(oldest, newest)`` over the scope's memories. Age is measured from
    ``newest`` (CPERSONA_PRIOR_AGE_ANCHOR=newest, the default) or from ``now``; a row
    newer than the anchor counts as age 0, and a row without a usable timestamp is
    placed at the middle of the scope's age range so that an unknown age cannot win
    (the bug-207 rule the confidence score already follows). The weight is recorded on
    each scored row as ``_prior`` for ``match_reason``.
    """
    if PRIOR_AGE_RATE <= 0 or not results or _confidence_orders():
        return results
    scored = [r for r in results if r.get("id") != -1]
    key = None
    for candidate in ("_rrf_score", "_rsf_score"):
        if scored and all(r.get(candidate) is not None for r in scored):
            key = candidate
            break
    oldest, newest = span
    if key is None or newest is None:
        return results
    width_hours = max(0.0, (newest - oldest).total_seconds() / 3600) if oldest else 0.0
    if PRIOR_AGE_ANCHOR == "now":
        anchor = now
        unknown_age = max(0.0, (now - newest).total_seconds() / 3600) + width_hours / 2.0
    else:
        anchor = newest
        unknown_age = width_hours / 2.0
    for r in scored:
        ts = _parse_timestamp_utc(r.get("timestamp") or "")
        age = (anchor - ts).total_seconds() / 3600 if ts else unknown_age
        r["_prior"] = _age_weight(age)
    # Stable, so rows the weight leaves tied keep the fusion order; the profile
    # sentinel sinks, as in the episode-penalty re-sort.
    results.sort(
        key=lambda r: r[key] * r["_prior"] if r.get("id") != -1 else float("-inf"),
        reverse=True,
    )
    return results


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
    # bug-394: ordered by instant, not by byte order. `created_at` is TEXT and SQLite
    # compares it byte by byte, so the separator alone decides: every row this server
    # writes carries datetime('now') with a space, while import and merge write the
    # record's own value through COALESCE with no format validation, and 'T' sorts
    # above ' ' at column 10. One imported row spelling the separator differently
    # therefore won the comparison whatever instant it named, and this boundary is a
    # scoring input -- it decides which memories are "this session". datetime() reads
    # both spellings and answers the same normalised form for each, so on a corpus
    # where every row already agrees this changes nothing. Rows whose stamp datetime()
    # cannot read sort last rather than winning on their bytes, which is the same
    # ruling the invalid-timestamp checks make: a stamp nobody can parse names no
    # instant. Validating created_at at the import seam is the other half and is a
    # decision about what an import may carry, so it is not made here.
    rows = await db.execute_fetchall(
        f"SELECT created_at FROM episodes{scope.where} "
        "ORDER BY datetime(created_at) DESC, created_at DESC LIMIT 1",
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
        """bug-301: chunked at the same width its sibling uses.

        ``vector._fetch_rows_by_id`` splits its ``IN (...)`` at
        ``_ID_FETCH_CHUNK``; this one built a single placeholder list of
        whatever length the caller's result set happened to be. Both read the
        same two tables by id, on the same recall, so the bound belongs to the
        pair rather than to one of them -- and the one without it is bounded
        only by how many rows a caller asked for, which the library layer lets
        reach ``RECALL_LIBRARY_MAX_LIMIT``. That is under the variable ceiling
        every SQLite this ships against reports today, which is why this is an
        asymmetry to close rather than a crash to chase: the sibling's limit is
        the one that was reasoned about, so the constant is imported rather than
        chosen again here.
        """
        blobs: dict[int, bytes | None] = {}
        for start in range(0, len(ids), vector._ID_FETCH_CHUNK):
            chunk = ids[start : start + vector._ID_FETCH_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = await db.execute_fetchall(
                f"SELECT id, embedding FROM {table} WHERE id IN ({placeholders})",
                chunk,
            )
            blobs.update({row[0]: row[1] for row in rows})
        return blobs

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
        # bug-300: width is not enough. A blob of the RIGHT width holding a NaN
        # multiplies through _cosine_batch to a NaN `_cosine`, which reaches
        # match_reason and confidence as a bare NaN -- a value RFC 8259 does not
        # admit, emitted from the one branch that exists to give these rows a
        # real signal. The write seam refuses to store a non-finite embedding
        # (pack_for_storage) and check_health reports the ones already stored,
        # but neither reaches the read side, which is where a blob written by an
        # older version or by import_memories' embedding_b64 arrives. Ask the
        # same question the health check asks, of the same helper, so the two
        # directions cannot come to disagree; a row that fails it is left at
        # _cosine=None with the others this backfill cannot serve, which is the
        # branch it already sat on before b2.
        if not vector.stored_blob_is_finite(blob):
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
    computes the operating point on exactly the per-row score the runtime
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
    # 2.6.0a7: the backfill exists to give the confidence score and the confidence
    # gate a real cosine, so it runs only where confidence still orders and gates.
    if _confidence_orders():
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
        # The span is read over the SAME isolation scope as the recall (bug-107),
        # and excludes rows whose timestamp is '' or unparseable (bug-237) — both
        # constraints, and the statement that carries them, live in scope_stats.py,
        # which also serves the identical query from a process-local cache. The
        # falsy guard below is the bug-237 one: it stays here because what an
        # absent span means is a scoring decision, not a storage one.
        min_ts, max_ts = await scope_stats.get_span(
            db, agent_id, project_id=project_id, channel=channel
        )
        if min_ts and max_ts:
            oldest = _parse_timestamp_utc(min_ts)
            newest = _parse_timestamp_utc(max_ts)
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
        # bug-318: chunked at the same width as _backfill_cosines above, for the
        # same reason and on the same pass. Its width came from the fused result
        # list before truncation rather than from the caller's limit, so 1,001
        # results produced one statement binding 1,001 parameters and 32,776 of
        # them raised "too many SQL variables". The bound belongs to the pair,
        # and its sibling was the one that was reasoned about, so the constant is
        # imported rather than chosen again here.
        for start in range(0, len(mem_ids), vector._ID_FETCH_CHUNK):
            chunk = mem_ids[start : start + vector._ID_FETCH_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rc_rows = await db.execute_fetchall(
                f"SELECT id, recall_count, last_recalled_at FROM memories WHERE id IN ({placeholders})",
                chunk,
            )
            recall_counts.update({r[0]: (r[1], r[2] or "") for r in rc_rows})

    # v2.4.14: Episode boundary soft penalty (L3) — weaken cross-session memories
    # before quality gate so current-session signals take precedence.
    if EPISODE_PENALTY_ENABLED:
        episode_boundary_ts = await _get_episode_boundary_ts(
            db, agent_id, project_id=project_id, channel=channel
        )
        if episode_boundary_ts is not None:
            penalized = False
            for r in results:
                # bug-257: episode rows are exempt from the boundary penalty. The
                # penalty's charter is to weaken cross-session MEMORIES so
                # current-session signals take precedence; an episode is a
                # cross-session summary by construction, and its retrieval purpose
                # is exactly the cross-session grounding the penalty suppresses.
                # Before the bug-213 created_at fallback, the NULL-start_time
                # majority of episodes was exempt by accident (empty timestamp
                # returned factor 1.0) while dated episodes were penalised; this
                # makes the exemption principled and uniform instead.
                if _is_episode_result(r):
                    continue
                factor = _episode_boundary_factor(r.get("timestamp"), episode_boundary_ts)
                trace_rec = recall_trace.current()
                if trace_rec is not None:
                    trace_rec.penalty(r, factor)
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
            if penalized and not _confidence_orders():
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

    if _confidence_orders():
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


def _recall_depth(limit: int) -> int:
    """Recall Depth for a response count of `limit` (2.6, "Depth is not count").

    `max(limit, CPERSONA_RECALL_DEPTH_FLOOR)`, clamped to the library ceiling.
    Read from `config` at call time so a process can be pointed at a floor
    without a restart of the module graph (tests do this; an operator changes
    the env and restarts). The floor never lowers the depth below the count:
    a caller asking for 200 rows still gets a fusion at least 200 deep.
    """
    return _clamp_limit(max(limit, config.RECALL_DEPTH_FLOOR), RECALL_LIBRARY_MAX_LIMIT)


async def _block_reserved_rows(
    db,
    hits: list,
    agent_id: str,
    *,
    project_id: str | None,
    channel: str,
    source_id: str,
    exclude_set: set[str],
    wanted: int,
) -> list[dict]:
    """Hydrate the records the block arm reached, best block first (§4, §5).

    The block index is not the authority on what a caller may see. Its rows
    carry a copy of their parent's isolation axes so the Hamming cut is not
    spent on rows that will be dropped, and this re-applies the real predicate
    against the record tables: a row the copies admitted and the authority does
    not is dropped here, fail-closed, and the reservation goes unfilled rather
    than being filled with it.

    The caller's other restrictions apply exactly as they do to every other arm:
    a source-id prefix filters memories, and it suppresses episodes unless a
    channel is also set, because episodes carry no per-user source tag. An
    excluded content is excluded here too.
    """
    mem_ids = [h.parent_id for h in hits if h.kind == "mem"]
    ep_ids = [h.parent_id for h in hits if h.kind == "ep"]
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src = source_id_where(source_id)

    mem_rows = await vector._fetch_rows_by_id(
        db,
        "SELECT id, msg_id, content, source, timestamp FROM memories "
        f"WHERE id IN ({{ph}}){iso.and_clause}{src.and_clause}",
        mem_ids,
        (*iso.params, *src.params),
    )
    # The episode rule mirrors the fused arms: no per-user source tag exists, so
    # a source-scoped recall sees episodes only when a channel scopes them too.
    ep_rows = {}
    if ep_ids and (not source_id or channel):
        ep_rows = await vector._fetch_rows_by_id(
            db,
            "SELECT id, summary, start_time, resolved, created_at FROM episodes "
            f"WHERE id IN ({{ph}}){iso.and_clause}",
            ep_ids,
            iso.params,
        )

    out: list[dict] = []
    for hit in hits:
        if len(out) >= wanted:
            break
        if hit.kind == "mem":
            row = mem_rows.get(hit.parent_id)
            if row is None or _content_excluded(row[2] or "", exclude_set):
                continue
            built = {
                "id": row[0],
                "msg_id": row[1],
                "content": row[2],
                "source": row[3],
                "timestamp": row[4],
                "_rid": ("mem", row[0]),
            }
        else:
            row = ep_rows.get(hit.parent_id)
            if row is None:
                continue
            built = {
                "id": row[0],
                "content": f"[Episode] {row[1]}",
                "source": {"System": "episode"},
                "timestamp": episode_timestamp(row[2], row[4]),
                "_rid": ("ep", row[0]),
                "_resolved": bool(row[3]),
            }
        # Why this row is here, carried on the row and rendered in the response.
        # It is deliberately not a score: no number from this arm reaches the
        # quality gate, and one that appeared beside the gate's own signals would
        # be read as comparable to them.
        built["_block_distance"] = hit.distance
        built["_block_index"] = hit.block_index
        built["_block_order"] = "hamming" if hit.cosine is None else "vector"
        out.append(built)
    return out


async def _episode_rows(db, iso, ranked: list[tuple[int, float | None]]) -> list[dict]:
    """Episode ids, in the given order, as recall rows (the shape every arm returns)."""
    payload = await vector._fetch_rows_by_id(
        db,
        f"SELECT id, summary, start_time, resolved, created_at FROM episodes e WHERE id IN ({{ph}}){iso.and_clause}",
        [ep_id for ep_id, _ in ranked],
        tuple(iso.params),
    )
    out = []
    for ep_id, score in ranked:
        row = payload.get(ep_id)
        if row is None:
            continue
        built = {
            "id": ep_id,
            "_rid": ("ep", ep_id),
            "content": f"[Episode] {row[1]}",
            "source": {"System": "episode"},
            "timestamp": episode_timestamp(row[2], row[4]),
            "_resolved": bool(row[3]),
        }
        if score is not None:
            built["_cosine"] = score
        out.append(built)
    return out


async def _search_cue_arm(
    db,
    agent_id: str,
    query: str,
    depth: int,
    window: tuple[datetime, datetime],
    *,
    channel: str,
    project_id: str | None,
    source_id: str,
    exclude_set: set[str],
    query_vec: list,
    lexical_terms: list[str] | None = None,
) -> list[dict]:
    """The cue arm (docs/RECALL_PROCESS_DESIGN.md §2.2): the memories and episodes whose
    time falls in `window`, ranked against the query.

    Vector and keyword search over the period only, each to `depth`, merged into one
    ranked list by reciprocal rank. The list orders rows for the bounded move and the
    held seat; none of its numbers reaches a fused score or the gate. The vector half
    reuses the query vector the ordinary vector arm embedded (`query_vec`), so a cue
    costs no second embedding; where no local vector was produced it is empty and the
    arm is keyword only. Isolation and the source filter are the recall's own; an
    episode carries no per-user source, so with a source filter episodes are searched
    only when a channel also scopes them, as in the ordinary arms (bug-080).

    Episodes are searched since cued-v0.2 (§2.10): searching memories only lifted the
    other memories of a period past an episode that held the answer.
    """
    start, end = (cue.sql_instant(w) for w in window)
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src = source_id_where(source_id)
    in_window = "datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)"
    lists: list[list[dict]] = []

    if query_vec and query.strip():
        import numpy as np

        qv = np.array(query_vec[0], dtype=np.float32)
        survivors = await vector._chunked_cosine_scan(
            db,
            f"""SELECT id, embedding FROM memories
               WHERE {iso.clause} AND embedding IS NOT NULL{src.and_clause} AND {in_window}
               ORDER BY created_at DESC, id ASC
               LIMIT ?""",
            (*iso.params, *src.params, start, end, MAX_MEMORIES),
            qv,
            len(qv),
            vector._get_vector_threshold(agent_id) * RRF_THRESHOLD_FACTOR,
            depth,
        )
        ranked = sorted(survivors, key=lambda s: (-s[2], s[0]))
        payload = await vector._fetch_rows_by_id(
            db,
            f"SELECT id, msg_id, content, source, timestamp FROM memories WHERE id IN ({{ph}})"
            f"{iso.and_clause}{src.and_clause}",
            [mem_id for _, mem_id, _ in ranked],
            (*iso.params, *src.params),
        )
        lists.append([
            {"id": mem_id, "_rid": ("mem", mem_id), "_cosine": score, "msg_id": payload[mem_id][1],
             "content": payload[mem_id][2], "source": payload[mem_id][3], "timestamp": payload[mem_id][4]}
            for _, mem_id, score in ranked
            if mem_id in payload
        ])

    # An empty query has no ranking to offer, so the keyword half returns the period's
    # newest records, as the ordinary recall does for an empty query.
    if FTS_ENABLED or not query.strip():
        keyword = await _search_memories_keyword(
            db, agent_id, query, depth, channel=channel, project_id=project_id, source_id=source_id,
            extra_terms=lexical_terms, window=(start, end),
        )
        lists.append([{**row, "_rid": ("mem", row["id"])} for row in keyword])

    if not source_id or channel:
        ep_iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")
        if query_vec and query.strip():
            import numpy as np

            qv = np.array(query_vec[0], dtype=np.float32)
            survivors = await vector._chunked_cosine_scan(
                db,
                f"""SELECT e.id, e.embedding FROM episodes e
                   WHERE {ep_iso.clause} AND e.embedding IS NOT NULL{_EPISODE_IN_WINDOW}
                   ORDER BY e.created_at DESC, e.id ASC
                   LIMIT ?""",
                (*ep_iso.params, start, end, MAX_MEMORIES),
                qv,
                len(qv),
                vector._get_vector_threshold(agent_id) * RRF_THRESHOLD_FACTOR,
                depth,
            )
            ranked = sorted(survivors, key=lambda s: (-s[2], s[0]))
            lists.append(await _episode_rows(db, ep_iso, [(ep_id, score) for _, ep_id, score in ranked]))
        if FTS_ENABLED and query.strip():
            lists.append(await _search_episodes_fts(
                db, agent_id, query, depth, channel=channel, project_id=project_id,
                extra_terms=lexical_terms, window=(start, end),
            ))
        elif not query.strip():
            rows_ = await db.execute_fetchall(
                f"""SELECT e.id FROM episodes e WHERE {ep_iso.clause}{_EPISODE_IN_WINDOW}
                   ORDER BY datetime(COALESCE(NULLIF(e.start_time, ''), e.created_at)) DESC, e.id ASC
                   LIMIT ?""",
                (*ep_iso.params, start, end, depth),
            )
            lists.append(await _episode_rows(db, ep_iso, [(r[0], None) for r in rows_]))

    votes: dict[tuple, float] = {}
    rows: dict[tuple, dict] = {}
    for ranked_list in lists:
        for rank, row in enumerate(ranked_list):
            if _content_excluded(row.get("content", ""), exclude_set):
                continue
            rows.setdefault(row["_rid"], row)
            votes[row["_rid"]] = votes.get(row["_rid"], 0.0) + 1.0 / (cue.RANK_CONSTANT + rank)
    ordered = sorted(votes, key=lambda rid: -votes[rid])  # stable: first-seen breaks ties
    return [rows[rid] for rid in ordered[:depth]]


async def do_recall(
    agent_id: str,
    query: str,
    limit: int,
    deep: bool = False,
    channel: str = "",
    exclude_contents: list | None = None,
    project_id: str | None = None,
    source_id: str = "",
    session_key: str = "",
    lexical_terms: list[str] | None = None,
    excerpt_chars: int = 0,
    trace: bool = False,
    time_cue: dict | None = None,
) -> dict:
    """Recall, optionally returning the recall trace (docs/RECALL_PROCESS_DESIGN.md §1).

    Without ``trace`` this is exactly ``_do_recall``. With it, a recorder is active for
    the duration of the call and the response carries ``trace``: references, ranks,
    scores and reasons per stage, never stored text. The recall itself is unchanged.

    ``time_cue`` (§2) says when the answer was stored, with a confidence; see
    ``cpersona/cue.py``. A cue that cannot be read is refused with ``error`` and no
    messages rather than ignored, so a caller never believes a cue it did not send
    was applied.
    """
    # The providers this recall runs with, read once, here: a set installed while
    # it runs applies to the recalls that start after it (cpersona/providers.py).
    active = providers.active()
    try:
        parsed_cue = active.cue_interpreter.parse(time_cue)
    except cue.TimeCueError as exc:
        return error_response(str(exc), messages=[])
    kwargs = dict(
        deep=deep, channel=channel, exclude_contents=exclude_contents, project_id=project_id,
        source_id=source_id, session_key=session_key, lexical_terms=lexical_terms,
        excerpt_chars=excerpt_chars, **({"time_cue": parsed_cue} if parsed_cue is not None else {}),
        providers_=active,
    )
    if not trace:
        return await _do_recall(agent_id, query, limit, **kwargs)
    from cpersona import __version__
    from cpersona import utils as _utils

    rec = recall_trace.TraceRecorder()
    rec.set("policy", {
        "scoring": _utils.SCORING_VERSION,
        "process": cue.POLICY if parsed_cue is not None else recall_trace.PROCESS_SINGLE_PASS,
    })
    rec.set("server_version", __version__)
    rec.set("scope", {"agent_id": agent_id, "project_id": project_id, "channel": channel, "source_id": source_id})
    rec.set("request", {
        "limit": limit, "deep": deep, "mode": RECALL_MODE,
        "confidence_enabled": CONFIDENCE_ENABLED, "confidence_ordering": CONFIDENCE_ORDERING,
        "prior": {"far_weight": PRIOR_FAR_WEIGHT, "age_rate": PRIOR_AGE_RATE,
                  "age_floor": PRIOR_AGE_FLOOR, "age_anchor": PRIOR_AGE_ANCHOR},
        "episode_penalty": EPISODE_PENALTY_ENABLED,
        **({"time_cue": parsed_cue.echo()} if parsed_cue is not None else {}),
    })
    rec.set("config", {
        "embedding_mode": config.EMBEDDING_MODE, "embedding_model": config.EMBEDDING_MODEL, "scan_window": MAX_MEMORIES,
        "vector_reach": config.VECTOR_REACH, "vector_far_limit": config.VECTOR_FAR_LIMIT,
        "fused_gate_enabled": config.FUSED_GATE_ENABLED, "autocut_enabled": AUTOCUT_ENABLED,
    })
    token = rec.activate()
    try:
        result = await _do_recall(agent_id, query, limit, **kwargs)
    finally:
        rec.deactivate(token)
    result["trace"] = rec.finish()
    return result


async def _do_recall(
    agent_id: str,
    query: str,
    limit: int,
    deep: bool = False,
    channel: str = "",
    exclude_contents: list | None = None,
    project_id: str | None = None,
    source_id: str = "",
    session_key: str = "",
    lexical_terms: list[str] | None = None,
    excerpt_chars: int = 0,
    time_cue: cue.TimeCue | None = None,
    providers_: providers.Providers | None = None,
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

    lexical_terms (2.6): additional terms for the two lexical arms ONLY -- the
    episode FTS and the memory keyword search. The vector arm, the scoring and
    the gate see ``query`` unchanged. Reconstructive recall passes the declared
    names and aliases of the entities a query mentions here
    (docs/ASSOCIATIVE_MEMORY_DESIGN.md §3); the ``recall`` tool never does, and
    with ``None`` or an empty list every statement is the one it was before.

    excerpt_chars (2.6): when positive, a row whose content is longer than the
    preview tier (config.RECALL_PREVIEW_CHARS) also carries ``excerpt`` — the
    part of the record that matched the query, at most this many characters —
    and ``excerpt_basis`` (cpersona/excerpts.py). Zero, the default, leaves every
    row exactly as before: the MCP boundary asks for it, library callers do not.
    """
    # bug-032: clamp the caller-supplied limit like the list handlers do. A
    # negative limit otherwise flows to SQLite as `LIMIT -1` (unbounded full-corpus
    # scan + O(N) scoring on the hot path) and to `results[:limit]` as a silent
    # tail-drop. do_recall_with_context delegates here, so this covers both entries.
    # 2.5.0: the ceiling is not 100 — the library layer bounds resource use only.
    # The context-explosion cap for agents lives at the MCP boundary (the recall
    # tools' JSON Schema declares `maximum: 100`); library callers (bench
    # full-ranking, bulk export, future rerank) may legitimately request full
    # depth. In rrf mode the fusion-list depth tracks `limit`, so the old
    # in-library 100 cap silently collapsed deep-ranking quality (bge-m3
    # LongMemEval 81.17 -> 48.98).
    #
    # The ceiling is RECALL_LIBRARY_MAX_LIMIT rather than the vector scan window
    # it used to be. Those are different questions — how far back the retriever
    # looks, and how many rows one call may materialise — and while they shared a
    # constant, widening the window for a larger corpus widened this bound by the
    # same factor without anyone choosing to.
    started = time.perf_counter()
    # The set do_recall read at its start; read here only for a direct library call.
    p = providers_ if providers_ is not None else providers.active()
    requested = limit
    limit = _clamp_limit(limit, RECALL_LIBRARY_MAX_LIMIT)
    if requested > limit:
        # Said out loud, because this is the failure mode of the 100 cap above:
        # a clamp that bites does not raise, it returns a worse ranking that
        # looks like a result. A bench reading its own scores cannot tell the
        # two apart; a line in the log can.
        logger.warning(
            "recall limit %d reduced to the library ceiling %d "
            "(raise CPERSONA_RECALL_LIBRARY_MAX_LIMIT for a deeper ranking)",
            requested,
            limit,
        )

    # 2.6 (Depth is not count): `limit` is how many rows come back; `depth` is
    # how far the fusion digs -- the per-arm top-K. They were one number, and the
    # coupling cost accuracy in a measurable way (a limit of 5 put rows
    # structurally out of reach at every gate value, see Goal-level notes in
    # docs/RELIABLE_RECALL_2_6.md section 4). At the default floor the two are
    # still equal, so nothing about today's ranking moves until the floor does.
    # The cascade path is untouched: it fills `limit` slots stage by stage and
    # fuses nothing, so a depth has no list to deepen there.
    depth = _recall_depth(limit) if RECALL_MODE in {"rrf", "rsf"} and query.strip() else limit

    # Detect the static degraded case (mode=none) before dispatch; the runtime fault case
    # is observed at the embedding boundary in vector._search_vector. See health.py.
    health.observe_config()

    exclude_set: set[str] = set()
    if exclude_contents:
        exclude_set = {c.strip().lower() for c in exclude_contents if c.strip()}

    # Filled by whichever fusion ran, with the one vector it embedded. Empty
    # wherever no local vector was produced -- no client, a remote search that
    # answered for itself, an embed that failed -- and the block arm reads that
    # emptiness as "nothing to rank on" rather than embedding the query again.
    query_vec_out: list = []
    async with connection() as db:
        results = await p.fusion.retrieve(
            db, agent_id=agent_id, query=query, depth=depth, limit=limit, deep=deep,
            channel=channel, exclude_set=exclude_set, project_id=project_id,
            source_id=source_id, query_vec_out=query_vec_out, lexical_terms=lexical_terms,
        )

        # Every row an ordinary arm reached, whatever the gate later decides: the
        # cue's held seat is only for a record no other arm found (§2.4), so a row
        # the gate refused cannot come back through it.
        reached = {r.get("_rid") or (("ep" if _is_episode_result(r) else "mem"), r.get("id")) for r in results}

        # Episode-boundary penalty + confidence scoring (factored so the gate calibration
        # produces the exact same per-row gate score the runtime gate keys on).
        # time_range_hours / recall_counts are reused below for the response metadata + the
        # recall-count update, so they are returned rather than recomputed.
        results, time_range_hours, recall_counts, newest_age_hours = await p.scoring.score(
            db, agent_id, results, deep, project_id=project_id, channel=channel, query=query
        )
        trace_rec = recall_trace.current()
        if trace_rec is not None:
            trace_rec.data["request"]["depth"] = depth
            trace_rec.mark("retrieve_and_score")

        # 2.6.0a7: the span the age weight is measured against, read inside this
        # connection (cached per scope). Nothing is read while the weight is off.
        prior_span: tuple[datetime | None, datetime | None] = (None, None)
        if PRIOR_AGE_RATE > 0 and not _confidence_orders():
            span_min, span_max = await scope_stats.get_span(
                db, agent_id, project_id=project_id, channel=channel
            )
            prior_span = (
                _parse_timestamp_utc(span_min) if span_min else None,
                _parse_timestamp_utc(span_max) if span_max else None,
            )

        # bug-216: count the pool the gate actually GOVERNS. The heuristic threshold was
        # computed over `memories` alone but applied to every row the retrievers found —
        # episodes and the profile sentinel included — so an agent holding only episodes
        # (session-summary-only client; memories removed by delete_memory/health repair
        # while its episodes stayed) scored count 0 and had every episode blocked. The
        # two COUNTs are scoped like the recall itself, for the same reason bug-107
        # scoped the temporal span: a tightly-scoped recall must not be gated by another
        # project's volume. Summing them HERE (rather than in scope_stats) keeps the two
        # tables separately visible to anything else that reads the same scope.
        pool_memories, pool_episodes = await scope_stats.get_pool_counts(
            db, agent_id, project_id=project_id, channel=channel
        )
        memory_count = pool_memories + pool_episodes

        # The block arm (docs/BLOCK_REACH_DESIGN.md §4-§5). It runs beside the
        # arms above and is fused with none of them: its hits are admitted by
        # reservation after the gate, so no score from here reaches a gate that
        # was calibrated on another population. Hydrated inside this connection
        # and filtered after the cut, because what the gate will admit is not
        # known yet and a hit whose record the gate admits anyway is not a
        # reserved row -- it is a row that was already there.
        block_rows: list[dict] = []
        if blocks.retrieval_enabled() and query.strip() and query_vec_out:
            block_rows = await p.block_candidates.reserved_rows(
                db,
                query_vec_out[0],
                agent_id=agent_id,
                project_id=project_id,
                channel=channel,
                source_id=source_id,
                exclude_set=exclude_set,
                limit=limit,
            )
            if trace_rec is not None:
                trace_rec.arm("block", block_rows, "_block_distance")

        # The time cue (docs/RECALL_PROCESS_DESIGN.md §2): the cue arm searches the
        # period, and if it finds nothing the loop suspects the period is wrong and
        # widens it once, running only the cue arm again. The ordinary arms above are
        # not run a second time.
        cue_rows: list[dict] = []
        cue_note: dict | None = None
        cue_ignored: dict | None = None
        if time_cue is not None:
            span_min, span_max = await scope_stats.get_span(
                db, agent_id, project_id=project_id, channel=channel
            )
            span = (cue.utc(span_min), cue.utc(span_max))
            now = datetime.now(timezone.utc)
            confidence = time_cue.confidence
            stages: list[dict] = []
            suspected: list[dict] = []
            if p.cue_interpreter.recent_only(time_cue, now, span):
                # §2.8: a cue that points only at today or later is not used. The rows
                # are exactly those of a recall without one; the response says so.
                own = p.envelope_planner.period(time_cue, "sure", now, span)
                cue_ignored = {"reason": "recent_only", "period": [w.isoformat() for w in own]}
                if trace_rec is not None:
                    trace_rec.set("cue_ignored", dict(cue_ignored))
            while cue_ignored is None:
                window = p.envelope_planner.period(time_cue, confidence, now, span)
                if window is not None:
                    cue_rows = await p.cue_candidates.search(
                        db, agent_id=agent_id, query=query, depth=depth, window=window,
                        channel=channel, project_id=project_id, source_id=source_id,
                        exclude_set=exclude_set, query_vec=query_vec_out, lexical_terms=lexical_terms,
                    )
                stage = {
                    "stage": len(stages),
                    "searched": "all arms and the cue period" if not stages else "the cue period only",
                    "confidence": confidence,
                    "period": [w.isoformat() for w in window] if window is not None else None,
                    "found": len(cue_rows),
                    "next": None,
                }
                stages.append(stage)
                if trace_rec is not None:
                    trace_rec.arm("cue" if len(stages) == 1 else f"cue_stage_{len(stages) - 1}", cue_rows, "_cosine")
                if cue_rows or len(stages) == 2:
                    break
                wider = p.envelope_planner.wider(confidence)
                suspected.append({"stage": stage["stage"], "code": "CANDIDATE_MISS",
                                  "reason": "the cue period holds no candidate"})
                if wider is None:
                    stage["next"] = "stop: no wider period (a vague cue widens to no period)"
                    break
                if (time.perf_counter() - started) * 1000 > config.RECALL_CUE_TIME_LIMIT_MS:
                    stage["next"] = "stop: time limit"
                    break
                stage["next"] = f"widen to the {wider} margin"
                confidence = wider
            if cue_ignored is None:
                cue_note = {"stages": stages, "suspected": suspected, "confidence": confidence}
                if trace_rec is not None:
                    trace_rec.set("stages", [dict(st) for st in stages])
                    trace_rec.set("suspected", suspected)
    min_score = _adaptive_min_score(memory_count)
    effective_min = min_score * 0.5 if deep else min_score
    # v2.4.26/27: use the calibrated gate for whichever branch is active.
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
    trace_rec = recall_trace.current()
    if trace_rec is not None:
        trace_rec.gate_summary(
            signal=gate_signal, calibrated=gate, heuristic_min=effective_min,
            origin="heuristic" if gate is None else "calibrated",
            pool=memory_count, gate_fallback=gate_fallback,
        )
        trace_rec.mark("gate")
    if AUTOCUT_ENABLED and not gate_fallback:
        before_autocut = results
        results = _autocut(results)
        if trace_rec is not None:
            trace_rec.autocut(before_autocut, results)

    # 2.6.0a7: the prior orders what the gate and autocut admitted; it never
    # admits or removes (docs/PRIOR_FUNCTION_DESIGN.md §3).
    admitted = list(results)
    results = p.prior.apply(results, prior_span, datetime.now(timezone.utc))
    providers.check_reorder("prior.apply", admitted, results)

    def _rid_of(r: dict) -> tuple:
        return r.get("_rid") or (("ep" if _is_episode_result(r) else "mem"), r.get("id"))

    cue_rank = {_rid_of(r): c for c, r in enumerate(cue_rows)}
    if trace_rec is not None:
        trace_rec.order(results, limit)
        trace_rec.mark("order")

    results = results[:limit]

    # The cue's bounded move (§2.3): after the gate, autocut, prior and the count
    # have decided which rows are returned and in what order, a row the cue arm
    # also found moves up by at most L places among them. The move comes after the
    # cut so that it cannot push a row out of the answer: the rows returned are
    # those of a recall without the cue, reordered, plus at most the one seat below.
    if cue_note is not None:
        bound = cue.LIFT[cue_note["confidence"]]
        lifted, moves = p.evidence_selector.lift(results, cue_rank, bound, _rid_of)
        providers.check_lift(results, lifted, bound)
        results = lifted
        for r in results:
            if _rid_of(r) in cue_rank:
                r["_cue_rank"] = cue_rank[_rid_of(r)]
        cue_note["lifted"] = moves

    # The reservation (§5). A fixed, small number of places are held for records
    # the block arm reached, filled in Hamming order, and the quality gate is not
    # consulted for them. They displace nothing: the gate's own rows keep every
    # place they had, so turning the feature on adds rows and removes none, and a
    # reservation that cannot be filled leaves the result shorter rather than
    # padding it. A record the gate already admitted is not reserved for -- it is
    # in the answer, which is the outcome the reservation exists to produce.
    if block_rows:
        present = {
            r.get("_rid") or (("ep" if _is_episode_result(r) else "mem"), r.get("id"))
            for r in results
        }
        reserved = [row for row in block_rows if row["_rid"] not in present]
        results.extend(reserved[: blocks.BLOCK_RESERVATION])
        if trace_rec is not None:
            trace_rec.reservation(reserved[: blocks.BLOCK_RESERVATION], "block")

    # The cue's held seat (§2.4): the best record only the cue arm found. The bounded
    # move cannot reach it because it is not in the admitted order; the seat adds it
    # and displaces nothing. A record an ordinary arm reached is not eligible, so a
    # row the gate refused does not come back this way.
    if cue_note is not None:
        present = {_rid_of(r) for r in results}
        eligible = [r for r in cue_rows if r["_rid"] not in reached and r["_rid"] not in present]
        seated = p.evidence_selector.seats(eligible, cue.SEATS)
        providers.check_seats(seated, eligible, cue.SEATS)
        for r in seated:
            r["_cue_seat"] = True
            r["_cue_rank"] = cue_rank[r["_rid"]]
        results.extend(seated)
        cue_note["seated"] = seated
        if trace_rec is not None:
            trace_rec.reservation(seated, "cue")
            trace_rec.cue(cue_note, cue.LIFT[cue_note["confidence"]])

    providers.check_recall_count(len(results), limit, cue.SEATS, blocks.BLOCK_RESERVATION)
    results.reverse()

    messages = []
    for r in results:
        content = r["content"]

        msg: dict = {"content": content}
        # A stable full-fetch handle. `id` below is the caller-supplied
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
        if r.get("_cue_seat"):
            # A held seat for the time cue (docs/RECALL_PROCESS_DESIGN.md §2.4). Checked
            # before the gate branches: a cue-arm row may carry a cosine, but no gate
            # read it, so it must not be reported as having passed one.
            msg["match_reason"] = {"signal": "cue", "admission": "reservation", "cue_rank": r["_cue_rank"]}
        elif gate_signal is not None:
            match_reason: dict = {"signal": gate_signal, "score": gate_score}
            if r.get("_cosine") is not None:
                match_reason["cosine"] = r["_cosine"]
            if r.get("_rrf_score") is not None:
                match_reason["rrf"] = r["_rrf_score"]
            if r.get("_rsf_score") is not None:
                match_reason["rsf"] = r["_rsf_score"]
            if r.get("_prior") is not None:
                # 2.6.0a7: the age weight that ordered this row, when one was applied.
                match_reason["prior"] = round(r["_prior"], 4)
            if r.get("_cue_rank") is not None:
                # The row's rank on the cue arm, which bounded how far it could move.
                match_reason["cue_rank"] = r["_cue_rank"]
            msg["match_reason"] = match_reason
        elif r.get("_block_distance") is not None:
            # A reserved row (docs/BLOCK_REACH_DESIGN.md §5) has no gate signal,
            # because no signal from the block arm is allowed to reach the gate.
            # It says why it is here in its own terms: `hamming` is a distance
            # where the branches above carry scores, so the two cannot be read
            # off against each other, and `admission` says the row occupied a
            # held place rather than passing anything. `order` says which order
            # filled the places (§4b): `vector` when the stored vectors re-ranked
            # the Hamming pass's best rows, `hamming` when some of them had no
            # vector yet. The re-rank's cosine is not shown, for the reason the
            # distance is shown instead of a score.
            msg["match_reason"] = {
                "signal": "block",
                "admission": "reservation",
                "order": r["_block_order"],
                "hamming": r["_block_distance"],
                "block": r["_block_index"],
            }
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
        r.pop("_prior", None)
        r.pop("_resolved", None)
        r.pop("_block_distance", None)
        r.pop("_block_index", None)
        r.pop("_block_order", None)
        r.pop("_cue_seat", None)
        r.pop("_cue_rank", None)
        messages.append(msg)

    # The excerpt a preview-cut row carries beside its prefix (cpersona/excerpts.py).
    # Only rows the preview will cut: a row the preview shows whole needs none, and
    # with the preview disabled nothing is cut.
    preview = config.RECALL_PREVIEW_CHARS
    if excerpt_chars > 0 and preview > 0 and query.strip():
        cut = [m for m in messages if m.get("ref") and len(m.get("content") or "") > preview]
        if cut:
            found = await excerpts.for_refs(
                agent_id, [m["ref"] for m in cut], query,
                query_vec_out[0] if query_vec_out else None, excerpt_chars,
            )
            for m in cut:
                if m["ref"] in found:
                    m["excerpt"] = found[m["ref"]]["excerpt"]
                    m["excerpt_basis"] = found[m["ref"]]["basis"]

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
    # Resolved once, here, and reused by the advisory below: this gate and that one
    # answer for the same caller, and two resolutions of one argument are two places
    # for the fallback rule to drift apart.
    session_key_resolved, session_key_declared = resolve_session_key(session_key)
    if (
        not deep
        and not gate_fallback
        and recall_counts
        and not session.is_paused_for(session_key_resolved)
    ):
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
                # scope_stats_neutral: the statement below touches neither the row
                # count nor a `timestamp` column — it increments recall_count and
                # writes last_recalled_at on rows that already exist — so it cannot
                # move the per-scope aggregates scope_stats caches. Without this the
                # bookkeeping write would invalidate, on every confidence-on recall,
                # the entry that same recall just filled: the cache would be dead in
                # exactly the configuration it exists for. The claim is about THESE
                # statements; adding an INSERT/DELETE, or a `timestamp` update, to
                # this body means dropping the keyword.
                async with transaction(scope_stats_neutral=True) as db:
                    await db.execute(
                        f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = datetime('now') WHERE id IN ({placeholders})",
                        returned_ids,
                    )
            except Exception as e:
                logger.warning("recall_count bump failed (non-fatal): %s", e)

    result: dict = {"messages": messages}
    # 2.6: say how deep the fusion looked, but only when that is not the count
    # the caller already knows. At the default floor the two are equal and the
    # key is absent, so every response recorded before the depth existed is
    # unchanged byte for byte; once a floor is set, the caller can see that the
    # ranking behind a 5-row answer considered more than 5 candidates per arm.
    if depth != limit:
        result["depth"] = depth
    # bug-183: present ONLY when the rescue fired. A `false` on every other recall would
    # change the payload of the whole surface (and every recorded golden) to say nothing.
    if gate_fallback:
        result["gate_fallback"] = True
    # The time cue (docs/RECALL_PROCESS_DESIGN.md §2), present only when one was
    # given: the period that was searched last, whether the loop widened it, how
    # many returned rows it moved, and whether it filled its seat.
    if cue_note is not None:
        last = cue_note["stages"][-1]
        result["time_cue"] = {
            "policy": cue.POLICY,
            "period": last["period"],
            "confidence": cue_note["confidence"],
            "revised": len(cue_note["stages"]) > 1,
            "moved": len(cue_note.get("lifted", [])),
            "seated": len(cue_note.get("seated", [])),
        }
    elif cue_ignored is not None:
        result["time_cue"] = {"policy": cue.POLICY, "ignored": cue_ignored["reason"], "period": cue_ignored["period"]}
    advisory = health.maybe_advisory(session_key_resolved, session_key_declared)
    if advisory is not None:
        result["advisory"] = advisory
    # Same contract as `advisory` above, and the same reason it rides on recall:
    # this is the one call every connected agent makes, so it is the only place a
    # notice reaches the operator without anyone going looking for it. Reads the
    # verdict the startup task left in memory — no fetch, no file, no latency —
    # and is absent unless there is something to say (a response key that is
    # present-and-empty on every other call changes the shape of the whole
    # surface to say nothing).
    update = update_check.notice(session_key_resolved, session_key_declared)
    if update is not None:
        result["update"] = update
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


#: The fields an ``external_context`` entry declares, each a string. ``role`` and
#: ``content`` were the only two the schema stated until 2.5.12; ``name``,
#: ``user_id`` and ``timestamp`` were read by the handler all along and named
#: nowhere a client could validate against (bug-292).
_CTX_DECLARED_STRINGS = ("role", "content", "name", "user_id", "timestamp")


def _ctx_string(entry: object, key: str, default: str = "") -> str:
    """Read one declared string field off an external_context entry.

    bug-291: a value that is present but not a string names nothing the field can
    mean, so it is read as absent and the field's own default applies. That is
    the answer ``_ctx_content`` already gives for a non-string ``content``, and
    the defaults here (``"User"`` for a name, ``""`` for a user_id or a stamp)
    are the same values an absent key produces — so the malformed case lands
    where the missing case already sat rather than somewhere new.

    Deliberately not ``str()`` coercion, which is what ``_ctx_role`` does. That
    coercion exists so the C13 disclosure can *name* the bad role; these three
    are consumed rather than reported — ``name`` and ``user_id`` become
    ``source.name`` and ``source.id``, so coercing invents an identity the caller
    never sent (``discord:{'a': 1}`` from a dict), and ``timestamp`` decides
    where a turn appears in a rendered conversation. The report in
    ``context_field_issues`` names the field instead; the value is not guessed.

    ``timestamp`` is the one that used to be fatal: ``_parse_timestamp_utc``
    calls ``.replace`` on what it is given and catches only ValueError/OSError,
    so an epoch int -- the shape a caller reaches for when nothing says the field
    is a string -- raised AttributeError out of the whole call, losing the recall
    hits and every well-formed entry beside it. Read as absent, it lands in the
    undated group, which is exactly where an entry with no ``timestamp`` key
    already sits (see ``_ts_sort_key``).
    """
    if not isinstance(entry, dict):
        return default
    val = entry.get(key, default)
    return val if isinstance(val, str) else default


def _ctx_bad_fields(entry: object) -> list[str]:
    """Declared fields present on this entry whose value is not a string.

    An entry that is not a dict at all is reported as a whole: every field is
    missing, and saying so once is more use than listing five names.
    """
    if not isinstance(entry, dict):
        return ["<entry>"]
    return sorted(
        key
        for key in _CTX_DECLARED_STRINGS
        if key in entry and not isinstance(entry[key], str)
    )


# The instant an undated message is placed at. Never compared against a real
# timestamp — the leading flag in the key separates the two groups first — so its
# value only has to be constant, which is what keeps the undated rows in the order
# the caller supplied them.
_UNDATED_ORDER_ANCHOR = datetime.min.replace(tzinfo=timezone.utc)


def _ts_sort_key(m: dict) -> tuple[bool, datetime]:
    """Order one merged message by the instant it names, not by how it is spelled.

    bug-287: this sort used to be ``m.get("timestamp", "")`` — the raw string. An
    ISO-8601 stamp's byte order equals its chronological order only while every
    stamp being compared carries the same UTC offset, and ``recall_with_context``
    is precisely where that stops holding: it merges rows the database wrote
    against entries a caller supplied, and the two need not agree on a spelling.
    ``2026-01-01T09:00:03+09:00`` and ``2026-01-01T00:00:03Z`` are the same
    instant, and the first sorted after every ``…T00:00:0NZ`` row because '9' > '0'
    in column 11. The caller reads this list as a conversation, so a stamp in
    another offset did not look wrong — it looked like a turn that happened later.

    Parsing lifts the comparison off the spelling. ``_parse_timestamp_utc`` also
    settles the naive case by the bug-114 invariant (a stamp SQLite wrote with
    ``datetime('now')`` is UTC), so a database row and a caller's entry become
    comparable rather than sorting into separate byte neighbourhoods.

    A stamp that does not parse names no instant, so it cannot be placed in the
    chronology; it can only be placed somewhere fixed. The flag puts every such
    message ahead of every dated one:

      * It keeps the ordinary case where it already was. An entry with no
        ``timestamp`` key reaches here as ``""`` and sorted first before this
        change, which is the case that actually occurs.
      * The end of the list is the one place an undated message must not land. The
        caller renders this as a conversation, so last reads as most recent — the
        position of maximum salience, handed to the rows whose time nobody knows.
        bug-207 refused the same bargain on the scoring axis for the same reason.
      * Under the old key the position depended on the first byte: ``"zzz"`` sorted
        last and ``"1999-ish"`` sorted into the middle, where an unreadable stamp
        would pose as a dated turn at a specific point in the conversation. One
        fixed bucket costs the (arbitrary) byte ordering among garbage and buys
        that no garbage can imitate a position.

    ``list.sort`` is stable, so messages sharing a key — the undated group, and any
    two rows naming the same instant in different spellings — stay in the order
    they were merged in: recall's ranked rows first, then the caller's entries as
    given.
    """
    parsed = _parse_timestamp_utc(m.get("timestamp", "") or "")
    return (parsed is not None, parsed if parsed is not None else _UNDATED_ORDER_ANCHOR)


async def do_recall_with_context(
    agent_id: str,
    query: str,
    external_context: list | None = None,
    limit: int = 10,
    channel: str = "",
    deep: bool = False,
    project_id: str | None = None,
    source_id: str = "",
    session_key: str = "",
    context_mode: str | None = None,
    excerpt_chars: int = 0,
) -> dict:
    """Recall memories and merge with external conversation context.

    project_id (v2.4.17): γ filter — passed through to do_recall.
    source_id (v2.4.20): per-user source prefix filter — passed through to do_recall.
    """
    ctx = external_context or []

    # bug-291: which entries do not match the shape the schema now declares. Counted
    # before the recall, so `reject` refuses without paying for a search whose
    # result it would discard.
    mode = config.EXTERNAL_CONTEXT_MODE if context_mode is None else context_mode
    field_issues = [
        {"index": i, "fields": bad} for i, e in enumerate(ctx) if (bad := _ctx_bad_fields(e))
    ]
    if field_issues and mode == "reject":
        # bug-232's shape: the caller's `messages` access must not become the
        # error path's second failure.
        return {
            "ok": False,
            "error": (
                "external_context entries have fields that are not strings: "
                + "; ".join(f"[{i['index']}] {', '.join(i['fields'])}" for i in field_issues)
                + ". Every declared field (role, content, name, user_id, timestamp) is a "
                "string; set CPERSONA_EXTERNAL_CONTEXT_MODE=warn to accept them again."
            ),
            "messages": [],
        }

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
        session_key=session_key,
        excerpt_chars=excerpt_chars,
    )
    messages = recall_result.get("messages", [])

    for entry in ctx:
        content = _ctx_content(entry)  # bug-035: null/type-safe, skips malformed entries
        if not content:
            continue
        role = _ctx_role(entry)

        if role == "assistant":
            source = {"type": "Agent", "id": "self"}
        elif role == "user":
            # bug-291: these three were `entry.get(...)` raw. A non-string reached
            # `source` verbatim, and a non-string stamp reached the sort, where
            # it raised.
            name = _ctx_string(entry, "name", "User")
            user_id = _ctx_string(entry, "user_id", "")
            uid = f"discord:{user_id}" if user_id else f"discord:{name}"
            source = {"type": "User", "id": uid, "name": name}
        else:
            continue

        messages.append(
            {
                "content": content,
                "source": source,
                "timestamp": _ctx_string(entry, "timestamp", ""),
                "context_type": "conversation",
            }
        )

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
    # bug-291: the declared shape is now stated in the schema, so an entry that does
    # not match it can be reported rather than absorbed. Same contract as the C13
    # disclosure above -- present only when it fired, so the common case pays no
    # payload -- and it names the entry index, because the caller built the list
    # and the index is what lets them find the row.
    if field_issues and mode == "warn":
        # bug-370: the note used to say "the entries were merged without them" for
        # every reported entry, which is true only when the unusable field is one
        # of the metadata fields. An entry whose CONTENT is not a string, or that
        # is not a mapping at all, is read as having no content -- so the merge
        # loop above skips it, the exclusion list skips it and the filter-only
        # disclosure skips it, and it reaches the caller as a report saying it was
        # merged when nothing of it survived. The two are different facts and a
        # caller acts differently on each: a dropped turn is one they must re-send.
        # Merging it instead would mean inventing content for it, so what changes
        # is the report, not the merge.
        dropped = [i for i in field_issues if not _ctx_content(ctx[i["index"]])]
        if not dropped:
            note = (
                "these fields were not strings and were read as absent; "
                "the entries were merged without them"
            )
        elif len(dropped) == len(field_issues):
            note = (
                "these entries carry no usable content and were dropped: they are "
                "absent from messages and did not filter the recall"
            )
        else:
            note = (
                "these fields were not strings and were read as absent; entries "
                "carrying no usable content were dropped (absent from messages and "
                "not used to filter the recall), the rest were merged without the "
                "named fields"
            )
        result["context_field_issues"] = {"entries": field_issues, "note": note}
        logger.warning(
            "recall_with_context: %d external_context entr%s carried a non-string "
            "declared field (%s). They were read as absent; set "
            "CPERSONA_EXTERNAL_CONTEXT_MODE=reject to refuse them instead.",
            len(field_issues),
            "y" if len(field_issues) == 1 else "ies",
            "; ".join(f"[{i['index']}] {', '.join(i['fields'])}" for i in field_issues),
        )
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
    # Forwarded for the same reason and with the same prohibition: update_check.notice()
    # is what CONSUMES this session's one delivery, so calling it again here would spend
    # it on a response the caller already has — and, on the keyless path, silence the
    # notice for every later recall in the process.
    update = recall_result.get("update")
    if update is not None:
        result["update"] = update
    return result


# get_contents batch size. A full row is worth ~800 tokens (the
# token-inventory measurement that motivated the preview tier), so 20 full rows
# already approaches a whole recall's pre-diet payload — a larger batch would
# reopen the context-explosion hole the preview exists to close.
GET_CONTENTS_MAX_REFS = 20

# The ref count alone stopped bounding this response. When the write
# cap was 2000 characters, 20 refs could not exceed ~40,000; raising the cap to
# 16,000 would have carried the same call to ~320,000 without a line of this
# file changing. A relaxation of the WRITE bound must not enlarge the READ blast
# radius, so the budget is stated here in characters and pinned at what the
# worst case used to be. It is deliberately not derived from MAX_CONTENT_LENGTH:
# raise that cap to any value and one response stays the size it is today.
GET_CONTENTS_MAX_CHARS = 40000


def _item_budget_cost(item: dict) -> int:
    """What one get_contents item costs against the character budget (bug-331).

    The content plus everything else the caller receives with it. `source` is the
    field that mattered: it carries its own, much higher cap, so a budget that
    counted content alone was not a bound on the response at all.
    """
    cost = len(item.get("content") or "")
    for key, value in item.items():
        if key == "content":
            continue
        cost += len(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return cost


# Range expansion (reconstruction v1.1). A ref may name part of its record
# instead of the whole row: {"ref": ..., "node": i | [first, last]} for overflow-tree
# nodes, or {"ref": ..., "span": [start, end]} for characters. Offsets are in the
# stored text -- a memory's content, an episode's summary -- which is what a
# reconstruct quote's node span is measured in, so a quote's span expands as given.
#
# A range the server cannot serve exactly is reported, never widened: returning
# the whole row for a node that does not exist would hand back the payload the
# caller asked to avoid, with nothing saying the request was not honoured.
RANGE_INVALID = "invalid_range"
RANGE_NO_CURRENT_NODES = "no_current_nodes"
RANGE_NODE_OUT_OF_RANGE = "node_out_of_range"
RANGE_SPAN_OUT_OF_RANGE = "span_out_of_range"
RANGE_NO_CURRENT_BLOCKS = "no_current_blocks"
RANGE_BLOCK_OUT_OF_RANGE = "block_out_of_range"
#: The record was rewritten since the offsets were handed out. Reported rather
#: than served: the same offsets in new text are a different passage, and a
#: caller quoting it would be quoting something nothing ever said.
RANGE_STALE_REVISION = "stale_revision"


def _is_index(value) -> bool:
    # bool is an int subclass; `True` is not a position.
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_ref_entry(entry) -> tuple[str, dict | None, str | None]:
    """`entry` -> (ref, range request or None, invalid-range reason or None).

    A string is a whole-row ref, as before. An object carries `ref` and at most one
    of `node` / `span`; an object with neither is a whole-row ref too. The range is
    only checked for shape here -- whether it fits the record needs the record.
    """
    if not isinstance(entry, dict):
        return str(entry), None, None
    ref = entry.get("ref")
    ref = ref if isinstance(ref, str) else str(ref)
    has_node, has_span, has_block = "node" in entry, "span" in entry, "block" in entry
    if sum((has_node, has_span, has_block)) > 1:
        return ref, None, RANGE_INVALID
    # The revision the offsets were measured in, when the caller was given one
    # (a reconstruct quote's expand attaches it). Checked against the record
    # before anything is served.
    revision = entry.get("revision")
    if revision is not None and not isinstance(revision, str):
        return ref, None, RANGE_INVALID
    if has_node or has_block:
        key = "node" if has_node else "block"
        value = entry[key]
        if _is_index(value):
            first = last = value
        elif isinstance(value, list) and len(value) == 2 and all(_is_index(v) for v in value):
            first, last = value
        else:
            return ref, None, RANGE_INVALID
        if first < 0 or last < first:
            return ref, None, RANGE_INVALID
        request = {key: (first, last)}
        if revision is not None:
            request["revision"] = revision
        return ref, request, None
    if has_span:
        span = entry["span"]
        if not (isinstance(span, list) and len(span) == 2 and all(_is_index(v) for v in span)):
            return ref, None, RANGE_INVALID
        start, end = span
        if start < 0 or end <= start:
            return ref, None, RANGE_INVALID
        request = {"span": (start, end)}
        if revision is not None:
            request["revision"] = revision
        return ref, request, None
    return ref, None, None


def _partitions(rows: list, text: str) -> bool:
    """Whether `(index, start, end)` rows cover `text` exactly, in order.

    One answer for nodes and for blocks: both are derived sets whose offsets are
    only usable when they partition the text as it is stored now, and two
    spellings of that test would eventually disagree about a set that is half
    there.
    """
    return (
        bool(rows)
        and [r[0] for r in rows] == list(range(len(rows)))
        and rows[0][1] == 0
        and rows[-1][2] == len(text)
        and all(a[2] == b[1] for a, b in zip(rows, rows[1:]))
    )


async def _resolve_range(db, kind: str, row_id: int, text: str, request: dict) -> tuple[dict | None, str | None]:
    """The characters a range request names in `text`: ({"span": [s, e], ...}, None) or (None, reason).

    A node range needs the record's node set to partition the text as it is stored
    now (tree invariant 4). The embedding model is not required to be current: the
    offsets depend on the text alone, and the triggers delete every node of a text
    that changed. A span's end past the text is clamped, and the span actually
    served is reported; a start at or past the end of the text serves nothing.
    """
    # A revision names the text the offsets were measured in. It is checked
    # before the range is resolved, so a rewritten record refuses rather than
    # serving different characters under the same numbers.
    revision = request.get("revision")
    if revision is not None and revision != blocks.text_revision(text):
        return None, RANGE_STALE_REVISION
    if "span" in request:
        start, end = request["span"]
        if start >= len(text):
            return None, RANGE_SPAN_OUT_OF_RANGE
        return {"span": [start, min(end, len(text))]}, None
    if "block" in request:
        rows = await db.execute_fetchall(
            "SELECT block_index, start_char, end_char FROM record_blocks "
            "WHERE parent_kind = ? AND parent_id = ? ORDER BY block_index",
            (kind, row_id),
        )
        if not _partitions(rows, text):
            return None, RANGE_NO_CURRENT_BLOCKS
        first, last = request["block"]
        if last >= len(rows):
            return None, RANGE_BLOCK_OUT_OF_RANGE
        return {"span": [rows[first][1], rows[last][2]], "block": [first, last], "of": len(rows)}, None
    rows = await db.execute_fetchall(
        "SELECT node_index, start_char, end_char FROM record_nodes "
        "WHERE parent_kind = ? AND parent_id = ? ORDER BY node_index",
        (kind, row_id),
    )
    if not _partitions(rows, text):
        return None, RANGE_NO_CURRENT_NODES
    first, last = request["node"]
    if last >= len(rows):
        return None, RANGE_NODE_OUT_OF_RANGE
    return {"span": [rows[first][1], rows[last][2]], "node": [first, last], "of": len(rows)}, None


async def do_get_contents(agent_id: str, refs: list) -> dict:
    """Resolve recall preview refs back to full, untrimmed rows (2.5.0).

    The recall tools' MCP boundary returns ``content`` as a preview
    (RECALL_PREVIEW_CHARS); every returned message carries a ``ref``
    (``mem:<id>`` / ``ep:<id>``) that this fetches in full. Reads are id-keyed
    (the ids came from an agent-scoped recall — the same provenance argument as
    the other id-keyed handlers) with the agent_id ownership predicate enforced,
    so a ref belonging to another agent lands in ``missing``, never in a leak.
    Malformed refs also land in ``missing`` (fail-soft: one bad ref must not
    abort the batch).

    A ref may be an object naming part of its record (reconstruction v1.1):
    ``{"ref", "node": i | [first, last]}`` or ``{"ref", "span": [start, end]}``.
    The item then carries that slice as ``content`` and a ``range`` object with
    the span served, the node range and node count when nodes were named, and
    the stored text's full length. A range that cannot be served exactly lands
    in ``unresolved`` with a reason, and is never widened to the whole row.
    """
    if not agent_id:
        return error_response("agent_id is required")
    if not isinstance(refs, list) or not refs:
        return error_response("refs must be a non-empty list of 'mem:<id>' / 'ep:<id>' refs or range objects")
    if len(refs) > GET_CONTENTS_MAX_REFS:
        return error_response(f"too many refs ({len(refs)}; max {GET_CONTENTS_MAX_REFS}) — split the fetch")

    items: list[dict] = []
    missing: list[str] = []
    unresolved: list[dict] = []
    deferred: list = []
    used = 0
    async with connection() as db:
        for position, entry in enumerate(refs):
            ref, request, invalid = _parse_ref_entry(entry)
            kind, _, raw = ref.partition(":")
            try:
                row_id = int(raw)
            except (TypeError, ValueError):
                row_id = -1
            if kind not in ("mem", "ep") or row_id <= 0:
                missing.append(ref)
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
                text = content
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
                text = summary
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
            # Checked after the ownership read, so a range on another agent's row
            # lands in `missing` like any other foreign ref and says nothing about
            # whether that row has nodes.
            if invalid is not None:
                unresolved.append({"ref": ref, "reason": invalid})
                continue
            if request is not None:
                served, reason = await _resolve_range(db, kind, row_id, text, request)
                if served is None:
                    unresolved.append({"ref": ref, "reason": reason})
                    continue
                start, end = served["span"]
                item["content"] = text[start:end]
                item["range"] = dict(served, content_len=len(text))
            # Whole rows only — the budget never cuts a content
            # string. get_contents is the ONLY path back to full text, so a
            # trimmed answer here would be indistinguishable from the preview it
            # was called to escape (the bug-117 failure mode: content with no
            # remaining handle). The first row is therefore admitted whatever
            # its size — one row must never become unreachable — and once the
            # budget is spent the REST of the batch is deferred rather than
            # partially served, so the caller re-fetches on a boundary it can
            # see instead of guessing which refs were dropped.
            # bug-331: charged for the whole item the caller receives, not for the
            # one field this loop happens to hold. `source` is capped separately
            # and much higher, so counting content alone let a batch of rows with
            # large source objects return about four times the budget with neither
            # a deferred list nor a budget field -- the coupling this constant
            # exists to break. Serialised because that is the size the caller
            # pays for, and the cheapest measure that does not depend on how the
            # transport spells the object.
            cost = _item_budget_cost(item)
            if items and used + cost > GET_CONTENTS_MAX_CHARS:
                # Deferred entries are echoed as they were sent, range objects
                # included, so the re-fetch is the same request.
                deferred = [r if isinstance(r, dict) else str(r) for r in refs[position:]]
                break
            used += cost
            items.append(item)
    result: dict = {"items": items, "missing": missing, "count": len(items)}
    if unresolved:
        # Absent unless a range was refused, so a caller that sends only string
        # refs sees the response shape it always has.
        result["unresolved"] = unresolved
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

    bug-215: punctuation is NOT stripped. The trigram tokenizer indexes it as an
    ordinary character, so 'CVE-2024-3094' and 'bug-183' only match with their
    hyphens intact — mangling them to 'CVE20243094' made the exact-match row
    invisible to the keyword channel, and _search_memories_keyword's LIKE
    fallback cannot save it (it only runs when FTS returned ZERO rows, so any
    other matching term hides the loss). The only character that needs
    neutralising is the FTS5 phrase quote, which is escaped by doubling.
    """
    terms: list[str] = []
    for tok in _TOKEN_RE.findall(query):
        if _CJK_RE.match(tok):
            if len(tok) >= 3:
                terms.extend(tok[i : i + 3] for i in range(len(tok) - 2))
            # shorter CJK runs (e.g. 'パン') can't match a trigram index -> LIKE
        elif len(tok) >= 3:
            terms.append(tok)
        # ASCII tokens < 3 chars also can't match a trigram index -> dropped
    if not terms:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in dict.fromkeys(terms))


def _build_fts_recall_query(query: str, extra_terms: list[str] | None = None) -> str:
    """Experimental edges policy; the literal FTS compiler stays unchanged.

    ``extra_terms`` (2.6, associative memory stage 1) are OR-ed in as whole
    phrases: a declared alias names one thing, so ``Miz Eye`` must not match a
    row that merely contains ``Eye``. A phrase shorter than a trigram cannot
    match the index and is left to the LIKE fallback, as short query terms are.
    Without extra terms the expression is exactly the one it always was.
    """
    normalized = " ".join(token.strip("\"'`.,;:!?()[]{}") for token in query.split())
    expression = _build_fts_query(normalized)
    phrases = ['"' + t.replace('"', '""') + '"' for t in dict.fromkeys(extra_terms or ()) if len(t) >= 3]
    if not phrases:
        return expression
    return " OR ".join([expression, *phrases] if expression else phrases)


# An episode's time as ``episode_timestamp`` reads it, compared as SQLite datetimes.
_EPISODE_IN_WINDOW = (
    " AND datetime(COALESCE(NULLIF(e.start_time, ''), e.created_at)) >= datetime(?)"
    " AND datetime(COALESCE(NULLIF(e.start_time, ''), e.created_at)) < datetime(?)"
)


async def _search_episodes_fts(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    channel: str = "",
    project_id: str | None = None,
    extra_terms: list[str] | None = None,
    window: tuple[str, str] | None = None,
) -> list[dict]:
    """Search episodes using FTS5.

    window (2.6, the cue arm) keeps episodes whose time -- ``start_time``, else
    ``created_at``, the rule ``episode_timestamp`` applies -- is in ``[start, end)``,
    both bounds as SQLite ``datetime()`` reads them.

    project_id (v2.4.17) applies the γ filter. channel (v2.4.22) applies an
    exact-match filter on the episode's channel — empty means no channel
    filter (all channels), mirroring the memory search paths.
    """
    fts_query = _build_fts_recall_query(query, extra_terms)
    if not fts_query:
        return []
    # isolation_where composes all three axes: exact agent, γ project,
    # and the knob2 v2 channel contract — an episode stored under channel '' is
    # global and surfaces in every channel-scoped recall, so old (pre-per-channel)
    # episodes are never orphaned once recall starts filtering by concrete channel.
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")

    rows = await db.execute_fetchall(
        f"""SELECT e.id, e.summary, e.start_time, e.resolved, bm25(episodes_fts), e.created_at
           FROM episodes_fts f
           JOIN episodes e ON f.rowid = e.id
           WHERE episodes_fts MATCH ?
           AND {iso.clause}{_EPISODE_IN_WINDOW if window is not None else ""}
           ORDER BY rank
           LIMIT ?""",
        (fts_query, *iso.params, *(window or ()), limit),
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
    extra_terms: list[str] | None = None,
    window: tuple[str, str] | None = None,
) -> list[dict]:
    """Search memories using FTS5 (preferred) or LIKE fallback.

    project_id (v2.4.17) applies the γ filter on both the bare and joined paths.
    source_id (v2.4.20) applies a prefix filter against ``json_extract(source, '$.id')``.
    extra_terms (2.6) are matched as well as the query, by FTS phrase and by the
    LIKE fallback; see ``_build_fts_recall_query``.
    window (2.6, the cue arm) keeps rows whose timestamp is in ``[start, end)``,
    both bounds as SQLite ``datetime()`` reads them; see ``cpersona/cue.py``.
    """
    # isolation_where composes all three axes: exact agent, γ project,
    # and the knob2 v2 channel contract (stored channel '' matches every
    # channel-scoped recall). The alias="m" variant serves the FTS-join path —
    # this replaces the old .replace("channel", "m.channel") rewrite hack.
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    iso_m = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="m")

    src_bare = source_id_where(source_id)
    src_m = source_id_where(source_id, alias="m")
    src_clause_bare = src_bare.and_clause
    src_params_bare = src_bare.params
    src_clause_m = src_m.and_clause
    src_params_m = src_m.params
    if window is not None:
        # Compared as instants, not as text: stored timestamps mix spellings (bug-394).
        src_clause_bare += " AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)"
        src_clause_m += " AND datetime(m.timestamp) >= datetime(?) AND datetime(m.timestamp) < datetime(?)"
        src_params_bare = (*src_params_bare, *window)
        src_params_m = (*src_params_m, *window)

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
        fts_query = _build_fts_recall_query(query, extra_terms)
        if fts_query:
            try:
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
            except sqlite3.OperationalError as e:
                # bug-326: FTS_ENABLED says the build supports FTS5, not that this
                # database still holds the index table — dropping it leaves the
                # triggers behind, so the flag stays true and the join raises. The
                # LIKE path below is the documented fallback for exactly "the
                # keyword channel cannot use FTS here"; raising instead took the
                # whole recall down with it. Repair is check_fts_integrity's job.
                logger.warning("Keyword FTS search unavailable, falling back to LIKE: %s", e)
                rows = []
            if rows:
                return [{"id": r[0], "msg_id": r[1], "content": r[2], "source": r[3], "timestamp": r[4], "_bm25": r[5]} for r in rows]

    # Note (bug-085 analysis): unlike vector._search_vector, this LIMIT is NOT a
    # recency scan window — the LIKE predicate filters BEFORE the LIMIT applies,
    # so it caps how many *matching* rows are fetched (always >= limit; the
    # return slices to limit). Old rows stay reachable; no decoupling needed.
    scan_limit = min(MAX_MEMORIES, max(limit * 5, 50))
    patterns = [_like_escape_contains(query)]
    patterns += [_like_escape_contains(t) for t in dict.fromkeys(extra_terms or ()) if t.strip() and t != query]
    like_clause = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in patterns)
    if len(patterns) > 1:
        like_clause = f"({like_clause})"
    rows = await db.execute_fetchall(
        f"""SELECT id, msg_id, content, source, timestamp
           FROM memories
           WHERE {iso.clause}{src_clause_bare}
           AND {like_clause}
           ORDER BY created_at DESC
           LIMIT ?""",
        (*iso.params, *src_params_bare, *patterns, scan_limit),
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
    session_key: str = "",
) -> dict:
    """Archive a conversation episode with pre-computed summary, keywords, and resolved status.

    project_id (v2.4.17): isolation axis. Defaults to '' (= global pool).
    channel (v2.4.22): conversation-channel tag for the episodic loop. Defaults
    to '' (= unscoped / shared). A channel-scoped recall returns episodes whose
    channel matches; unfiltered recall returns all of them.
    """
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "episode_id": None, "id": 0}, "archive_episode", key
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
    # Overflow tree (§3). Measured on row[2] for the same reason as the index push:
    # the stored summary is the text the nodes will span.
    if await nodes.runs_past_window(row[2]):
        queued = await nodes.queue_build("ep", episode_id, agent_id, key)
        if queued:
            result["nodes"] = queued
    # Blocks (BLOCK_REACH_DESIGN.md §6), on the same text and for the same reason
    # do_store queues them: an episode divides into clauses whether or not it runs
    # past the window, and a write path that skipped them would leave every
    # episode to the backfill sweep.
    if blocks.building_enabled():
        queued = await blocks.queue_build("ep", episode_id, agent_id, key)
        if queued:
            result["blocks"] = queued
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

    # bug-299: the sibling of bug-291, on the write side rather than the read one.
    # A history entry carries the same declared string fields an external_context
    # entry does, and a non-string `timestamp` reached min()/max() unnormalised --
    # so a list mixing an epoch int (or a dict) with an ISO string raised TypeError
    # out of the whole call, BEFORE the embed and the INSERT. The episode was lost,
    # not merely stamped poorly. `_ctx_string` reads such a value as absent, which
    # is the ruling bug-291 already made for the read side: a stamp that is not a
    # string names no instant, and coercing one invents a position in the
    # chronology. Homogeneous epochs used to sort among themselves and be stored,
    # but what they stored was an int in a column every other row fills with an ISO
    # string -- a span that cannot be compared with its neighbours (the shape
    # bug-286 is about). Absent is the honest answer for both.
    timestamps = [stamp for msg in history if (stamp := _ctx_string(msg, "timestamp"))]
    # bug-369: the two ends are chosen by parsed instant, and the ORIGINAL strings
    # are what gets stored. min()/max() over the raw strings is byte order, and
    # byte order equals chronological order only while every stamp carries the
    # same offset -- a history whose first entry is an hour earlier in a different
    # offset stored a start after its own end. Unlike the read-side ordering
    # defects, this value is written into the row and never recomputed: it is what
    # the confidence scoring and the content expansion read afterwards, so the
    # episode is ranked and displayed at the wrong instant for the rest of its life.
    # A stamp nobody can parse names no instant, so it takes no part in choosing
    # the ends -- the same ruling _ctx_string already makes for a non-string one --
    # and if none of them parse the span falls back to byte order rather than
    # becoming absent, because two unparseable ends still bound the same history.
    parseable = [(instant, stamp) for stamp in timestamps if (instant := _parse_timestamp_utc(stamp))]
    if parseable:
        start_time = min(parseable, key=lambda pair: pair[0])[1]
        end_time = max(parseable, key=lambda pair: pair[0])[1]
    else:
        start_time = min(timestamps) if timestamps else None
        end_time = max(timestamps) if timestamps else None

    embedding_blob = None
    if vector._embedding_client and summary:
        try:
            embeddings = await vector._embedding_client.embed([summary])
            if embeddings:
                embedding_blob = vector.pack_for_storage(embeddings[0])
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
