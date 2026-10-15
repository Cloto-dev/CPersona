"""Administrative tool handlers for CPersona.

Tools: profile (get/update), list, delete, update, lock/unlock, agent data wipe,
threshold calibration, episode delete, export/import, merge, queue status.

Accesses `vector._embedding_client` (remote vector index sync) and
`tasks._task_queue` (queue status) as module attributes.
"""

import base64
import contextlib
import glob
import hashlib
import json
import logging
import math
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from cpersona._vendored_mcp_common.embedding_client import redact_urls
from cpersona.isolation import isolation_where

from cpersona import acl
from cpersona import associations
from cpersona import blocks
from cpersona import config
from cpersona import fileperms
from cpersona import generation
from cpersona import nodes
from cpersona import session
from cpersona import tasks
from cpersona import vector
from cpersona import vector_index
from cpersona.session import resolve_session_key
from cpersona.config import (
    CALIBRATE_FLOOR,
    CALIBRATE_MAX_SAMPLE,
    CALIBRATE_METHOD,
    CALIBRATE_METHODS,
    CALIBRATE_PERCENTILE,
    CALIBRATE_SAMPLE_SIZE,
    CALIBRATE_TEMPORAL_WINDOW_MIN,
    CALIBRATE_Z_FACTOR,
    REMOTE_INDEX_TIMEOUT_SECS,
    STORE_BLOB,
    local_blobs_stored,
    TASK_MAX_RETRIES,
    TASK_QUEUE_ENABLED,
    TASK_RETRY_DELAY,
    VECTOR_SEARCH_MODE,
)
from cpersona.database import background_connection, connection, read_snapshot, transaction
from cpersona.utils import (
    _clamp_limit,
    _parse_timestamp_utc,
    content_cap,
    error_response,
    fits_sqlite_integer,
    future_timestamp_issue,
    json_dumps_finite,
    profile_cap,
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


async def do_update_profile(agent_id: str, profile: str = "", session_key: str = "") -> dict:
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

    The seam is shared, the ceiling is not. This path caps at
    MAX_PROFILE_LENGTH, which is the row's only bound because the recall
    injection never preview-trims it.
    """
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "profiles_updated": 0}, "update_profile", key
        )

    # bug-392: both branches answered ok:true. docs/behavior-contracts.md section 10
    # tells every caller to branch on `ok is false`, and `store` — the seam this
    # path explicitly shares, and whose boundary states it — calls these same two
    # inputs (empty, and text that sanitises away) a REJECTION rather than a skip.
    # Answering true here meant a caller following the published rule recorded a
    # stale profile as saved, and kept injecting it into every recall. The pause
    # branch above stays true: that one is a skip, and nothing is wrong with it.
    if not profile:
        return {"ok": False, "profiles_updated": 0, "reason": "empty profile"}

    profile, truncated = sanitize_profile_with_flag(profile)
    if not profile:
        return {"ok": False, "profiles_updated": 0, "reason": "empty after sanitization"}

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
# bug-385: the row ceiling each listing clamps the caller's `limit` to. It was two
# bare literals at the two call sites, published on no surface a caller can read,
# so a listing that stopped at the cap was indistinguishable from one that had
# reached the end of the data. Named here because it is also the basis of the
# character budgets below and, since bug-385, the number the two tool descriptions
# render — a cap written in three places is a cap that drifts in two of them.
LIST_MEMORIES_MAX_ROWS = 500
LIST_EPISODES_MAX_ROWS = 200

LIST_MEMORIES_MAX_CHARS = 1_000_000  # LIST_MEMORIES_MAX_ROWS x the old 2000-character cap
LIST_EPISODES_MAX_CHARS = 800_000  # LIST_EPISODES_MAX_ROWS x 2 text columns x the same cap


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
    # bug-385: a listing clamped to its row cap looked exactly like one that had
    # reached the end of the data -- "count < limit means the end" stops at the cap
    # either way. When the caller asked for more than the cap, one row past it is
    # read, only to learn whether the cap withheld anything; the response then
    # carries budget_rows. At or under the cap it is the caller's own limit that
    # cuts, which needs no marker, so nothing extra is read.
    wanted = _clamp_limit(limit, LIST_MEMORIES_MAX_ROWS)
    read = wanted + 1 if limit > LIST_MEMORIES_MAX_ROWS else wanted
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"SELECT id, agent_id, project_id, msg_id, content, source, timestamp, created_at, locked, channel "
            f"FROM memories{iso.where} ORDER BY created_at DESC LIMIT ?",
            (*iso.params, read),
        )
    rows_withheld = len(rows) > wanted
    rows = rows[:wanted]
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
    if rows_withheld:
        # bug-385: the row cap reported the way the character budget is -- absent
        # unless it withheld something, and naming the bound when it did.
        result["budget_rows"] = LIST_MEMORIES_MAX_ROWS
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
    # bug-385: see do_list_memories.
    wanted = _clamp_limit(limit, LIST_EPISODES_MAX_ROWS)
    read = wanted + 1 if limit > LIST_EPISODES_MAX_ROWS else wanted
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"SELECT id, agent_id, project_id, summary, keywords, start_time, end_time, created_at "
            f"FROM episodes{iso.where} ORDER BY created_at DESC LIMIT ?",
            (*iso.params, read),
        )
    rows_withheld = len(rows) > wanted
    rows = rows[:wanted]
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
    if rows_withheld:
        result["budget_rows"] = LIST_EPISODES_MAX_ROWS
    return result


async def _post_remote_side_effect(route: str, payload: dict, *, action: str, namespace: str) -> bool:
    """Post one index side effect, and say so when it does not land.

    bug-323: these four posts (delete, update, purge, episode delete) kept the
    shape the bulk push shed in bug-304 -- the response was never bound, so a
    service answering 500 or 401 counted as a success, and the only trace was a
    debug line that is off in any normal deployment. Measured across all four
    handlers against HTTP 500, HTTP 401 and a connection error: twelve of twelve
    committed locally, answered ok:true and warned about nothing.

    Still non-fatal, and deliberately so -- the local change is already
    committed and a failed index is not a failed delete. What changes is that
    the shortfall is said once, at a level that is on, because the state it
    leaves is the one that does not look like a fault: an orphaned remote vector
    keeps taking a top-K slot until the by-id rehydrate finds nothing, and a
    stale index entry keeps serving pre-edit text against a post-edit row.

    The post states the index deadline every other call to this service states.
    Without it each one inherited the embedding client's own request timeout
    (30 s by default), so a service that accepts connections and stops answering
    held every delete and edit three times longer than it holds a store.

    Returns True when the post timed out, so a caller posting in chunks can stop
    instead of spending the same deadline again on every remaining chunk.

    ``route`` is named rather than built by each caller, so every post derives it
    the one way ``vector.remote_route`` does, keeping the configured URL's query;
    with no route to derive there is nothing to post.
    """
    path = vector.remote_route(route)
    if path is None:
        return False
    try:
        response = await vector._embedding_client._client.post(
            path, json=payload, timeout=REMOTE_INDEX_TIMEOUT_SECS
        )
    except httpx.TimeoutException as e:
        logger.warning(
            "Remote index %s timed out after %ss for %s (non-fatal, local change already "
            "committed; the index is now out of step with the database): %s",
            action,
            REMOTE_INDEX_TIMEOUT_SECS,
            namespace,
            # The request URL carries the configured query, so it is redacted.
            redact_urls(str(e)),
        )
        return True
    except Exception as e:  # noqa: BLE001 - non-fatal by contract, reported by design
        # The request URL carries the configured query, so it is redacted.
        logger.warning(
            "Remote index %s failed for %s (non-fatal, local change already committed; "
            "the index is now out of step with the database): %s",
            action,
            namespace,
            redact_urls(str(e)),
        )
        return False
    # A non-2xx answer is a refusal the service chose to give; it raises nothing,
    # so without this it was indistinguishable from success. Read defensively: the
    # local change is already committed, and a client that hands back something
    # without a status must not turn a completed delete into an exception.
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        logger.warning(
            "Remote index %s refused for %s with HTTP %s (non-fatal, local change "
            "already committed; the index is now out of step with the database)",
            action,
            namespace,
            status,
        )
    return False


def _by_id_refusal(memory_id: int, agent_id: str, unscoped: str) -> dict:
    """Refuse a by-id write without saying which of the three reasons applied.

    bug-367: the by-id write handlers distinguished, in their error strings, a row
    that belongs to another agent, a row that is locked, and a row that does not
    exist. A client is authorised against the agent_id it sends and never learns
    which agent owns a row, so for a scoped caller those strings were the only
    boundary -- existence and lock state crossed it. The episode handlers and the
    delete path's own post-DML refusal already use one ambiguous message; this
    makes the pre-checks agree with them.

    Ambiguity is only owed to a caller that named an agent. An unscoped call
    (agent_id "") is the operator-side path with no ownership predicate to hide
    behind, so it keeps the specific message it has always had -- collapsing that
    one would remove diagnosis without closing any boundary.
    """
    if agent_id:
        return error_response(f"Memory {memory_id} not found, not owned by agent, or locked")
    return error_response(unscoped)


async def do_delete_memory(memory_id: int, agent_id: str = "", session_key: str = "") -> dict:
    """Delete a single memory by ID.

    When agent_id is provided (non-empty), enforces ownership.
    """
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "deleted_id": memory_id}, "delete_memory", key
        )
    _warn_if_unscoped("do_delete_memory", memory_id, agent_id)
    if not fits_sqlite_integer(memory_id):
        # No row has an id SQLite cannot hold, and binding it would raise.
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    # aiosqlite 0.22 has execute_fetchall but no execute_fetchone — using the
    # former avoids a silent AttributeError that previously broke every delete.
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT locked, agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    owner_agent_id = rows[0][1]
    # bug-367: the ownership test comes first, so a foreign row is never inspected
    # for its lock state on behalf of a caller that may not see it at all.
    if agent_id and owner_agent_id != agent_id:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    if rows[0][0]:
        return _by_id_refusal(
            memory_id, agent_id, f"Memory {memory_id} is locked and cannot be deleted"
        )

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
        await _post_remote_side_effect(
            "remove",
            {"namespace": ns, "ids": [f"mem:{memory_id}"]},
            action="removal",
            namespace=ns,
        )

    return {"ok": True, "deleted_id": memory_id}


async def _same_bucket_holder(db, memory_id: int, content: str) -> int | None:
    """The id of another row in this row's (agent, project, channel) holding ``content``.

    That bucket is exactly what the content unique index covers, so this is the
    row an UPDATE of ``memory_id`` to ``content`` would collide with.
    """
    bucket = await db.execute_fetchall(
        "SELECT agent_id, project_id, channel FROM memories WHERE id = ?", (memory_id,)
    )
    if not bucket:
        return None
    found = await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND project_id = ? AND channel = ?"
        " AND content = ? AND id != ? LIMIT 1",
        (*bucket[0], content, memory_id),
    )
    return found[0][0] if found else None


def _held_elsewhere_refusal(memory_id: int, holder_id: int) -> dict:
    # Named, because the holder is in the caller's own agent bucket and is the
    # row to edit or delete instead; the raw constraint error named only columns.
    return error_response(
        f"Memory {memory_id} was not edited: memory {holder_id} in the same agent, "
        "project and channel already holds this content, and one copy is kept per bucket"
    )


async def do_update_memory(
    memory_id: int, content: str, agent_id: str = "", session_key: str = ""
) -> dict:
    """Update memory content by ID. Rejects if memory is locked."""
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "updated_id": memory_id}, "update_memory", key
        )
    if not content or not content.strip():
        return error_response("Content cannot be empty")
    _warn_if_unscoped("do_update_memory", memory_id, agent_id)
    if not fits_sqlite_integer(memory_id):
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")

    async with connection() as db:
        rows = await db.execute_fetchall("SELECT locked, agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    row = rows[0]
    # bug-367: ownership first, then the lock -- and one message for a scoped caller.
    if agent_id and row[1] != agent_id:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    if row[0]:
        return _by_id_refusal(
            memory_id, agent_id, f"Memory {memory_id} is locked and cannot be edited"
        )

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

    # The content unique index keeps one copy of a text per bucket, so an edit into
    # text another row there holds cannot land. Asked before the embedding round
    # trip, which would otherwise be spent on a write that is then refused; the
    # UPDATE below still catches a copy that lands after this read.
    async with connection() as db:
        holder_id = await _same_bucket_holder(db, memory_id, content)
    if holder_id is not None:
        return _held_elsewhere_refusal(memory_id, holder_id)

    # bug-011: recompute the embedding for the new text with the same policy as
    # do_store. On failure (or no client) the BLOB is NULLed rather than left
    # stale, so recall stops matching the old wording and check_health's
    # null-embedding repair can re-embed the row later.
    embedding_blob = None
    # Rewritten with the blob, so a NULLed vector does not keep the old one's label.
    embedding_label = ""
    if vector._embedding_client and local_blobs_stored(VECTOR_SEARCH_MODE, STORE_BLOB):
        try:
            label = await generation.record_label()
            embeddings = await vector._embedding_client.embed([content])
            if embeddings:
                embedding_blob = vector.pack_for_storage(embeddings[0])
                embedding_label = label
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
    holder_id = None
    still_there = None
    async with transaction() as db:
        try:
            cursor = await db.execute(
                "UPDATE memories SET content = ?, embedding = ?, embedding_model = ? WHERE id = ? AND locked = 0",
                (content, embedding_blob, embedding_label, memory_id),
            )
        except sqlite3.IntegrityError:
            # A copy of this text landed in the bucket during the embedding await
            # (a concurrent store or edit). Answered as the refusal above, not as
            # a raw constraint error that carries no ok:false.
            holder_id = await _same_bucket_holder(db, memory_id, content)
            if holder_id is None:
                raise
        else:
            if cursor.rowcount == 0:
                # Read under the same write lock as the UPDATE: a lock_memory that
                # landed after the pre-check is not the only way to match nothing --
                # a delete or a merge-move may have removed the row, and calling
                # that "locked" sends the caller to unlock_memory for a row that is gone.
                still_there = await db.execute_fetchall(
                    "SELECT locked FROM memories WHERE id = ?", (memory_id,)
                )
    if holder_id is not None:
        return _held_elsewhere_refusal(memory_id, holder_id)
    if cursor.rowcount == 0:
        if still_there and still_there[0][0]:
            return _by_id_refusal(
                memory_id, agent_id, f"Memory {memory_id} is locked and cannot be edited"
            )
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")

    # Keep the remote vector entry in step with the new text (same
    # namespace/id scheme as do_store; non-fatal).
    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        await _post_remote_side_effect(
            "index",
            {
                "namespace": f"cpersona:{row[1]}",
                "items": [{"id": f"mem:{memory_id}", "text": content}],
            },
            action="update",
            namespace=f"cpersona:{row[1]}",
        )

    # `truncated` is additive and mirrors do_store's success shape (absent unless the
    # cap actually bit), so a caller that edits a too-long body learns the row it now
    # holds is a prefix instead of having to re-read it to find out.
    result = {"ok": True, "updated_id": memory_id}
    if truncated:
        result["truncated"] = True
    # Overflow tree (§4): the UPDATE's trigger removed the nodes of the old text; a
    # new text that runs past the window gets new ones queued.
    if await nodes.runs_past_window(content):
        queued = await nodes.queue_build("mem", memory_id, row[1], key)
        if queued:
            result["nodes"] = queued
    # Blocks (BLOCK_REACH_DESIGN.md §6): the UPDATE's trigger dropped the blocks of
    # the old text, because they quoted spans of something the record no longer
    # says. Queueing here is what keeps an edited record reachable by the block arm
    # without waiting for a backfill sweep to come round to it.
    if blocks.building_enabled():
        queued = await blocks.queue_build("mem", memory_id, row[1], key)
        if queued:
            result["blocks"] = queued
    return result


async def do_lock_memory(memory_id: int, agent_id: str = "", session_key: str = "") -> dict:
    """Lock a memory to prevent deletion and editing."""
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "locked_id": memory_id}, "lock_memory", key
        )
    _warn_if_unscoped("do_lock_memory", memory_id, agent_id)
    if not fits_sqlite_integer(memory_id):
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    if agent_id and rows[0][0] != agent_id:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")

    async with transaction() as db:  # bug-042/043: serialise write+commit
        cur = await db.execute("UPDATE memories SET locked = 1 WHERE id = ?", (memory_id,))
    if cur.rowcount == 0:
        # bug-099: the ownership pre-check and this UPDATE straddle an await — a
        # concurrent delete in between must not be acknowledged as a lock.
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    return {"ok": True, "locked_id": memory_id}


async def do_unlock_memory(memory_id: int, agent_id: str = "", session_key: str = "") -> dict:
    """Unlock a memory to allow deletion and editing."""
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "unlocked_id": memory_id}, "unlock_memory", key
        )
    _warn_if_unscoped("do_unlock_memory", memory_id, agent_id)
    if not fits_sqlite_integer(memory_id):
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
    if agent_id and rows[0][0] != agent_id:
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")

    async with transaction() as db:  # bug-042/043: serialise write+commit
        cur = await db.execute("UPDATE memories SET locked = 0 WHERE id = ?", (memory_id,))
    if cur.rowcount == 0:
        # bug-099: see do_lock_memory.
        return _by_id_refusal(memory_id, agent_id, f"Memory {memory_id} not found")
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
    # The declared graph (schema v15). Deleting the records above already took
    # their mentions and anchored relations through the triggers; what remains
    # is the agent's entities (their aliases and mentions follow by trigger)
    # and any relation that names no record at all — an agent-level assertion
    # only the agent axis can reach.
    await db.execute("DELETE FROM entities WHERE agent_id = ?", (agent_id,))
    await db.execute("DELETE FROM relations WHERE agent_id = ?", (agent_id,))
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
    # A calibration of this agent may be suspended in one of its awaits right now;
    # this tells it that what it is measuring was deleted, so it discards the result
    # instead of writing it back after this function has run.
    vector._note_agent_calibration_purge(agent_id)
    # This is the textbook deliberate removal, so claim it: the rewrite below can
    # fail (OSError -> _save_calibration_state returns False) and leave the agent
    # in the file, and without the claim the very next calibration would carry
    # those entries back out of it — resurrecting calibration for a corpus that
    # no longer exists, which is the bug-036 this function was written to close.
    vector._record_agent_calibration_change(agent_id, *vector._CALIBRATION_AGENT_AXES)
    state = _load_calibration_state()
    if state is not None:
        for _key in ("agent_thresholds", "agent_fused_gates", "agent_betas"):
            _sub = state.get(_key)
            if isinstance(_sub, dict):
                removed_cal = (_sub.pop(agent_id, None) is not None) or removed_cal
        if removed_cal:
            purged = _save_calibration_state(
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
                # The same reason for the time: deep_check ages every agent's
                # calibration from this one stamp, so restamping it here made a
                # months-old calibration of every other agent read as fresh.
                calibrated_at=state.get("calibrated_at"),
            )
            # The removal is on disk, so it is no longer pending: a later calibration
            # of a same-id agent by another process must not be deleted again here.
            if purged:
                for _axis in vector._CALIBRATION_AGENT_AXES:
                    vector._calibration_pending[_axis].discard(agent_id)
    return removed_cal


async def _remove_moved_source_vectors(source_agent_id: str, tally: "_MergeTally") -> None:
    """Drop the moved rows' vectors from the SOURCE namespace (network I/O, post-commit).

    bug-346: a move-mode merge deletes the copied rows from the database, and that is
    the third deleting path in this file. The other two -- the by-id memory delete and
    the by-id episode delete -- each post a ``/remove`` for the row they deleted. This
    one posted nothing, and the only source-side cleanup in the merge is the
    whole-namespace purge, which runs only when the move left the source with nothing
    at all. A single leftover row -- most commonly a profile the copy pass skipped
    because the target already had one -- skips the purge, and then every moved row's
    vector stays indexed against an agent that no longer holds it: the ghost keeps
    taking a top-K slot and rehydrates to nothing.

    The ids are the ones the move actually copied, which is the same list the delete is
    keyed on, so this removes exactly what left. Chunked at the bulk push's width and
    non-fatal for the same reason: the rows are already gone from the database, and a
    failed de-index is not a failed move -- it is said once, at a level that is on.
    """
    if not (
        VECTOR_SEARCH_MODE == "remote"
        and vector._embedding_client
        and vector._embedding_client._http_url
    ):
        return
    refs = [f"mem:{row_id}" for row_id in tally.copied_memory_ids]
    refs += [f"ep:{row_id}" for row_id in tally.copied_episode_ids]
    if not refs:
        return
    namespace = f"cpersona:{source_agent_id}"
    for start in range(0, len(refs), 128):
        timed_out = await _post_remote_side_effect(
            "remove",
            {"namespace": namespace, "ids": refs[start : start + 128]},
            action="removal",
            namespace=namespace,
        )
        if timed_out:
            # A service that let one chunk run out its deadline is not asked for the
            # next: the wait would multiply by the chunk count (a hundred chunks is a
            # hundred deadlines) for removals that most likely fail the same way. A
            # refusal or a refused connection answers at once, so those keep going.
            logger.warning(
                "Skipped the remaining remote removals for %s after a timeout; %d more "
                "moved row(s) stay indexed under the source namespace",
                namespace,
                max(0, len(refs) - start - 128),
            )
            break


def _purge_local_vector_index(counts: dict) -> list:
    """Drop the derived index files a purge just made stale: contiguous and coarse (bug-337).

    The index is a derived copy of the rows: it carries their embeddings and
    their agent / project / channel / source identifiers, so a purge that left
    it in place would keep the deleted agent's data on disk under a tool whose
    contract is that all of it is gone. Nothing in the running server rebuilds
    or invalidates the file — building it is an operator action — so the residue
    would otherwise survive until someone next ran that command.

    Removal rather than a rebuild without the agent: the module states the file
    is derived, never repaired and safe to delete, and rebuilding it here would
    move an operator-paced cost onto a delete. Queries fall back to the live
    scan, which is the behaviour before any index was built.

    Only the tables that actually lost rows, so a delete that matched nothing
    does not cost an operator their index. The lookup defaults to "it changed"
    on a key this function does not recognise: the failure worth avoiding here
    is leaving residue behind, not dropping a file that is safe to drop.

    Only for a caller whose grant covers every agent. Each file holds every
    agent's rows, so dropping it moves every agent's recall onto the slower
    read until the file is rebuilt -- a change to shared state that a grant on the
    one agent being purged does not authorise, and one such a caller could
    repeat at will (store a row, purge it). Under a narrower grant the files
    stay: every reader hydrates its candidates from the database, so the purged
    rows drop out of every answer, and the next build leaves them out of the
    files as well.

    Non-DB side effect — call AFTER the delete transaction commits, like the
    calibration and remote-namespace purges beside it.
    """
    from cpersona import acl, block_index, coarse_index

    # The coarse index carries the same identifiers and a one-bit form of the same
    # vectors, so it is residue in exactly the same sense and goes with the table.
    stale = [(table, vector_index.index_path(table)) for table in vector_index.INDEXED_TABLES]
    stale += [(table, coarse_index.index_path(table)) for table in coarse_index.COARSE_TABLES]
    # So does the block index file: it holds the bits of both kinds' blocks, and
    # goes when either table lost rows (the second entry finds it already gone).
    stale += [(table, block_index.index_path()) for table in ("memories", "episodes")]
    stale = [(table, path) for table, path in stale if counts.get(f"deleted_{table}", 1)]
    if not stale:
        return []
    if not acl.caller_holds_every_agent():
        kept = sorted({path for _table, path in stale if os.path.exists(path)})
        if kept:
            logger.info(
                "Kept the shared index files after a purge authorised on one agent; the "
                "purged rows are no longer served and leave the files at their next build: %s",
                ", ".join(kept),
            )
        return []
    # Counted before the first unlink, so a build that read its rows before this
    # purge committed discards its output instead of publishing it afterwards.
    vector_index.note_purge()
    removed = []
    for _table, path in stale:
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            # A file we cannot remove is residue we cannot claim to have removed,
            # and the rows are already gone — say so rather than fail the delete.
            logger.warning("Could not remove the stale vector index %s: %s", path, exc)
            continue
        removed.append(path)
    return removed


async def _purge_agent_remote_namespace(agent_id: str) -> None:
    """Purge the agent's remote vector namespace (network I/O — post-commit only)."""
    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        await _post_remote_side_effect(
            "purge",
            {"namespace": f"cpersona:{agent_id}"},
            action="purge",
            namespace=f"cpersona:{agent_id}",
        )


async def do_delete_agent_data(agent_id: str, session_key: str = "") -> dict:
    """Delete ALL data for a specific agent (memories, profiles, episodes)."""
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
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
            key,
        )
    if not agent_id:
        return error_response("agent_id is required for bulk deletion")

    # bug-042/043: transaction() serialises the multi-table delete + commit behind
    # the shared lock; its auto-rollback keeps a partial wipe from surviving as
    # another writer's commit.
    async with transaction() as db:
        counts = await _delete_agent_rows(db, agent_id)

    _purge_agent_calibration(agent_id)
    dropped_indexes = _purge_local_vector_index(counts)
    await _purge_agent_remote_namespace(agent_id)

    result = {"ok": True, "agent_id": agent_id, **counts}
    if dropped_indexes:
        # Not in the response: the shape of this result is the tool's contract.
        # An operator whose next query runs the live scan should still find the
        # reason in the log beside the delete that caused it.
        logger.info(
            "Dropped the stale contiguous index after purging %s: %s",
            agent_id,
            ", ".join(dropped_indexes),
        )
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

    ``beta`` is the precision point — knob 3. ``beta == 1`` reproduces the
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
    # bug-313: the same numbers, without the allocation that outgrew the sample
    # ceiling meant to bound it. The broadcast this replaces materialised a
    # 256 x len(null) boolean array -- measured at 290 bytes per pair against the
    # 21-26 the ceiling was sized with, i.e. 3.6 GB at the clamped ceiling of
    # 5,000 samples, which is the OOM the clamp exists to prevent. Counting
    # `x >= c` on a sorted array is exactly that count: everything from the first
    # index not less than c to the end. One sort, no per-candidate temporary.
    def _tail_fraction(values):
        ordered = np.sort(values)
        # np.sort places NaN last, and `nan >= c` is False, so the tail the
        # comparison would have counted ends where the NaNs begin. Excluding
        # them from the search while keeping them in the denominator is what
        # makes this identical to the comparison for every input, not only for
        # the finite ones.
        finite_end = len(ordered) - int(np.isnan(ordered).sum())
        counted = finite_end - np.searchsorted(ordered[:finite_end], candidates, side="left")
        return counted / len(ordered)

    tpr = _tail_fraction(pos)
    fpr = _tail_fraction(null)
    objective = tpr + beta * (1.0 - fpr)
    best = int(np.argmax(objective))
    return float(max(candidates[best], floor)), float(tpr[best] - fpr[best])


def _parse_ts_seconds(ts):
    """Parse an ISO-8601 timestamp to epoch seconds, or None when unparseable.

    bug-341: this was the one reader in the package that did not apply the
    naive-is-UTC rule the behaviour contract publishes. ``.timestamp()`` on a
    naive datetime assumes system-local time, so on a host at a nine-hour offset
    the same instant spelled naively and with an offset parsed 32,400 s apart --
    and naive stamps are routine here, because the episode ``created_at`` column
    defaults to SQLite's own naive ``datetime('now')``. Driving the consumer,
    two memories written 60 s apart in the two spellings yielded no same-session
    pair at all, since the adjacency window is 30 minutes and the shift is nine
    hours: the calibration then sits on a population it mislabelled. Routed
    through the shared helper whose docstring names this exact hazard, so there
    is one implementation of the rule rather than two.
    """
    parsed = _parse_timestamp_utc(ts) if isinstance(ts, str) else None
    return parsed.timestamp() if parsed is not None else None


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


def _modal_width(counts) -> int:
    """The width a mixed-dimension corpus is treated as being, deterministically.

    bug-344: three places decide this and they must not disagree. ``Counter.most_common``
    breaks a tie by insertion order, which here is row order -- so the same corpus can
    answer differently between one call and the next, and between the samplers and the
    startup probe that compares against what they persisted. Ordering by count and then
    by the wider value makes the answer a property of the corpus rather than of the scan.
    """
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _safe_frombuffer(blob):
    """Decode a stored embedding blob to a float32 array, or None if the bytes are corrupt
    (bug-061). np.frombuffer raises `ValueError: buffer size must be a multiple of element
    size` when len(blob) is not divisible by 4 — a truncated write or a hand-crafted
    embedding_b64 import can plant such a blob, and the unguarded decode in calibration was
    reachable from ensure_calibrated_on_startup, crashing the whole server before it served
    a single request. Returning None lets the caller skip the poison row instead. A valid
    float32 embedding is always a multiple of 4 bytes, so this never rejects a good row.

    bug-310-era width gate, bug-312 value gate: a blob of the right width whose values
    are not finite is poison of the same kind, and worse because it is silent. One NaN
    element propagates through the whole similarity matrix, and `round(max(raw, floor), 4)`
    returns NaN because `floor > nan` is False — so calibration answered ok:true with a NaN
    threshold, persisted it to the sidecar (json.dump writes NaN by default) and survived
    restart. Downstream that threshold is not merely strict but inconsistent: the arms that
    filter with `>=` admit nothing while the arm that filters with `<` admits everything.
    A finite float32 embedding is what every write seam produces, so this never rejects a
    good row either; the reachable sources are data written by an older version or arriving
    through import."""
    import numpy as np

    if not blob or len(blob) % 4 != 0:
        return None
    try:
        vec = np.frombuffer(blob, dtype=np.float32)
    except (ValueError, TypeError):
        return None
    if not np.isfinite(vec).all():
        return None
    return vec


async def _temporal_adjacency_sims(
    db, agent_id: str, limit: int, window_min: float, target_dim: int | None = None
):
    """Fetch (timestamp, embedding) ordered by time and build same-session pair sims.

    ``target_dim`` is the width the caller's other population settled on. bug-395:
    without it this arm reduced to its OWN modal width while the null arm reduced to
    its own, and one threshold sweep was handed both -- on a mixed-dimension corpus
    that places the operating point between two different models. Measured on one
    agent holding 300 older 16-wide rows and 150 newest 8-wide rows: the null sample
    resolved to 16 and this window to 8, giving a null mean of -0.0015 against a
    positive mean of 0.0226 and a threshold describing neither, while the response
    and the sidecar recorded only the null's width. A model swap is exactly when the
    newest rows are one model and the corpus majority another, and it is also when
    startup calibration fires.
    """
    import numpy as np

    # Per-agent when agent_id provided, deliberate all-agents fallback when empty —
    # the empty case is the typed no-filter form of the helper.
    iso = isolation_where(agent_id=agent_id or None)
    # bug-396: ORDER BY the parsed instant, not the raw TEXT. `timestamp` is
    # caller-supplied (memory_handlers), so one client stamping '+09:00' and another
    # stamping 'Z' fill the same column and a byte-order sort keeps an older row while
    # dropping a newer one -- measured with three rows where DESC LIMIT 2 returned the
    # oldest and dropped the chronologically middle one. `datetime()` normalises every
    # spelling this package actually writes (offset, Z, space separator, fractional
    # seconds) and answers NULL for what it cannot read, which sorts last under DESC --
    # those rows are dropped by the parser below anyway.
    rows = await db.execute_fetchall(
        f"SELECT timestamp, embedding FROM memories WHERE embedding IS NOT NULL "
        f"AND timestamp IS NOT NULL{iso.and_clause} ORDER BY datetime(timestamp) DESC LIMIT ?",
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
    # is not ragged on a mixed-dimension corpus during a model swap. bug-395: when the
    # caller names the width its other population settled on, use that instead of a
    # second independent vote -- the two have to describe the same model to be compared.
    from collections import Counter

    if target_dim is None:
        target_dim = _modal_width(Counter(v.shape[0] for v in vecs))
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
    if state.get("scoring_version") != _live_fingerprint():
        return {}
    return {
        axis: {
            agent_id: value
            for agent_id, value in (state.get(axis) or {}).items()
            if agent_id not in vector._calibration_authority[axis]
        }
        for axis in vector._CALIBRATION_AGENT_AXES
    }


#: "Read the sidecar yourself": distinct from None, which means it is absent or unreadable.
_UNREAD = object()


def _stored_calibration_to_carry(embedding_dim: int, state: dict | None | object = _UNREAD) -> dict:
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

    *state* lets the writer pass the payload it already read, so the carry and the
    write decide from the same read of the file.
    """
    if state is _UNREAD:
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


def _refresh_unchanged_calibration(state: dict | None, embedding_dim: int) -> None:
    """Take the sidecar's value for every owned entry this process has not changed.

    Several server processes can share one database, and so one sidecar. Owning an
    entry (by restoring it at boot) says this process once held its current value, not
    that it still does: when another process later changed or removed it, writing the
    in-memory copy back reverted that acknowledged change on the next restart -- or
    resurrected the calibration of an agent the other process had purged. The entries
    this process changed since its last successful write (``vector._calibration_pending``)
    are the only ones it is newer for; every other owned entry is refreshed from the file
    just read, in memory as well as in the write, so the next write does not revert it.

    Skipped when the stored payload would not be carried (absent, unreadable, another
    embedding dimension or scoring version): those values are not adopted anywhere.
    """
    if (
        not state
        or state.get("embedding_dim") != embedding_dim
        or state.get("scoring_version") != _live_fingerprint()
    ):
        return
    live_maps = {
        "agent_thresholds": vector._agent_thresholds,
        "agent_fused_gates": vector._agent_fused_gates,
        "agent_betas": vector._agent_betas,
    }
    for axis, live in live_maps.items():
        stored = state.get(axis) or {}
        for agent_id in vector._calibration_authority[axis] - vector._calibration_pending[axis]:
            if agent_id in stored:
                live[agent_id] = stored[agent_id]
            else:
                live.pop(agent_id, None)
    # Same None rule as _restore_calibration_state: a stored None leaves the live value.
    unchanged = vector._global_calibration_authority - vector._global_calibration_pending
    if "global_threshold" in unchanged and state.get("global_threshold") is not None:
        config.VECTOR_MIN_SIMILARITY = state["global_threshold"]
    if "global_fused_gate" in unchanged and state.get("global_fused_gate") is not None:
        vector._global_fused_gate = state["global_fused_gate"]
    if "fused_gate_signal" in unchanged and state.get("fused_gate_signal") is not None:
        vector._fused_gate_signal = state["fused_gate_signal"]


#: "Stamp the live fingerprint" (or the current time): distinct from None, which a
#: rewrite may pass through.
_LIVE = object()


def _live_fingerprint() -> str:
    """The fingerprint the running scoring produces (bug-470). Imported lazily:
    memory_handlers imports this module's neighbours at load."""
    from cpersona.memory_handlers import calibration_fingerprint

    return calibration_fingerprint()


def _save_calibration_state(
    embedding_dim: int,
    embedding_model: str,
    global_threshold: float | None,
    agent_thresholds: dict,
    global_fused_gate: float | None = None,
    agent_fused_gates: dict | None = None,
    fused_gate_signal: str | None = None,
    agent_betas: dict | None = None,
    scoring_version: str | None | object = _LIVE,
    calibrated_at: str | None | object = _LIVE,
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
    It defaults to the live fingerprint (``memory_handlers.calibration_fingerprint``,
    SCORING_VERSION plus a non-default far weight, bug-470) because every caller that
    MEASURES thresholds measures them on the running scoring function; the one caller
    that only REWRITES an existing payload (``_purge_agent_calibration``) passes the
    stored value through, so dropping an agent cannot launder a stale sidecar into
    looking freshly calibrated. ``calibrated_at`` follows the same rule: it defaults
    to now, and the rewrite passes the stored time through.
    """
    if scoring_version is _LIVE:
        scoring_version = _live_fingerprint()
    if calibrated_at is _LIVE:
        calibrated_at = datetime.now(timezone.utc).isoformat()
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
        "calibrated_at": calibrated_at,
    }
    path = _calibration_sidecar_path()
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with fileperms.open_private(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        logger.warning("Could not persist calibration sidecar: %s", exc)
        return False


def _read_calibration_sidecar() -> tuple[dict | None, bool]:
    """Read the sidecar. Returns (state, unreadable).

    bug-347: an unreadable sidecar used to be indistinguishable from an absent one --
    both answered None and neither said anything, at any level. Every
    evidence-preserving guard on the startup path tests that same value, so a corrupt
    file took none of them: the boot reported ``initial``, the word reserved for a new
    install, wrote no backup, and the next save replaced the only copy an operator
    could have repaired by hand. Measured with a sidecar truncated by one byte, an
    operator-set precision override was gone from disk with no copy anywhere.

    So the two answers are separated here. Absent is a fresh install and stays quiet.
    Unreadable is a fault: it is reported at WARNING (the file it names is the one an
    operator has to look at) and flagged to the caller, which backs the bytes aside
    before it writes over them.
    """
    path = _calibration_sidecar_path()
    try:
        with open(path) as fh:
            return json.load(fh), False
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError) as exc:
        logger.warning(
            "The calibration sidecar at %s exists but could not be read (%s: %s). "
            "Treating it as a fault rather than a first-ever boot: the recall gates "
            "it held are not applied, and the file is copied aside before anything "
            "replaces it.",
            path,
            type(exc).__name__,
            exc,
        )
        return None, True


def _load_calibration_state() -> dict | None:
    """Load the calibration sidecar, or None when absent/unreadable."""
    state, _unreadable = _read_calibration_sidecar()
    return state


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
    """Copy the sidecar aside before a staleness recalibration replaces it.

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
            "bug-246: the calibration sidecar for scoring version %r is "
            "already backed up at %s; not writing another copy.",
            version_tag,
            existing[-1],
        )
        return existing[-1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.before-{version_tag}-{stamp}"
    try:
        with open(path, "rb") as src, fileperms.open_private(backup, "wb") as dst:
            dst.write(src.read())
        _prune_calibration_backups(path)
        return backup
    except OSError as exc:
        logger.warning(
            "Could not back up the calibration sidecar before "
            "recalibration; the previous gate values will not survive: %s",
            exc,
        )
        return None


def _backup_unreadable_calibration_sidecar() -> str | None:
    """Copy an unreadable sidecar aside, one copy per distinct content.

    The per-tag dedup above is right for a superseded scoring version, whose file is
    byte-identical from boot to boot. Under a fixed ``unreadable`` tag it was wrong: a
    later, DIFFERENT corrupt file found the first one's copy, copied nothing and was
    then replaced, so the second set of hand-edited overrides was lost with a log line
    naming a backup that did not hold them. Tagging by a content digest keeps the
    dedup for the same bytes and gives different bytes their own copy.

    Returns None when the bytes cannot be read at all: then they cannot be copied
    either, and the caller must not replace them.
    """
    try:
        with open(_calibration_sidecar_path(), "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError as exc:
        logger.warning("Could not read the unreadable calibration sidecar to copy it aside: %s", exc)
        return None
    return _backup_calibration_sidecar(f"unreadable-{digest}")


async def _corpus_embedding_dim() -> int | None:
    """Return the float32 dimension the corpus is treated as having, or None when empty.

    bug-344: this read one arbitrary row -- a LIMIT 1 with no ordering -- on the premise
    that the width is corpus-invariant. It is not while a model swap is in flight, and
    that is the only time this comparison matters. Calibration drops ragged rows and
    persists the modal width, so on a mixed corpus the two quantities disagreed
    permanently: measured with three legacy wide rows beside twenty current ones, the
    probe answered 16 while calibration persisted 8, and four consecutive startup checks
    over an unchanged corpus each reported the dimension as changed -- backing up the
    sidecar, discarding every per-agent threshold and gate it held, and re-running a full
    calibration per agent, without ever settling. It could not even oscillate into
    agreement, because the legacy rows are the oldest and a plain scan reaches them
    first, so the same wrong answer came back every boot.

    Asking for the modal width makes this the same question calibration answers, and
    calibration persists this value (``_corpus_embedding_dim_on``) rather than the width
    of its own sample: a per-agent sample's modal width is that agent's, and an agent
    already re-embedded by the new model then flipped every later boot to "changed".
    """
    async with connection() as db:
        return await _corpus_embedding_dim_on(db)


async def _corpus_embedding_dim_on(db) -> int | None:
    """``_corpus_embedding_dim`` on a connection the caller already holds."""
    # Deliberately global: the width is a property of the corpus the embedding model
    # produced, not of one agent's data (the bug-062 rule is about sizes and
    # contents). The typed no-filter helper call replaces the old waiver comment.
    iso = isolation_where(agent_id=None)
    rows = await db.execute_fetchall(
        f"SELECT length(embedding), COUNT(*) FROM memories "
        f"WHERE embedding IS NOT NULL{iso.and_clause} GROUP BY length(embedding)",
        iso.params,
    )
    counts = {int(width): int(n) for width, n in rows if width}
    if not counts:
        return None
    return _modal_width(counts) // 4  # 4 bytes per float32


def _calibration_signal() -> str | None:
    """The gate signal a calibration measures: the one the runtime gate compares.

    Confidence only where it still orders and gates (CPERSONA_CONFIDENCE_ORDERING=legacy),
    else the fused score of the active mode. From 2.6.0a7 an enabled confidence score is
    returned beside each row but gates nothing, so calibrating on it would collect no row
    whose signal matches and store no gate. Cascade with confidence off has no fusion gate
    (the cosine vector threshold owns precision there): None.
    """
    # bug-482: asked of the runtime gate's own predicate. This read config's values
    # while the gate reads memory_handlers' copies, so the two spellings could be
    # patched apart and the calibration would measure a signal the gate never keys on.
    from cpersona.memory_handlers import _confidence_orders

    if _confidence_orders():
        return "confidence"
    if config.RECALL_MODE in ("rsf", "rrf"):
        return config.RECALL_MODE
    return None


async def _calibrate_fused_gate(
    db,
    agent_id: str,
    sample_queries: int,
    window_min: float,
    beta: float,
    floor: float,
) -> dict | None:
    """Simulate-query calibration of the recall quality gate (v2.4.27).

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
    signal = _calibration_signal()
    if signal is None:
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
    """Median-of-K wrapper over ``_calibrate_fused_gate``.

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
    ever tripped the first until mutation M08 pointed at the gap.
    """
    import numpy as np

    # Per-agent when agent_id is provided, deliberate all-agents calibration when
    # empty (the typed no-filter form of the helper).
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

        target_dim = _modal_width(Counter(v.shape[0] for v in vecs))
        vecs = [v for v in vecs if v.shape[0] == target_dim]
    if len(vecs) < 10:
        return None, {"ok": False, "error": f"Need at least 10 same-dimension embeddings, found {len(vecs)}"}
    return np.array(vecs), None


def _null_distribution(vecs):
    """All-pairs dot products, and their upper triangle.

    Treating every pair of distinct memories as a NON-match is what makes this a
    null distribution: the threshold is then placed where a genuine match would
    have to stand out from unrelated noise. The full matrix comes back too — the
    separation method's nearest-neighbour fallback reuses it rather than
    recomputing an O(n^2) product.

    bug-250: this said "cosine similarities" and computes ``vecs @ vecs.T``,
    which is a cosine only while every stored vector is unit-norm — a property
    nothing in this package establishes or checks for the ``http`` embedding
    path. The name mattered because the OTHER population this calibration
    compares against, ``_adjacency_sims_core``, divides by the norms first: on a
    non-unit corpus the operating point is placed between two populations
    measured on different scales, and a docstring asserting they are the same
    quantity is how that stays invisible. Corrected to say what it computes.
    ``deep_embedding_norm`` reports whether the invariant actually holds;
    making the four call sites agree is a scoring change and is not this one.
    """
    import numpy as np

    sim_matrix = vecs @ vecs.T
    pairwise_sims = sim_matrix[np.triu_indices(len(vecs), k=1)]
    return sim_matrix, pairwise_sims


def _purged_during_calibration(agent_id: str) -> dict:
    """The answer of a calibration whose agent was deleted while it measured (bug-036).

    Writing the result would put back, in memory and in the sidecar, calibration the
    delete just removed, and a later agent created under the same id would inherit
    numbers measured on a corpus that no longer exists. So nothing is applied, and the
    caller is told so rather than handed a threshold that is not in effect.
    """
    logger.info(
        "Calibration [%s]: the agent's data was deleted while it was measured; "
        "the result was discarded.",
        agent_id,
    )
    return error_response(
        f"the data of agent '{agent_id}' was deleted while it was being calibrated; "
        "the measurement was discarded and nothing was applied"
    )


#: "Measure the gate at the agent's current beta": the default of do_calibrate_threshold's
#: ``precision_override``, distinct from None (measure at the global default, drop the override).
_CURRENT_PRECISION = object()


async def do_calibrate_threshold(
    agent_id: str,
    sample_size: int = 0,
    z_factor: float = 0,
    method: str = "",
    percentile: float = 0,
    session_key: str = "",
    *,
    precision_override: float | None | object = _CURRENT_PRECISION,
) -> dict:
    """Auto-calibrate the vector-similarity threshold from the embedding distribution.

    Uses the null distribution of pairwise cosine similarities (mostly unrelated
    pairs). When *agent_id* is provided, writes a per-agent override into
    ``vector._agent_thresholds``; when empty, calibrates the global
    ``config.VECTOR_MIN_SIMILARITY`` from the all-agents corpus (v2.4.15).

    v2.4.24: the threshold is placed ABOVE the null mean (see ``_threshold_from_sims``);
    ``method`` defaults to ``percentile``. The result is persisted to a sidecar keyed by
    embedding dimension so a later embedding-model swap triggers recalibration at startup.

    ``precision_override`` is set_recall_precision's, and no tool argument reaches it:
    a beta (or None, for "clear the override") the gate is measured at and the sidecar
    write records for the agent, while process memory keeps the current beta. The
    caller adopts the new beta only once this call has persisted it, so a calibration
    that fails leaves no tentative value anywhere another calibration could save it.
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
    # NaN is truthy, so it survived the `or` defaults below, and min(10.0, nan) is 10.0:
    # NaN and +/-Infinity (which a JSON number such as 1e999 parses to) became the
    # extreme z of the clamp instead of an error. Refuse them before either step.
    for name, value in (("z_factor", z_factor), ("percentile", percentile)):
        if not math.isfinite(value):
            return {"ok": False, "error": f"{name} must be a finite number, got {value}"}
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
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
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
                "embedding_model": config.reported_embedding_model(),
                "distribution": None,
                "null_admit_rate": None,
                "old_threshold": vector._get_vector_threshold(agent_id),
                "new_threshold": None,
            },
            "calibrate_threshold",
            key,
        )

    # Noted before the first await: a delete_agent_data that lands while this call is
    # suspended purges the agent once, at that instant, and everything measured here
    # from then on describes a corpus that no longer exists.
    purges_at_start = vector._agent_calibration_purges(agent_id)

    # The read seam stays open through the fused-gate calibration below — it issues
    # simulate-query recalls against the same connection. It is the background seam:
    # each simulate query is a whole stored record, and on a large store its keyword
    # search takes seconds, so on the request connection every search would wait
    # behind one.
    async with background_connection() as db:
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
            # bug-395: the null sample above has already settled a width, and the
            # sweep below compares the two populations directly, so the positive proxy
            # is drawn at that same width rather than voting on one of its own.
            pos_sims = await _temporal_adjacency_sims(
                db,
                agent_id,
                sample_n,
                CALIBRATE_TEMPORAL_WINDOW_MIN,
                target_dim=int(vecs.shape[1]),
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
        sample_dim = int(vecs.shape[1])
        # The sidecar is fingerprinted with the corpus's modal width, the quantity the
        # startup guard compares it with. This sample's width answers for one agent (or
        # one random draw); persisted, it made a mixed-width corpus look changed on every
        # boot and dropped every other agent's carried entries as "another width". The
        # response still reports the width this calibration measured.
        embedding_dim = await _corpus_embedding_dim_on(db) or sample_dim
        # mean + z*std has a floor but no ceiling, and on a narrow null distribution a
        # z well inside the clamp lands above 1.0 -- a cosine no vector can reach, so
        # the agent's vector arm would return nothing. Refuse it before it is applied.
        if new_threshold > 1.0:
            return {
                "ok": False,
                "error": (
                    f"the computed threshold {new_threshold} is above the maximum cosine "
                    f"similarity of 1.0 (method={cal_method}, z_factor={z}); use a smaller z_factor"
                ),
            }

        if agent_id and vector._agent_calibration_purges(agent_id) != purges_at_start:
            return _purged_during_calibration(agent_id)

        # Apply: per-agent dict when agent_id provided, global fallback when empty.
        # Claiming the axis makes this process the owner of what it just measured, so
        # the sidecar write below overwrites the stored value instead of carrying it
        # (and, later, so a deletion of this entry can reach disk).
        if agent_id:
            vector._agent_thresholds[agent_id] = new_threshold
            vector._record_agent_calibration_change(agent_id, "agent_thresholds")
        else:
            config.VECTOR_MIN_SIMILARITY = new_threshold
            vector._record_global_calibration_change("global_threshold")

        # Post-fusion quality-gate calibration (v2.4.26). Per-agent and
        # fusion-mode only: recall is per-agent, and the gate lives on the active mode's
        # fused-score scale. Calibrating the curve here makes precision driven by data in
        # every mode (cascade via the vector floor above, rsf/rrf via this gate) instead of
        # the pool-size heuristic _adaptive_min_score.
        fused_stats = None
        gate_beta = None
        if agent_id and config.FUSED_GATE_ENABLED:
            if precision_override is _CURRENT_PRECISION:
                gate_beta = vector._get_precision_beta(agent_id)
            elif precision_override is None:
                gate_beta = config.FUSED_GATE_BETA
            else:
                gate_beta = precision_override
            # The simulate-query pass issues live fusion recalls; a flaky embedding backend
            # must not abort calibration and lose the vector threshold computed above (which
            # is persisted below). Degrade to the heuristic gate on any failure.
            try:
                fused_stats = await _calibrate_fused_gate_median(
                    db,
                    agent_id,
                    config.FUSED_GATE_SAMPLE_QUERIES,
                    CALIBRATE_TEMPORAL_WINDOW_MIN,
                    gate_beta,
                    CALIBRATE_FLOOR,
                )
            except Exception as exc:
                logger.warning(
                    "Fused-gate calibration failed for [%s]; keeping the heuristic gate: %s",
                    agent_id or "global",
                    exc,
                )
                fused_stats = None

    # From here to the sidecar write there is no await, so no other call can land
    # between the checks below, the writes into process memory and the save that
    # persists them. The gate is written here rather than where it was measured for
    # that reason: set_recall_precision's gate and beta must arrive together, and a
    # gate written before the await above could be replaced in it by a calibration
    # measured at the old beta, leaving the new beta saved beside the old gate.
    if agent_id and vector._agent_calibration_purges(agent_id) != purges_at_start:
        return _purged_during_calibration(agent_id)

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
    stored_state, stored_unreadable = _read_calibration_sidecar()
    # An unreadable sidecar carries nothing, so the write below would replace the only
    # copy of every other agent's thresholds, gates and precision overrides with this
    # process's memory. The startup path copies the bytes aside first (bug-347); a
    # runtime calibration must too, and must leave the file alone when it cannot.
    may_replace = not stored_unreadable or _backup_unreadable_calibration_sidecar() is not None
    if may_replace:
        _refresh_unchanged_calibration(stored_state, embedding_dim)

    # The gate sits on the separation curve at the beta it was measured at. When the
    # agent's beta changed during the measurement -- set_recall_precision applied a new
    # one, typically while the startup guard was calibrating this agent -- the agent
    # already holds a gate measured at its new beta, and this one would replace it with
    # a value for a beta the agent no longer has, in memory and in the sidecar.
    if (
        fused_stats is not None
        and precision_override is _CURRENT_PRECISION
        and vector._get_precision_beta(agent_id) != gate_beta
    ):
        logger.info(
            "Fused gate [%s]: discarded a gate measured at beta %s; the agent's beta "
            "changed to %s while it was measured.",
            agent_id,
            gate_beta,
            vector._get_precision_beta(agent_id),
        )
        fused_stats = None
    if fused_stats is not None:
        # The 0.1544 collapse produced zero log lines —
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
        vector._record_agent_calibration_change(agent_id, "agent_fused_gates")
        vector._record_global_calibration_change("fused_gate_signal")

    if may_replace:
        carried = _stored_calibration_to_carry(embedding_dim, stored_state)
        agent_betas = {**carried.get("agent_betas", {}), **dict(vector._agent_betas)}
        if precision_override is None:
            agent_betas.pop(agent_id, None)
        elif precision_override is not _CURRENT_PRECISION:
            agent_betas[agent_id] = precision_override
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
            agent_betas=agent_betas,
        )
        if sidecar_persisted:
            vector._clear_calibration_pending()
    else:
        logger.warning(
            "Not persisting calibration: the sidecar at %s is unreadable and could not "
            "be copied aside, so replacing it would destroy the only copy.",
            _calibration_sidecar_path(),
        )
        sidecar_persisted = False

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
        "embedding_dim": sample_dim,
        "embedding_model": config.reported_embedding_model(),
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
        sample_dim,
    )
    return result


async def do_set_recall_precision(
    agent_id: str, precision: str = "", beta: float = 0, session_key: str = ""
) -> dict:
    """Set an agent's recall precision (knob 3, v2.4.29) and recalibrate its gate.

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
    # A JSON number such as 1e999 parses to Infinity. At beta=inf the separation
    # objective is inf*0 = NaN at the lowest candidate, which argmax picks, so the
    # strictest request installed the MOST lenient gate, and the stored Infinity is not
    # valid JSON in any later response. NaN fails `beta > 0` and was read as a clear.
    if beta and not math.isfinite(beta):
        return {"ok": False, "error": f"beta must be a finite number, got {beta}"}

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
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
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
            key,
        )

    # Recalibrate at the new beta so the change takes effect now. The beta is handed to
    # the calibration rather than written into process memory first: the calibration
    # measures the gate at it and writes it into the sidecar together with that gate,
    # and it is adopted here only once that write succeeded. A tentative value in
    # vector._agent_betas was visible to every other calibration for as long as this
    # one ran -- another agent's calibration saved it to the sidecar while this call
    # went on to fail and answer ok:false, and the next restart applied it (bug-613).
    #
    # bug-096: a RAISED calibration failure (transient DB lock, numpy fault) is a
    # failure like any other, and since nothing was applied there is nothing to undo.
    try:
        # bug-342: the caller's own session bucket, not the shared keyless one.
        # This handler gates itself on `key` and then asked calibration to decide
        # again with nothing declared, so a pause armed by some other caller
        # suppressed a calibration this caller was entitled to: measured with a
        # keyless pause armed and this caller declaring an unpaused key, the call
        # answered ok:true with the beta applied and new_threshold None -- nothing
        # measured, no sidecar written, and no field to branch on.
        cal = await do_calibrate_threshold(
            agent_id=agent_id,
            session_key=session_key,
            precision_override=None if clear else resolved_beta,
        )
    except Exception as exc:
        cal = {"ok": False, "error": f"calibration raised: {exc}"}
    # A skipped calibration is ok:true by design (that is the no-persist shape), so
    # `ok` alone cannot tell "measured and applied" from "declined to measure". A skip
    # wrote nothing, so adopting the beta below would leave an un-persisted weight in
    # process memory: it counts as a failure to apply here even though it is not an
    # error.
    # A calibration whose sidecar write failed is the same state reached the other way:
    # measured, but the override would be gone on the next restart (the previous sidecar
    # is what a restart restores), so it is not adopted and reported as not applied.
    if cal.get("ok") and cal.get("persisted") is not False and cal.get("sidecar_persisted") is False:
        cal = {
            **cal,
            "ok": False,
            "error": (
                "the calibration sidecar could not be written, so the change would not "
                "survive a restart; the precision override was not applied"
            ),
        }
    if not cal.get("ok") or cal.get("persisted") is False:
        # bug-149: nothing to restore. The override never entered process memory, so a
        # concurrent set_recall_precision for the same agent that applied its own value
        # while this call was suspended keeps it.
        return {
            "ok": False,
            "agent_id": agent_id,
            "precision": resolved_precision,
            "beta": resolved_beta,
            "cleared": clear,
            "error": cal.get("error", "calibration failed"),
        }
    # The calibration has persisted the override with the gate measured at it, and no
    # await separates that write from this one, so the two arrive together. Claimed
    # but not pending: the value is on disk, so a later write by this process must not
    # carry the stored entry back over a clear, and need not rewrite it either.
    if clear:
        vector._agent_betas.pop(agent_id, None)
    else:
        vector._agent_betas[agent_id] = resolved_beta
    vector._claim_agent_calibration(agent_id, "agent_betas")
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


def _unchanged_stored_entries(state: dict, axis: str) -> dict:
    """The stored entries of *axis*, without those this process changed since its last write."""
    pending = vector._calibration_pending[axis]
    return {
        agent_id: value
        for agent_id, value in (state.get(axis) or {}).items()
        if agent_id not in pending
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

    An entry this process changed since its last sidecar write is newer than the file
    and is left alone, as ``_refresh_unchanged_calibration`` leaves it. The startup
    guard restores beside serving (bug-258), so a delete_agent_data whose rewrite of
    the file failed, or a calibration that has measured and not yet saved, can come
    first; restoring over them brought the deleted agent's calibration back, or
    replaced the measurement the calibration then saved with the stale stored value.
    """
    pending_globals = vector._global_calibration_pending
    global_threshold = state.get("global_threshold")
    if global_threshold is not None and "global_threshold" not in pending_globals:
        config.VECTOR_MIN_SIMILARITY = global_threshold
    vector._agent_thresholds.update(_unchanged_stored_entries(state, "agent_thresholds"))
    global_fused_gate = state.get("global_fused_gate")
    if global_fused_gate is not None and "global_fused_gate" not in pending_globals:
        vector._global_fused_gate = global_fused_gate
    vector._agent_fused_gates.update(_unchanged_stored_entries(state, "agent_fused_gates"))
    fused_gate_signal = state.get("fused_gate_signal")
    if fused_gate_signal is not None and "fused_gate_signal" not in pending_globals:
        vector._fused_gate_signal = fused_gate_signal
    # Per-agent precision overrides (knob 3, v2.4.29). Backward compatible: a pre-v2.4.29
    # sidecar has no key, leaving every agent on the global beta default.
    vector._agent_betas.update(_unchanged_stored_entries(state, "agent_betas"))
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
    live_dim = await _corpus_embedding_dim()
    # Read after the await, not before it: this guard runs beside serving (bug-258), and
    # a delete_agent_data that landed in the await rewrote the file without the agent.
    # Applying a copy read earlier restored the calibration it had just removed. From
    # here to the restore and the beta preload below there is no await.
    state, sidecar_unreadable = _read_calibration_sidecar()
    dim_changed = (
        state is not None and live_dim is not None and state.get("embedding_dim") != live_dim
    )
    scoring_stale = state is not None and state.get("scoring_version") != _live_fingerprint()

    restored = False
    if state and not dim_changed and not scoring_stale and not auto_calibrate:
        _restore_calibration_state(state)
        restored = True
        # A pre-v2.4.27 sidecar (or one never gate-calibrated) restores the vector
        # threshold but carries no recall gate. With FUSED_GATE_ENABLED, fall through to
        # calibrate the gate so it actually bites in production; otherwise the
        # restore is sufficient. (This is what activates the gate on a v2.4.25 -> v2.4.27
        # upgrade where the embedding dimension is unchanged, so no dim-change recalibrate
        # would otherwise fire.)
        if not (config.FUSED_GATE_ENABLED and vector._fused_gate_signal is None):
            return {"action": "restored", "embedding_dim": state.get("embedding_dim")}
        logger.info("Calibration sidecar has no recall-gate signal; calibrating the gate.")

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
    # The condition is "nothing was restored", not a list of triggers: the three ways to
    # recalibrate (stale fingerprint, dimension change, AUTO_CALIBRATE) all bypass
    # _restore_calibration_state, and AUTO_CALIBRATE is the worst of them — it wipes the
    # overrides on EVERY boot, not just an upgrade one. Keying on the triggers would make
    # the invariant hold or fail depending on an unrelated config flag. It also runs
    # BEFORE the no-recalibration return below: a stale boot with recalibration disabled
    # restores nothing either, so without the preload the overrides were not applied for
    # the life of the process, and the next calibrate_threshold — the documented repair
    # for that state — wrote the sidecar from the empty dict and deleted them.
    if not restored and state is not None:
        vector._agent_betas.update(_unchanged_stored_entries(state, "agent_betas"))
        # Claim the beta axis ONLY. This process now holds every stored beta, so a
        # later clear of one is a real deletion and must reach disk; it holds none of
        # the thresholds and gates deliberately left behind above, so those stay
        # carried into the sidecar until this boot's recalibration replaces them.
        for stored_agent_id in state.get("agent_betas") or {}:
            vector._claim_agent_calibration(stored_agent_id, "agent_betas")

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
                _live_fingerprint(),
            )
        return {"action": "noop"}

    # A staleness recalibration is about to replace the only record of the
    # values that gated recall until this boot. Back the sidecar up first (evidence for
    # the before/after comparison the log below reports), and remember what was stored
    # so the report can distinguish it from the runtime default that
    # ``do_calibrate_threshold`` will log as its "from" value — with the restore
    # skipped, that "from" is config, not anything that ever gated a query.
    sidecar_backup = None
    replaced = None
    if sidecar_unreadable:
        # bug-347: the same evidence rule, for the file that could not be parsed. There
        # is no `replaced` report to build -- nothing was read out of it -- but the bytes
        # are the only thing an operator can repair by hand, and the calibration below
        # is about to write over them.
        sidecar_backup = _backup_unreadable_calibration_sidecar()
        logger.warning(
            "The unreadable calibration sidecar was copied aside (backup: %s) before "
            "this boot recalibrated over it.",
            sidecar_backup or "FAILED",
        )
    elif state is not None and (dim_changed or scoring_stale):
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
            _live_fingerprint(),
        )

    global_result = await do_calibrate_threshold(agent_id="")
    agents = []
    if global_result.get("ok"):
        # Deliberate corpus-wide agent enumeration (typed no-filter helper —
        # the structural gate's sanctioned spelling for a global scan).
        iso_all = isolation_where(agent_id=None)
        async with background_connection() as db:
            agent_rows = await db.execute_fetchall(
                f"SELECT DISTINCT agent_id FROM memories WHERE embedding IS NOT NULL{iso_all.and_clause}",
                iso_all.params,
            )
        for (aid,) in agent_rows:
            r = await do_calibrate_threshold(agent_id=aid)
            if r.get("ok"):
                agents.append(aid)

    if replaced is not None:
        # The per-calibration log lines above each printed
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
            "Staleness recalibration replaced the stored calibration "
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
            # bug-347: an existing file that did not parse is not a first-ever boot, and
            # the word for it must not be the one reserved for one. This is a new value
            # of the existing field rather than a new field: what the operator needs to
            # learn is which of the two happened, not one more key to branch on.
            else "recalibrated_unreadable" if sidecar_unreadable
            else "auto" if auto_calibrate
            else "initial"
        ),
        "dim_changed": dim_changed,
        "scoring_stale": scoring_stale,
        "global_ok": bool(global_result.get("ok")),
        "agents": agents,
        # Machine-readable form of the replacement report above; None on
        # the boots (initial, plain auto-calibrate) that replace nothing stale.
        "sidecar_backup": sidecar_backup,
        "calibration_replaced": replaced,
    }


async def do_delete_episode(episode_id: int, agent_id: str = "", session_key: str = "") -> dict:
    """Delete a single episode by ID (FTS5 triggers handle the LOCAL index cleanup)."""
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "deleted_id": episode_id}, "delete_episode", key
        )
    _warn_if_unscoped("do_delete_episode", episode_id, agent_id)
    if not fits_sqlite_integer(episode_id):
        return error_response(f"Episode {episode_id} not found or not owned by agent")
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
        await _post_remote_side_effect(
            "remove",
            {"namespace": ns, "ids": [f"ep:{episode_id}"]},
            action="removal",
            namespace=ns,
        )

    return {"ok": True, "deleted_id": episode_id}


async def _equal_memory_id(db, agent_id: str, project_id: str, channel: str, content: str) -> int | None:
    """The id of the row a copy or restore skipped as equal to `content` in that scope.

    The skipped record lives on as that row, so the declarations naming it are
    carried onto it.
    """
    rows = await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND project_id = ? AND channel = ? AND content = ? LIMIT 1",
        (agent_id, project_id, channel, content),
    )
    return rows[0][0] if rows else None


def _imported_label(record: dict, blob: bytes | None) -> str:
    """The ``embedding_model`` label a restored row takes.

    Only a restored vector keeps one: a row whose vector was refused, or that
    carried none, is stored without a vector, and a label beside nothing would
    describe nothing. A file written before labels existed, or one edited by hand
    into something that is not a string, gives '' -- unknown, which is what such a
    file can say about the model that produced its vectors.
    """
    label = record.get("embedding_model")
    if blob is None or not isinstance(label, str):
        return ""
    return label


def _decode_embedding(record: dict, tally: "_ImportTally", line_num: int, kind: str) -> bytes | None:
    """Decode a base64 embedding blob from an export record.

    Tolerates a missing or malformed value (returns None) so one bad embedding
    cannot raise mid-restore and abort the whole import (bug-016); check_health's
    null-embedding repair then re-embeds the row.

    bug-343: it also asks the storage seam's question. The width check below is not the
    same question as "are these numbers" -- a whole number of float32s can be a vector
    of NaNs, and ``vector.pack_for_storage`` refuses exactly that on the store path. So
    the same file was accepted here and refused there: a restore wrote a non-finite
    vector durably, where it scores against every query (the similarity floor is a ``<``
    comparison, which a NaN does not fall below) and poisons the agent's threshold
    calibration until an operator runs a repairing health check. The two write paths now
    give the same verdict on the same values.

    And a rejection says so. All three refusals used to return None silently, so a
    restore that dropped every embedding it carried was indistinguishable from one that
    carried none -- reported as ok:true with a full tally either way. The row is still
    imported without its vector, because a bad embedding is not a bad memory; what
    changes is that the caller learns which line lost one.
    """
    b64 = record.get("embedding_b64")
    if not b64:
        return None

    def _reject(why: str) -> None:
        tally.errors.append(
            f"Line {line_num}: {kind} embedding not restored ({why}); the row is stored "
            "without one and check_health's null-embedding repair can re-embed it"
        )

    try:
        decoded = base64.b64decode(b64)
    except (ValueError, TypeError):
        _reject("not valid base64")
        return None
    # bug-061: reject a blob whose length is not a whole number of float32s at ingestion,
    # so a truncated/crafted embedding cannot be stored and later crash np.frombuffer in
    # calibration (and, via ensure_calibrated_on_startup, the server boot). check_health's
    # null-embedding repair re-embeds the row from content.
    if not decoded or len(decoded) % 4 != 0:
        _reject("not a whole number of float32s")
        return None
    if not vector.stored_blob_is_finite(decoded):
        _reject("non-finite values")
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

    A path holding a NUL byte names no file. Every filesystem call raises
    ValueError on it rather than OSError, which the handlers and the ACL guard's
    file readers do not catch, so it is refused here, before any of them runs.
    """
    if not path or "\x00" in path:
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


# The ACL pattern meaning "every agent". Spelled out rather than imported: the
# guard imports this module lazily, and this one must not import the guard.
_EVERY_AGENT = "*"


def _header_agent(header: dict) -> str:
    """The agent an export header says the file belongs to.

    An export of one agent records that agent. An export of every agent records
    an empty agent_id, so the file belongs to every agent; a header that names
    no string at all cannot be tied to anyone and is treated the same way.
    """
    owner = header.get("agent_id")
    return owner if isinstance(owner, str) and owner else _EVERY_AGENT


def import_file_agents(input_path: str) -> set[str]:
    """Every agent an import file names, in its header and in its records.

    Under CPERSONA_EXPORT_DIR the export directory is shared by every agent, and
    a file's name says nothing about whose rows it holds. The ACL guard demands
    read on each agent returned here, so restoring a file into one agent
    requires being allowed to read every agent the file came from.

    The file is read the way do_import_memories reads it: confined, a regular
    file, bounded by MAX_IMPORT_BYTES, UTF-8 and one JSON object per line. A
    file the import would refuse to read names nothing; the import then
    returns its own error. A record without an agent_id names no agent; one
    whose agent_id is not a string names every agent.
    """
    confined = _confine_io_path(input_path)
    if confined is None:
        return set()
    try:
        st = os.stat(confined)
        if not stat.S_ISREG(st.st_mode) or st.st_size > config.MAX_IMPORT_BYTES:
            return set()
        with open(confined, "rb") as f:
            raw = f.read(config.MAX_IMPORT_BYTES + 1)
        if len(raw) > config.MAX_IMPORT_BYTES:
            return set()
        lines = raw.decode("utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return set()
    agents: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        rtype = record.get("_type")
        if rtype == "header":
            agents.add(_header_agent(record))
        elif rtype in ("memory", "episode", "profile", "entity", "relation"):
            # A declared-graph record is its agent's data as much as a memory is.
            aid = record.get("agent_id")
            if isinstance(aid, str):
                if aid:
                    agents.add(aid)
            elif aid is not None:
                agents.add(_EVERY_AGENT)
    return agents


def export_file_owner(output_path: str) -> str | None:
    """The agent an existing file at an export destination belongs to.

    do_export_memories replaces whatever is at its destination, and the export
    directory is shared by every agent. The ACL guard demands write on the
    agent returned here, so replacing a file requires being allowed to write the
    agent that file belongs to.

    None when nothing is there to replace (or the path is refused, which the
    export reports itself). The header's agent for an export. Every agent for
    anything that is not an export -- a file whose first line is not an export
    header cannot be shown to belong to any one agent.
    """
    confined = _confine_io_path(output_path)
    if confined is None:
        return None
    try:
        st = os.stat(confined)
    except FileNotFoundError:
        return None
    except OSError:
        return _EVERY_AGENT
    if not stat.S_ISREG(st.st_mode):
        return _EVERY_AGENT
    try:
        with open(confined, "rb") as f:
            first = f.readline(64 * 1024)
        header = json.loads(first.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return _EVERY_AGENT
    if not isinstance(header, dict) or header.get("_type") != "header":
        return _EVERY_AGENT
    return _header_agent(header)


def _export_json_column(text) -> tuple[object, str | None]:
    """A stored JSON column as an export record carries it: (value, verbatim text).

    A column holding JSON is written as the value it parses to, and the text is
    None. One holding text that is not JSON -- a legacy attribution string, a hand
    edit -- used to be written as {} with nothing to say so, and the restore stored
    '{}' in its place, locked rows included, which the invalid-JSON repair leaves
    alone on purpose. That text now travels verbatim beside the {} (as
    ``source_text`` / ``metadata_text``), and the import stores it back as it was.
    A reader that does not know the extra key still sees the {} it always saw.
    """
    if not text:
        return {}, None
    try:
        return json.loads(text), None
    except (ValueError, TypeError, RecursionError):
        # Only text can be written into the file; anything else keeps the old {}.
        return {}, text if isinstance(text, str) else None


async def do_export_memories(agent_id: str, output_path: str, include_embeddings: bool = False) -> dict:
    """Export memories, episodes, profiles and the declared graph to a JSONL file.

    The graph -- entities with their aliases, mentions and relations -- follows the
    records, keyed on the ids the file gives them, so import can point it at the
    ids the records get on the way back in. It is declared data that nothing can
    re-derive, so a logical backup without it is not a backup of it.

    **Deliberately not gated on no-persist, and it takes no ``session_key``.**
    A pause means "do not write to my memory"; this writes a file the caller
    asked for and not a single row. Gating it would return a fabricated
    all-zero tally that hides what a real export would have produced — the
    failure bug-048 and bug-079 corrected on the merge and import previews,
    where a read path had been short-circuited by the write guard. Because it
    consults no pause, a session key here would decide nothing, and it would be
    a description every client loads for a parameter that cannot act.

    Pinned by ``test_export_is_not_gated_by_a_pause``: if this ever grows a
    pause gate, that test names why it should not have one.
    """
    confined = _confine_io_path(output_path)
    if confined is None:
        return error_response(f"output_path rejected (path traversal or outside export dir): {output_path}")
    output_path = confined

    iso = isolation_where(agent_id=agent_id or None)
    iso_e = isolation_where(agent_id=agent_id or None, alias="e")
    iso_r = isolation_where(agent_id=agent_id or None, alias="r")

    out_dir = os.path.dirname(output_path)
    if out_dir:
        fileperms.makedirs_private(out_dir)

    exported_memories = 0
    exported_episodes = 0
    exported_profiles = 0
    exported_graph = {"entities": 0, "mentions": 0, "relations": 0}

    # bug-091: write to a temp file and os.replace() on success. Opening the
    # destination directly with "w" destroyed the previous backup the moment the
    # export started, and a mid-export fault (disk full, process kill) left a
    # truncated JSONL that a later restore accepted as complete.
    # bug-350: the name was derived from the output path and the process id, which
    # separates two PROCESSES and not two calls inside one. Two coroutines exporting
    # different agents to the same path got the identical name, both opened it
    # truncating, and the write spans the paged-read suspension points below -- so
    # they interleaved into one inode and whichever reached the rename first
    # published a mixed, torn backup under ok:true while the other raised out of the
    # rename. mkstemp mints the name per call, the way the ledger write already does;
    # the rename stays the publication step. The descriptor is closed immediately and
    # the path reopened through fileperms, so the 0600 the mode is created with is the
    # mode the published file carries.
    _tmp_fd, tmp_path = tempfile.mkstemp(
        dir=out_dir or ".", prefix=os.path.basename(output_path) + ".tmp."
    )
    os.close(_tmp_fd)
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
            graph_sql = {
                "entities": (f"FROM entities e{iso_e.where}", iso_e.params),
                "mentions": (
                    f"FROM entity_mentions m JOIN entities e ON e.id = m.entity_id{iso_e.where}", iso_e.params
                ),
                "relations": (f"FROM relations r{iso_r.where}", iso_r.params),
            }
            graph_counts = {}
            for kind, (from_sql, params) in graph_sql.items():
                cur = await db.execute(f"SELECT COUNT(*) {from_sql}", params)
                graph_counts[kind] = (await cur.fetchone())[0]

            with fileperms.open_private(tmp_path, "w", encoding="utf-8") as f:
                header = {
                    "_type": "header",
                    # 1.1 adds the entity / mention / relation records and their counts.
                    # A 1.0 file has neither and imports exactly as it did.
                    "version": "cpersona-export/1.1",
                    "agent_id": agent_id,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "memory_count": memory_count,
                    "episode_count": episode_count,
                    "has_profile": profile_count > 0,
                    # bug-110: an exact count, so the import can detect a file cut at
                    # the episode/profile boundary (has_profile alone cannot).
                    "profile_count": profile_count,
                    "entity_count": graph_counts["entities"],
                    "mention_count": graph_counts["mentions"],
                    "relation_count": graph_counts["relations"],
                }
                f.write(json.dumps(header, ensure_ascii=False) + "\n")

                # bug-016: carry the project_id / channel γ-isolation axes and the
                # locked flag through export. bug-092: carry recall stats too.
                cur = await db.execute(
                    "SELECT id, agent_id, msg_id, content, source, timestamp, metadata, embedding, created_at,"
                    " project_id, channel, locked, recall_count, last_recalled_at, embedding_model"
                    f" FROM memories{iso.where} ORDER BY id",
                    iso.params,
                )
                while rows := await cur.fetchmany(500):
                    for row in rows:
                        source, source_text = _export_json_column(row[4])
                        metadata, metadata_text = _export_json_column(row[6])
                        record: dict = {
                            "_type": "memory",
                            "id": row[0],
                            "agent_id": row[1],
                            "msg_id": row[2],
                            "content": row[3],
                            "source": source,
                            "timestamp": row[5],
                            "metadata": metadata,
                            "created_at": row[8],
                            "project_id": row[9],
                            "channel": row[10],
                            "locked": int(row[11]) if row[11] else 0,
                            "recall_count": int(row[12]) if row[12] else 0,
                            "last_recalled_at": row[13],
                        }
                        if source_text is not None:
                            record["source_text"] = source_text
                        if metadata_text is not None:
                            record["metadata_text"] = metadata_text
                        if include_embeddings and row[7]:
                            record["embedding_b64"] = base64.b64encode(row[7]).decode("ascii")
                            # The label travels with the vector it describes, and only with it.
                            record["embedding_model"] = row[14]
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported_memories += 1

                cur = await db.execute(
                    "SELECT id, agent_id, summary, keywords, start_time, end_time, embedding, created_at, resolved,"
                    " project_id, channel, embedding_model"
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
                            record["embedding_model"] = row[11]
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

                # The declared graph, after the records it names so an import has
                # mapped them by the time it reads a reference.
                cur = await db.execute(
                    f"SELECT {associations.ENTITY_COLUMNS} {graph_sql['entities'][0]} ORDER BY e.id",
                    iso_e.params,
                )
                while rows := await cur.fetchmany(500):
                    aliases = await associations.aliases_of(db, [row[0] for row in rows])
                    for row in rows:
                        record = {"_type": "entity", **associations.entity_record(row, aliases.get(row[0], []))}
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported_graph["entities"] += 1
                cur = await db.execute(
                    f"SELECT {associations.MENTION_COLUMNS} {graph_sql['mentions'][0]} ORDER BY m.entity_id, m.ref",
                    iso_e.params,
                )
                while rows := await cur.fetchmany(500):
                    for row in rows:
                        record = {"_type": "mention", **associations.mention_record(row)}
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported_graph["mentions"] += 1
                cur = await db.execute(
                    f"SELECT {associations.RELATION_COLUMNS} {graph_sql['relations'][0]} ORDER BY r.id",
                    iso_r.params,
                )
                while rows := await cur.fetchmany(500):
                    for row in rows:
                        record = {"_type": "relation", **associations.relation_record(row)}
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported_graph["relations"] += 1
        os.replace(tmp_path, output_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise

    result = {
        "ok": True,
        "path": output_path,
        "memories": exported_memories,
        "episodes": exported_episodes,
        "profiles": exported_profiles,
    }
    if any(exported_graph.values()):
        result["associations"] = exported_graph
    return result


# How many per-line messages an import lists before it only counts the rest.
_IMPORT_ERRORS_SHOWN = 100


class _ImportErrors(list):
    """An import's per-line messages, kept to the first ``_IMPORT_ERRORS_SHOWN``.

    Every bad line used to add one message, and the whole list went back in the
    response. A file of short lines that are not export records -- a log or a CSV
    named by mistake, up to the size cap -- then held one string per line in memory
    and answered with a response some 24 times the size of the file. The first
    messages say what is wrong with the file; the rest are counted, so the report
    stays bounded and still says how many lines failed.

    Every handler adds through ``append``, which is why the bound lives here rather
    than at each of the sites that report a line.
    """

    def __init__(self) -> None:
        super().__init__()
        self.omitted = 0

    def append(self, message: str) -> None:
        if len(self) < _IMPORT_ERRORS_SHOWN:
            super().append(message)
        else:
            self.omitted += 1

    def reported(self) -> list[str]:
        """The messages for the response, with a count of those not listed."""
        if not self.omitted:
            return list(self)
        return [*self, f"{self.omitted} more errors were not listed"]


#: Episodes read in one statement while an agent's summary digests are collected.
_EPISODE_KEY_PAGE = 500


class _EpisodeSummaryIndex:
    """Where an agent's stored episode summaries live, for the episode dedup gate.

    Episodes have no uniqueness constraint and no index holding ``summary``, so the
    gate used to be one probe per incoming episode that SQLite answered by reading
    every episode in the (agent_id, project_id) bucket -- each row of a merge or a
    restore paid for everything stored before it, rows of the same run included, with
    the write lock held: quadratic in the bucket. Without a schema change, the bucket
    is read once instead: the first probe for an agent walks its episodes in keyset
    pages and keeps a 16-byte digest of each (project_id, channel, summary) mapped to
    the row id, and every later probe is a dictionary lookup. The summaries themselves
    are never kept, so what stays resident is a fixed few dozen bytes per episode,
    not the corpus.

    A digest hit is confirmed against the row by its primary key before it is
    believed, so a digest collision cannot skip a distinct episode. A value that is
    not text (a summary stored as a BLOB, say) cannot be digested with the meaning
    SQL's ``=`` gives it, so it falls back to the original probe.
    """

    def __init__(self) -> None:
        self._agents: dict[str, dict[bytes, int]] = {}
        # Agents holding a row the digest cannot stand for: every probe asks SQL.
        self._untyped: set[str] = set()

    @staticmethod
    def _key(project_id, channel, summary) -> bytes | None:
        if not (isinstance(project_id, str) and isinstance(channel, str) and isinstance(summary, str)):
            return None
        h = hashlib.blake2b(digest_size=16)
        for part in (project_id, channel, summary):
            raw = part.encode("utf-8", "surrogatepass")
            # Length-prefixed, so ("ab", "c") and ("a", "bc") cannot share a digest.
            h.update(len(raw).to_bytes(8, "little"))
            h.update(raw)
        return h.digest()

    async def _keys_for(self, db, agent_id: str) -> dict[bytes, int]:
        keys = self._agents.get(agent_id)
        if keys is not None:
            return keys
        keys = {}
        last_id = 0
        while True:
            # The unary plus walks the primary key from the last id, as the merge's
            # source reads do; an agent index would sort the agent's rows per page.
            rows = await db.execute_fetchall(
                "SELECT id, project_id, channel, summary FROM episodes"
                " WHERE +agent_id = ? AND id > ? ORDER BY id LIMIT ?",
                (agent_id, last_id, _EPISODE_KEY_PAGE),
            )
            if not rows:
                break
            last_id = rows[-1][0]
            for row_id, project_id, channel, summary in rows:
                key = self._key(project_id, channel, summary)
                if key is None:
                    self._untyped.add(agent_id)
                else:
                    keys.setdefault(key, row_id)
        self._agents[agent_id] = keys
        return keys

    async def _probe(self, db, agent_id: str, project_id, channel, summary) -> int | None:
        rows = await db.execute_fetchall(
            "SELECT id FROM episodes WHERE agent_id = ? AND project_id = ? AND channel = ?"
            " AND summary = ? LIMIT 1",
            (agent_id, project_id, channel, summary),
        )
        return rows[0][0] if rows else None

    async def find(self, db, agent_id: str, project_id, channel, summary) -> int | None:
        """The id of an episode the agent holds with this summary in this bucket."""
        keys = await self._keys_for(db, agent_id)
        key = self._key(project_id, channel, summary)
        if key is None or agent_id in self._untyped:
            return await self._probe(db, agent_id, project_id, channel, summary)
        row_id = keys.get(key)
        if row_id is None:
            return None
        rows = await db.execute_fetchall(
            "SELECT 1 FROM episodes WHERE id = ? AND agent_id = ? AND project_id = ? AND channel = ?"
            " AND summary = ?",
            (row_id, agent_id, project_id, channel, summary),
        )
        if rows:
            return row_id
        return await self._probe(db, agent_id, project_id, channel, summary)

    def add(self, agent_id: str, project_id, channel, summary, row_id: int) -> None:
        """Record an episode this run inserted, so a repeat later in the run finds it."""
        keys = self._agents.get(agent_id)
        key = self._key(project_id, channel, summary)
        if keys is None:
            return  # not read yet: the first probe will read this row from the table
        if key is None:
            self._untyped.add(agent_id)
        else:
            keys.setdefault(key, row_id)


async def _queue_derived_builds(agent_id: str, items: list[dict], session_key: str) -> None:
    """Queue node and block construction for the records a merge inserted.

    store and update_memory queue these for the record they write; a merge inserts
    rows by the batch and used to queue none, so a copied record had no blocks (and,
    past the token window, no nodes) until a restart's backfill or a health repair
    came round to it -- and a move had already dropped the source's blocks with the
    source row. (An import is documented not to queue them: OVERFLOW_TREE_DESIGN §4.)
    ``items`` are the remote-index entries the run queued, which are exactly the rows
    it inserted: ``{"id": "<kind>:<row id>", "text": ...}``.

    Called after the commit: the token report is a network call and must not run under
    the write lock. Never raises -- the records are stored, and the derived layer's
    absence changes no answer; the health check still finds anything missed here.
    """
    if not items:
        return
    records = []
    for item in items:
        kind, _, row_id = item["id"].partition(":")
        records.append((kind, int(row_id), item["text"]))
    if nodes.building_enabled():
        try:
            past_window = await nodes.overflowing(records)
        except Exception as e:  # the report is advisory to the write path
            logger.warning("token report failed after a bulk write, no nodes queued: %s", e)
            past_window = None
        for kind, row_id, _text in past_window or ():
            await nodes.queue_build(kind, row_id, agent_id, session_key)
    if blocks.building_enabled():
        for kind, row_id, _text in records:
            await blocks.queue_build(kind, row_id, agent_id, session_key)


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
    that stood between a preview and a live index write, and an audit found
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
    # N-03: rows restored carrying a timestamp ahead of this clock. Counted and
    # reported, never refused — see _import_memory_record for why a restore is
    # not the seam that gets to say no.
    future_timestamps: int = 0
    errors: _ImportErrors = field(default_factory=_ImportErrors)
    # Rows restored from records that carry no project_id key. An export written
    # before the project and channel axes were exported looks like that, and its rows
    # land in the global pool whatever project they lived in, so the import says how
    # many. Its header carries the same version string a later export's does, so the
    # records are the only witness.
    axisless_memories: int = 0
    axisless_episodes: int = 0
    # The (agent_id, user_id) key of each profile this file has restored, and the
    # line it came from. Several agents imported into one target share that key.
    profile_lines: dict[tuple[str, str], int] = field(default_factory=dict)
    # bug-091: track the header and the per-type record counts actually present in
    # the file, so a truncated backup (mid-export crash, partial copy) is rejected
    # instead of silently restoring a partial corpus with ok:true.
    file_header: dict | None = None
    file_memories: int = 0
    file_episodes: int = 0
    file_profiles: int = 0
    file_entities: int = 0
    file_mentions: int = 0
    file_relations: int = 0
    # The declared graph the file carries, and where each of its records now lives:
    # the file's `mem:<id>` / `ep:<id>` to the row imported for it, or the equal row
    # that was already here. The graph is written through this map after the records.
    graph: associations.Graph = field(default_factory=associations.Graph)
    ref_map: dict[str, str] = field(default_factory=dict)
    associations: dict | None = None
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
    # The episode dedup gate, read once per agent rather than once per record.
    episode_keys: _EpisodeSummaryIndex = field(default_factory=_EpisodeSummaryIndex)
    # bug-132: queue only rows actually inserted, then sync after commit.
    remote_items: dict[str, list[dict]] = field(default_factory=dict)
    # Whether each agent id the file names may receive rows, asked once per id.
    reserved_agents: dict[str, bool] = field(default_factory=dict)

    def queue_remote(self, agent_id: str, ref: str, text: str) -> None:
        """Queue a written row for the remote index. A preview queues nothing."""
        if self.dry_run:
            return
        self.remote_items.setdefault(agent_id, []).append({"id": ref, "text": text})

    def map_record(self, kind: str, record: dict, row_id: int | None) -> None:
        """Record which row now holds the file's record. A preview has no row id, so
        the file's own ref stands in: what is counted is that it maps at all."""
        file_id = record.get("id")
        if isinstance(file_id, int) and not isinstance(file_id, bool):
            self.ref_map[f"{kind}:{file_id}"] = f"{kind}:{file_id if row_id is None else row_id}"

    def refuses_agent(self, aid: str, kind: str, line_num: int) -> bool:
        """Whether a record must be passed over because its agent id is reserved.

        The ACL guard refuses a reserved ``target_agent_id``; without one, each
        record names its own agent, and only this loop sees those names. A
        restore must not create the alias-shaped agent the next server start
        would refuse (``acl.reserved_for_new_rows``).
        """
        refused = self.reserved_agents.get(aid)
        if refused is None:
            refused = self.reserved_agents[aid] = acl.reserved_for_new_rows(aid)
        if refused:
            self.errors.append(
                f"Line {line_num}: {kind} agent_id {aid!r} is reserved for subject aliases "
                "while per_subject is configured; it was not imported"
            )
        return refused


def _validate_import_preconditions(
    input_path: str, dry_run: bool, session_key: str = ""
) -> tuple[str | None, dict | None]:
    """Vet the request before any file or database work. Returns (path, error)."""
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
            f"input file exceeds CPERSONA_MAX_IMPORT_BYTES ({config.MAX_IMPORT_BYTES}): {confined}"
        )
    # Snapshot once: a TTL boundary mid-loop must not leave a half-written corpus.
    # bug-079: only gate the WRITE path on no-persist (the bug-048 fix, applied to the
    # import twin). A dry_run=True preview is write-free — every INSERT/UPSERT is
    # guarded by `if not dry_run` and the dry_run path runs on the read seam — so
    # short-circuiting it into a fabricated all-zero response masks what a real import
    # would do and contradicts the "read tools unaffected" no-persist contract.
    # bug-365: and it sits AFTER the vetting above, which is the doctrine the
    # calibration gates already state and credit to this path. A missing file, a path
    # outside the export directory, a directory and an oversized file were all
    # answered ok:true with an all-zero tally while writes were paused, so a rehearsal
    # of a restore that could never run read as a clean skip. Nothing above this line
    # writes anything, so returning the real error costs the pause nothing.
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key) and not dry_run:
        return None, session.make_skipped_response(
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
            key,
        )
    return confined, None


class _FieldRefused(ValueError):
    """A record value the import cannot store; its line is refused, not the file."""


def _bindable_text(key: str, value) -> str:
    """``value`` if it is text SQLite can bind, else _FieldRefused naming ``key``.

    A value bound as whatever JSON type the file held -- an object, a list, a
    number where text belongs -- or a string holding a lone surrogate (which
    JSON escapes can spell and UTF-8 cannot encode) raised at the INSERT. That
    aborted and rolled back the whole restore over one line, and only on the real
    run: the preview binds nothing and answered ok:true for the same file. Every
    value is checked here, on both arms, before the dry_run split, so a bad line
    is refused and reported the way an invalid JSON line is.
    """
    if not isinstance(value, str):
        raise _FieldRefused(f"{key} is not a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _FieldRefused(f"{key} is not valid UTF-8 text") from None
    return value


def _null_as_empty(record: dict, key: str) -> str:
    """A record field, with JSON null read as the empty value a missing key gets.

    bug-527: these fields land in NOT NULL columns. A null bound straight in was
    dropped without a word by the memory INSERT OR IGNORE and aborted the whole
    restore on the episode and profile INSERTs, while the preview -- which binds
    nothing -- counted the row as imported. Read on both arms, before the
    dry_run split, as bug-094 does for keywords.
    """
    value = record.get(key)
    return "" if value is None else _bindable_text(key, value)


def _optional_text(record: dict, key: str) -> str | None:
    """A nullable text field (a stamp the INSERT defaults or leaves NULL)."""
    value = record.get(key)
    return None if value is None else _bindable_text(key, value)


def _recall_count(record: dict) -> int:
    """The record's recall_count as an integer SQLite can bind.

    The values the old ``int(...)`` accepted -- an integer, a numeric string, a
    float -- still are. One it raised on, or one past SQLite's integer range,
    is refused for its line instead of aborting the restore.
    """
    value = record.get("recall_count")
    if not value:
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _FieldRefused("recall_count is not a whole number") from None
    if not fits_sqlite_integer(count):
        raise _FieldRefused("recall_count is out of range")
    return count


def _record_agent(record: dict, kind: str, target_agent_id: str, tally: "_ImportTally", line_num: int) -> str | None:
    """The agent a record is restored into, or None when its line is passed over."""
    aid = target_agent_id or record.get("agent_id", "")
    if not aid:
        tally.errors.append(f"Line {line_num}: {kind} missing agent_id")
        return None
    try:
        aid = _bindable_text("agent_id", aid)
    except _FieldRefused as e:
        tally.errors.append(f"Line {line_num}: {kind} {e}; it was not imported")
        return None
    if tally.refuses_agent(aid, kind, line_num):
        return None
    return aid


async def _import_line(handler, db, record: dict, aid: str, tally: "_ImportTally", line_num: int, kind: str) -> None:
    """Run one record handler; a value it cannot store refuses this line only.

    The handlers read and check every value before they count or write anything,
    so a refusal leaves nothing half-done for this line, and the preview refuses
    exactly what the real run does.
    """
    try:
        await handler(db, record, aid, tally, line_num)
    except _FieldRefused as e:
        if kind == "memory":
            tally.skipped_memories += 1
        elif kind == "episode":
            tally.skipped_episodes += 1
        tally.errors.append(f"Line {line_num}: {kind} {e}; it was not imported")


async def _import_memory_record(db, record: dict, aid: str, tally: _ImportTally, line_num: int) -> None:
    """Import one memory row, or account for why it was skipped.

    The two paths are asymmetric by necessity, not by accident: a real run lets
    the UNIQUE indexes do the deduplication and reads the outcome back from
    `rowcount`, while a preview has no INSERT to learn from and has to probe for
    the same collisions by hand. Keeping the previewed counts equal to a real
    run's is the whole contract (bug-056 / bug-070).

    bug-221: the content goes through the write path's own sanitiser first, like
    ``_import_profile_record`` does (bug-188). This file may have been
    produced by another DB, an older version or a hand edit, so import is the
    stricter of the two seams, not the looser one — a body that ``do_store``
    would refuse (empty after sanitisation) must not enter through the restore,
    and one it would cut must not enter longer than the cap it publishes.
    """
    content = record.get("content", "")
    if not content:
        tally.skipped_memories += 1
        return

    # Every value is read and checked before anything is counted or written: a
    # _FieldRefused raised here refuses the line (see _import_line).
    content = _bindable_text("content", content)
    msg_id = _null_as_empty(record, "msg_id")
    pid = _null_as_empty(record, "project_id")
    chan = _null_as_empty(record, "channel")
    # bug-527: the timestamp is bound on the real arm only, but the future-stamp
    # report below parses it on both, so a value that is not a string aborted the
    # preview and the restore alike. It is refused for this line instead.
    timestamp = _null_as_empty(record, "timestamp")
    # json.loads above reads NaN / Infinity / 1e999 into non-finite floats;
    # written back as bare tokens they are JSON SQLite rejects (see
    # json_dumps_finite), so the restore writes null in their place. A column the
    # export carried as text (see _export_json_column) is stored back verbatim.
    source = json_dumps_finite(record.get("source", {}))
    metadata = json_dumps_finite(record.get("metadata", {}))
    source = _optional_text(record, "source_text") or source
    metadata = _optional_text(record, "metadata_text") or metadata
    recall_count = _recall_count(record)
    last_recalled_at = _optional_text(record, "last_recalled_at")
    created_at = _optional_text(record, "created_at")

    content, truncated = sanitize_content_with_flag(content)
    if not content:
        tally.skipped_memories += 1
        tally.errors.append(f"Line {line_num}: memory is empty after sanitisation; it was not imported")
        return

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
            tally.map_record("mem", record, existing[0][0] if existing else None)
            return

    # bug-349: the content axis, asked on BOTH arms rather than only on the preview.
    # The real run used to lean entirely on INSERT OR IGNORE resolving against the v12
    # UNIQUE index -- and that index is created by a best-effort upgrade step whose
    # failure is swallowed with a warning saying SELECT deduplication remains. That was
    # true of the message-id axis, which has the probe above, and false of this one: on
    # a database where the index could not be created (two locked rows colliding on the
    # dedup key is enough, and the ladder then stamps the newer version so the step
    # never runs again) the same export imported twice reported one memory imported each
    # time and left two identical rows -- with a preview in between calling it a skip.
    # The index stays as the second line of defence; it is no longer the only one.
    dup = await db.execute_fetchall(
        "SELECT 1 FROM memories WHERE agent_id = ? AND project_id = ? AND channel = ? AND content = ? LIMIT 1",
        (aid, pid, chan, content),
    )
    if dup or (tally.dry_run and (aid, pid, chan, content) in tally.seen_content):
        tally.skipped_memories += 1
        tally.map_record("mem", record, await _equal_memory_id(db, aid, pid, chan, content) if dup else None)
        return

    if truncated:
        # Reported only for a row that is stored: a skipped line kept nothing, so
        # nothing of it was cut. The number is the cap the sanitiser cut at, read
        # through the same accessor, so the message cannot disagree with the cut.
        tally.errors.append(
            f"Line {line_num}: memory content exceeded the {content_cap()}-character cap "
            "and was truncated"
        )

    if not tally.dry_run:
        # bug-016: carry the project_id / channel γ-axes, the locked
        # flag and the embedding through, and INSERT OR IGNORE so a
        # collision with the v12 dedup UNIQUE index is a counted skip
        # rather than an uncaught IntegrityError that aborts the restore.
        # bug-092: created_at and the recall stats ride along too — the
        # old INSERT re-stamped every restored row with import time and
        # zeroed its recall-frequency boost.
        blob = _decode_embedding(record, tally, line_num, "memory")
        cur = await db.execute(
            "INSERT OR IGNORE INTO memories"
            " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata,"
            "  embedding, embedding_model, locked, recall_count, last_recalled_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (
                aid,
                pid,
                chan,
                msg_id,
                content,
                source,
                timestamp,
                metadata,
                blob,
                _imported_label(record, blob),
                1 if record.get("locked") else 0,
                recall_count,
                last_recalled_at,
                created_at,
            ),
        )
        if cur.rowcount == 0:
            # bug-527: both dedup keys were probed above, so an insert the database
            # still ignored was dropped for another reason -- OR IGNORE absorbs
            # NOT NULL and CHECK failures as well. Counting it as a duplicate would
            # hide a lost row behind the counter that means "already there".
            tally.skipped_memories += 1
            tally.errors.append(
                f"Line {line_num}: memory was not stored (the database ignored the insert)"
            )
            return
        tally.queue_remote(aid, f"mem:{cur.lastrowid}", content)
        tally.map_record("mem", record, cur.lastrowid)
    else:
        # bug-056/bug-070: the probe itself now runs on both arms (above). What stays
        # here is the half a preview has and a real run does not need: a real INSERT is
        # visible to the next record's probe on the same connection, so within-file
        # repeats deduplicate themselves; a preview writes nothing and has to remember
        # what it previewed.
        if msg_id:
            tally.seen_msgid.add((aid, pid, msg_id))
        tally.seen_content.add((aid, pid, chan, content))
        tally.map_record("mem", record, None)
    # N-03: a restore reports a stamp ahead of the clock and imports it anyway.
    #
    # This seam is deliberately not the one that refuses. `store` is an external
    # boundary — a caller handing us a moment it believes in — so a value past
    # the allowance is a claim to check. A restore is the same corpus coming
    # back, and the only faithful answer to "this row was already like this" is
    # to put it back like this. Refusing here would make export -> import
    # lossy for exactly the rows an operator most needs to see, and the round
    # trip is an invariant this project keeps (the same reasoning that keeps
    # import's dedup probes exact-bucket while store's are gamma-visible).
    #
    # So the finding is surfaced twice instead, in both directions: here as a
    # count on the run that carried them in, and afterwards by the
    # `future_timestamp` health check, which can name and repair the rows.
    if future_timestamp_issue(timestamp):
        tally.future_timestamps += 1
    if _lacks_isolation_axes(record):
        tally.axisless_memories += 1
    tally.imported_memories += 1


def _is_export_header(header: dict | None) -> bool:
    """Whether an import file's header is one an export of this server wrote."""
    version = header.get("version") if header else None
    return isinstance(version, str) and version.startswith("cpersona-export/")


def _lacks_isolation_axes(record: dict) -> bool:
    """Whether a record predates the export of the project and channel axes.

    The exporter has written project_id on every memory and episode since those
    axes were first exported, '' for the global pool included, so a record without
    the key came from an older export and is restored into the global pool.
    """
    return "project_id" not in record


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

    # As for memories, every value is checked before anything is counted or written.
    summary = _bindable_text("summary", summary)
    keywords = _bindable_text("keywords", keywords)
    ep_pid = _null_as_empty(record, "project_id")
    ep_chan = _null_as_empty(record, "channel")
    start_time = _optional_text(record, "start_time")
    end_time = _optional_text(record, "end_time")
    created_at = _optional_text(record, "created_at")

    summary, truncated = sanitize_content_with_flag(summary)
    if not summary:
        tally.skipped_episodes += 1
        tally.errors.append(f"Line {line_num}: episode summary is empty after sanitisation; it was not imported")
        return

    # bug-220: dedup against what is already stored. The dry_run arm carries the
    # within-file set for the same reason the memory path does (bug-070): a
    # preview has no INSERT of its own to collide against, while a real run sees
    # its own uncommitted rows (recorded in episode_keys as it inserts them).
    existing = await tally.episode_keys.find(db, aid, ep_pid, ep_chan, summary)
    if existing is not None or (tally.dry_run and (aid, ep_pid, ep_chan, summary) in tally.seen_episode):
        tally.skipped_episodes += 1
        tally.map_record("ep", record, existing)
        return

    if truncated:
        # As for memories: only a row that is stored can have been cut.
        tally.errors.append(
            f"Line {line_num}: episode summary exceeded the {content_cap()}-character cap "
            "and was truncated"
        )

    if not tally.dry_run:
        blob = _decode_embedding(record, tally, line_num, "episode")
        cur = await db.execute(
            "INSERT INTO episodes"
            " (agent_id, project_id, channel, summary, keywords, start_time, end_time, resolved,"
            "  embedding, embedding_model, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (
                aid,
                ep_pid,
                ep_chan,
                summary,
                keywords,
                start_time,
                end_time,
                1 if record.get("resolved") else 0,
                blob,
                _imported_label(record, blob),
                created_at,
            ),
        )
        tally.episode_keys.add(aid, ep_pid, ep_chan, summary, cur.lastrowid)
        tally.queue_remote(aid, f"ep:{cur.lastrowid}", summary)
        tally.map_record("ep", record, cur.lastrowid)
    else:
        tally.seen_episode.add((aid, ep_pid, ep_chan, summary))
        tally.map_record("ep", record, None)
    if _lacks_isolation_axes(record):
        tally.axisless_episodes += 1
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

    # As for memories, every value is checked before anything is counted or written.
    content = _bindable_text("content", content)
    user_id = _null_as_empty(record, "user_id")
    project_id = _null_as_empty(record, "project_id")
    stamp = _optional_text(record, "updated_at")

    content, truncated = sanitize_profile_with_flag(content)
    if not content:
        tally.errors.append(
            f"Line {line_num}: profile is empty after sanitisation; the existing profile was kept"
        )
        return

    # Several agents' profiles imported into one target all land on the target's
    # (agent_id, user_id) key, and the upsert below let each replace the one before:
    # only the last in file order survived, reported as profile_updated. The first
    # one this file restores to a key is kept and every later one is reported, the
    # way merge_memories skips a profile its target already has.
    first = tally.profile_lines.get((aid, user_id))
    if first is not None:
        tally.errors.append(
            f"Line {line_num}: a profile for agent {aid!r} and user_id {user_id!r} was already "
            f"restored from line {first}; this one was not imported"
        )
        return
    # bug-528: a restore does not move a profile backwards. The upsert below
    # replaces the stored row with the file's, so an older backup imported over a
    # live store -- a restore drill -- replaced newer text, moved updated_at back
    # and reported profile_updated. Memories and episodes skip what is already
    # there; a profile has no content key to deduplicate on, so its stamp decides.
    # Asked on both arms so the preview says what the real run will do. A file
    # stamp that does not parse makes no claim to be older, and is applied.
    file_time = _parse_timestamp_utc(stamp) if stamp is not None else None
    if file_time is not None:
        stored = await db.execute_fetchall(
            "SELECT updated_at FROM profiles WHERE agent_id = ? AND user_id = ?",
            (aid, user_id),
        )
        stored_stamp = stored[0][0] if stored else None
        stored_time = _parse_timestamp_utc(stored_stamp) if isinstance(stored_stamp, str) else None
        if stored_time is not None and stored_time > file_time:
            tally.errors.append(
                f"Line {line_num}: the stored profile (updated {stored_stamp}) is newer than "
                f"the file's ({stamp}); the stored profile was kept"
            )
            return

    if truncated:
        # Reported only for a profile that is applied, against the cap the
        # sanitiser cut at, read through the same accessor.
        tally.errors.append(
            f"Line {line_num}: profile exceeded the {profile_cap()}-character profile cap "
            "and was truncated"
        )

    tally.profile_lines[(aid, user_id)] = line_num
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
                project_id,
                user_id,
                content,
                stamp,
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
    # The graph section comes last, so it is where a cut file ends. A file written
    # before the graph was exported declares no graph counts and is not checked here.
    declared_graph = [tally.file_header.get(f"{kind}_count") for kind in ("entity", "mention", "relation")]
    in_file = [tally.file_entities, tally.file_mentions, tally.file_relations]
    if any(isinstance(d, int) and d != n for d, n in zip(declared_graph, in_file)):
        raise ValueError(
            "file truncated or inconsistent: header declares "
            "{} entities / {} mentions / {} relations, file contains {} / {} / {}".format(*declared_graph, *in_file)
        )


def _collect_graph_record(record: dict, rtype: str, target_agent_id: str, tally: _ImportTally, line_num: int) -> None:
    """Hold one declared-graph record until every record it can name has been imported."""
    if rtype == "entity":
        tally.file_entities += 1
    elif rtype == "mention":
        tally.file_mentions += 1
        tally.graph.mentions.append(record)
        return
    else:
        tally.file_relations += 1
    aid = target_agent_id or record.get("agent_id", "")
    if not aid or not isinstance(aid, str):
        tally.errors.append(f"Line {line_num}: {rtype} missing agent_id")
        return
    if rtype == "relation":
        tally.graph.relations.append({**record, "agent_id": aid})
        return
    entity_id = record.get("id")
    if not isinstance(entity_id, int) or isinstance(entity_id, bool):
        tally.errors.append(f"Line {line_num}: entity missing id")
        return
    tally.graph.entities[entity_id] = {**record, "agent_id": aid}


async def _import_graph(db, tally: _ImportTally) -> None:
    """Write the file's declared graph, pointed at the rows its records now live in."""
    if not tally.graph:
        return
    carried = await associations.carry(db, tally.graph, tally.ref_map, dry_run=tally.dry_run)
    tally.associations = carried.counts()
    if carried.dropped:
        tally.errors.append(
            f"{carried.dropped} declared associations were not restored: they are malformed, or name a "
            "record or entity this import did not restore"
        )
    if carried.aliases_dropped:
        tally.errors.append(
            f"{carried.aliases_dropped} aliases were not restored: each already names another entity "
            "in its scope"
        )


async def do_import_memories(
    input_path: str, target_agent_id: str = "", dry_run: bool = False, session_key: str = ""
) -> dict:
    """Import memories, episodes, and profiles from a JSONL file."""
    input_path, error = _validate_import_preconditions(input_path, dry_run, session_key)
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
                f"input file exceeds CPERSONA_MAX_IMPORT_BYTES ({config.MAX_IMPORT_BYTES}): {input_path}"
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
                except (json.JSONDecodeError, RecursionError) as e:
                    tally.errors.append(f"Line {line_num}: invalid JSON: {e}")
                    continue
                # A line that parses to something other than an object is no
                # record; reading a field from it raised and aborted the restore.
                if not isinstance(record, dict):
                    tally.errors.append(f"Line {line_num}: not a JSON object")
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
                    aid = _record_agent(record, "memory", target_agent_id, tally, line_num)
                    if aid is None:
                        continue
                    await _import_line(_import_memory_record, db, record, aid, tally, line_num, "memory")

                elif rtype == "episode":
                    tally.file_episodes += 1
                    aid = _record_agent(record, "episode", target_agent_id, tally, line_num)
                    if aid is None:
                        continue
                    await _import_line(_import_episode_record, db, record, aid, tally, line_num, "episode")

                elif rtype == "profile":
                    tally.file_profiles += 1
                    aid = _record_agent(record, "profile", target_agent_id, tally, line_num)
                    if aid is None:
                        continue
                    await _import_line(_import_profile_record, db, record, aid, tally, line_num, "profile")

                elif rtype in ("entity", "mention", "relation"):
                    _collect_graph_record(record, rtype, target_agent_id, tally, line_num)

                else:
                    if rtype:
                        tally.errors.append(f"Line {line_num}: unknown type '{rtype}'")

            # Inside the transaction scope: the raise must roll a real run back
            # rather than commit a partial restore (bug-091).
            _validate_file_header(tally)
            await _import_graph(db, tally)
            # Said only of a file that presents itself as an export. A hand-written
            # file without the key means the global pool; an export without it means
            # the axis was never written, whatever project the rows lived in.
            if (tally.axisless_memories or tally.axisless_episodes) and _is_export_header(tally.file_header):
                tally.errors.append(
                    f"{tally.axisless_memories} memory and {tally.axisless_episodes} episode records "
                    "carry no project_id, as an export written before the project and channel axes "
                    "were exported does not; they were restored into the global pool, which every "
                    "project reads"
                )

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
            result["errors"] = tally.errors.reported()
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
    if tally.associations is not None:
        result["associations"] = tally.associations
    if tally.future_timestamps:
        result["future_timestamps"] = tally.future_timestamps
    if tally.errors:
        result["errors"] = tally.errors.reported()
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
    # the dry_run preview matches.
    seen_summary: set = field(default_factory=set)
    # The memory axes of the same preview problem. The source's UNIQUE indexes would
    # make intra-batch content/msg_id collisions impossible, but their creation is best
    # effort (bug-349): on a database where it failed the source can hold two identical
    # rows, the real run skips the second against its own uncommitted copy, and a
    # preview that remembers nothing counted both as merged.
    seen_msgid: set = field(default_factory=set)
    seen_content: set = field(default_factory=set)
    # The episode dedup gate, read once for the target rather than once per row.
    episode_keys: _EpisodeSummaryIndex = field(default_factory=_EpisodeSummaryIndex)
    # bug-218/bug-222: the SOURCE ids this merge actually copied. mode='move' deletes
    # exactly these, so a row the copy pass skipped — a profile the target already holds
    # under the same user_id (profiles have no content-equivalence key, unlike the
    # memory msg_id/content and episode summary probes), a locked memory whose content
    # collided — stays where it is instead of being wiped with the agent.
    copied_memory_ids: list[int] = field(default_factory=list)
    copied_episode_ids: list[int] = field(default_factory=list)
    copied_profile_ids: list[int] = field(default_factory=list)
    # The source ref of every record that now exists in the target -- the copy, or
    # the equal row the target already held -- mapped to its target ref. The declared
    # graph is carried through it: a declaration names records by ref, and a move
    # deletes the source rows it names.
    ref_map: dict[str, str] = field(default_factory=dict)
    # False when some of the source's declarations could not be carried; the move
    # then leaves the source's graph where it is rather than delete the only copy.
    graph_carried: bool = True
    # bug-132: queue only rows actually inserted, then sync after commit.
    remote_items: list[dict] = field(default_factory=list)

    def queue_remote(self, ref: str, text: str) -> None:
        """Queue a written row for the remote index. A preview queues nothing."""
        if self.dry_run:
            return
        self.remote_items.append({"id": ref, "text": text})

    def map_record(self, kind: str, src_id: int, target_id: int | None) -> None:
        """Record where a source record lives in the target. A preview has no target
        id, so the source ref stands in: what is counted is that it maps at all."""
        self.ref_map[f"{kind}:{src_id}"] = f"{kind}:{src_id if target_id is None else target_id}"


async def _merge_memory_rows(db, source_agent_id: str, target_agent_id: str, tally: _MergeTally) -> None:
    """Copy the source agent's memory rows into the target, skipping collisions."""
    # bug-131: preserve the ranking metadata used by confidence and decay.
    # bug-222: the source id rides along so mode='move' can delete the copied rows
    # only — a skipped row (locked, or colliding on msg_id/content) is not ours to drop.
    # bug-345: read in pages. This statement used to return thirteen columns -- content
    # and the embedding among them -- for the entire source agent with no LIMIT, and it
    # ran inside the transaction, so the resident set of one read equalled the corpus
    # while the process-wide write lock was held. Measured over 1200 rows carrying
    # 1024-dimension vectors: 5.23 MB in a single list, lock held. The peer bulk path in
    # this same file already pages, with a comment saying the corpus must not be
    # materialised.
    #
    # `+agent_id`: the unary plus keeps the planner from serving the agent term
    # from an agent index, whose order is not id order -- it then sorted every row
    # of the source agent to return each page, inside the transaction. Without the
    # index the page walks the primary key from the last id it returned.
    last_src_id = 0
    while True:
        rows = await db.execute_fetchall(
            "SELECT id, project_id, msg_id, content, source, timestamp, metadata, channel, embedding, locked,"
            " created_at, recall_count, last_recalled_at, embedding_model"
            " FROM memories WHERE +agent_id = ? AND id > ? ORDER BY id LIMIT ?",
            (source_agent_id, last_src_id, _MERGE_READ_CHUNK),
        )
        if not rows:
            return
        last_src_id = rows[-1][0]
        await _merge_memory_page(db, rows, target_agent_id, tally)


async def _merge_memory_page(db, rows, target_agent_id: str, tally: _MergeTally) -> None:
    """Copy one page of source memory rows (bug-345 split out of the paging loop)."""
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
        embedding_model,
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
            if existing or (tally.dry_run and (project_id, msg_id) in tally.seen_msgid):
                tally.skipped_memories += 1
                tally.map_record("mem", src_id, existing[0][0] if existing else None)
                continue
        # bug-349: the import twin, for the same reason -- strategy='skip' must not
        # depend on an index whose creation is allowed to fail. Measured on a database
        # without it: the preview reported one skip and the real merge copied the
        # duplicate in.
        dup = await db.execute_fetchall(
            "SELECT 1 FROM memories WHERE agent_id = ? AND project_id = ? AND channel = ? AND content = ? LIMIT 1",
            (target_agent_id, project_id, channel, content),
        )
        if dup or (tally.dry_run and (project_id, channel, content) in tally.seen_content):
            tally.skipped_memories += 1
            tally.map_record(
                "mem",
                src_id,
                await _equal_memory_id(db, target_agent_id, project_id, channel, content) if dup else None,
            )
            continue
        if not tally.dry_run:
            cur = await db.execute(
                "INSERT OR IGNORE INTO memories"
                " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata, embedding,"
                "  embedding_model, locked, created_at, recall_count, last_recalled_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    embedding_model,
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
            if msg_id:
                tally.seen_msgid.add((project_id, msg_id))
            tally.seen_content.add((project_id, channel, content))
        tally.map_record("mem", src_id, None if tally.dry_run else cur.lastrowid)
        tally.merged_memories += 1


async def _merge_episode_rows(db, source_agent_id: str, target_agent_id: str, tally: _MergeTally) -> None:
    """Copy the source agent's episodes. No uniqueness index — the probe is the gate."""
    # bug-219: created_at is carried through, like _merge_memory_rows carries it
    # (bug-131) and the import path does (bug-092). Re-stamping a merged episode with
    # merge time moves the episode-boundary timestamp _get_episode_boundary_ts derives
    # from MAX(created_at) — every memory the target already held then scores as
    # prior-session — and becomes the episode's own recall timestamp whenever
    # start_time is NULL (bug-213).
    # bug-345: paged for the same reason as the memory pass, and with the same keyset
    # (and the same unary plus, for the same plan).
    last_src_id = 0
    while True:
        rows = await db.execute_fetchall(
            "SELECT id, summary, keywords, start_time, end_time, resolved, project_id, channel, embedding,"
            " created_at, embedding_model"
            " FROM episodes WHERE +agent_id = ? AND id > ? ORDER BY id LIMIT ?",
            (source_agent_id, last_src_id, _MERGE_READ_CHUNK),
        )
        if not rows:
            return
        last_src_id = rows[-1][0]
        await _merge_episode_page(db, rows, target_agent_id, tally)


async def _merge_episode_page(db, rows, target_agent_id: str, tally: _MergeTally) -> None:
    """Copy one page of source episodes (bug-345 split out of the paging loop)."""
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
        ep_embedding_model,
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
        # axes so the preview counts match a real run. The probe reads the target's
        # episodes once (_EpisodeSummaryIndex) instead of scanning the bucket per row.
        existing = await tally.episode_keys.find(db, target_agent_id, ep_project_id, ep_channel, summary)
        if existing is not None or (
            tally.dry_run and (target_agent_id, ep_project_id, ep_channel, summary) in tally.seen_summary
        ):
            tally.skipped_episodes += 1
            tally.map_record("ep", src_id, existing)
            continue
        if not tally.dry_run:
            cur = await db.execute(
                "INSERT INTO episodes"
                " (agent_id, project_id, channel, summary, keywords, start_time, end_time, resolved,"
                "  embedding, embedding_model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
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
                    ep_embedding_model,
                    created_at,
                ),
            )
            tally.copied_episode_ids.append(src_id)
            tally.episode_keys.add(target_agent_id, ep_project_id, ep_channel, summary, cur.lastrowid)
            tally.queue_remote(f"ep:{cur.lastrowid}", summary)
        else:
            tally.seen_summary.add((target_agent_id, ep_project_id, ep_channel, summary))
        tally.map_record("ep", src_id, None if tally.dry_run else cur.lastrowid)
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
    # updated_at rides along like created_at does on the memory (bug-131) and episode
    # (bug-219) passes, and as the import path keeps it (bug-223): stamping the merge
    # time made a long-stale profile look current, and deep_stale_profile, which reads
    # nothing else, stayed silent about it for its whole window.
    rows = await db.execute_fetchall(
        "SELECT id, user_id, content, project_id, updated_at FROM profiles WHERE agent_id = ?",
        (source_agent_id,),
    )
    for src_id, user_id, content, prof_project_id, updated_at in rows:
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
                " VALUES (?, ?, ?, ?, COALESCE(?, datetime('now')))",
                (target_agent_id, prof_project_id, user_id, content, updated_at),
            )
            tally.copied_profile_ids.append(src_id)
        tally.profile_copied = True


# bug-345: the copy passes read the source in pages of this size instead of handing the
# whole table back in one list. The bound is the export path's, so the two bulk paths in
# this file agree. Keyset paging (``id > last`` + ORDER BY + LIMIT) rather than one open
# cursor with fetchmany: the copy INSERTs into the same table it is reading, on the same
# connection, and SQLite leaves the result of a cursor whose table is written under it
# undefined. Each page is its own statement, so nothing is read through a mutation.
_MERGE_READ_CHUNK = 500

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

# The declared graph a move can leave behind. Reported only when nonzero.
_MOVE_LEFT_GRAPH_SQL = (
    ("entities", "SELECT COUNT(*) FROM entities WHERE agent_id = ?"),
    ("relations", "SELECT COUNT(*) FROM relations WHERE agent_id = ?"),
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

    The declared graph was carried to the target before this runs. Deleting the
    copied rows takes their mentions and anchored relations by trigger; what remains
    is the agent's entities and its record-free relations. Those go too when the
    move empties the source and the graph arrived whole -- they are copies now, as
    ``_delete_agent_rows`` treats them -- and otherwise they stay and are counted in
    ``left_at_source``, so a source still holding declarations is not reported empty.
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
        if tally.graph_carried:
            await db.execute("DELETE FROM entities WHERE agent_id = ?", (source_agent_id,))
            await db.execute("DELETE FROM relations WHERE agent_id = ?", (source_agent_id,))
    for kind, sql in _MOVE_LEFT_GRAPH_SQL:
        rows = await db.execute_fetchall(sql, (source_agent_id,))
        if rows[0][0]:
            left[kind] = rows[0][0]
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
    session_key: str = "",
) -> dict:
    """Merge memories, episodes, and profiles from one agent into another."""
    invalid = _validate_merge_arguments(source_agent_id, target_agent_id, strategy, mode)
    if invalid is not None:
        return invalid

    # Snapshot once: a TTL boundary mid-loop must not leave a half-written corpus.
    # bug-048: only gate the WRITE path on no-persist. A dry_run=True preview is
    # write-free (every mutation below is guarded by `if not dry_run`), so short-
    # circuiting it into a fabricated all-zero no-op masks what a real merge would
    # do and contradicts the "read tools unaffected" no-persist contract.
    # bug-365: the gate follows the argument validation above rather than preceding
    # it. Identical source and target, an unknown strategy and an unknown mode were
    # answered ok:true with an all-zero tally under a pause, so a request that can
    # never merge anything looked the same as one that merely waited.
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key) and not dry_run:
        return session.make_skipped_response(
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
            key,
        )

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

            # The declared graph follows the records it names, before a move deletes
            # them (their delete triggers would take every declaration with them).
            graph = await associations.read_graph(db, source_agent_id)
            carried = None
            if graph:
                carried = await associations.carry(
                    db, graph, tally.ref_map, agent_id=target_agent_id, dry_run=dry_run
                )
                tally.graph_carried = not (carried.dropped or carried.aliases_dropped)

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
    # The copies are new rows with no blocks or nodes of their own (and a move's
    # delete trigger has dropped the source's); queue them as store would.
    await _queue_derived_builds(target_agent_id, tally.remote_items, key)
    move_result = None
    if move_counts is not None:
        # bug-218/bug-222: the agent-level teardown (its calibration, its remote
        # namespace) belongs to a move that emptied the source. With rows left behind,
        # the agent is still live — purging would de-index memories it still holds.
        if not any(left_at_source.values()):
            _purge_agent_calibration(source_agent_id)
            await _purge_agent_remote_namespace(source_agent_id)
        else:
            # bug-346: the source is still live, so the namespace must stay -- but the
            # rows this move took out of it must not. The purge above is the teardown
            # for an emptied agent; this is the per-row cleanup the other two deleting
            # paths already do.
            await _remove_moved_source_vectors(source_agent_id, tally)
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
    if carried is not None:
        result["associations"] = carried.counts()
        not_carried = carried.dropped + carried.aliases_dropped
        if not_carried:
            # A declaration naming a record that was not copied, or an alias that
            # already names another entity in the target.
            result["associations"]["not_carried"] = not_carried
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
    # bug-393: the inert branch used to be a two-key literal, so the retry state
    # this tool advertises ("pending tasks, retry config"; docs/tools.md calls it
    # the depth AND retry state) was readable on one deployment and absent on the
    # other. The two values are module-level constants that exist whether or not a
    # queue runs, so nothing was preventing the answer.
    return {
        "enabled": False,
        "pending": 0,
        "max_retries": TASK_MAX_RETRIES,
        "retry_delay": TASK_RETRY_DELAY,
    }
