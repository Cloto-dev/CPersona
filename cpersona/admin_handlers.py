"""Administrative tool handlers for CPersona.

Tools: profile (get/update), list, delete, update, lock/unlock, agent data wipe,
threshold calibration, episode delete, export/import, merge, queue status.

Accesses `vector._embedding_client` (remote vector index sync) and
`tasks._task_queue` (queue status) as module attributes.
"""

import base64
import contextlib
import glob
import json
import logging
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from cpersona._vendored_mcp_common import no_persist
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.isolation import isolation_where

from cpersona import config
from cpersona import tasks
from cpersona import vector
from cpersona.config import (
    CALIBRATE_FLOOR,
    CALIBRATE_MAX_SAMPLE,
    CALIBRATE_METHOD,
    CALIBRATE_METHODS,
    CALIBRATE_PERCENTILE,
    CALIBRATE_SAMPLE_SIZE,
    CALIBRATE_TEMPORAL_WINDOW_MIN,
    CALIBRATE_Z_FACTOR,
    STORE_BLOB,
    local_blobs_stored,
    TASK_QUEUE_ENABLED,
    VECTOR_SEARCH_MODE,
)
from cpersona.database import connection, read_snapshot, transaction
from cpersona.utils import (
    SCORING_VERSION,
    _clamp_limit,
    _try_parse_json,
    error_response,
    sanitize_content_with_flag,
    sanitize_profile_with_flag,
)

logger = logging.getLogger(__name__)


def _warn_if_unscoped(operation: str, row_id: int, agent_id: str) -> None:
    # bug-137: direct callers may omit the kernel-injected ownership scope.
    if not agent_id:
        logger.warning(
            "%s is running UNSCOPED (no ownership enforcement) for row id %s",
            operation,
            row_id,
        )


async def do_get_profile(agent_id: str) -> dict:
    """Get the current profile for an agent."""
    async with connection() as db:
        rows = await db.execute_fetchall(
            "SELECT content FROM profiles WHERE agent_id = ? AND user_id = '' LIMIT 1",
            (agent_id,),
        )
    return {"profile": rows[0][0] if rows else ""}


async def do_update_profile(agent_id: str, profile: str = "") -> dict:
    """Update agent profile with pre-computed content.

    bug-188: the profile write used to be the one text path with no bounding at
    all — no sanitisation, no cap, and a raw truthiness test for "empty". Two
    consequences, both silent. A whitespace-only profile (``"   "``) is truthy,
    so it overwrote a useful profile with blanks and reported
    ``profiles_updated: 1``: destruction reported as success. And an arbitrarily
    large profile was stored verbatim, then injected into every recall response
    through the id=-1 sentinel row, which bypasses the scoring gate and the
    preview trimming that bound every other row.

    Both now go through the same seam ``store`` uses. Text that sanitises to
    empty is refused as the no-op it should always have been (``skipped``, with
    a reason, rather than a destructive write), and oversized text is truncated
    with the flag the caller needs to know it happened. Note this bounds the
    write path only — a profile stored oversized by an earlier version stays
    that size until it is rewritten.

    an earlier decision: the seam is shared, the ceiling is not. This path caps at
    MAX_PROFILE_LENGTH, which is the row's only bound because the recall
    injection never preview-trims it.
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "profiles_updated": 0}, "update_profile")

    if not profile:
        return {"ok": True, "profiles_updated": 0, "reason": "empty profile"}

    profile, truncated = sanitize_profile_with_flag(profile)
    if not profile:
        return {"ok": True, "profiles_updated": 0, "reason": "empty after sanitization"}

    # bug-042/043: transaction() serialises the write+commit behind the shared lock
    # so this commit cannot flush a concurrent import/merge's partial transaction.
    async with transaction() as db:
        await db.execute(
            """INSERT INTO profiles (agent_id, user_id, content, updated_at)
               VALUES (?, '', ?, datetime('now'))
               ON CONFLICT(agent_id, user_id) DO UPDATE SET
                   content = excluded.content,
                   updated_at = excluded.updated_at""",
            (agent_id, profile),
        )
    result = {"ok": True, "profiles_updated": 1}
    if truncated:
        result["truncated"] = True
    return result


# bug-255: the two list tools were bounded by a row count and nothing else, so
# they inherited the write cap as their real ceiling. At a 2000-character cap the
# row caps did bound them (500 rows for memories; 200 rows x 2 text columns for
# episodes); that cap was later raised to 16000 and both worst cases grew 8x — to
# 8M and 6.4M characters — without a line of this file changing, in the same
# release that budgeted get_contents. Same doctrine as GET_CONTENTS_MAX_CHARS and
# server.RECALL_FULL_CONTENT_MAX_CHARS: the budget is stated here in characters
# and pinned at what the worst case USED to be, deliberately NOT derived from
# MAX_CONTENT_LENGTH, so the next relaxation of the write bound cannot enlarge
# this read again.
LIST_MEMORIES_MAX_CHARS = 1_000_000  # 500 rows x the old 2000-character cap
LIST_EPISODES_MAX_CHARS = 800_000  # 200 rows x 2 text columns x the same cap


def _apply_list_budget(items: list[dict], fields: tuple[str, ...], budget: int, kind: str) -> bool:
    """Bound a list response in characters; returns whether any row was DEGRADED (bug-255).

    The return drives ``budget_chars`` on the response, so it reports rows
    actually trimmed — not merely that the running total crossed the budget. A
    listing can cross the budget and still degrade nothing (every later row
    already fits under the preview cap); advertising ``budget_chars`` there
    would send a caller hunting for ``ref`` markers that do not exist.

    The budget is honest about what it bounds: whole rows are never cut, the
    first row is always admitted, and a degraded row still carries a preview-cap
    prefix — so the effective ceiling is the budget plus one whole row plus up
    to rows x preview-cap of prefixes, and an operator who raises
    CPERSONA_RECALL_PREVIEW_CHARS raises that ceiling with it (at or above
    MAX_CONTENT_LENGTH nothing is longer than the cap, and the budget is
    effectively off — the same trade _apply_full_content_budget makes).

    Rows past the budget are not dropped — a listing that silently returns fewer
    rows than it was asked for is indistinguishable from an empty corpus — they
    DEGRADE to the recall preview tier: each budgeted field becomes a pure prefix
    of config.RECALL_PREVIEW_CHARS with ``<field>_len`` / ``<field>_truncated``
    markers and a ``ref`` (``mem:<id>`` / ``ep:<id>``) that get_contents resolves
    in full. Pure prefix, no ellipsis, for the same reason _apply_preview uses
    one: a prefix still starts-with-matches the stored text.

    Spent front-to-back, because these rows arrive newest-first and the valuable
    end of a listing is the newest one. (bug-214 spends recall's budget from the
    tail for the same reason: there the ranked list is reversed before it is
    emitted, so its valuable end is the last row. The rule is "spend from the
    valuable end", not "spend from the front".)

    Whole rows only: the budget never cuts a row that fits, and the FIRST row is
    admitted whatever its size, so one oversized memory cannot make the whole
    listing a wall of prefixes. Markers are additive and only appear on the rows
    the budget actually reached.

    CPERSONA_RECALL_PREVIEW_CHARS=0 disables the preview tier wholesale; the
    operator opted out of trimming, so it disables this budget too — the same
    stance server._apply_full_content_budget takes, and the reason degradation
    is never allowed to become deletion.
    """
    cap = config.RECALL_PREVIEW_CHARS
    if cap <= 0:
        return False

    def charge(item: dict) -> int:
        return sum(len(item[f]) for f in fields if isinstance(item.get(f), str))

    used = 0
    over_budget = False
    any_degraded = False
    for item in items:
        if not over_budget:
            if used and used + charge(item) > budget:
                over_budget = True
            else:
                used += charge(item)
                continue
        trimmed = False
        # `field` is dataclasses.field at module scope (F402) — name it `column`.
        for column in fields:
            value = item.get(column)
            if isinstance(value, str) and len(value) > cap:
                item[f"{column}_len"] = len(value)
                item[column] = value[:cap]
                item[f"{column}_truncated"] = True
                trimmed = True
        if trimmed:
            any_degraded = True
            # The handle back to the full row. Without it a truncated listing row
            # would be the bug-117 failure mode: content with no way to expand it.
            # It resolves through get_contents under the row's OWN agent_id: in an
            # all-agents listing (agent_id=""), a ref expanded under a different
            # agent comes back missing, so callers must pair it with the row's
            # agent_id field.
            item["ref"] = f"{kind}:{item['id']}"
        used += charge(item)
    return any_degraded


async def do_list_memories(agent_id: str, limit: int, project_id: str | None = None) -> dict:
    """List recent memories for dashboard display.

    project_id (v2.4.17): γ filter — None = no filter, '' = global pool only,
    'X' = bucket 'X' ∪ global pool.

    bug-255: bounded at LIST_MEMORIES_MAX_CHARS of content; see
    _apply_list_budget for what a row past the budget looks like.
    """
    # Empty agent_id = all agents (the tool schema documents it) — hence `or None`.
    iso = isolation_where(agent_id=agent_id or None, project_id=project_id)
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"SELECT id, agent_id, project_id, msg_id, content, source, timestamp, created_at, locked, channel "
            f"FROM memories{iso.where} ORDER BY created_at DESC LIMIT ?",
            (*iso.params, _clamp_limit(limit, 500)),
        )
    memories = []
    for row in rows:
        source = {}
        try:
            source = json.loads(row[5]) if row[5] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        memories.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "project_id": row[2],
                "content": row[4],
                "source": source,
                "timestamp": row[6],
                "created_at": row[7],
                "locked": bool(row[8]),
                # channel (knob2 v2): lets the kernel group unarchived memories
                # per channel for per-channel episode archival.
                "channel": row[9],
            }
        )
    over_budget = _apply_list_budget(memories, ("content",), LIST_MEMORIES_MAX_CHARS, "mem")
    result = {"memories": memories, "count": len(memories)}
    if over_budget:
        # Absent unless the budget actually bit, so a caller that never meets it
        # sees the response shape it always saw (the get_contents convention).
        result["budget_chars"] = LIST_MEMORIES_MAX_CHARS
    return result


async def do_list_episodes(agent_id: str, limit: int, project_id: str | None = None) -> dict:
    """List archived episodes for dashboard display. Same γ semantics as do_list_memories.

    bug-255: bounded at LIST_EPISODES_MAX_CHARS across BOTH text columns —
    `keywords` goes through the same write cap as `summary` (do_archive_episode
    sanitises it), so budgeting only the summary would leave half the response
    unbounded.

    One honest asymmetry: the `ref` on a degraded row expands the SUMMARY
    (get_contents returns '[Episode] <summary>'), and no tool returns an
    episode's keywords in full except export_data. Trimming it anyway is the
    lesser evil — the alternative is a response with no upper bound at all —
    but a caller that needs the whole keyword string must export.
    """
    # Empty agent_id = all agents (the tool schema documents it) — hence `or None`.
    iso = isolation_where(agent_id=agent_id or None, project_id=project_id)
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"SELECT id, agent_id, project_id, summary, keywords, start_time, end_time, created_at "
            f"FROM episodes{iso.where} ORDER BY created_at DESC LIMIT ?",
            (*iso.params, _clamp_limit(limit, 200)),
        )
    episodes = []
    for row in rows:
        episodes.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "project_id": row[2],
                "summary": row[3],
                "keywords": row[4],
                "start_time": row[5],
                "end_time": row[6],
                "created_at": row[7],
            }
        )
    over_budget = _apply_list_budget(
        episodes, ("summary", "keywords"), LIST_EPISODES_MAX_CHARS, "ep"
    )
    result = {"episodes": episodes, "count": len(episodes)}
    if over_budget:
        result["budget_chars"] = LIST_EPISODES_MAX_CHARS
    return result


async def do_delete_memory(memory_id: int, agent_id: str = "") -> dict:
    """Delete a single memory by ID.

    When agent_id is provided (non-empty), enforces ownership.
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "deleted_id": memory_id}, "delete_memory")
    _warn_if_unscoped("do_delete_memory", memory_id, agent_id)
    # aiosqlite 0.22 has execute_fetchall but no execute_fetchone — using the
    # former avoids a silent AttributeError that previously broke every delete.
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT locked, agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return error_response(f"Memory {memory_id} not found")
    if rows[0][0]:
        return error_response(f"Memory {memory_id} is locked and cannot be deleted")
    owner_agent_id = rows[0][1]

    # bug-024: fold `AND locked = 0` into the DML so a lock_memory that commits
    # locked=1 between the SELECT above and this DELETE (a concurrent call over the
    # single shared connection) can no longer be defeated — the atomicity the
    # bug-007 dedup DELETE fix established, extended to the admin path.
    # bug-042/043: transaction() serialises the DELETE+commit behind the shared lock.
    async with transaction() as db:
        if agent_id:
            cursor = await db.execute(
                "DELETE FROM memories WHERE id = ? AND agent_id = ? AND locked = 0",
                (memory_id, agent_id),
            )
        else:
            cursor = await db.execute("DELETE FROM memories WHERE id = ? AND locked = 0", (memory_id,))
    if cursor.rowcount == 0:
        return error_response(f"Memory {memory_id} not found, not owned by agent, or locked")

    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        # bug-023: the row was indexed under its OWNER's namespace (cpersona:{owner}).
        # Deleting by id without passing agent_id used to compute "cpersona:" and
        # leave the remote vector entry orphaned. Use the owner we just read.
        ns = f"cpersona:{owner_agent_id}"
        try:
            base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
            await vector._embedding_client._client.post(
                f"{base_url}/remove",
                json={"namespace": ns, "ids": [f"mem:{memory_id}"]},
            )
        except Exception as e:
            logger.debug("Remote remove failed (non-fatal): %s", e)

    return {"ok": True, "deleted_id": memory_id}


async def do_update_memory(memory_id: int, content: str, agent_id: str = "") -> dict:
    """Update memory content by ID. Rejects if memory is locked."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "updated_id": memory_id}, "update_memory")
    if not content or not content.strip():
        return error_response("Content cannot be empty")
    _warn_if_unscoped("do_update_memory", memory_id, agent_id)

    async with connection() as db:
        rows = await db.execute_fetchall("SELECT locked, agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return error_response(f"Memory {memory_id} not found")
    row = rows[0]
    if row[0]:
        return error_response(f"Memory {memory_id} is locked and cannot be edited")
    if agent_id and row[1] != agent_id:
        return error_response(f"Memory {memory_id} not owned by agent {agent_id}")

    # 2.5.2a2 audit (C12): the edit path enforces the SAME content policy as the write
    # path, via the same helper. Before, this was a bare `.strip()`, so an update could
    # grow a row past MAX_CONTENT_LENGTH — a cap do_store applies on every insert — and
    # the uncapped string was then handed to embed() and pushed verbatim to the remote
    # /index as well, so the two write seams disagreed on what a stored row may contain.
    raw_content = content
    # bug-175: same seam, same flag — the raw length counted the annotation this
    # helper strips, so an update could report truncated:true without a cut.
    content, truncated = sanitize_content_with_flag(raw_content)
    if not content:
        # _sanitize_content also strips [Memory from ...] annotations, so a body made of
        # nothing else is empty at the seam. Reject as the empty-input guard above does
        # rather than writing an empty row (do_store's "empty after sanitization").
        return error_response("Content cannot be empty after sanitization")

    # bug-011: recompute the embedding for the new text with the same policy as
    # do_store. On failure (or no client) the BLOB is NULLed rather than left
    # stale, so recall stops matching the old wording and check_health's
    # null-embedding repair can re-embed the row later.
    embedding_blob = None
    if vector._embedding_client and local_blobs_stored(VECTOR_SEARCH_MODE, STORE_BLOB):
        try:
            embeddings = await vector._embedding_client.embed([content])
            if embeddings and embeddings[0]:
                embedding_blob = EmbeddingClient.pack_embedding(embeddings[0])
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, TypeError) as e:
            logger.warning("Embedding failed during update_memory: %s", e)

    # The memories_fts index is kept in sync by the AFTER UPDATE trigger
    # (bug-008); the previous manual UPDATE of the external-content FTS table ran
    # after the base row was already rewritten and left stale trigrams behind.
    # bug-024: `AND locked = 0` + rowcount guard closes the same read-then-write
    # window as the delete path — a lock_memory landing after the SELECT above
    # must not be overwritten, and a raced no-op must not report success or push
    # the new text to the remote index.
    # bug-042/043: transaction() serialises the UPDATE+commit behind the shared lock.
    async with transaction() as db:
        cursor = await db.execute(
            "UPDATE memories SET content = ?, embedding = ? WHERE id = ? AND locked = 0",
            (content, embedding_blob, memory_id),
        )
    if cursor.rowcount == 0:
        return error_response(f"Memory {memory_id} is locked and cannot be edited")

    # Keep the remote vector entry in step with the new text (same
    # namespace/id scheme as do_store; non-fatal).
    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        try:
            base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
            await vector._embedding_client._client.post(
                f"{base_url}/index",
                json={
                    "namespace": f"cpersona:{row[1]}",
                    "items": [{"id": f"mem:{memory_id}", "text": content}],
                },
            )
        except Exception as e:
            logger.debug("Remote index update failed (non-fatal): %s", e)

    # `truncated` is additive and mirrors do_store's success shape (absent unless the
    # cap actually bit), so a caller that edits a too-long body learns the row it now
    # holds is a prefix instead of having to re-read it to find out.
    result = {"ok": True, "updated_id": memory_id}
    if truncated:
        result["truncated"] = True
    return result


async def do_lock_memory(memory_id: int, agent_id: str = "") -> dict:
    """Lock a memory to prevent deletion and editing."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "locked_id": memory_id}, "lock_memory")
    _warn_if_unscoped("do_lock_memory", memory_id, agent_id)
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return error_response(f"Memory {memory_id} not found")
    if agent_id and rows[0][0] != agent_id:
        return error_response(f"Memory {memory_id} not owned by agent {agent_id}")

    async with transaction() as db:  # bug-042/043: serialise write+commit
        cur = await db.execute("UPDATE memories SET locked = 1 WHERE id = ?", (memory_id,))
    if cur.rowcount == 0:
        # bug-099: the ownership pre-check and this UPDATE straddle an await — a
        # concurrent delete in between must not be acknowledged as a lock.
        return error_response(f"Memory {memory_id} not found")
    return {"ok": True, "locked_id": memory_id}


async def do_unlock_memory(memory_id: int, agent_id: str = "") -> dict:
    """Unlock a memory to allow deletion and editing."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "unlocked_id": memory_id}, "unlock_memory")
    _warn_if_unscoped("do_unlock_memory", memory_id, agent_id)
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return error_response(f"Memory {memory_id} not found")
    if agent_id and rows[0][0] != agent_id:
        return error_response(f"Memory {memory_id} not owned by agent {agent_id}")

    async with transaction() as db:  # bug-042/043: serialise write+commit
        cur = await db.execute("UPDATE memories SET locked = 0 WHERE id = ?", (memory_id,))
    if cur.rowcount == 0:
        # bug-099: see do_lock_memory.
        return error_response(f"Memory {memory_id} not found")
    return {"ok": True, "unlocked_id": memory_id}


async def _delete_agent_rows(db, agent_id: str) -> dict:
    """Leaf: delete the agent's rows inside the CALLER's open transaction.

    Takes the caller's transaction() connection so a composite operation
    (merge mode='move', bug-088) can make the wipe part of its own atomic
    unit instead of a second transaction with a loss window in between.
    Also clears the agent's crash-recovery queue rows (bug-093): a wiped
    agent must not have the drain resurrect its data later.
    """
    mem_cursor = await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent_id,))
    prof_cursor = await db.execute("DELETE FROM profiles WHERE agent_id = ?", (agent_id,))
    ep_cursor = await db.execute("DELETE FROM episodes WHERE agent_id = ?", (agent_id,))
    task_cursor = await db.execute(
        "DELETE FROM pending_memory_tasks WHERE agent_id = ?", (agent_id,)
    )
    return {
        "deleted_memories": mem_cursor.rowcount,
        "deleted_profiles": prof_cursor.rowcount,
        "deleted_episodes": ep_cursor.rowcount,
        "deleted_pending_tasks": task_cursor.rowcount,
    }


def _purge_agent_calibration(agent_id: str) -> bool:
    """Drop the agent's calibration from the in-process dicts + sidecar (bug-036).

    Otherwise a stale threshold/beta/gate computed from the now-deleted corpus
    survives in-process and reloads on restart via _restore_calibration_state,
    so a later same-id agent silently inherits it and under-/over-recalls until
    it recalibrates. Non-DB side effect — call AFTER the delete transaction
    commits, never inside it."""
    removed_cal = False
    for _d in (vector._agent_thresholds, vector._agent_fused_gates, vector._agent_betas):
        removed_cal = (_d.pop(agent_id, None) is not None) or removed_cal
    # This is the textbook deliberate removal, so claim it: the rewrite below can
    # fail (OSError -> _save_calibration_state returns False) and leave the agent
    # in the file, and without the claim the very next calibration would carry
    # those entries back out of it — resurrecting calibration for a corpus that
    # no longer exists, which is the bug-036 this function was written to close.
    vector._claim_agent_calibration(agent_id, *vector._CALIBRATION_AGENT_AXES)
    state = _load_calibration_state()
    if state is not None:
        for _key in ("agent_thresholds", "agent_fused_gates", "agent_betas"):
            _sub = state.get(_key)
            if isinstance(_sub, dict):
                removed_cal = (_sub.pop(agent_id, None) is not None) or removed_cal
        if removed_cal:
            _save_calibration_state(
                state.get("embedding_dim"),
                state.get("embedding_model"),
                state.get("global_threshold"),
                state.get("agent_thresholds") or {},
                global_fused_gate=state.get("global_fused_gate"),
                agent_fused_gates=state.get("agent_fused_gates") or {},
                fused_gate_signal=state.get("fused_gate_signal"),
                agent_betas=state.get("agent_betas") or {},
                # bug-184: this rewrite drops one agent's entries, it does not
                # re-measure anything — carry the stored fingerprint (None for a
                # pre-2.5.2b2 sidecar) so the startup guard still sees the staleness.
                scoring_version=state.get("scoring_version"),
            )
    return removed_cal


async def _purge_agent_remote_namespace(agent_id: str) -> None:
    """Purge the agent's remote vector namespace (network I/O — post-commit only)."""
    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        try:
            base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
            await vector._embedding_client._client.post(
                f"{base_url}/purge",
                json={"namespace": f"cpersona:{agent_id}"},
            )
        except Exception as e:
            logger.debug("Remote purge failed (non-fatal): %s", e)


async def do_delete_agent_data(agent_id: str) -> dict:
    """Delete ALL data for a specific agent (memories, profiles, episodes)."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {
                "ok": True,
                "agent_id": agent_id,
                "deleted_memories": 0,
                "deleted_profiles": 0,
                "deleted_episodes": 0,
                # bug-111: same shape as the real response (bug-093 added it).
                "deleted_pending_tasks": 0,
            },
            "delete_agent_data",
        )
    if not agent_id:
        return error_response("agent_id is required for bulk deletion")

    # bug-042/043: transaction() serialises the multi-table delete + commit behind
    # the shared lock; its auto-rollback keeps a partial wipe from surviving as
    # another writer's commit.
    async with transaction() as db:
        counts = await _delete_agent_rows(db, agent_id)

    _purge_agent_calibration(agent_id)
    await _purge_agent_remote_namespace(agent_id)

    result = {"ok": True, "agent_id": agent_id, **counts}
    logger.info(
        "Deleted agent data for %s: %d memories, %d profiles, %d episodes, %d pending tasks",
        agent_id,
        counts["deleted_memories"],
        counts["deleted_profiles"],
        counts["deleted_episodes"],
        counts["deleted_pending_tasks"],
    )
    return result


def _separation_threshold(null_sims, pos_sims, floor: float, beta: float = 1.0) -> tuple:
    """Two-population threshold: the point that best separates null from positives.

    Sweeps candidate thresholds and returns the one maximizing the weighted Youden
    objective ``sensitivity + beta*specificity`` (``TPR + beta*(1 - FPR)``) where
    positives are a label-free proxy for related pairs (e.g. same-session similarity
    or, for the post-fusion gate, fused scores of temporally-adjacent rows) and the
    null is the random-pair / unrelated-row distribution. Unlike the percentile
    method, the operating point is derived from the corpus's actual separability
    rather than a fixed quantile.

    ``beta`` is the precision point — knob 3 (an earlier decision). ``beta == 1`` reproduces the
    balanced Youden's J point (``argmax TPR - FPR``); ``beta > 1`` favours specificity
    (strict — fewer contaminants, more misses); ``beta < 1`` favours sensitivity
    (lenient — fewer misses, more contaminants). The curve is calibrated from data;
    beta is the single policy choice of where on it to sit.

    Returns ``(threshold, youden_j)`` where ``youden_j`` is the true ``TPR - FPR`` at
    the chosen point (for observability), independent of ``beta``.
    """
    import numpy as np

    null = np.asarray(null_sims, dtype=np.float64)
    pos = np.asarray(pos_sims, dtype=np.float64)
    lo = min(float(null.min()), float(pos.min()))
    hi = max(float(null.max()), float(pos.max()))
    if hi <= lo:
        return float(max(lo, floor)), 0.0
    candidates = np.linspace(lo, hi, 256)
    tpr = (pos[None, :] >= candidates[:, None]).mean(axis=1)
    fpr = (null[None, :] >= candidates[:, None]).mean(axis=1)
    objective = tpr + beta * (1.0 - fpr)
    best = int(np.argmax(objective))
    return float(max(candidates[best], floor)), float(tpr[best] - fpr[best])


def _parse_ts_seconds(ts):
    """Parse an ISO-8601 timestamp to epoch seconds, or None when unparseable."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _adjacency_sims_core(times_seconds, vecs, window_sec: float):
    """Cosine similarities of memories stored within ``window_sec`` of each other.

    Memories sorted by time; consecutive pairs whose gap is within the window are a
    representative (non-extreme) proxy for related pairs — same-session content. Unlike
    the nearest-neighbour max, this samples the body of the related distribution rather
    than its extreme tail, which is what makes the two-population operating point useful.
    """
    import numpy as np

    t = np.asarray(times_seconds, dtype=np.float64)
    v = np.asarray(vecs, dtype=np.float64)
    if len(t) < 2:
        return np.array([])
    order = np.argsort(t)
    t, v = t[order], v[order]
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vn = v / norms
    mask = np.diff(t) <= window_sec
    if not mask.any():
        return np.array([])
    return np.sum(vn[:-1][mask] * vn[1:][mask], axis=1)


def _safe_frombuffer(blob):
    """Decode a stored embedding blob to a float32 array, or None if the bytes are corrupt
    (bug-061). np.frombuffer raises `ValueError: buffer size must be a multiple of element
    size` when len(blob) is not divisible by 4 — a truncated write or a hand-crafted
    embedding_b64 import can plant such a blob, and the unguarded decode in calibration was
    reachable from ensure_calibrated_on_startup, crashing the whole server before it served
    a single request. Returning None lets the caller skip the poison row instead. A valid
    float32 embedding is always a multiple of 4 bytes, so this never rejects a good row."""
    import numpy as np

    if not blob or len(blob) % 4 != 0:
        return None
    try:
        return np.frombuffer(blob, dtype=np.float32)
    except (ValueError, TypeError):
        return None


async def _temporal_adjacency_sims(db, agent_id: str, limit: int, window_min: float):
    """Fetch (timestamp, embedding) ordered by time and build same-session pair sims."""
    import numpy as np

    # Per-agent when agent_id provided, deliberate all-agents fallback when empty —
    # the empty case is the typed no-filter form of the helper (an earlier decision).
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT timestamp, embedding FROM memories WHERE embedding IS NOT NULL "
        f"AND timestamp IS NOT NULL{iso.and_clause} ORDER BY timestamp DESC LIMIT ?",
        (*iso.params, limit),
    )
    times, vecs = [], []
    for ts, blob in rows:
        sec = _parse_ts_seconds(ts)
        if sec is None:
            continue
        v = _safe_frombuffer(blob)  # bug-061: skip a corrupt (non-4-multiple) blob, don't crash
        if v is None:
            continue
        times.append(sec)
        vecs.append(v)
    if len(times) < 2:
        return np.array([])
    # bug-025: drop off-modal-dimension rows (times/vecs in lockstep) so np.array
    # is not ragged on a mixed-dimension corpus during a model swap.
    from collections import Counter

    target_dim = Counter(v.shape[0] for v in vecs).most_common(1)[0][0]
    paired = [(t, v) for t, v in zip(times, vecs) if v.shape[0] == target_dim]
    if len(paired) < 2:
        return np.array([])
    times = [t for t, _ in paired]
    vecs = [v for _, v in paired]
    return _adjacency_sims_core(times, np.array(vecs), window_min * 60.0)


def _threshold_from_sims(
    pairwise_sims,
    *,
    method: str,
    z_factor: float,
    percentile: float,
    floor: float,
    pos_sims=None,
) -> dict:
    """Derive a vector-similarity threshold from a null (random-pair) distribution.

    The threshold is placed ABOVE the mean of the random-pair similarities so that
    unrelated pairs are rejected:

    - ``percentile``: the given quantile of the null distribution. Distribution-free
      and robust to the narrow, high-mean cosine geometry of anisotropic models such
      as bge-m3 (mean random-pair similarity ~0.51, small spread).
    - ``zscore``: ``mean + z*std`` — rejects pairs within +z standard deviations of
      the random baseline.
    - ``separation``: the operating point that best separates the null from a
      label-free positive proxy (``pos_sims``, the per-memory nearest-neighbour
      similarity), via Youden's J. Removes the fixed-quantile choice — the point is
      learned from the corpus's own separability. Requires ``pos_sims``.

    The pre-2.4.24 formula used ``mean - z*std``, which placed the floor BELOW the
    null mean and admitted the majority of unrelated pairs (topic-drift contamination).

    Returns the threshold plus distribution statistics for observability, including
    ``null_admit_rate`` (fraction of random pairs admitted — a lower value is stricter).
    """
    import numpy as np

    sims = np.asarray(pairwise_sims, dtype=np.float64)
    sim_mean = float(np.mean(sims))
    sim_std = float(np.std(sims))
    sim_median = float(np.median(sims))

    youden_j = None
    if method == "zscore":
        raw = sim_mean + z_factor * sim_std
    elif method == "separation":
        if pos_sims is None:
            raise ValueError("separation method requires pos_sims")
        raw, youden_j = _separation_threshold(sims, pos_sims, floor)
    else:  # "percentile" (default)
        raw = float(np.quantile(sims, percentile))

    threshold = round(max(raw, floor), 4)
    result = {
        "threshold": threshold,
        "mean": round(sim_mean, 4),
        "std": round(sim_std, 4),
        "median": round(sim_median, 4),
        "p95": round(float(np.quantile(sims, 0.95)), 4),
        "null_admit_rate": round(float(np.mean(sims >= threshold)), 4),
    }
    if pos_sims is not None:
        pos = np.asarray(pos_sims, dtype=np.float64)
        result["pos_mean"] = round(float(np.mean(pos)), 4)
        result["pos_admit_rate"] = round(float(np.mean(pos >= threshold)), 4)
    if youden_j is not None:
        result["youden_j"] = round(youden_j, 4)
    return result


def _calibration_sidecar_path() -> str:
    """Path of the JSON sidecar that persists calibration state next to the DB."""
    return config.DB_PATH + ".calibration.json"


def _stored_agent_maps_to_carry(embedding_dim: int, state: dict | None = None) -> dict[str, dict]:
    """Per-agent sidecar entries this process did not measure but must not drop.

    bug-189: the sidecar is written as a snapshot of process state
    (``dict(vector._agent_thresholds)`` and friends), so it records only the
    agents the writing process happens to hold. Whenever the startup restore did
    not run — ``AUTO_CALIBRATE`` on, a stale-fingerprint boot, or a bare
    ``calibrate_threshold`` call early in a process — calibrating one agent
    rewrites the file without every other agent's threshold, gate and beta. The
    loss is silent: the next start restores the truncated sidecar and the missing
    agents fall back to the global default. No error, just quietly wrong recall
    breadth for agents nobody touched.

    So carry the stored entries forward. The caller overlays what it measured on
    top, because a fresh measurement is the better value for an agent this
    process actually calibrated.

    Entries this process is AUTHORITATIVE for are excluded (see
    ``vector._calibration_authority``). A blanket carry cannot express a
    deletion: ``set_recall_precision(agent, "")`` clears the override by popping
    it out of ``vector._agent_betas`` and letting the sidecar be rewritten from
    process state, so carrying the stored beta back over it returned
    ``cleared: true`` while the override reappeared on the next restart. The
    absence of an entry means "deleted" when this process owns it and "never
    seen" when it does not, and only the owner may turn absence into a deletion.

    *state* lets a caller that already read the sidecar pass it in, so the whole
    payload is decided from ONE read of the file (see
    ``_stored_calibration_to_carry``); it is loaded here when omitted.

    Returns ``{}`` when the stored payload must NOT be carried — a different
    embedding dimension or scoring version means those numbers describe a
    quantity the runtime no longer produces, which is exactly what bug-184
    refuses to restore. Preserving them here would launder them back in through
    the write path after the read path declined them.
    """
    if state is None:
        state = _load_calibration_state()
    if not state:
        return {}
    if state.get("embedding_dim") != embedding_dim:
        return {}
    if state.get("scoring_version") != SCORING_VERSION:
        return {}
    return {
        axis: {
            agent_id: value
            for agent_id, value in (state.get(axis) or {}).items()
            if agent_id not in vector._calibration_authority[axis]
        }
        for axis in vector._CALIBRATION_AGENT_AXES
    }


def _stored_calibration_to_carry(embedding_dim: int) -> dict:
    """Everything in the sidecar this process must not overwrite, per-agent AND global.

    The global axes (``global_threshold`` / ``global_fused_gate`` /
    ``fused_gate_signal``) are the same class of loss as the per-agent maps and
    were the half bug-189's first fix missed: they live in ``config`` and
    ``vector`` module state, so a process that never restored writes its ENV
    DEFAULTS over them the moment it calibrates a single agent. That is wider
    than the per-agent axis, not narrower — ``_get_vector_threshold`` falls back
    to ``config.VECTOR_MIN_SIMILARITY`` for every agent without an override, so
    one per-agent calibration in a non-restored process could reset the recall
    floor of the whole deployment on the next boot.

    Nothing in the process ever MEASURES ``global_fused_gate``: only a restore
    puts a value there, so without the carry it can only ever be lost.

    Keys are absent, not None, for axes this process owns — ``None`` is a
    meaningful stored value (no gate calibrated yet), so the caller distinguishes
    the two with ``carried.get(axis, <live value>)``. Returns ``{}`` when the
    stored payload is stale (bug-184), which correctly makes every axis fall back
    to the freshly measured live value.
    """
    state = _load_calibration_state()
    if not state:
        return {}
    carried: dict = _stored_agent_maps_to_carry(embedding_dim, state)
    if not carried:
        return {}
    for axis in vector._CALIBRATION_GLOBAL_AXES:
        if axis not in vector._global_calibration_authority:
            carried[axis] = state.get(axis)
    return carried


def _save_calibration_state(
    embedding_dim: int,
    embedding_model: str,
    global_threshold: float | None,
    agent_thresholds: dict,
    global_fused_gate: float | None = None,
    agent_fused_gates: dict | None = None,
    fused_gate_signal: str | None = None,
    agent_betas: dict | None = None,
    scoring_version: str | None = SCORING_VERSION,
) -> bool:
    """Persist calibrated thresholds + the embedding fingerprint to the sidecar.

    Persistence lets thresholds survive a restart without recomputation, and lets the
    startup guard detect an embedding-model (dimension) change. The post-fusion gate
    (v2.4.26) is persisted alongside the vector threshold and keyed by the same
    embedding fingerprint, plus the RECALL_MODE it was calibrated for. Per-agent precision
    overrides (knob 3, v2.4.29) are persisted next to the gates they produced so a restore
    keeps each agent's gate and the beta it sits on in sync.

    bug-095: written via temp-file + os.replace — an in-place json.dump killed
    mid-write corrupted the sidecar for EVERY agent (the loader then returns None
    and, with auto-calibration off, all agents silently fall back to the global
    default threshold). Returns False when persistence failed so callers can
    surface it instead of reporting the calibration as durably saved.

    bug-184 (2.5.2): the payload also carries ``scoring_version`` — the embedding
    fingerprint answers "was this measured on the same vectors?", the scoring
    fingerprint answers "was it measured on the same score?". Additive and
    rollback-safe: older code ignores the key, newer code reads its absence as stale.
    It defaults to the live ``utils.SCORING_VERSION`` because every caller that
    MEASURES thresholds measures them on the running scoring function; the one caller
    that only REWRITES an existing payload (``_purge_agent_calibration``) passes the
    stored value through, so dropping an agent cannot launder a stale sidecar into
    looking freshly calibrated.
    """
    payload = {
        "embedding_dim": embedding_dim,
        "embedding_model": embedding_model,
        "scoring_version": scoring_version,
        "global_threshold": global_threshold,
        "agent_thresholds": agent_thresholds,
        "global_fused_gate": global_fused_gate,
        "agent_fused_gates": agent_fused_gates or {},
        "fused_gate_signal": fused_gate_signal,
        "agent_betas": agent_betas or {},
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _calibration_sidecar_path()
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        logger.warning("Could not persist calibration sidecar: %s", exc)
        return False


def _load_calibration_state() -> dict | None:
    """Load the calibration sidecar, or None when absent/unreadable."""
    try:
        with open(_calibration_sidecar_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


_CALIBRATION_BACKUP_KEEP = 5


def _prune_calibration_backups(path: str) -> None:
    """Keep the newest _CALIBRATION_BACKUP_KEEP sidecar backups, drop the rest (bug-246).

    The per-version guard above already stops the identical-copy-per-boot growth; this
    bounds the case where the version tag itself keeps moving. Newest by mtime, with the
    filename as the tiebreak (the stamp has one-second resolution). Best-effort: a
    failure here must not surface as a failed backup.
    """
    backups = glob.glob(f"{glob.escape(path)}.before-*")
    if len(backups) <= _CALIBRATION_BACKUP_KEEP:
        return
    try:
        ordered = sorted(backups, key=lambda p: (os.path.getmtime(p), p), reverse=True)
    except OSError:
        return
    for stale in ordered[_CALIBRATION_BACKUP_KEEP:]:
        with contextlib.suppress(OSError):
            os.remove(stale)


def _backup_calibration_sidecar(old_scoring_version: str | None) -> str | None:
    """Copy the sidecar aside before a staleness recalibration replaces it (an earlier decision).

    A staleness-triggered recalibration (scoring change, dimension change) overwrites
    the only record of the gate values that were effective until this boot. Twice in
    production the previous values survived only because someone had copied the file
    by hand — and the second time (the 2026-08-17 fused-gate collapse to 0.1544) that
    manual copy was the entire evidence base for diagnosing the estimator instability.
    This makes the copy a property of the code path instead of of the operator.

    The name mirrors the manual convention already in the field:
    ``<sidecar>.before-<old_scoring_version>-<UTC>``. Failure is reported and swallowed
    — a backup must never block the recalibration it is evidence for.

    bug-246: one backup per superseded scoring version, not one per boot. The staleness
    flag only clears when a successful recalibration rewrites the sidecar, so a
    deployment where calibration cannot succeed (fewer than 10 embeddings, or the
    no-persist early return) is stale again on the next boot and copied the same file
    again — indefinitely, and every copy byte-identical, so the evidence value of the
    Nth is zero. An existing backup for the same version is the evidence; it is
    returned rather than duplicated. The retention sweep bounds the directory even when
    the version tag does move (a boot loop across an upgrade cycle).
    """
    path = _calibration_sidecar_path()
    version_tag = old_scoring_version or "unversioned"
    existing = sorted(glob.glob(f"{glob.escape(path)}.before-{glob.escape(version_tag)}-*"))
    if existing:
        logger.debug(
            "an earlier decision / bug-246: the calibration sidecar for scoring version %r is "
            "already backed up at %s; not writing another copy.",
            version_tag,
            existing[-1],
        )
        return existing[-1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.before-{version_tag}-{stamp}"
    try:
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        _prune_calibration_backups(path)
        return backup
    except OSError as exc:
        logger.warning(
            "an earlier decision: could not back up the calibration sidecar before "
            "recalibration; the previous gate values will not survive: %s",
            exc,
        )
        return None


async def _corpus_embedding_dim() -> int | None:
    """Return the float32 dimension of one stored embedding, or None when empty."""
    async with connection() as db:
        # Embedding dimension is corpus-invariant (any agent's row answers it) — the
        # typed no-filter helper call replaces the old waiver comment (an earlier decision).
        iso = isolation_where(agent_id=None)
        rows = await db.execute_fetchall(
            f"SELECT embedding FROM memories WHERE embedding IS NOT NULL{iso.and_clause} LIMIT 1"
        )
    if not rows or rows[0][0] is None:
        return None
    return len(rows[0][0]) // 4  # 4 bytes per float32


async def _calibrate_fused_gate(
    db,
    agent_id: str,
    sample_queries: int,
    window_min: float,
    beta: float,
    floor: float,
) -> dict | None:
    """Simulate-query calibration of the recall quality gate (an earlier decision, v2.4.27).

    The quality gate keys on a per-row score that — unlike pairwise cosine similarity —
    only exists relative to a query: the confidence score when CONFIDENCE_ENABLED, else
    the fused score (``_rsf_score`` / ``_rrf_score``). The null and positive distributions
    are therefore produced by *simulation*: sample stored memories as pseudo-queries, run
    the live recall pipeline AND the same post-recall scoring do_recall applies
    (``_apply_recall_scoring`` — episode penalty + confidence), take each row's gate score
    via ``_gate_score``, and label it against the pseudo-query by temporal adjacency —
    rows stored within ``window_min`` (same-session ≈ related) are the positive proxy, the
    rest the null. Separation over the two populations gives the operating point. Only the
    rows whose gate signal matches the active one contribute, so the curve is built on the
    exact value the runtime gate compares. Cost is at most ``sample_queries`` recalls per
    calibration (an offline / startup event), never per user recall.

    Returns a stats dict, or None when there is no fusion/confidence gate to calibrate
    (cascade + confidence-off), the embedding client is absent, or too few samples were
    collected (the caller then keeps the heuristic gate). The calibration applies the
    same ``_apply_recall_scoring`` do_recall runs (episode penalty + confidence), so the
    operating point matches the runtime gate score rather than the raw fused score.
    """
    import numpy as np

    from cpersona.memory_handlers import (
        _apply_recall_scoring,
        _gate_score,
        _is_episode_result,
        _recall_cascade,
        _recall_rrf,
        _recall_rsf,
    )

    mode = config.RECALL_MODE
    if mode == "rsf":
        recall_fn = _recall_rsf
    elif mode == "rrf":
        recall_fn = _recall_rrf
    else:
        recall_fn = _recall_cascade
    # The gate keys on confidence when enabled (it takes precedence in any mode), else on
    # the fused score. Cascade with confidence off has no fusion gate — the cosine vector
    # threshold owns precision there.
    if config.CONFIDENCE_ENABLED:
        signal = "confidence"
    elif mode in ("rsf", "rrf"):
        signal = mode
    else:
        return None
    if vector._embedding_client is None:
        return None

    rows = await db.execute_fetchall(
        "SELECT id, content, timestamp FROM memories "
        "WHERE agent_id = ? AND embedding IS NOT NULL AND content IS NOT NULL "
        "AND timestamp IS NOT NULL ORDER BY RANDOM() LIMIT ?",
        (agent_id, sample_queries),
    )
    window_sec = window_min * 60.0
    null_scores: list[float] = []
    pos_scores: list[float] = []
    queries_run = 0
    for qid, qcontent, qts in rows:
        if not qcontent or not qcontent.strip():
            continue
        q_sec = _parse_ts_seconds(qts)
        if q_sec is None:
            continue
        results = await recall_fn(db, agent_id, qcontent, 20, False)
        # Apply the same penalty + confidence scoring do_recall runs, so _gate_score
        # returns the exact value the runtime gate compares (confidence when enabled).
        # bug-155: pass the pseudo-query so the FTS-only rows in the sample get
        # backfilled cosines exactly like the runtime recall path does — otherwise
        # the calibration curve would be built on a signal shape (cosine-less
        # confidence branch) the runtime no longer produces.
        results, _, _, _ = await _apply_recall_scoring(db, agent_id, results, False, query=qcontent)
        queries_run += 1
        for r in results:
            rid = r.get("id")
            if not isinstance(rid, int) or rid <= 0:
                continue  # skip profiles (-1)
            # bug-101: only skip the pseudo-query MEMORY — an EPISODE that merely
            # shares its integer id (the AUTOINCREMENT spaces are independent) is
            # a legitimate sample and was silently dropped from the curve.
            if rid == qid and not _is_episode_result(r):
                continue
            score, row_signal = _gate_score(r)
            if score is None or row_signal != signal:
                continue  # only the active gate signal contributes to the curve
            r_sec = _parse_ts_seconds(r.get("timestamp"))
            if r_sec is not None and abs(r_sec - q_sec) <= window_sec:
                pos_scores.append(float(score))
            else:
                null_scores.append(float(score))

    if len(null_scores) < 10 or len(pos_scores) < 5:
        return None  # insufficient separation data — keep the pool-size heuristic

    threshold, youden_j = _separation_threshold(null_scores, pos_scores, floor, beta)
    null = np.asarray(null_scores, dtype=np.float64)
    pos = np.asarray(pos_scores, dtype=np.float64)
    return {
        "threshold": round(threshold, 4),
        "signal": signal,
        "beta": beta,
        "youden_j": round(youden_j, 4),
        "queries_run": queries_run,
        "n_null": len(null_scores),
        "n_pos": len(pos_scores),
        "null_admit_rate": round(float((null >= threshold).mean()), 4),
        "pos_admit_rate": round(float((pos >= threshold).mean()), 4),
        "null_mean": round(float(null.mean()), 4),
        "pos_mean": round(float(pos.mean()), 4),
    }


async def _calibrate_fused_gate_median(
    db,
    agent_id: str,
    sample_queries: int,
    window_min: float,
    beta: float,
    floor: float,
    draws: int | None = None,
) -> dict | None:
    """Median-of-K wrapper over ``_calibrate_fused_gate`` (an earlier decision).

    The single-draw estimator is unstable: ``_separation_threshold``'s objective
    J(θ) = TPR + β(1−FPR) is multimodal over a real corpus (a second mode near
    θ≈0.152 reproduced on every probe draw, 2026-08-17), and the calibration
    samples its pseudo-queries with ``ORDER BY RANDOM()`` — so one unlucky draw
    can hand the argmax to the minor mode. Production shipped a 0.1544 gate that
    21 subsequent probe draws never produced again (median 0.4288, stdev
    0.025–0.044). A median across K independent draws cannot land on a mode that
    fewer than half the draws select.

    Returns the stats dict of the draw holding the (upper) median threshold —
    the reported numbers stay one coherent measurement rather than an average of
    incompatible runs — annotated with every successful draw's threshold under
    ``threshold_draws``. None when every draw returns None (same degrade
    contract as the single-draw calibration: the caller keeps the heuristic
    gate).
    """
    if draws is None:
        draws = config.FUSED_GATE_CALIBRATION_DRAWS
    results: list[dict] = []
    for _ in range(max(1, draws)):
        stats = await _calibrate_fused_gate(
            db, agent_id, sample_queries, window_min, beta, floor
        )
        if stats is not None:
            results.append(stats)
    if not results:
        return None
    results.sort(key=lambda s: s["threshold"])
    chosen = results[len(results) // 2]
    if len(results) > 1:
        chosen["threshold_draws"] = [s["threshold"] for s in results]
    return chosen


async def _sample_embeddings(db, agent_id: str, sample_n: int):
    """Draw the embedding sample the null distribution is built from.

    Returns (vecs, None) on success or (None, error_response) when the corpus
    cannot support a calibration. Two independent floors have to hold, and they
    fail for different reasons: too few rows to draw from at all, and too few of
    them sharing one dimension once the ragged ones are dropped. The suite only
    ever tripped the first until an earlier decision (mutation M08) pointed at the gap.
    """
    import numpy as np

    # Per-agent when agent_id is provided, deliberate all-agents calibration when
    # empty (the typed no-filter form of the helper, an earlier decision).
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT embedding FROM memories WHERE embedding IS NOT NULL{iso.and_clause} ORDER BY RANDOM() LIMIT ?",
        (*iso.params, sample_n),
    )

    if len(rows) < 10:
        return None, {"ok": False, "error": f"Need at least 10 embeddings, found {len(rows)}"}

    vecs = []
    for (blob,) in rows:
        vec = _safe_frombuffer(blob)  # bug-061: skip a corrupt blob instead of crashing calibration/startup
        if vec is None:
            continue
        vecs.append(vec.copy())
    # bug-025: a mixed-embedding-dimension corpus (e.g. a mid-flight jina-768d ->
    # bge-1024d model swap) yields ragged rows; np.array of ragged vectors raises
    # ValueError on numpy>=1.24, surfacing an opaque error from
    # calibrate/set_recall_precision and able to abort ensure_calibrated_on_startup.
    # Keep only the modal dimension — off-dimension rows are stale relative to the
    # live model and get re-embedded by check_embedding_dimension.
    if vecs:
        from collections import Counter

        target_dim = Counter(v.shape[0] for v in vecs).most_common(1)[0][0]
        vecs = [v for v in vecs if v.shape[0] == target_dim]
    if len(vecs) < 10:
        return None, {"ok": False, "error": f"Need at least 10 same-dimension embeddings, found {len(vecs)}"}
    return np.array(vecs), None


def _null_distribution(vecs):
    """All-pairs cosine similarities, and their upper triangle.

    Treating every pair of distinct memories as a NON-match is what makes this a
    null distribution: the threshold is then placed where a genuine match would
    have to stand out from unrelated noise. The full matrix comes back too — the
    separation method's nearest-neighbour fallback reuses it rather than
    recomputing an O(n^2) product.
    """
    import numpy as np

    sim_matrix = vecs @ vecs.T
    pairwise_sims = sim_matrix[np.triu_indices(len(vecs), k=1)]
    return sim_matrix, pairwise_sims


async def do_calibrate_threshold(
    agent_id: str,
    sample_size: int = 0,
    z_factor: float = 0,
    method: str = "",
    percentile: float = 0,
) -> dict:
    """Auto-calibrate the vector-similarity threshold from the embedding distribution.

    Uses the null distribution of pairwise cosine similarities (mostly unrelated
    pairs). When *agent_id* is provided, writes a per-agent override into
    ``vector._agent_thresholds``; when empty, calibrates the global
    ``config.VECTOR_MIN_SIMILARITY`` from the all-agents corpus (v2.4.15).

    v2.4.24: the threshold is placed ABOVE the null mean (see ``_threshold_from_sims``);
    ``method`` defaults to ``percentile``. The result is persisted to a sidecar keyed by
    embedding dimension so a later embedding-model swap triggers recalibration at startup.
    """
    import numpy as np

    # bug-053: sample_size is a caller-supplied MCP tool parameter that feeds both a
    # LIMIT scan and an O(n^2) dense cosine matrix (vecs @ vecs.T) + np.triu_indices
    # (and a copy on the separation path). An unclamped large value allocates multi-GB
    # transient arrays and OOM-kills the whole server process, taking recall down for
    # every agent on the shared connection. Clamp to CALIBRATE_MAX_SAMPLE before it
    # reaches the LIMIT / quadratic path (this same sample_n also bounds the
    # _temporal_adjacency_sims call below), mirroring the _clamp_limit discipline.
    # bug-081: min() only bounds the UPPER side — a negative sample_size is truthy,
    # survives the `or` default, and reaches SQLite as `LIMIT -1`, which SQLite treats
    # as UNBOUNDED: the whole corpus flows into the O(n^2) matrix and the OOM bug-053
    # fixed is reopened through the lower bound. Clamp both sides.
    sample_n = max(1, min(sample_size or CALIBRATE_SAMPLE_SIZE, CALIBRATE_MAX_SAMPLE))
    z = z_factor or CALIBRATE_Z_FACTOR
    cal_method = method or CALIBRATE_METHOD
    cal_percentile = percentile or CALIBRATE_PERCENTILE
    # bug-066: percentile/z_factor are caller-supplied MCP floats that reach numpy
    # unvalidated (bug-053 clamped sample_size but left these siblings). np.quantile
    # requires q in [0, 1]; a natural 95-vs-0.95 confusion otherwise raises an opaque
    # ValueError. Interpret a value >1 as a percent (95 → 0.95), then validate, and clamp
    # z to a sane band so an absurd z can't yield a degenerate threshold.
    if cal_percentile > 1:
        cal_percentile = cal_percentile / 100.0
    if not (0.0 < cal_percentile <= 1.0):
        return {"ok": False, "error": f"percentile must be in (0, 1] (or a percent 1-100), got {percentile}"}
    z = max(-10.0, min(10.0, z))
    # bug-231: an unrecognised method fell through _threshold_from_sims' else branch to
    # percentile while the response echoed the caller's spelling back, so 'z-score' was
    # answered with a percentile threshold — persisted to the sidecar and applied to
    # vector._agent_thresholds — in a payload self-consistent enough that nothing
    # prompted a re-run. A bad enum value is refused, as merge_memories refuses its own.
    if cal_method not in CALIBRATE_METHODS:
        return error_response(
            f"Invalid method '{cal_method}'. Supported: {', '.join(sorted(CALIBRATE_METHODS))}"
        )

    # bug-119 (bug-111 sibling class): the old skeleton carried a phantom
    # `sample_size` key and none of the real payload's keys, so no-persist
    # consumers branching on the success shape broke. Mirror the real success
    # shape (echo keys resolved above, computed fields nulled); the guard sits
    # AFTER the pure param validation so invalid input still returns the real
    # error response, pause or not (same doctrine as the import/merge gates).
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {
                "ok": True,
                "sidecar_persisted": False,
                "scope": "per_agent" if agent_id else "global",
                "agent_id": agent_id,
                "sampled_embeddings": 0,
                "num_pairs": 0,
                "method": cal_method,
                "z_factor": z,
                "percentile": cal_percentile,
                "embedding_dim": None,
                "embedding_model": config.EMBEDDING_MODEL,
                "distribution": None,
                "null_admit_rate": None,
                "old_threshold": vector._get_vector_threshold(agent_id),
                "new_threshold": None,
            },
            "calibrate_threshold",
        )

    # The read seam stays open through the fused-gate calibration below — it issues
    # simulate-query recalls against the same connection.
    async with connection() as db:
        vecs, sample_error = await _sample_embeddings(db, agent_id, sample_n)
        if sample_error is not None:
            return sample_error

        sim_matrix, pairwise_sims = _null_distribution(vecs)

        n = len(vecs)
        num_pairs = len(pairwise_sims)
        old_threshold = vector._get_vector_threshold(agent_id)

        # Positive proxy for the separation method (label-free). Preferred: temporal
        # adjacency (same-session memories ≈ related — a representative sample of the
        # related distribution). Fallback: nearest-neighbour max, used only when too few
        # temporally-adjacent pairs exist (it overestimates relatedness — extreme tail —
        # so the threshold trends high and recall suffers).
        pos_sims = None
        proxy_source = None
        if cal_method == "separation":
            pos_sims = await _temporal_adjacency_sims(
                db, agent_id, sample_n, CALIBRATE_TEMPORAL_WINDOW_MIN
            )
            proxy_source = "temporal"
            if pos_sims is None or len(pos_sims) < 10:
                nn = sim_matrix.copy()
                np.fill_diagonal(nn, -np.inf)
                pos_sims = nn.max(axis=1)
                proxy_source = "nn_fallback"

        stats = _threshold_from_sims(
            pairwise_sims,
            method=cal_method,
            z_factor=z,
            percentile=cal_percentile,
            floor=CALIBRATE_FLOOR,
            pos_sims=pos_sims,
        )
        new_threshold = stats["threshold"]
        embedding_dim = int(vecs.shape[1])

        # Apply: per-agent dict when agent_id provided, global fallback when empty.
        # Claiming the axis makes this process the owner of what it just measured, so
        # the sidecar write below overwrites the stored value instead of carrying it
        # (and, later, so a deletion of this entry can reach disk).
        if agent_id:
            vector._agent_thresholds[agent_id] = new_threshold
            vector._claim_agent_calibration(agent_id, "agent_thresholds")
        else:
            config.VECTOR_MIN_SIMILARITY = new_threshold
            vector._claim_global_calibration("global_threshold")

        # Post-fusion quality-gate calibration (v2.4.26, an earlier decision). Per-agent and
        # fusion-mode only: recall is per-agent, and the gate lives on the active mode's
        # fused-score scale. Calibrating the curve here makes precision driven by data in
        # every mode (cascade via the vector floor above, rsf/rrf via this gate) instead of
        # the pool-size heuristic _adaptive_min_score.
        fused_stats = None
        if agent_id and config.FUSED_GATE_ENABLED:
            # The simulate-query pass issues live fusion recalls; a flaky embedding backend
            # must not abort calibration and lose the vector threshold computed above (which
            # is persisted below). Degrade to the heuristic gate on any failure.
            try:
                fused_stats = await _calibrate_fused_gate_median(
                    db,
                    agent_id,
                    config.FUSED_GATE_SAMPLE_QUERIES,
                    CALIBRATE_TEMPORAL_WINDOW_MIN,
                    vector._get_precision_beta(agent_id),
                    CALIBRATE_FLOOR,
                )
            except Exception as exc:
                logger.warning(
                    "Fused-gate calibration failed for [%s]; keeping the heuristic gate: %s",
                    agent_id or "global",
                    exc,
                )
                fused_stats = None
            if fused_stats is not None:
                # an earlier decision / an earlier decision: the 0.1544 collapse produced zero log lines —
                # the gate is recall's effective filter, so a replacement is worth one.
                logger.info(
                    "Fused gate [%s]: %s -> %.4f (signal=%s, draws=%s)",
                    agent_id,
                    vector._agent_fused_gates.get(agent_id, "unset"),
                    fused_stats["threshold"],
                    fused_stats["signal"],
                    fused_stats.get("threshold_draws", [fused_stats["threshold"]]),
                )
                vector._agent_fused_gates[agent_id] = fused_stats["threshold"]
                vector._fused_gate_signal = fused_stats["signal"]
                # Only on success: a degraded run (no embedding client, a flaky
                # backend) measured no gate, so it owns neither the agent's gate
                # nor the signal and must leave the stored ones alone.
                vector._claim_agent_calibration(agent_id, "agent_fused_gates")
                vector._claim_global_calibration("fused_gate_signal")

    # Persist for restart survival + embedding-change detection (Tier 4).
    # bug-095: surface a failed sidecar write — the in-memory calibration applied,
    # but a restart would restore the PREVIOUS sidecar, so the caller must not be
    # told the save was durable.
    # bug-189: overlay this process's measurements on the stored state instead of
    # replacing the file with a snapshot of process memory, so calibrating one agent
    # cannot silently delete the thresholds of agents — or the global values — this
    # process never loaded. The helper returns {} when the stored numbers are stale,
    # so nothing invalid survives, and it omits every entry this process OWNS, so a
    # deliberate deletion (a cleared precision override, a purged agent) still
    # reaches disk instead of being carried back over.
    carried = _stored_calibration_to_carry(embedding_dim)
    sidecar_persisted = _save_calibration_state(
        embedding_dim,
        config.EMBEDDING_MODEL,
        carried.get("global_threshold", config.VECTOR_MIN_SIMILARITY),
        {**carried.get("agent_thresholds", {}), **dict(vector._agent_thresholds)},
        global_fused_gate=carried.get("global_fused_gate", vector._global_fused_gate),
        agent_fused_gates={
            **carried.get("agent_fused_gates", {}),
            **dict(vector._agent_fused_gates),
        },
        fused_gate_signal=carried.get("fused_gate_signal", vector._fused_gate_signal),
        agent_betas={**carried.get("agent_betas", {}), **dict(vector._agent_betas)},
    )

    result = {
        "ok": True,
        "sidecar_persisted": sidecar_persisted,
        "scope": "per_agent" if agent_id else "global",
        "agent_id": agent_id,
        "sampled_embeddings": n,
        "num_pairs": num_pairs,
        "method": cal_method,
        "z_factor": z,
        "percentile": cal_percentile,
        "embedding_dim": embedding_dim,
        "embedding_model": config.EMBEDDING_MODEL,
        "distribution": {
            "mean": stats["mean"],
            "std": stats["std"],
            "median": stats["median"],
            "p95": stats["p95"],
        },
        "null_admit_rate": stats["null_admit_rate"],
        "old_threshold": old_threshold,
        "new_threshold": new_threshold,
    }
    if proxy_source is not None:
        result["proxy_source"] = proxy_source
    if "youden_j" in stats:
        result["youden_j"] = stats["youden_j"]
    if "pos_admit_rate" in stats:
        result["pos_admit_rate"] = stats["pos_admit_rate"]
        result["pos_mean"] = stats["pos_mean"]
    if fused_stats is not None:
        result["fused_gate"] = fused_stats
    logger.info(
        "Calibrated threshold [%s]: %.4f -> %.4f (method=%s z=%.1f pct=%.2f of %d pairs, "
        "mean=%.4f std=%.4f admit=%.3f dim=%d)",
        agent_id or "global",
        old_threshold,
        new_threshold,
        cal_method,
        z,
        cal_percentile,
        num_pairs,
        stats["mean"],
        stats["std"],
        stats["null_admit_rate"],
        embedding_dim,
    )
    return result


async def do_set_recall_precision(agent_id: str, precision: str = "", beta: float = 0) -> dict:
    """Set an agent's recall precision (knob 3, v2.4.29, an earlier decision) and recalibrate its gate.

    ``precision`` is one of ``strict`` / ``balanced`` / ``lenient``, mapped to a specificity
    weight (beta) of 2.0 / 1.0 / 0.5 in the gate separation objective
    (sensitivity + beta*specificity): higher beta sits the gate higher on the curve (fewer
    contaminants, more misses), lower beta lower (fewer misses, more contaminants). A raw
    ``beta`` > 0 overrides the named level. An empty ``precision`` with ``beta`` <= 0 clears
    the per-agent override, returning the agent to the global CPERSONA_RECALL_PRECISION
    default. The agent's post-fusion quality gate is recalibrated at the new beta
    immediately (no restart needed) and the (beta, gate) pair is persisted to the
    calibration sidecar. Unlike a recall argument, precision cannot be a per-call override:
    the gate threshold is precomputed on the separation curve at a fixed beta, so changing
    it requires recalibration, which this tool performs once rather than per recall.
    """
    if not agent_id:
        return {"ok": False, "error": "agent_id is required"}

    # Resolve the target beta. Raw beta wins; then the named level; then (empty + beta<=0)
    # is the clear-override signal.
    clear = False
    if beta and beta > 0:
        resolved_beta = float(beta)
        # bug-234: label the beta that was actually stored, the same way the read-back
        # companion does. Echoing the NAME the caller sent described the persisted state
        # falsely — set_recall_precision(precision='strict', beta=3.0) answered
        # precision:'strict' while get_recall_precision reported 'custom' for the very
        # same row, so a UI's selected level changed on reload with nothing having
        # changed. _precision_label is the single inversion of _PRECISION_BETA.
        resolved_precision = _precision_label(resolved_beta)
    elif precision:
        level = precision.lower()
        if level not in config._PRECISION_BETA:
            return {
                "ok": False,
                "error": f"Unknown precision '{precision}'; expected strict / balanced / lenient",
            }
        resolved_beta = config._PRECISION_BETA[level]
        resolved_precision = level
    else:
        clear = True
        resolved_beta = config.FUSED_GATE_BETA
        resolved_precision = "default"

    # bug-119 (bug-111 sibling class): mirror the real success shape — the old
    # skeleton dropped cleared / fused_gate / calibrate and nulled precision/beta
    # instead of echoing the resolved values. The guard sits AFTER the pure
    # resolution above so invalid input still returns the real error response.
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {
                "ok": True,
                "agent_id": agent_id,
                "precision": resolved_precision,
                "beta": resolved_beta,
                "cleared": clear,
                "fused_gate": None,
                "calibrate": None,
            },
            "set_recall_precision",
        )

    # Apply the override, then recalibrate so the change takes effect now (this also
    # persists the sidecar, including agent_betas, via do_calibrate_threshold). Keep it
    # atomic: if calibration cannot run (e.g. an agent with too few embeddings returns
    # ok=False before the sidecar is saved), roll the in-memory override back so it never
    # diverges from the unpersisted sidecar.
    had_override = agent_id in vector._agent_betas
    prev_beta = vector._agent_betas.get(agent_id)
    had_authority = agent_id in vector._calibration_authority["agent_betas"]
    if clear:
        vector._agent_betas.pop(agent_id, None)
    else:
        vector._agent_betas[agent_id] = resolved_beta
    # The clear is the reason the authority set exists: popping the beta is how the
    # removal is expressed, and the sidecar write inside do_calibrate_threshold is
    # what makes it durable. Without the claim that write carries the stored beta
    # back over the hole, so the tool answers {cleared: true} and the override is
    # live again after the next restart.
    vector._claim_agent_calibration(agent_id, "agent_betas")

    # bug-096: the rollback must also cover a RAISED calibration failure (transient
    # DB lock, numpy fault) — the old ok:False-only path leaked the un-persisted
    # beta into process memory, and the next routine calibration then persisted a
    # gate at a beta the caller was told failed to apply.
    try:
        cal = await do_calibrate_threshold(agent_id=agent_id)
    except Exception as exc:
        cal = {"ok": False, "error": f"calibration raised: {exc}"}
    if not cal.get("ok"):
        # bug-149 (bug-096 residual): compare-and-restore. do_calibrate_threshold awaits, so a
        # concurrent set_recall_precision for the SAME agent can apply + persist its own
        # value while this call is suspended. Roll back only if this call's own write is
        # still the live value; otherwise leave the concurrent writer's applied value in
        # place rather than clobbering it with our stale pre-await snapshot.
        if clear:
            this_write_survived = agent_id not in vector._agent_betas
        else:
            this_write_survived = vector._agent_betas.get(agent_id) == resolved_beta
        if this_write_survived:
            if had_override:
                vector._agent_betas[agent_id] = prev_beta
            else:
                vector._agent_betas.pop(agent_id, None)
            # Roll the ownership claim back with the value. A failed call that left
            # the claim behind would teach the next calibration to treat "this
            # process has no beta for the agent" as a deletion and drop a stored
            # override this call was told it never applied. Only inside the
            # this_write_survived branch: a concurrent writer that overtook us owns
            # its own claim.
            if not had_authority:
                vector._release_agent_calibration(agent_id, "agent_betas")
        return {
            "ok": False,
            "agent_id": agent_id,
            "precision": resolved_precision,
            "beta": resolved_beta,
            "cleared": clear,
            "error": cal.get("error", "calibration failed"),
        }
    return {
        "ok": True,
        "agent_id": agent_id,
        "precision": resolved_precision,
        "beta": resolved_beta,
        "cleared": clear,
        "fused_gate": cal.get("fused_gate"),
        "calibrate": {k: cal.get(k) for k in ("ok", "scope", "new_threshold", "error") if k in cal},
    }


def _precision_label(beta: float) -> str:
    """Invert a specificity weight (beta) back to its named precision level.

    The named levels store exact betas (strict=2.0 / balanced=1.0 / lenient=0.5), so an
    exact match is reliable; a raw beta set via the override returns 'custom'.
    """
    for name, value in config._PRECISION_BETA.items():
        if value == beta:
            return name
    return "custom"


async def do_get_recall_precision(agent_id: str) -> dict:
    """Read an agent's effective recall precision (knob 3, read-back for set_recall_precision).

    Returns the resolved specificity weight (``beta``) and its named ``precision`` level,
    flagging whether it comes from a per-agent override (``overridden``) or the global
    CPERSONA_RECALL_PRECISION default. This is the read companion to set_recall_precision:
    a client can load the current value, let the user edit it, and write it back, instead
    of the pill being write-only. Read-only — it never recalibrates and never persists, so
    it is not gated by no-persist pause (like recall).
    """
    if not agent_id:
        return {"ok": False, "error": "agent_id is required"}

    overridden = agent_id in vector._agent_betas
    beta = vector._get_precision_beta(agent_id)
    global_beta = config.FUSED_GATE_BETA
    return {
        "ok": True,
        "agent_id": agent_id,
        "precision": _precision_label(beta),
        "beta": beta,
        "overridden": overridden,
        "global_precision": _precision_label(global_beta),
        "global_beta": global_beta,
    }


def _restore_calibration_state(state: dict) -> None:
    """Load persisted thresholds from a sidecar payload into live config + dict.

    Backward compatible: a pre-v2.4.26 sidecar without the fused-gate keys restores the
    vector threshold only, leaving the fused gate uncalibrated (heuristic fallback).

    Restoring is also what makes this process AUTHORITATIVE (bug-189 follow-up): it has
    now read the whole payload, so from here on an entry missing from the dicts is
    missing because something removed it, not because this process never saw it — and
    only then may the sidecar write drop it. The claims are per axis and per stored key
    for the same reason the restore itself is: a key absent from the file is not owned,
    so a concurrent writer that adds one later still has it carried.
    """
    global_threshold = state.get("global_threshold")
    if global_threshold is not None:
        config.VECTOR_MIN_SIMILARITY = global_threshold
    vector._agent_thresholds.update(state.get("agent_thresholds") or {})
    global_fused_gate = state.get("global_fused_gate")
    if global_fused_gate is not None:
        vector._global_fused_gate = global_fused_gate
    vector._agent_fused_gates.update(state.get("agent_fused_gates") or {})
    fused_gate_signal = state.get("fused_gate_signal")
    if fused_gate_signal is not None:
        vector._fused_gate_signal = fused_gate_signal
    # Per-agent precision overrides (knob 3, v2.4.29). Backward compatible: a pre-v2.4.29
    # sidecar has no key, leaving every agent on the global beta default.
    vector._agent_betas.update(state.get("agent_betas") or {})
    for axis in vector._CALIBRATION_AGENT_AXES:
        for stored_agent_id in state.get(axis) or {}:
            vector._claim_agent_calibration(stored_agent_id, axis)
    vector._claim_global_calibration(*vector._CALIBRATION_GLOBAL_AXES)


async def ensure_calibrated_on_startup(auto_calibrate: bool, on_model_change: bool) -> dict:
    """Startup guard for the vector-similarity threshold (Tier 4, v2.4.24).

    Restores persisted thresholds when the embedding dimension is unchanged, and
    (re)calibrates on first run or on an embedding-dimension change (e.g. a silent
    jina 768d -> bge-m3 1024d swap), even when ``AUTO_CALIBRATE`` is off. A stale
    threshold calibrated for a previous embedding model is a known cause of recall
    contamination. Returns a small status dict for logging.

    bug-184 (2.5.2): a scoring-function change is the same staleness class as a
    dimension change — the sidecar's thresholds were measured on a score distribution
    the runtime no longer produces — so ``scoring_stale`` takes the identical path:
    skip the restore, recalibrate even with ``AUTO_CALIBRATE`` off. An absent
    ``scoring_version`` counts as stale: that is exactly the pre-2.5.2b2 sidecar the
    cosine backfill invalidated. Both triggers are reported in the status dict; the
    action names whichever fired (dimension first — it also implies new vectors).
    """
    state = _load_calibration_state()
    live_dim = await _corpus_embedding_dim()
    dim_changed = (
        state is not None and live_dim is not None and state.get("embedding_dim") != live_dim
    )
    scoring_stale = state is not None and state.get("scoring_version") != SCORING_VERSION

    restored = False
    if state and not dim_changed and not scoring_stale and not auto_calibrate:
        _restore_calibration_state(state)
        restored = True
        # A pre-v2.4.27 sidecar (or one never gate-calibrated) restores the vector
        # threshold but carries no recall gate. With FUSED_GATE_ENABLED, fall through to
        # calibrate the gate so an earlier decision actually bites in production; otherwise the
        # restore is sufficient. (This is what activates the gate on a v2.4.25 -> v2.4.27
        # upgrade where the embedding dimension is unchanged, so no dim-change recalibrate
        # would otherwise fire.)
        if not (config.FUSED_GATE_ENABLED and vector._fused_gate_signal is None):
            return {"action": "restored", "embedding_dim": state.get("embedding_dim")}
        logger.info("Calibration sidecar has no recall-gate signal; calibrating the gate.")

    if not restored and not (
        auto_calibrate
        or (on_model_change and (state is None or dim_changed or scoring_stale))
    ):
        if scoring_stale:
            # bug-184: not restoring is the safe half (a gate measured on another scoring
            # function must not be applied), but with CALIBRATE_ON_MODEL_CHANGE off there
            # is no repair either, and nothing else in the boot path says so. Without this
            # line the condition is silent and permanent — every boot leaves the gate
            # uncalibrated and every deep_check keeps reporting stale_scoring_version.
            logger.warning(
                "bug-184: calibration sidecar was written for scoring version %r, runtime "
                "is %r, but recalibration is disabled by config (AUTO_CALIBRATE and "
                "CALIBRATE_ON_MODEL_CHANGE both off). The stale gate is NOT applied; the "
                "gate stays uncalibrated and deep_check will keep reporting "
                "stale_scoring_version until calibration is enabled or run manually.",
                state.get("scoring_version"),
                SCORING_VERSION,
            )
        return {"action": "noop"}

    # bug-184 follow-on: skipping the restore drops the operator's knob-3 precision
    # overrides along with the measurements, and the recalibration below then (a) measures
    # every gate at the DEFAULT beta instead of the configured one and (b) persists an
    # empty agent_betas — so the boot would silently and permanently delete a setting the
    # operator chose. Betas are not measurements: they are policy INPUTS the measurement
    # consumes (_calibrate_fused_gate reads _get_precision_beta), and neither a scoring
    # change, an embedding change, nor auto-calibration invalidates a stated preference.
    # Thresholds and gates are deliberately NOT preloaded here — those are exactly the
    # stale measurements being re-derived.
    #
    # The condition is "we are about to recalibrate and nothing was restored", not a list
    # of triggers: the three ways to get here (stale fingerprint, dimension change,
    # AUTO_CALIBRATE) all bypass _restore_calibration_state, and AUTO_CALIBRATE is the
    # worst of them — it wipes the overrides on EVERY boot, not just an upgrade one.
    # Keying on the triggers would make the invariant hold or fail depending on an
    # unrelated config flag.
    if not restored and state is not None:
        vector._agent_betas.update(state.get("agent_betas") or {})
        # Claim the beta axis ONLY. This process now holds every stored beta, so a
        # later clear of one is a real deletion and must reach disk; it holds none of
        # the thresholds and gates deliberately left behind above, so those stay
        # carried into the sidecar until this boot's recalibration replaces them.
        for stored_agent_id in state.get("agent_betas") or {}:
            vector._claim_agent_calibration(stored_agent_id, "agent_betas")

    # an earlier decision: a staleness recalibration is about to replace the only record of the
    # values that gated recall until this boot. Back the sidecar up first (evidence for
    # the before/after comparison the log below reports), and remember what was stored
    # so the report can distinguish it from the runtime default that
    # ``do_calibrate_threshold`` will log as its "from" value — with the restore
    # skipped, that "from" is config, not anything that ever gated a query.
    sidecar_backup = None
    replaced = None
    if state is not None and (dim_changed or scoring_stale):
        sidecar_backup = _backup_calibration_sidecar(state.get("scoring_version"))
        replaced = {
            "stored_global_threshold": state.get("global_threshold"),
            "runtime_default_threshold": vector._get_vector_threshold(""),
            "stored_agent_thresholds": dict(state.get("agent_thresholds") or {}),
            "stored_agent_fused_gates": dict(state.get("agent_fused_gates") or {}),
        }

    if dim_changed:
        logger.warning(
            "Embedding dimension changed (%s -> %s); recalibrating vector threshold. "
            "A stale threshold from a previous embedding model causes recall contamination.",
            state.get("embedding_dim"),
            live_dim,
        )
    if scoring_stale:
        logger.warning(
            "bug-184: calibration sidecar was written for scoring version %r, runtime is %r; "
            "recalibrating. A gate restored across a scoring change gates a different "
            "quantity than it was calibrated on and silently over-filters recall.",
            state.get("scoring_version"),
            SCORING_VERSION,
        )

    global_result = await do_calibrate_threshold(agent_id="")
    agents = []
    if global_result.get("ok"):
        # Deliberate corpus-wide agent enumeration (typed no-filter helper —
        # the structural gate's sanctioned spelling for a global scan).
        iso_all = isolation_where(agent_id=None)
        async with connection() as db:
            agent_rows = await db.execute_fetchall(
                f"SELECT DISTINCT agent_id FROM memories WHERE embedding IS NOT NULL{iso_all.and_clause}",
                iso_all.params,
            )
        for (aid,) in agent_rows:
            r = await do_calibrate_threshold(agent_id=aid)
            if r.get("ok"):
                agents.append(aid)

    if replaced is not None:
        # an earlier decision (b): the per-calibration log lines above each printed
        # "old -> new" where old is the runtime default, because the stale restore
        # was skipped. This is the line that reports the quantity an operator
        # actually needs after an upgrade boot: what gated recall until now
        # (stored), what the process booted with instead (runtime default), and
        # what gates it from here (new) — for the cosine threshold AND the fused
        # gates, which the per-calibration lines never mention at all (the
        # 2026-08-17 fused-gate collapse produced zero log lines).
        replaced["new_global_threshold"] = vector._get_vector_threshold("")
        replaced["new_agent_thresholds"] = dict(vector._agent_thresholds)
        replaced["new_agent_fused_gates"] = dict(vector._agent_fused_gates)
        agent_ids = sorted(
            set(replaced["stored_agent_thresholds"])
            | set(replaced["new_agent_thresholds"])
            | set(replaced["stored_agent_fused_gates"])
            | set(replaced["new_agent_fused_gates"])
        )

        def _fmt(value: float | None) -> str:
            return "none" if value is None else f"{value:.4f}"

        per_agent = "; ".join(
            f"[{aid}] threshold {_fmt(replaced['stored_agent_thresholds'].get(aid))}"
            f" -> {_fmt(replaced['new_agent_thresholds'].get(aid))},"
            f" fused_gate {_fmt(replaced['stored_agent_fused_gates'].get(aid))}"
            f" -> {_fmt(replaced['new_agent_fused_gates'].get(aid))}"
            for aid in agent_ids
        )
        logger.warning(
            "an earlier decision: staleness recalibration replaced the stored calibration "
            "(backup: %s). Global cosine threshold: stored=%s (effective until this "
            "boot; not applied, stale), runtime_default=%s, new=%s. Per-agent "
            "(stored -> new): %s",
            sidecar_backup or "FAILED",
            _fmt(replaced["stored_global_threshold"]),
            _fmt(replaced["runtime_default_threshold"]),
            _fmt(replaced["new_global_threshold"]),
            per_agent or "none",
        )

    return {
        "action": (
            "gate_calibrated" if restored
            else "recalibrated" if dim_changed
            else "recalibrated_scoring" if scoring_stale
            else "auto" if auto_calibrate
            else "initial"
        ),
        "dim_changed": dim_changed,
        "scoring_stale": scoring_stale,
        "global_ok": bool(global_result.get("ok")),
        "agents": agents,
        # an earlier decision: machine-readable form of the replacement report above; None on
        # the boots (initial, plain auto-calibrate) that replace nothing stale.
        "sidecar_backup": sidecar_backup,
        "calibration_replaced": replaced,
    }


async def do_delete_episode(episode_id: int, agent_id: str = "") -> dict:
    """Delete a single episode by ID (FTS5 triggers handle the LOCAL index cleanup)."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "deleted_id": episode_id}, "delete_episode")
    _warn_if_unscoped("do_delete_episode", episode_id, agent_id)
    # Read the owner up front so the remote-vector removal below targets the namespace
    # the episode was indexed under (cpersona:{owner}), matching how do_delete_memory
    # resolves the owner for its bug-023 removal even when the caller omits agent_id.
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT agent_id FROM episodes WHERE id = ?", (episode_id,))
    if not rows:
        return error_response(f"Episode {episode_id} not found or not owned by agent")
    owner_agent_id = rows[0][0]
    # bug-042/043: transaction() serialises the DELETE+commit behind the shared lock.
    async with transaction() as db:
        if agent_id:
            cursor = await db.execute(
                "DELETE FROM episodes WHERE id = ? AND agent_id = ?",
                (episode_id, agent_id),
            )
        else:
            cursor = await db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    if cursor.rowcount == 0:
        return error_response(f"Episode {episode_id} not found or not owned by agent")

    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        # bug-023 sibling: episodes are pushed into the same remote index on create
        # (bug-049, id=ep:{id} under cpersona:{owner}). delete_episode used to remove
        # only the SQLite row, orphaning the remote vector — it kept surfacing in
        # remote-mode recall, wasting top-K slots until the by-id rehydrate missed.
        # Mirror do_delete_memory's /remove; a removal failure must not fail the delete.
        ns = f"cpersona:{owner_agent_id}"
        try:
            base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
            await vector._embedding_client._client.post(
                f"{base_url}/remove",
                json={"namespace": ns, "ids": [f"ep:{episode_id}"]},
            )
        except Exception as e:
            logger.debug("Remote remove failed (non-fatal): %s", e)

    return {"ok": True, "deleted_id": episode_id}


def _decode_embedding(record: dict) -> bytes | None:
    """Decode a base64 embedding blob from an export record.

    Tolerates a missing or malformed value (returns None) so one bad embedding
    cannot raise mid-restore and abort the whole import (bug-016); check_health's
    null-embedding repair then re-embeds the row.
    """
    b64 = record.get("embedding_b64")
    if not b64:
        return None
    try:
        decoded = base64.b64decode(b64)
    except (ValueError, TypeError):
        return None
    # bug-061: reject a blob whose length is not a whole number of float32s at ingestion,
    # so a truncated/crafted embedding cannot be stored and later crash np.frombuffer in
    # calibration (and, via ensure_calibrated_on_startup, the server boot). check_health's
    # null-embedding repair re-embeds the row from content.
    if not decoded or len(decoded) % 4 != 0:
        return None
    return decoded


def _confine_io_path(path: str) -> str | None:
    """bug-054: validate export/import caller-supplied filesystem paths.

    Returns the path to use, or None if it must be rejected. When
    config.EXPORT_DIR is set the resolved realpath must stay within it (blocks
    traversal and absolute escapes into config/cron/dotfiles). When unset, only
    ``..`` traversal segments are rejected (backward-compatible for ad-hoc
    backups); the destructiveHint tool annotation makes the host confirm the write.

    bug-130: import uses the same confinement as export.
    """
    if not path:
        return None
    if config.EXPORT_DIR:
        root = os.path.realpath(config.EXPORT_DIR)
        real = os.path.realpath(path)
        if real == root or real.startswith(root + os.sep):
            return real
        return None
    if ".." in path.split(os.sep):
        return None
    return path


async def do_export_memories(agent_id: str, output_path: str, include_embeddings: bool = False) -> dict:
    """Export memories, episodes, and profiles to a JSONL file."""
    confined = _confine_io_path(output_path)
    if confined is None:
        return error_response(f"output_path rejected (path traversal or outside export dir): {output_path}")
    output_path = confined

    iso = isolation_where(agent_id=agent_id or None)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    exported_memories = 0
    exported_episodes = 0
    exported_profiles = 0

    # bug-091: write to a temp file and os.replace() on success. Opening the
    # destination directly with "w" destroyed the previous backup the moment the
    # export started, and a mid-export fault (disk full, process kill) left a
    # truncated JSONL that a later restore accepted as complete.
    # PID-suffixed so two processes exporting the same path cannot interleave
    # writes into one temp file (bug-091 hardening).
    tmp_path = f"{output_path}.tmp.{os.getpid()}"
    try:
        async with read_snapshot() as db:
            # bug-073/091: one private read transaction gives the COUNT header
            # and all three streamed bodies the same WAL snapshot. Rows are read
            # in bounded chunks instead of materialising the corpus in memory.
            counts = []
            for table in ("memories", "episodes", "profiles"):
                cur = await db.execute(
                    f"SELECT COUNT(*) FROM {table}{iso.where}", iso.params
                )
                row = await cur.fetchone()
                counts.append(row[0])
            memory_count, episode_count, profile_count = counts

            with open(tmp_path, "w", encoding="utf-8") as f:
                header = {
                    "_type": "header",
                    "version": "cpersona-export/1.0",
                    "agent_id": agent_id,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "memory_count": memory_count,
                    "episode_count": episode_count,
                    "has_profile": profile_count > 0,
                    # bug-110: an exact count, so the import can detect a file cut at
                    # the episode/profile boundary (has_profile alone cannot).
                    "profile_count": profile_count,
                }
                f.write(json.dumps(header, ensure_ascii=False) + "\n")

                # bug-016: carry the project_id / channel γ-isolation axes and the
                # locked flag through export. bug-092: carry recall stats too.
                cur = await db.execute(
                    "SELECT id, agent_id, msg_id, content, source, timestamp, metadata, embedding, created_at,"
                    " project_id, channel, locked, recall_count, last_recalled_at"
                    f" FROM memories{iso.where} ORDER BY id",
                    iso.params,
                )
                while rows := await cur.fetchmany(500):
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
                            "project_id": row[9],
                            "channel": row[10],
                            "locked": int(row[11]) if row[11] else 0,
                            "recall_count": int(row[12]) if row[12] else 0,
                            "last_recalled_at": row[13],
                        }
                        if include_embeddings and row[7]:
                            record["embedding_b64"] = base64.b64encode(row[7]).decode("ascii")
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported_memories += 1

                cur = await db.execute(
                    "SELECT id, agent_id, summary, keywords, start_time, end_time, embedding, created_at, resolved,"
                    " project_id, channel"
                    f" FROM episodes{iso.where} ORDER BY id",
                    iso.params,
                )
                while rows := await cur.fetchmany(500):
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
                            "project_id": row[9],
                            "channel": row[10],
                        }
                        if include_embeddings and row[6]:
                            record["embedding_b64"] = base64.b64encode(row[6]).decode("ascii")
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported_episodes += 1

                cur = await db.execute(
                    "SELECT agent_id, user_id, content, updated_at, project_id"
                    f" FROM profiles{iso.where} ORDER BY agent_id",
                    iso.params,
                )
                while rows := await cur.fetchmany(500):
                    for row in rows:
                        record = {
                            "_type": "profile",
                            "agent_id": row[0],
                            "user_id": row[1],
                            "content": row[2],
                            "updated_at": row[3],
                            "project_id": row[4],
                        }
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported_profiles += 1
        os.replace(tmp_path, output_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise

    return {
        "ok": True,
        "path": output_path,
        "memories": exported_memories,
        "episodes": exported_episodes,
        "profiles": exported_profiles,
    }


@dataclass
class _ImportTally:
    """The state one import run threads through its per-record handlers.

    `dry_run` lives here rather than travelling as a separate argument because
    the preview's real hazard is not the database. dry_run has two write targets
    and they are not equally defended:

        database       read seam + per-record guard   -> rolled back
        remote index   per-record guard, alone        -> nothing

    `remote_items` is shipped after the transaction closes (see the tail of
    do_import_memories), where no rollback reaches — so that one guard was all
    that stood between a preview and a live index write, and an earlier decision found
    it by mutating the guard away: the database stayed spotless and every
    DB-watching test in the suite stayed green.

    Routing the queue through `queue_remote` makes a preview structurally unable
    to publish, rather than leaving it to every future edit to remember the
    guard — which matters because bundling this state was itself the change that
    could most easily have dropped it.
    """

    dry_run: bool
    imported_memories: int = 0
    skipped_memories: int = 0
    imported_episodes: int = 0
    # bug-220: episodes are deduplicated now, so they have a skip count like memories —
    # a re-import that reports 0 imported / N skipped is the tool being idempotent, not
    # a failure.
    skipped_episodes: int = 0
    profile_updated: bool = False
    errors: list[str] = field(default_factory=list)
    # bug-091: track the header and the per-type record counts actually present in
    # the file, so a truncated backup (mid-export crash, partial copy) is rejected
    # instead of silently restoring a partial corpus with ok:true.
    file_header: dict | None = None
    file_memories: int = 0
    file_episodes: int = 0
    file_profiles: int = 0
    # bug-070: dry_run performs no INSERT OR IGNORE, so it cannot collide against a row
    # it "inserted" earlier IN THE SAME FILE — a duplicate later in the file was
    # over-counted as imported (a real run sees its own uncommitted row on the shared
    # connection and skips it). Track within-file identities on BOTH dedup axes (the v12
    # (agent_id,project_id,msg_id) and (agent_id,project_id,channel,content) UNIQUE
    # indexes) so the preview matches a real run. Populated only on the dry_run path.
    seen_msgid: set = field(default_factory=set)
    seen_content: set = field(default_factory=set)
    # bug-220: the episode axis of the same preview problem (bug-071's import twin).
    seen_episode: set = field(default_factory=set)
    # bug-132: queue only rows actually inserted, then sync after commit.
    remote_items: dict[str, list[dict]] = field(default_factory=dict)

    def queue_remote(self, agent_id: str, ref: str, text: str) -> None:
        """Queue a written row for the remote index. A preview queues nothing."""
        if self.dry_run:
            return
        self.remote_items.setdefault(agent_id, []).append({"id": ref, "text": text})


def _validate_import_preconditions(input_path: str, dry_run: bool) -> tuple[str | None, dict | None]:
    """Vet the request before any file or database work. Returns (path, error)."""
    # Snapshot once: a TTL boundary mid-loop must not leave a half-written corpus.
    # bug-079: only gate the WRITE path on no-persist (the bug-048 fix, applied to the
    # import twin). A dry_run=True preview is write-free — every INSERT/UPSERT is
    # guarded by `if not dry_run` and the dry_run path runs on the read seam — so
    # short-circuiting it into a fabricated all-zero response masks what a real import
    # would do and contradicts the "read tools unaffected" no-persist contract.
    if no_persist.is_paused() and not dry_run:
        return None, no_persist.make_skipped_response(
            {
                "ok": True,
                "dry_run": dry_run,
                "imported_memories": 0,
                "skipped_memories": 0,
                "imported_episodes": 0,
                "skipped_episodes": 0,
                "profile_updated": False,
            },
            "import_memories",
        )
    confined = _confine_io_path(input_path)
    if confined is None:
        return None, error_response(
            f"input_path rejected (path traversal or outside export dir): {input_path}"
        )
    # bug-130: reject oversized imports before opening or reading the file.
    # bug-241: the size guard only bounds a REGULAR file. os.path.getsize reports 0 for
    # a character device, a FIFO and most /proc entries, so the cap passed exactly the
    # inputs that have no size and the readlines() below ran unbounded (/dev/zero grows
    # a string until the allocator gives out). One stat answers both questions.
    try:
        st = os.stat(confined)
    except OSError:
        return None, error_response(f"File not found: {confined}")
    if not stat.S_ISREG(st.st_mode):
        return None, error_response(f"input_path is not a regular file: {confined}")
    if st.st_size > config.MAX_IMPORT_BYTES:
        return None, error_response(
            f"input file exceeds MAX_IMPORT_BYTES ({config.MAX_IMPORT_BYTES}): {confined}"
        )
    return confined, None


async def _import_memory_record(db, record: dict, aid: str, tally: _ImportTally, line_num: int) -> None:
    """Import one memory row, or account for why it was skipped.

    The two paths are asymmetric by necessity, not by accident: a real run lets
    the UNIQUE indexes do the deduplication and reads the outcome back from
    `rowcount`, while a preview has no INSERT to learn from and has to probe for
    the same collisions by hand. Keeping the previewed counts equal to a real
    run's is the whole contract (bug-056 / bug-070).

    bug-221: the content goes through the write path's own sanitiser first, like
    ``_import_profile_record`` does (bug-188 / an earlier decision). This file may have been
    produced by another DB, an older version or a hand edit, so import is the
    stricter of the two seams, not the looser one — a body that ``do_store``
    would refuse (empty after sanitisation) must not enter through the restore,
    and one it would cut must not enter longer than the cap it publishes.
    """
    content = record.get("content", "")
    if not content:
        tally.skipped_memories += 1
        return

    content, truncated = sanitize_content_with_flag(content)
    if not content:
        tally.skipped_memories += 1
        tally.errors.append(f"Line {line_num}: memory is empty after sanitisation; it was not imported")
        return
    if truncated:
        # The number is the text that was actually kept, not a second read of the
        # constant (an earlier decision): len(content) after a truncation IS the cap.
        tally.errors.append(
            f"Line {line_num}: memory content exceeded the {len(content)}-character cap "
            "and was truncated"
        )

    msg_id = record.get("msg_id", "")
    pid = record.get("project_id", "")
    chan = record.get("channel", "")

    # bug-044: dedup on the full identity (agent_id, project_id,
    # msg_id) — the same axes do_store and the idx_memories_dedup_msg_id
    # UNIQUE index use. A project-blind check would drop a legitimately
    # distinct cross-project memory (same msg_id, different project_id)
    # that INSERT OR IGNORE against the composite index would accept.
    if msg_id:
        existing = await db.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = ? AND project_id = ? AND msg_id = ? LIMIT 1",
            (aid, pid, msg_id),
        )
        if existing or (tally.dry_run and (aid, pid, msg_id) in tally.seen_msgid):
            tally.skipped_memories += 1
            return

    if not tally.dry_run:
        source = json.dumps(record.get("source", {}))
        timestamp = record.get("timestamp", "")
        metadata = json.dumps(record.get("metadata", {}))
        # bug-016: carry the project_id / channel γ-axes, the locked
        # flag and the embedding through, and INSERT OR IGNORE so a
        # collision with the v12 dedup UNIQUE index is a counted skip
        # rather than an uncaught IntegrityError that aborts the restore.
        # bug-092: created_at and the recall stats ride along too — the
        # old INSERT re-stamped every restored row with import time and
        # zeroed its recall-frequency boost.
        cur = await db.execute(
            "INSERT OR IGNORE INTO memories"
            " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata,"
            "  embedding, locked, recall_count, last_recalled_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (
                aid,
                pid,
                chan,
                msg_id,
                content,
                source,
                timestamp,
                metadata,
                _decode_embedding(record),
                1 if record.get("locked") else 0,
                int(record.get("recall_count") or 0),
                record.get("last_recalled_at"),
                record.get("created_at"),
            ),
        )
        if cur.rowcount == 0:
            tally.skipped_memories += 1
            return
        tally.queue_remote(aid, f"mem:{cur.lastrowid}", content)
    else:
        # bug-056: in dry_run the INSERT OR IGNORE rowcount==0 skip
        # never runs, so a content-UNIQUE-index collision (empty msg_id,
        # same agent/project/channel/content, or a repeat within the
        # file) would be over-counted as an import. Replicate the
        # content-uniqueness probe so the previewed imported/skipped
        # counts match a real run.
        dup = await db.execute_fetchall(
            "SELECT 1 FROM memories WHERE agent_id = ? AND project_id = ? AND channel = ? AND content = ? LIMIT 1",
            (aid, pid, chan, content),
        )
        if dup or (aid, pid, chan, content) in tally.seen_content:
            tally.skipped_memories += 1
            return
        # bug-070: record this record's identities so a later duplicate in
        # the same file is previewed as skipped, matching the real run.
        if msg_id:
            tally.seen_msgid.add((aid, pid, msg_id))
        tally.seen_content.add((aid, pid, chan, content))
    tally.imported_memories += 1


async def _import_episode_record(db, record: dict, aid: str, tally: _ImportTally, line_num: int) -> None:
    """Import one episode row, or account for why it was skipped.

    bug-220: episodes carry no uniqueness index, so this probe — the same
    (agent_id, project_id, channel, summary) identity ``_merge_episode_rows``
    uses (bug-076) — is the only dedup gate there is. Without it a re-import
    multiplied every episode while the tool advertised ``idempotentHint=True``,
    which is exactly what a host retrying a lost response acts on.

    bug-221: the summary goes through the write path's sanitiser first, like the
    memory and profile records above it.
    """
    summary = record.get("summary", "")
    if not summary:
        return

    summary, truncated = sanitize_content_with_flag(summary)
    if not summary:
        tally.skipped_episodes += 1
        tally.errors.append(f"Line {line_num}: episode summary is empty after sanitisation; it was not imported")
        return
    if truncated:
        tally.errors.append(
            f"Line {line_num}: episode summary exceeded the {len(summary)}-character cap "
            "and was truncated"
        )

    # bug-094: coerce field types on BOTH paths — a JSON-array
    # keywords value (the natural hand-authored format) reached
    # db.execute as a Python list and aborted the whole import
    # with an InterfaceError, while the same file previewed
    # cleanly under dry_run (the binding was write-path only).
    keywords = record.get("keywords", "")
    if isinstance(keywords, list):
        keywords = " ".join(str(k) for k in keywords)
    else:
        keywords = str(keywords or "")

    ep_pid = record.get("project_id", "")
    ep_chan = record.get("channel", "")
    # bug-220: dedup against what is already stored. The dry_run arm carries the
    # within-file set for the same reason the memory path does (bug-070): a
    # preview has no INSERT of its own to collide against, while a real run sees
    # its own uncommitted rows on the shared connection.
    existing = await db.execute_fetchall(
        "SELECT id FROM episodes WHERE agent_id = ? AND project_id = ? AND channel = ?"
        " AND summary = ? LIMIT 1",
        (aid, ep_pid, ep_chan, summary),
    )
    if existing or (tally.dry_run and (aid, ep_pid, ep_chan, summary) in tally.seen_episode):
        tally.skipped_episodes += 1
        return

    if not tally.dry_run:
        cur = await db.execute(
            "INSERT INTO episodes"
            " (agent_id, project_id, channel, summary, keywords, start_time, end_time, resolved,"
            "  embedding, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (
                aid,
                ep_pid,
                ep_chan,
                summary,
                keywords,
                record.get("start_time"),
                record.get("end_time"),
                1 if record.get("resolved") else 0,
                _decode_embedding(record),
                record.get("created_at"),
            ),
        )
        tally.queue_remote(aid, f"ep:{cur.lastrowid}", summary)
    else:
        tally.seen_episode.add((aid, ep_pid, ep_chan, summary))
    tally.imported_episodes += 1


async def _import_profile_record(db, record: dict, aid: str, tally: _ImportTally, line_num: int) -> None:
    """Upsert one profile row, keyed on (agent_id, user_id).

    bug-188's second call site. That fix bounded the profile write — sanitiser,
    cap, and a real emptiness test instead of raw truthiness — but only in
    ``do_update_profile``; the commit described both writers as going "through
    store's seam" while this one still had the original three defects. So a
    whitespace-only profile in an import file kept doing what the write path no
    longer allowed: overwriting a useful profile with blanks, through an
    ON CONFLICT DO UPDATE, and reporting ``profile_updated: true``. Destruction
    reported as success, and no health check looks for it — ``check_empty_content``
    covers memories only.

    Import is the stricter of the two paths, not the looser one: this content
    arrives from a file that may have been produced by another DB, an older
    version, or a hand edit, and the row it lands on is one the operator already
    has. Both outcomes are now reported per line rather than inferred from a
    silent counter — a skip says the existing profile was kept, a truncation
    says the stored text is shorter than the file's.

    The third writer, ``_merge_profile_rows``, is deliberately left verbatim: it
    copies rows that were already bounded on their own way in, and it never
    overwrites — a target that already has a profile is skipped — so neither
    defect this fixes can occur there.
    """
    content = record.get("content", "")
    if not content:
        tally.errors.append(f"Line {line_num}: profile has no content; the existing profile was kept")
        return

    content, truncated = sanitize_profile_with_flag(content)
    if not content:
        tally.errors.append(
            f"Line {line_num}: profile is empty after sanitisation; the existing profile was kept"
        )
        return
    if truncated:
        # an earlier decision: the number comes from the text that was actually kept, not
        # from a second read of the constant. A message that quotes its own cap
        # can disagree with the cut; len(content) after a truncation IS the cap.
        tally.errors.append(
            f"Line {line_num}: profile exceeded the {len(content)}-character profile cap "
            "and was truncated"
        )

    if not tally.dry_run:
        # bug-223: carry the record's own updated_at, as memories and episodes carry
        # created_at (bug-092). A restore that re-stamps every profile with import time
        # disarms deep_stale_profile — its sole input — for _STALE_PROFILE_DAYS.
        # `excluded.updated_at` on the conflict path IS this bound value.
        await db.execute(
            "INSERT INTO profiles (agent_id, project_id, user_id, content, updated_at)"
            " VALUES (?, ?, ?, ?, COALESCE(?, datetime('now')))"
            " ON CONFLICT(agent_id, user_id) DO UPDATE SET"
            "   content = excluded.content,"
            "   updated_at = excluded.updated_at",
            (
                aid,
                record.get("project_id", ""),
                record.get("user_id", ""),
                content,
                record.get("updated_at"),
            ),
        )
    tally.profile_updated = True


def _validate_file_header(tally: _ImportTally) -> None:
    """Check the body against the header's own declared counts.

    bug-091: a truncated export (or a torn last line, already collected in
    `errors`) shows up as a count shortfall. The caller runs this INSIDE the
    transaction scope so the raise rolls a real run back rather than committing a
    partial restore.
    """
    if tally.file_header is None:
        return

    declared_mem = tally.file_header.get("memory_count")
    declared_ep = tally.file_header.get("episode_count")
    # bug-110: profiles are validated too — a file cut exactly at the
    # episode/profile boundary passed the two-count check and restored
    # a profile-less corpus with ok:true.
    declared_prof = tally.file_header.get("profile_count")
    if (
        (isinstance(declared_mem, int) and declared_mem != tally.file_memories)
        or (isinstance(declared_ep, int) and declared_ep != tally.file_episodes)
        or (isinstance(declared_prof, int) and declared_prof != tally.file_profiles)
    ):
        raise ValueError(
            f"file truncated or inconsistent: header declares "
            f"{declared_mem} memories / {declared_ep} episodes / "
            f"{declared_prof} profiles, file contains "
            f"{tally.file_memories} / {tally.file_episodes} / {tally.file_profiles}"
        )


async def do_import_memories(input_path: str, target_agent_id: str = "", dry_run: bool = False) -> dict:
    """Import memories, episodes, and profiles from a JSONL file."""
    input_path, error = _validate_import_preconditions(input_path, dry_run)
    if error is not None:
        return error

    tally = _ImportTally(dry_run=dry_run)

    # bug-016/bug-042: the whole restore runs inside one transaction() — the lock is
    # held across [first INSERT … commit/rollback] so no concurrent committer can
    # flush this import's partial rows at an await point, and an unexpected fault
    # auto-rolls-back instead of leaving a half-written corpus on the shared
    # connection. dry_run does no writes, so it runs on the read seam.
    # bug-102: read the input file BEFORE entering the write seam — blocking file
    # I/O inside transaction() stalls the event loop (and with it every reader)
    # for as long as the disk takes, while the write lock is held.
    try:
        # bug-241 (residual): the st_size gate in _validate_import_preconditions
        # raced this read — a regular file that grew in between could still be
        # slurped whole. Bound the read itself: one byte past the cap is enough
        # to convict, so the guard holds however the file got its size.
        with open(input_path, "rb") as f:
            raw = f.read(config.MAX_IMPORT_BYTES + 1)
        if len(raw) > config.MAX_IMPORT_BYTES:
            return error_response(
                f"input file exceeds MAX_IMPORT_BYTES ({config.MAX_IMPORT_BYTES}): {input_path}"
            )
        # split('\n') rather than splitlines(): exports are '\n'-separated, and
        # splitlines() would also cut on U+2028/U+2029, which are legal unescaped
        # inside a JSON string. Trailing '\r' is removed by the strip() below.
        lines = raw.decode("utf-8").split("\n")
    # bug-242: a file that is not valid UTF-8 fails on CONTENT, not on the OS call —
    # UnicodeDecodeError derives from ValueError, so it escaped this handler entirely
    # and the registry wrapper answered a bare {"error": ...} with no `ok` and none of
    # the tally keys. Every import refusal returns the one documented shape.
    except (OSError, UnicodeDecodeError) as e:
        return error_response(f"could not read {input_path}: {e}")

    try:
        async with (connection() if dry_run else transaction()) as db:
            for line_num, line in enumerate(lines, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    tally.errors.append(f"Line {line_num}: invalid JSON: {e}")
                    continue

                rtype = record.get("_type", "")

                if rtype == "header":
                    tally.file_header = record
                    continue

                elif rtype == "memory":
                    # Counted before the agent_id check on purpose: this is the
                    # counter _validate_file_header compares against, so it must
                    # count records PRESENT in the file, not records accepted.
                    tally.file_memories += 1
                    aid = target_agent_id or record.get("agent_id", "")
                    if not aid:
                        tally.errors.append(f"Line {line_num}: memory missing agent_id")
                        continue
                    await _import_memory_record(db, record, aid, tally, line_num)

                elif rtype == "episode":
                    tally.file_episodes += 1
                    aid = target_agent_id or record.get("agent_id", "")
                    if not aid:
                        tally.errors.append(f"Line {line_num}: episode missing agent_id")
                        continue
                    await _import_episode_record(db, record, aid, tally, line_num)

                elif rtype == "profile":
                    tally.file_profiles += 1
                    aid = target_agent_id or record.get("agent_id", "")
                    if not aid:
                        tally.errors.append(f"Line {line_num}: profile missing agent_id")
                        continue
                    await _import_profile_record(db, record, aid, tally, line_num)

                else:
                    if rtype:
                        tally.errors.append(f"Line {line_num}: unknown type '{rtype}'")

            # Inside the transaction scope: the raise must roll a real run back
            # rather than commit a partial restore (bug-091).
            _validate_file_header(tally)

    except Exception as e:
        # bug-110: keep the per-line diagnostics (a torn last line lands in
        # `errors` AND triggers the count mismatch — the operator needs both),
        # and do not claim a rollback on the write-free dry_run path.
        aborted = "aborted (dry run — nothing was written)" if dry_run else "aborted and rolled back"
        result = {
            "ok": False,
            "error": f"import {aborted}: {e}",
            "dry_run": dry_run,
            "imported_memories": 0,
            "skipped_memories": tally.skipped_memories,
            "imported_episodes": 0,
            "skipped_episodes": tally.skipped_episodes,
            "profile_updated": False,
        }
        if tally.errors:
            result["errors"] = tally.errors
        return result

    # Outside the transaction, so nothing here is covered by its rollback. A
    # preview reaches this loop with an empty queue by construction — see
    # _ImportTally.queue_remote.
    for aid, items in tally.remote_items.items():
        await vector.remote_index_upsert(aid, items)

    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "imported_memories": tally.imported_memories,
        "skipped_memories": tally.skipped_memories,
        "imported_episodes": tally.imported_episodes,
        "skipped_episodes": tally.skipped_episodes,
        "profile_updated": tally.profile_updated,
    }
    if tally.errors:
        result["errors"] = tally.errors
    return result


@dataclass
class _MergeTally:
    """The state one merge threads through its three row-copy passes.

    `dry_run` lives here for the same reason it does in _ImportTally: the remote
    index write at the tail of do_merge_memories runs outside the transaction,
    so the per-row guard is the only thing standing between a preview and a live
    index write. `queue_remote` makes a preview structurally unable to publish.
    """

    dry_run: bool
    merged_memories: int = 0
    skipped_memories: int = 0
    merged_episodes: int = 0
    skipped_episodes: int = 0
    profile_copied: bool = False
    skipped_profile: bool = False
    # bug-071: episodes have NO uniqueness constraint on summary and are inserted with a
    # bare INSERT, so intra-batch dedup relies on the target existing-check seeing the
    # real run's own uncommitted rows. dry_run inserts nothing, so two source episodes
    # with the same summary were both counted as merged. Track within-batch summaries so
    # the dry_run preview matches (memories don't need this — the source's own UNIQUE
    # indexes already make intra-batch content/msg_id collisions impossible).
    seen_summary: set = field(default_factory=set)
    # bug-218/bug-222: the SOURCE ids this merge actually copied. mode='move' deletes
    # exactly these, so a row the copy pass skipped — a profile the target already holds
    # under the same user_id (profiles have no content-equivalence key, unlike the
    # memory msg_id/content and episode summary probes), a locked memory whose content
    # collided — stays where it is instead of being wiped with the agent.
    copied_memory_ids: list[int] = field(default_factory=list)
    copied_episode_ids: list[int] = field(default_factory=list)
    copied_profile_ids: list[int] = field(default_factory=list)
    # bug-132: queue only rows actually inserted, then sync after commit.
    remote_items: list[dict] = field(default_factory=list)

    def queue_remote(self, ref: str, text: str) -> None:
        """Queue a written row for the remote index. A preview queues nothing."""
        if self.dry_run:
            return
        self.remote_items.append({"id": ref, "text": text})


async def _merge_memory_rows(db, source_agent_id: str, target_agent_id: str, tally: _MergeTally) -> None:
    """Copy the source agent's memory rows into the target, skipping collisions."""
    rows = await db.execute_fetchall(
        "SELECT id, project_id, msg_id, content, source, timestamp, metadata, channel, embedding, locked,"
        " created_at, recall_count, last_recalled_at"
        " FROM memories WHERE agent_id = ?",
        (source_agent_id,),
    )
    # bug-131: preserve the ranking metadata used by confidence and decay.
    # bug-222: the source id rides along so mode='move' can delete the copied rows
    # only — a skipped row (locked, or colliding on msg_id/content) is not ours to drop.
    for (
        src_id,
        project_id,
        msg_id,
        content,
        source,
        timestamp,
        metadata,
        channel,
        embedding,
        locked,
        created_at,
        recall_count,
        last_recalled_at,
    ) in rows:
        if not content:
            continue
        # bug-047: dedup against the target on the full identity (agent_id,
        # project_id, msg_id), matching the INSERT OR IGNORE's composite UNIQUE
        # index. A project-blind check drops a distinct source memory whose
        # msg_id collides with a target row in an unrelated project bucket.
        if msg_id:
            existing = await db.execute_fetchall(
                "SELECT id FROM memories WHERE agent_id = ? AND project_id = ? AND msg_id = ? LIMIT 1",
                (target_agent_id, project_id, msg_id),
            )
            if existing:
                tally.skipped_memories += 1
                continue
        if not tally.dry_run:
            cur = await db.execute(
                "INSERT OR IGNORE INTO memories"
                " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata, embedding,"
                "  locked, created_at, recall_count, last_recalled_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target_agent_id,
                    project_id,
                    channel,
                    msg_id,
                    content,
                    source,
                    timestamp,
                    metadata,
                    embedding,
                    locked,
                    created_at,
                    recall_count,
                    last_recalled_at,
                ),
            )
            if cur.rowcount == 0:
                tally.skipped_memories += 1
                continue
            tally.copied_memory_ids.append(src_id)
            tally.queue_remote(f"mem:{cur.lastrowid}", content)
        else:
            # bug-057: in dry_run the INSERT OR IGNORE rowcount==0 skip never
            # runs, so a content-UNIQUE-index collision (source content already
            # in the target under a different/empty msg_id) would be over-counted
            # as a merge. Replicate the content-uniqueness probe so the preview
            # counts equal a real merge.
            dup = await db.execute_fetchall(
                "SELECT 1 FROM memories WHERE agent_id = ? AND project_id = ? AND channel = ? AND content = ? LIMIT 1",
                (target_agent_id, project_id, channel, content),
            )
            if dup:
                tally.skipped_memories += 1
                continue
        tally.merged_memories += 1


async def _merge_episode_rows(db, source_agent_id: str, target_agent_id: str, tally: _MergeTally) -> None:
    """Copy the source agent's episodes. No uniqueness index — the probe is the gate."""
    # bug-219: created_at is carried through, like _merge_memory_rows carries it
    # (bug-131) and the import path does (bug-092). Re-stamping a merged episode with
    # merge time moves the episode-boundary timestamp _get_episode_boundary_ts derives
    # from MAX(created_at) — every memory the target already held then scores as
    # prior-session — and becomes the episode's own recall timestamp whenever
    # start_time is NULL (bug-213).
    rows = await db.execute_fetchall(
        "SELECT id, summary, keywords, start_time, end_time, resolved, project_id, channel, embedding,"
        " created_at"
        " FROM episodes WHERE agent_id = ?",
        (source_agent_id,),
    )
    for (
        src_id,
        summary,
        keywords,
        start_time,
        end_time,
        resolved,
        ep_project_id,
        ep_channel,
        ep_embedding,
        created_at,
    ) in rows:
        if not summary:
            continue
        # bug-076: scope the episode dedup probe by the γ isolation axes, exactly
        # like the memory probes above (bug-047/057). Episodes have NO uniqueness
        # constraint, so this pre-check is the only dedup gate — a summary-only
        # probe skipped a legitimately distinct episode whenever the target held
        # the same summary text under ANY other project/channel bucket, and
        # mode='move' then deleted it with the source agent (permanent
        # cross-project data loss). The dry_run seen_summary key carries the same
        # axes so the preview counts match a real run.
        existing = await db.execute_fetchall(
            "SELECT id FROM episodes WHERE agent_id = ? AND project_id = ? AND channel = ?"
            " AND summary = ? LIMIT 1",
            (target_agent_id, ep_project_id, ep_channel, summary),
        )
        if existing or (
            tally.dry_run and (target_agent_id, ep_project_id, ep_channel, summary) in tally.seen_summary
        ):
            tally.skipped_episodes += 1
            continue
        if not tally.dry_run:
            cur = await db.execute(
                "INSERT INTO episodes"
                " (agent_id, project_id, channel, summary, keywords, start_time, end_time, resolved,"
                "  embedding, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
                (
                    target_agent_id,
                    ep_project_id,
                    ep_channel,
                    summary,
                    keywords,
                    start_time,
                    end_time,
                    resolved,
                    ep_embedding,
                    created_at,
                ),
            )
            tally.copied_episode_ids.append(src_id)
            tally.queue_remote(f"ep:{cur.lastrowid}", summary)
        else:
            tally.seen_summary.add((target_agent_id, ep_project_id, ep_channel, summary))
        tally.merged_episodes += 1


async def _merge_profile_rows(db, source_agent_id: str, target_agent_id: str, tally: _MergeTally) -> None:
    """Copy the source agent's profiles, leaving any the target already has.

    Verbatim on purpose (bug-188's third call site, checked and exempt): every
    row here was bounded by the write path that stored it, the copy never
    overwrites — a target that already has the profile is skipped, above — and
    re-sanitising an intra-DB move would silently rewrite content the operator
    did not edit. The import path, which takes content from outside this DB and
    does overwrite, goes through the seam instead.
    """
    rows = await db.execute_fetchall(
        "SELECT id, user_id, content, project_id FROM profiles WHERE agent_id = ?",
        (source_agent_id,),
    )
    for src_id, user_id, content, prof_project_id in rows:
        if not content:
            continue
        # bug-218: this probe is pure existence on (agent_id, user_id) — profiles have
        # no equivalence key, so "the target already has one" says nothing about the two
        # documents being the same. A skipped source profile therefore exists nowhere
        # else, and mode='move' must leave it where it is (_delete_merged_source_rows).
        existing = await db.execute_fetchall(
            "SELECT id FROM profiles WHERE agent_id = ? AND user_id = ? LIMIT 1",
            (target_agent_id, user_id),
        )
        if existing:
            tally.skipped_profile = True
            continue
        if not tally.dry_run:
            await db.execute(
                "INSERT INTO profiles (agent_id, project_id, user_id, content, updated_at)"
                " VALUES (?, ?, ?, ?, datetime('now'))",
                (target_agent_id, prof_project_id, user_id, content),
            )
            tally.copied_profile_ids.append(src_id)
        tally.profile_copied = True


# One statement stays well under SQLite's bound-variable ceiling (999 on older
# builds) with the agent_id parameter riding along. The ids are BOUND into the
# ``{ph}`` placeholders and never interpolated (the vector._fetch_rows_by_id
# convention).
_MOVE_DELETE_CHUNK = 500

_MOVE_DELETE_SQL = (
    ("deleted_memories", "DELETE FROM memories WHERE agent_id = ? AND id IN ({ph})"),
    ("deleted_profiles", "DELETE FROM profiles WHERE agent_id = ? AND id IN ({ph})"),
    ("deleted_episodes", "DELETE FROM episodes WHERE agent_id = ? AND id IN ({ph})"),
)

_MOVE_LEFT_SQL = (
    ("memories", "SELECT COUNT(*) FROM memories WHERE agent_id = ?"),
    ("episodes", "SELECT COUNT(*) FROM episodes WHERE agent_id = ?"),
    ("profiles", "SELECT COUNT(*) FROM profiles WHERE agent_id = ?"),
)


async def _delete_merged_source_rows(db, source_agent_id: str, tally: _MergeTally) -> tuple[dict, dict]:
    """Move phase: remove exactly the rows this merge copied (bug-218 / bug-222).

    A move used to hand the whole source agent to ``_delete_agent_rows``, which wiped
    rows the copy pass had SKIPPED — and a skip is not evidence that the data survives
    in the target. The profile probe is pure existence on (agent_id, user_id), so two
    entirely different documents count as a collision; the memory probes carry no
    ``locked = 0`` guard, so a memory the user locked against deletion was dropped while
    the surviving target row (written by some earlier, unrelated path) may be unlocked.
    Both destroyed data that had been copied nowhere, under ok:true.

    So the delete is keyed on the ids that were actually inserted. Whatever stays is
    reported as ``left_at_source``, counted from the tables rather than from the skip
    tallies so rows the copy pass never considered (empty content) are visible too.

    The crash-recovery queue is per agent, not per row (bug-093): it is drained only
    when the move leaves the source with nothing at all — the wiped-agent case that
    guard was written for.
    """
    ids_for = {
        "deleted_memories": tally.copied_memory_ids,
        "deleted_profiles": tally.copied_profile_ids,
        "deleted_episodes": tally.copied_episode_ids,
    }
    deleted: dict = {}
    for key, sql in _MOVE_DELETE_SQL:
        ids = ids_for[key]
        count = 0
        for start in range(0, len(ids), _MOVE_DELETE_CHUNK):
            chunk = ids[start : start + _MOVE_DELETE_CHUNK]
            cur = await db.execute(
                sql.replace("{ph}", ",".join("?" * len(chunk))),
                (source_agent_id, *chunk),
            )
            count += cur.rowcount
        deleted[key] = count

    left = {}
    for kind, sql in _MOVE_LEFT_SQL:
        rows = await db.execute_fetchall(sql, (source_agent_id,))
        left[kind] = rows[0][0]

    if any(left.values()):
        deleted["deleted_pending_tasks"] = 0
    else:
        cur = await db.execute(
            "DELETE FROM pending_memory_tasks WHERE agent_id = ?", (source_agent_id,)
        )
        deleted["deleted_pending_tasks"] = cur.rowcount
    return deleted, left


def _validate_merge_arguments(source_agent_id: str, target_agent_id: str, strategy: str, mode: str) -> dict | None:
    """Return an error response if the request is not a merge we can perform."""
    if not source_agent_id:
        return error_response("source_agent_id is required")
    if not target_agent_id:
        return error_response("target_agent_id is required")
    if source_agent_id == target_agent_id:
        return error_response("source_agent_id and target_agent_id must differ")
    if strategy != "skip":
        return error_response(f"Unsupported strategy '{strategy}'. Currently supported: 'skip'")
    if mode not in ("copy", "move"):
        return error_response(f"Invalid mode '{mode}'. Supported: 'copy', 'move'")
    return None


async def do_merge_memories(
    source_agent_id: str,
    target_agent_id: str,
    strategy: str = "skip",
    mode: str = "copy",
    dry_run: bool = False,
) -> dict:
    """Merge memories, episodes, and profiles from one agent into another."""
    # Snapshot once: a TTL boundary mid-loop must not leave a half-written corpus.
    # bug-048: only gate the WRITE path on no-persist. A dry_run=True preview is
    # write-free (every mutation below is guarded by `if not dry_run`), so short-
    # circuiting it into a fabricated all-zero no-op masks what a real merge would
    # do and contradicts the "read tools unaffected" no-persist contract.
    if no_persist.is_paused() and not dry_run:
        return no_persist.make_skipped_response(
            {
                "ok": True,
                "dry_run": dry_run,
                # bug-111 sibling: mirror the real success shape (echo keys too).
                "source_agent_id": source_agent_id,
                "target_agent_id": target_agent_id,
                "strategy": strategy,
                "mode": mode,
                "merged_memories": 0,
                "skipped_memories": 0,
                "merged_episodes": 0,
                "skipped_episodes": 0,
                "profile_copied": False,
                "skipped_profile": False,
            },
            "merge_memories",
        )
    invalid = _validate_merge_arguments(source_agent_id, target_agent_id, strategy, mode)
    if invalid is not None:
        return invalid

    tally = _MergeTally(dry_run=dry_run)

    # bug-020/022: whole merge in one transaction, carrying the project_id /
    # channel / locked axes + embedding and using INSERT OR IGNORE so a content
    # collision is a counted skip (honouring strategy='skip') instead of an
    # uncaught IntegrityError that half-merges the corpus.
    # bug-043: transaction() holds the shared write lock for the whole merge so no
    # concurrent committer can flush its partial rows (and vice versa), commits at
    # exit and auto-rolls-back on fault. dry_run does no writes → read seam.
    try:
        async with (connection() if dry_run else transaction()) as db:
            await _merge_memory_rows(db, source_agent_id, target_agent_id, tally)
            await _merge_episode_rows(db, source_agent_id, target_agent_id, tally)
            await _merge_profile_rows(db, source_agent_id, target_agent_id, tally)

            move_counts = None
            left_at_source = None
            if mode == "move" and not dry_run:
                # bug-218/bug-222: a move deletes what it copied, not the agent.
                move_counts, left_at_source = await _delete_merged_source_rows(
                    db, source_agent_id, tally
                )

    except Exception as e:
        return {
            "ok": False,
            "error": f"merge aborted and rolled back: {e}",
            "dry_run": dry_run,
            "source_agent_id": source_agent_id,
            "target_agent_id": target_agent_id,
            "merged_memories": 0,
            "skipped_memories": tally.skipped_memories,
            "merged_episodes": 0,
            "skipped_episodes": tally.skipped_episodes,
            "profile_copied": False,
            "skipped_profile": tally.skipped_profile,
        }

    # The delete happened inside the merge transaction (bug-088); only the
    # non-DB side effects (calibration purge, remote namespace) run here,
    # post-commit, mirroring do_delete_agent_data's ordering.
    # Outside the transaction: no rollback covers this. A preview arrives with
    # an empty queue by construction — see _MergeTally.queue_remote.
    await vector.remote_index_upsert(target_agent_id, tally.remote_items)
    move_result = None
    if move_counts is not None:
        # bug-218/bug-222: the agent-level teardown (its calibration, its remote
        # namespace) belongs to a move that emptied the source. With rows left behind,
        # the agent is still live — purging would de-index memories it still holds.
        if not any(left_at_source.values()):
            _purge_agent_calibration(source_agent_id)
            await _purge_agent_remote_namespace(source_agent_id)
        move_result = {"ok": True, "agent_id": source_agent_id, **move_counts}

    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "source_agent_id": source_agent_id,
        "target_agent_id": target_agent_id,
        "strategy": strategy,
        "mode": mode,
        "merged_memories": tally.merged_memories,
        "skipped_memories": tally.skipped_memories,
        "merged_episodes": tally.merged_episodes,
        "skipped_episodes": tally.skipped_episodes,
        "profile_copied": tally.profile_copied,
        "skipped_profile": tally.skipped_profile,
    }
    if move_result:
        result["source_deleted"] = move_result
        # bug-218/bug-222: the residue is part of the answer — a caller that reads
        # "moved" as "the source is empty now" must be able to see otherwise.
        result["left_at_source"] = left_at_source

    logger.info(
        "Merge %s → %s (%s, %s): %d memories (+%d skipped), %d episodes (+%d skipped), profile=%s%s",
        source_agent_id,
        target_agent_id,
        strategy,
        mode,
        tally.merged_memories,
        tally.skipped_memories,
        tally.merged_episodes,
        tally.skipped_episodes,
        "copied" if tally.profile_copied else ("skipped" if tally.skipped_profile else "none"),
        " [DRY RUN]" if dry_run else "",
    )
    return result


async def do_get_queue_status() -> dict:
    """Get the status of the background task queue."""
    if tasks._task_queue and TASK_QUEUE_ENABLED:
        return await tasks._task_queue.get_status()
    return {"enabled": False, "pending": 0}
