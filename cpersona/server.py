"""Cloto MCP Server: CPersona Memory.

Thin orchestration shell. Tool implementations live in module siblings:

  - config.py             — env var configuration
  - utils.py              — stateless helpers
  - database.py           — connection, schema, migrations
  - tasks.py              — MemoryTaskQueue + _task_queue singleton
  - vector.py             — _embedding_client singleton + _search_vector (EmbeddingClient from _vendored_mcp_common)
  - memory_handlers.py    — store / recall / recall_with_context / archive_episode
  - admin_handlers.py     — profile / list / delete / update / lock / agent_data / threshold / export / import / merge / queue_status
  - maintenance_handlers.py — check_health / deep_check

This shell:
  1. Imports do_* handlers
  2. Defines orchestration wrappers (do_update_profile_or_queue / do_archive_episode_or_queue)
  3. Registers the MCP tools (see the Tool Registry section below for the count)
  4. Wires HTTP/stdio transport
  5. main() initializes singletons (vector._embedding_client, tasks._task_queue) and runs the server
"""

import asyncio
import contextlib
import hmac
import ipaddress
import logging
import os
from typing import NamedTuple
from urllib.parse import urlparse

from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from cpersona import acl
from cpersona._vendored_mcp_common import no_persist
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona._vendored_mcp_common.mcp_utils import ToolRegistry, install_mgp_validation_filter

from cpersona import session
from cpersona import tasks
from cpersona import vector
from cpersona.admin_handlers import (
    do_calibrate_threshold,
    do_delete_agent_data,
    do_delete_episode,
    do_delete_memory,
    do_export_memories,
    do_get_profile,
    do_get_queue_status,
    do_get_recall_precision,
    do_import_memories,
    do_list_episodes,
    do_list_memories,
    do_lock_memory,
    do_merge_memories,
    do_set_recall_precision,
    do_unlock_memory,
    do_update_memory,
    do_update_profile,
    ensure_calibrated_on_startup,
)
from cpersona.config import (
    AUTO_CALIBRATE,
    CALIBRATE_ON_MODEL_CHANGE,
    EMBEDDING_API_KEY,
    EMBEDDING_API_URL,
    EMBEDDING_CACHE_SIZE,
    EMBEDDING_CACHE_TTL,
    EMBEDDING_MODE,
    EMBEDDING_MODEL,
    EMBEDDING_URL,
    STORE_BLOB,
    TASK_QUEUE_ENABLED,
    VECTOR_SEARCH_MODE,
    local_blobs_stored,
)
from cpersona import config
from cpersona import operating_context
from cpersona.session import resolve_session_key
from cpersona.database import close_db, init_db
from cpersona.maintenance_handlers import (
    do_check_health,
    do_deep_check,
    do_get_session_findings,
    do_migrate_channel_axis,
)
from cpersona.memory_handlers import (
    do_archive_episode,
    do_get_contents,
    do_recall,
    do_recall_with_context,
    do_store,
)
from cpersona import checks as checks_module
from cpersona.checks import HEALTH_CHECK_NAMES
from cpersona.utils import CANONICAL_SOURCE_TYPES, error_response, source_type_alias_summary

logger = logging.getLogger(__name__)


# =============================================================================
# Queue dispatch wrappers (thin orchestration over handlers + _task_queue)
# =============================================================================


async def do_update_profile_or_queue(agent_id: str, profile: str = "", session_key: str = "") -> dict:
    """Save pre-computed profile. Queue is bypassed since no LLM processing is needed."""
    return await do_update_profile(agent_id, profile=profile, session_key=session_key)


async def do_archive_episode_or_queue(
    agent_id: str,
    history: list,
    summary: str = "",
    keywords: str = "",
    resolved: bool | None = None,
    project_id: str = "",
    channel: str = "",
    session_key: str = "",
) -> dict:
    """Enqueue episode archival if task queue is enabled, otherwise run synchronously.

    When summary/keywords are pre-computed, bypass the queue and store directly
    (no LLM call needed, so queuing for retry is unnecessary).
    """
    # Gate the wrapper too: enqueue() itself writes to pending_memory_tasks,
    # so guarding only the synchronous do_archive_episode would still let the
    # queue path leak rows into SQLite.
    key, _declared = resolve_session_key(session_key)
    if session.is_paused_for(key):
        return session.make_skipped_response(
            {"ok": True, "queued": False, "task_id": None, "episode_id": None, "id": 0},
            "archive_episode",
            key,
        )
    if summary:
        return await do_archive_episode(
            agent_id,
            history,
            summary=summary,
            keywords=keywords,
            resolved=resolved,
            project_id=project_id,
            channel=channel,
            session_key=session_key,
        )
    # Server-side summary synthesis was removed prior to v2.4.10 (the queue no
    # longer has an LLM to turn raw history into a summary). Enqueuing an
    # empty-summary archive therefore produced a task the worker completed as a
    # no-op — the episode was silently dropped while the caller got
    # {ok:true, queued:true} (bug-006). Surface the misuse instead: callers MUST
    # pre-compute the summary (see the archive_episode cost-efficiency guidance).
    return {
        "ok": False,
        "episode_id": None,
        "error": (
            "summary is required: server-side episode summarisation was removed; "
            "pre-compute summary (and keywords) before calling archive_episode"
        ),
    }


# =============================================================================
# Operating-context boundary (2.5.1, docs/OPERATING_CONTEXT_DESIGN.md §5)
# =============================================================================

# Hard-layer gate for the six project_id-accepting tools. Boundary-layer only,
# same layering as the preview tier below: library callers (do_store/do_recall)
# never see the sentinel or the registry — resolution and validation happen
# here, so a direct library caller keeps today's exact semantics.


def _oc_annotate(result: dict, passed: str | None, resolved: str | None, warning: str | None) -> dict:
    """Attach the gate's advisory fields to a handler response (additive only)."""
    if warning:
        result["operating_context_warning"] = warning
    if passed == operating_context.AUTO_SENTINEL:
        # §5.2 transparency: the caller can always see what @auto became.
        result["resolved_project_id"] = resolved
    return result


def _oc_reject(error: str) -> dict:
    context = operating_context.get_context()
    return {
        "ok": False,
        "error": error,
        "operating_context_revision": context.revision if context else None,
    }


async def do_store_boundary(
    agent_id: str,
    message: dict,
    channel: str = "",
    project_id: str = "",
    session_key: str = "",
) -> dict:
    resolved, warning, error = operating_context.check_project_id(project_id, agent_id, write=True)
    if error:
        # b1-1: `result` is total over every store response, so the gate refusal
        # speaks the same contract as the handler's own rejections — a caller
        # branches on `result` alone and never has to know the gate exists.
        # `error` is kept for the shape the other five gated tools share.
        return {**_oc_reject(error), "result": "rejected", "reason": error}
    result = await do_store(
        agent_id, message, channel=channel, project_id=resolved, session_key=session_key
    )
    return _oc_annotate(result, project_id, resolved, warning)


async def do_archive_episode_boundary(
    agent_id: str,
    history: list,
    summary: str = "",
    keywords: str = "",
    resolved: bool | None = None,
    project_id: str = "",
    channel: str = "",
    session_key: str = "",
) -> dict:
    pid, warning, error = operating_context.check_project_id(project_id, agent_id, write=True)
    if error:
        # bug-169: every OTHER archive_episode failure carries episode_id (the
        # handler's own refusal, the or_queue wrapper's, the paused no-op), and
        # utils.error_response names it as the archetypal field a failure still
        # owes its caller. Only the gate refusal dropped it, so a caller reading
        # resp["episode_id"] hit a KeyError on exactly one path.
        return {**_oc_reject(error), "episode_id": None}
    result = await do_archive_episode_or_queue(
        agent_id,
        history,
        summary=summary,
        keywords=keywords,
        resolved=resolved,
        project_id=pid,
        channel=channel,
        session_key=session_key,
    )
    return _oc_annotate(result, project_id, pid, warning)


async def do_list_memories_boundary(agent_id: str, limit: int, project_id: str | None = None) -> dict:
    resolved, warning, error = operating_context.check_project_id(project_id, agent_id, write=False)
    if error:
        # bug-232 (the bug-169 class, on the read side): every OTHER failure mode of
        # this tool keeps its collection, so a caller reading resp["memories"] hit a
        # KeyError on exactly the gate-refusal path. A failure still owes its caller
        # the shape it documents.
        return {**_oc_reject(error), "memories": [], "count": 0}
    result = await do_list_memories(agent_id, limit, project_id=resolved)
    return _oc_annotate(result, project_id, resolved, warning)


async def do_list_episodes_boundary(agent_id: str, limit: int, project_id: str | None = None) -> dict:
    resolved, warning, error = operating_context.check_project_id(project_id, agent_id, write=False)
    if error:
        return {**_oc_reject(error), "episodes": [], "count": 0}  # bug-232
    result = await do_list_episodes(agent_id, limit, project_id=resolved)
    return _oc_annotate(result, project_id, resolved, warning)


# =============================================================================
# Recall preview boundary (2.5.0)
# =============================================================================

# The library layer (do_recall / do_recall_with_context) always returns full
# content — trimming is an MCP-boundary concern, the same layering as the Task
# #190 limit cap (library bounds resources, the boundary shapes the agent-facing
# payload). Direct library callers (bench full-ranking, future rerank) are
# untouched by design.


def _apply_preview(result: dict) -> dict:
    """Trim message content to the preview tier (config.RECALL_PREVIEW_CHARS).

    The preview is a PURE prefix — no ellipsis marker — so a preview fed back
    into a later call's exclude_contents still starts-with-matches the stored
    full text (the _content_excluded dedup contract). Trimmed messages gain
    content_truncated=true + content_len; their `ref` resolves the full row via
    get_contents. A cap of 0 disables trimming entirely.
    """
    cap = config.RECALL_PREVIEW_CHARS
    if cap <= 0:
        return result
    for m in result.get("messages", []):
        # bug-117: injected rows without a ref ([Profile], external_context echoes)
        # have no get_contents handle — truncating them would make their full
        # content permanently unreachable. Only trim rows the caller can expand.
        if not m.get("ref"):
            continue
        content = m.get("content")
        if isinstance(content, str) and len(content) > cap:
            m["content_len"] = len(content)
            m["content"] = content[:cap]
            m["content_truncated"] = True
    return result


# bug-211: the write-cap raise (2000 -> 16000) was followed
# on the read side only at get_contents. recall(full_content=true) bypasses
# _apply_preview entirely, so its worst case grew 8x with it — limit 100 x
# 16000 = 1.6M characters, an opt-in flag away. Same doctrine as
# GET_CONTENTS_MAX_CHARS: the budget is pinned at what the worst case USED to
# be (100 x 2000), deliberately not derived from MAX_CONTENT_LENGTH, so a
# relaxation of the write bound never enlarges the read blast radius again.
RECALL_FULL_CONTENT_MAX_CHARS = 200_000


def _apply_full_content_budget(result: dict) -> dict:
    """Bound a full_content recall response in characters (bug-211).

    Mirrors the get_contents budget discipline, adapted to recall's contract:
    recall returns the RANKED LIST, so rows past the budget are not dropped or
    deferred — they DEGRADE to the preview tier (pure prefix + content_len +
    content_truncated + ref), exactly what the caller would have seen without
    full_content, and get_contents fetches them whole.

    bug-214: the budget is spent from the END of `messages` backwards, because
    the tail is the valuable end in BOTH callers — do_recall reverses its ranked
    list before emitting it (most relevant LAST, memory_handlers.do_recall's
    `results.reverse()`), and do_recall_with_context sorts chronologically
    (newest last). Spending front-to-back admitted the weakest rows whole and
    degraded the strongest ones, i.e. the caller that paid for full_content got
    the full text of the rows it cares about least.

    Whole rows only: the budget never cuts a row that fits, and the LAST row is
    admitted whatever its size (the top-ranked row must stay reachable, not be
    made unreadable by its own length). Rows without a ref are never trimmed
    (bug-117 — no handle would remain to the lost tail) but still spend the
    budget they occupy.

    When the budget bites, the response carries full_content_budget_chars —
    absent otherwise, so a caller that never meets it sees the same shape as
    before (additive). CPERSONA_RECALL_PREVIEW_CHARS=0 disables the preview
    tier wholesale; degradation to a disabled tier would silently drop content,
    so it disables this budget too (the operator opted out of trimming).
    """
    cap = config.RECALL_PREVIEW_CHARS
    if cap <= 0:
        return result
    used = 0
    over_budget = False
    # bug-214: rank order, not payload order — the most valuable row is last.
    for m in reversed(result.get("messages", [])):
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if not over_budget:
            if used and used + len(content) > RECALL_FULL_CONTENT_MAX_CHARS:
                over_budget = True
            else:
                used += len(content)
                continue
        if not m.get("ref"):
            used += len(content)
            continue
        if len(content) > cap:
            m["content_len"] = len(content)
            m["content"] = content[:cap]
            m["content_truncated"] = True
        used += len(m["content"])
    if over_budget:
        result["full_content_budget_chars"] = RECALL_FULL_CONTENT_MAX_CHARS
    return result


async def do_recall_boundary(
    agent_id: str,
    query: str,
    limit: int,
    deep: bool,
    channel: str,
    exclude_contents: list,
    project_id: str | None,
    source_id: str,
    full_content: bool = False,
    session_key: str = "",
) -> dict:
    pid, warning, error = operating_context.check_project_id(project_id, agent_id, write=False)
    if error:
        # bug-232: `messages` is the documented shape of every recall response (the
        # preview / get_contents workflow tells callers to read it), so the gate
        # refusal carries the empty collection rather than making one path KeyError.
        return {**_oc_reject(error), "messages": []}
    result = await do_recall(
        agent_id,
        query,
        limit,
        deep=deep,
        channel=channel,
        exclude_contents=exclude_contents,
        project_id=pid,
        source_id=source_id,
        session_key=session_key,
    )
    result = _apply_full_content_budget(result) if full_content else _apply_preview(result)
    return _oc_annotate(result, project_id, pid, warning)


async def do_recall_with_context_boundary(
    agent_id: str,
    query: str,
    external_context: list,
    limit: int,
    channel: str,
    deep: bool,
    project_id: str | None,
    source_id: str,
    full_content: bool = False,
    session_key: str = "",
) -> dict:
    pid, warning, error = operating_context.check_project_id(project_id, agent_id, write=False)
    if error:
        return {**_oc_reject(error), "messages": []}  # bug-232
    result = await do_recall_with_context(
        agent_id,
        query,
        external_context=external_context,
        limit=limit,
        channel=channel,
        deep=deep,
        project_id=pid,
        source_id=source_id,
        session_key=session_key,
    )
    result = _apply_full_content_budget(result) if full_content else _apply_preview(result)
    return _oc_annotate(result, project_id, pid, warning)


# =============================================================================
# C26 (d): '@auto' is accepted by every project_id-taking tool (the six
# *_boundary wrappers above), but it used to be documented only on store —
# so five of the six schemas hid a sentinel the server implements. One
# clause, appended everywhere it is true.
_AUTO_PROJECT_ID_CLAUSE = (
    "v2.5.1: pass '@auto' to resolve this agent's default from the server's operating context (the resolution is echoed as resolved_project_id; an unmapped agent yields operating_context_warning). "
    "bug-186: resolution requires a configured operating context. With none — the default, and equally the outcome of a sidecar that fails to parse — the sentinel is NOT resolved: it is stored and filtered as the literal project_id '@auto', resolved_project_id echoes '@auto', and no warning is raised. Read resolved_project_id before relying on the resolution."
)

# MCP Tool Registry — 29 tools
# =============================================================================

# 2.5.1 Soft layer (§4): the sidecar's instructions.summary rides the MCP
# initialize response verbatim.
#
# bug-252: it is read ONCE, here, at import time, and it stays that way for the
# life of the process. The SDK's Server keeps the string as an attribute set in
# __init__, and every create_initialization_options() returns that attribute —
# so a client that reconnects to a RUNNING server is re-served this text however
# old it is. Only restarting the process publishes an operator's edit. (A stdio
# client relaunches the server per session and therefore gets one for free; the
# streamable-HTTP transport, where one long-lived process serves every client,
# does not.) The comment here used to claim the opposite — "clients see operator
# edits on reconnect" — which was true of nothing.
#
# The Hard layer is NOT frozen with it: operating_context.get_context() re-parses
# the sidecar whenever its mtime changes, so project_id validation, '@auto'
# resolution and get_operating_context are live within the same process.
registry = ToolRegistry("cloto-mcp-cpersona", instructions=operating_context.instructions_text())


# One description, referenced by every schema that takes the key. The parameter is
# not free — each tool that accepts it carries this text in the tool list every client
# loads on every session — so it is written once and kept short on purpose
# (docs/SESSION_IDENTITY_DESIGN.md §6, which also commits to measuring the cost).
_SESSION_KEY_PROPERTY = {
    "type": "string",
    "description": (
        "Opaque session identity you declare — a partition hint, NOT authentication. "
        "It scopes this process's per-session state: the degraded-recall advisory's "
        '"already told you" memory, and which no-persist pause applies to this call. '
        "It does NOT "
        "filter stored data (use agent_id / project_id / channel for that), and it "
        "never reaches the database. Omit it to share one bucket with every other "
        "caller that omits it, which is the behaviour that predates this parameter."
    ),
    "default": "",
}

# Arm D of the stage 2 cost decision (design §6): the full text above is kept only
# on recall / recall_with_context, where "does this filter which memories I can
# see?" is the question a reader actually arrives with. Every other keyed tool
# carries this compressed form. It keeps the clause that prevents that misreading
# — dropping it is what makes arm C cheaper — and drops the elaboration around it.
# Measured on the serialized tool list: 509 chars per tool for the full text, 289
# for this one. That difference across twenty tools is what makes stage 2
# affordable; see docs/SESSION_IDENTITY_DESIGN.md §6 for the arms.
_SESSION_KEY_PROPERTY_SHORT = {
    "type": "string",
    "description": (
        "Opaque session identity you declare: a partition hint, not authentication "
        "and not a data filter. Selects which no-persist pause applies to this call. "
        "Omit to share one bucket with every caller that omits it. Full text on "
        "recall."
    ),
    "default": "",
}

# Session no-persist controls — registered first for discoverability.
async def do_pause_persistence(
    ttl_seconds: int = no_persist.DEFAULT_TTL_SECONDS, session_key: str = ""
) -> dict:
    """Pause persistence for the caller's session (or the shared bucket) for a TTL window.

    ``session_key`` scopes the pause: a declared key silences that session's writes and
    nobody else's, and the response says so in ``scope``. A caller that declares nothing
    lands in the one bucket every keyless caller shares — which under stdio is the
    session, and under the shared HTTP transport is still process-wide.
    """
    key, declared = resolve_session_key(session_key)
    try:
        return session.pause_for(key, declared, ttl_seconds)
    except ValueError as e:
        return error_response(str(e))


async def do_resume_persistence(session_key: str = "") -> dict:
    """Re-enable persistence immediately, clearing this bucket's active TTL.

    A caller clears the bucket its key names and no other: ``was_active`` reports whether
    *this* key was paused. Reaching a different key's pause requires sending that key —
    which nothing prevents, because the key is compared and never verified. The partition
    is between keys, not between callers.
    """
    key, declared = resolve_session_key(session_key)
    return session.resume_for(key, declared)


async def do_persistence_status(session_key: str = "") -> dict:
    """Report whether this caller's writes are currently being skipped, and the TTL left."""
    key, declared = resolve_session_key(session_key)
    return session.pause_status_for(key, declared)


registry.auto_tool(
    "pause_persistence",
    "Pause write operations on this MCP server for an opt-in TTL window. While "
    # C26 (c): the list was four tools short and omitted the fix-downgrade.
    # bug-166: it also promised a uniform no-op body that only two tools return.
    # `persisted: false` is the one field every skipped write carries, so it is
    # the field to branch on; the id sentinel and the dry-run downgrade are
    # per-tool details, stated here as such rather than as a blanket rule.
    "paused, every write tool — store, archive_episode, update_memory, delete_memory, "
    "delete_episode, delete_agent_data, lock_memory, unlock_memory, update_profile, "
    "import_memories, merge_memories, calibrate_threshold, set_recall_precision — "
    "returns a no-op response carrying `persisted: false`, `dry_run: true` and a "
    "`reason` (with the TTL remaining) instead of writing to the database. "
    "`persisted: false` is the authoritative signal: branch on it, not on an id. "
    'Where the success shape has an `id`, it reads `"no-persist"` (store, '
    "archive_episode); action-specific id keys (deleted_id / updated_id / "
    "locked_id / unlocked_id / episode_id) are blanked to null so a truthy echo "
    "cannot read as success. migrate_channel_axis is gated differently — it is "
    "forced to dry_run and reports repairs_skipped rather than returning a "
    "skipped-response, so it carries no `persisted` key. check_health and "
    "deep_check are not blocked but downgrade to fix=false (they answer with "
    "repairs_skipped: true). Read tools (recall, list_*, get_profile, etc.) still "
    "answer normally, except that recall suppresses its recall_count / "
    "last_recalled_at bump — a write that would otherwise move ranking state during "
    "a paused session. **Blast radius follows session_key (response `scope`). Pass the "
    'same session_key here and on your write calls and the pause covers that key alone '
    '(`scope: "session"`): a session that sends a different key is neither silenced by '
    "it nor able to clear it. The key is a partition hint, not a credential — it is "
    "compared, never verified — so anyone who sends the same string shares the pause. "
    "Omit "
    'it and you arm the bucket every keyless caller shares (`scope: "process"`) — on a '
    "streamable-HTTP deployment a single process serves every connected client, so a "
    "keyless pause silences writes for every other keyless session until resume or TTL "
    "elapse, and those sessions get no signal. Under stdio (one process per client) "
    "that bucket is the session.** This affects only this MCP server "
    "(cpersona); call cscheduler's pause_persistence too if you want both paused. "
    "Use for benchmarking, AB testing, or ephemeral exploration where memory "
    "contamination must be avoided. Default TTL: 1800 seconds (30 minutes); upper "
    "bound: 86400 seconds (1 day).",
    {
        "type": "object",
        "properties": {
            "ttl_seconds": {
                "type": "integer",
                "description": "TTL until automatic resume. Min 1, max 86400 (clamped). Default 1800.",
                "default": no_persist.DEFAULT_TTL_SECONDS,
                "minimum": 1,
                "maximum": no_persist.MAX_TTL_SECONDS,
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
    },
    do_pause_persistence,
    [("ttl_seconds", int, no_persist.DEFAULT_TTL_SECONDS), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "resume_persistence",
    "Re-enable persistence immediately, clearing this caller's active no-persist TTL. "
    "Returns was_active=true if THIS bucket was paused before the call. **It clears "
    "only the bucket session_key selects (response `scope`): with a session_key, your "
    "own pause and no other session's; without one, the shared keyless bucket, which "
    "on a streamable-HTTP deployment re-enables writes for every other keyless session "
    "too.**",
    {"type": "object", "properties": {"session_key": _SESSION_KEY_PROPERTY_SHORT}},
    do_resume_persistence,
    [("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "persistence_status",
    "Report whether persistence is currently paused and the TTL remaining (in "
    "seconds). It reports the bucket session_key selects (response `scope`), not the "
    "server as a whole: with a session_key it answers for your session only, so "
    "`paused: false` here does not mean no other session is paused. Without one it "
    "reflects the bucket every keyless caller shares, which on a streamable-HTTP "
    "deployment means `paused: true` may have been armed by a different keyless session.",
    {"type": "object", "properties": {"session_key": _SESSION_KEY_PROPERTY_SHORT}},
    do_persistence_status,
    [("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=True),
)


async def do_get_operating_context(section: str = "") -> dict:
    """Serve the operator-owned operating context (v2.5.1, read-only).

    No args → preview tier (revision, summary, registry, defaults, section
    names). section="X" → that doctrine section's full body. The write path is
    the filesystem, deliberately not MCP (§7).
    """
    context = operating_context.get_context()
    if context is None:
        state = operating_context.load_state()
        if not state["enabled"]:
            reason = "disabled (CPERSONA_OPERATING_CONTEXT=off)"
        elif state["parse_error"]:
            reason = f"sidecar unusable: {state['parse_error']}"
        else:
            reason = f"no sidecar file at {state['path']}"
        return {"ok": False, "enabled": False, "error": f"operating context is dormant — {reason}"}
    if section:
        body = context.doctrine.get(section)
        if body is None:
            return {
                "ok": False,
                "error": f"unknown doctrine section '{section}'",
                "doctrine_sections": sorted(context.doctrine),
            }
        return {"ok": True, "context_revision": context.revision, "section": section, "body": body}
    return {
        "ok": True,
        "context_revision": context.revision,
        "instructions_summary": context.summary,
        "registry": {"project_ids": context.project_ids, "enforce": context.enforce},
        "defaults": context.defaults,
        "doctrine_sections": sorted(context.doctrine),
    }


registry.auto_tool(
    "get_operating_context",
    "Read the server-served operating context (v2.5.1): the operator-owned doctrine "
    "distributed to every connected client. Without arguments returns the preview tier "
    "— context_revision, instructions_summary, project_id registry (+ enforce mode), "
    "@auto defaults, and doctrine section names. Pass section to fetch one section's "
    "full body. Read-only: the context is edited by the operator on the filesystem "
    "(~/.cpersona/operating-context.toml), never via MCP.",
    {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": "Doctrine section name to fetch in full (from doctrine_sections). Empty = preview tier.",
                "default": "",
            },
        },
    },
    do_get_operating_context,
    [("section", str, "")],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "store",
    "Store a message in agent memory for future recall. "
    # b1-1 (2.5.2b1, CONTRACT BREAK): `result` replaces the old `skipped` flag.
    "Every response carries result — the one field to branch on: "
    "'stored' (a new row was written; {ok:true, result:'stored', id:<row-id>, "
    "embedded:<bool>}, embedded true iff a local blob was persisted or the remote "
    "index push succeeded — false under EMBEDDING_MODE=none; the response also "
    "carries truncated:true when content exceeded the length cap and was shortened), "
    "'skipped' (nothing written and nothing wrong: {ok:true, result:'skipped', "
    "reason:...}; the msg_id / content dedup branches echo the pre-existing row's id, "
    "the OR IGNORE fallback reason='duplicate (unique index)' omits id by design — "
    "TOCTOU seam), or "
    "'rejected' (nothing written because the request was refused: {ok:false, "
    "result:'rejected', reason:...} — empty content, content that sanitizes to empty, "
    "or an operating-context project_id refusal, which also carries error). "
    "Note for pre-2.5.2b1 callers: ok is no longer unconditionally true, and "
    "skipped:true is gone — a rejection used to look like a success. "
    "reason is human-readable, not a stable machine token. "
    # bug-141: the no-persist branch has its own shape — document it so
    # consumers branch on `persisted`, not on key presence.
    "Under pause_persistence the write is skipped (result:'skipped') and the response "
    "carries persisted:false (id:'no-persist', embedded:false) — branch on persisted "
    "to tell a paused write apart from a dedup hit.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "message": {
                "type": "object",
                "description": (
                    "ClotoMessage to store. Legacy source shapes are normalized "
                    "server-side where unambiguous (e.g. lowercase type words, "
                    "Rust serde externally-tagged dicts, bare 'user'/'assistant' "
                    "strings); unknown shapes are stored verbatim and surfaced by "
                    "check_health(invalid_source_type)."
                ),
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Caller-supplied message id used for msg_id-based dedup (γ-project-scoped). Optional.",
                    },
                    "content": {
                        "type": "string",
                        # bug-168: "skipped" means ok:true in this tool's own
                        # vocabulary, so the old wording told callers to treat a
                        # refusal as a harmless no-op.
                        "description": "The text to store. Content that is empty — or that sanitizes to empty — is refused with ok:false, result:'rejected'.",
                    },
                    "source": {
                        "type": "object",
                        "description": (
                            "Attribution of who produced the content. Canonical shape is "
                            "{type, id, name}. Type is the discriminator; id / name identify "
                            "the concrete producer. Store null / empty {} only when the "
                            "producer is genuinely unknown. "
                            # bug-140: null and {} converge at the write seam;
                            # recall echoes {} for rows stored either way.
                            "A null source is normalized to {} at the write seam, so "
                            "both persist (and recall) as the anonymous {}."
                        ),
                        "properties": {
                            "type": {
                                "type": "string",
                                # bug-233: NO `enum` here. The MCP SDK validates every
                                # call against inputSchema before dispatch, so an enum of
                                # the canonical values rejected the legacy spellings the
                                # very next sentence promises to fold — the call died at
                                # the schema, normalize_source was never reached, and the
                                # write was lost instead of normalized. The canonical list
                                # stays in the description (still derived from
                                # utils.CANONICAL_SOURCE_TYPES / _TYPE_ALIASES, so the
                                # published contract cannot drift from the write seam);
                                # enforcement is the write seam's plus
                                # check_health(invalid_source_type).
                                "description": (
                                    "Producer role — send one of "
                                    + ", ".join(f"'{t}'" for t in CANONICAL_SOURCE_TYPES)
                                    + ". Legacy producers that cannot are folded server-side "
                                    "at the write seam (" + source_type_alias_summary() + "), "
                                    "and shapes outside that table are stored verbatim for "
                                    "check_health(invalid_source_type) to surface."
                                ),
                            },
                            "id": {
                                "type": "string",
                                "description": "Stable producer id (e.g. discord user id, agent id). Empty when anonymous.",
                            },
                            "name": {
                                "type": "string",
                                "description": "Human-readable label for display. Empty when unknown.",
                            },
                        },
                    },
                    "timestamp": {
                        "type": "string",
                        "description": (
                            "UTC ISO-8601 timestamp with offset "
                            "(e.g. '2026-07-22T12:00:00+00:00'). Defaults to server-time UTC "
                            "when omitted. Aware non-UTC offsets are accepted; naive strings "
                            "are surfaced by check_health(timestamp_format_drift)."
                        ),
                    },
                    "metadata": {
                        "type": "object",
                        "description": (
                            "Free-form JSON object for producer-specific context. Empty when unused. "
                            # audit C12: the sidecars are bounded as of 2.5.2b1.
                            f"Serialised size is capped at {config.MAX_METADATA_LENGTH} characters "
                            "(same cap for source); an oversized field is refused with "
                            "result='rejected' rather than truncated, because a truncated JSON "
                            "document is not a JSON document."
                        ),
                    },
                },
            },
            "channel": {
                "type": "string",
                "description": "Memory channel for context separation (e.g. 'chat', 'discord'). Default: '' (shared).",
            },
            "project_id": {
                "type": "string",
                "description": (
                    "v2.4.17 isolation axis. Optional — omit or pass '' to "
                    "store in the global pool. Reads via γ semantics: a "
                    "recall with project_id='X' returns 'X' rows + global pool. "
                    + _AUTO_PROJECT_ID_CLAUSE
                ),
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["agent_id", "message"],
    },
    do_store_boundary,
    [
        ("agent_id", str),
        ("message", dict),
        ("channel", str, ""),
        ("project_id", str, ""),
        ("session_key", str, ""),
    ],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "recall",
    "Recall relevant memories using multi-strategy search (vector + FTS5 + keyword). "
    "Message content is returned as a preview tier by default — expand selected rows "
    "with get_contents(refs), or opt out wholesale with full_content=true. "
    "full_content is itself budgeted (200k chars per response, bug-211): rows "
    "past the budget degrade to the preview tier and the response carries "
    "full_content_budget_chars (absent when the budget never bites). "
    "v2.5.2 additive: each scored message carries match_reason={signal, score, ...} where "
    "signal is the branch the ranking / quality gate keyed on (confidence > rsf > cosine > rrf) "
    "and the remaining keys (cosine / rrf / rsf) surface the internal per-retriever "
    "contributions present on that row. Unscored rows (cascade FTS/keyword) omit match_reason. "
    "A response carrying gate_fallback=true (absent otherwise) means every candidate fell below "
    "the quality gate and the below-gate lexical matches were returned instead of an empty "
    "result — treat them as low-confidence.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "query": {"type": "string", "description": "Search query (empty returns recent memories)"},
            "limit": {
                "type": "integer",
                "description": "Per-retriever search depth, not a pure response cap: the value is handed to each retrieval channel (vector / episode FTS / keyword) as its top-K, so lowering it shrinks the candidate pool itself — rows beyond the depth are unreachable at any gate value, and score normalization / autocut operate on the smaller pool, which can also reorder what remains. Fewer rows than this may be returned. (Agent-facing cap; the library layer accepts up to the scan window for direct callers.)",
                "default": 10,
                "minimum": 0,
                "maximum": 100,
            },
            "deep": {
                "type": "boolean",
                "description": "Deep recall — halves the quality gate (and the calibrated fused gate), so weaker matches are admitted. It also disables time and completion decay, which are inert unless CPERSONA_CONFIDENCE_ENABLED=true, and it does NOT widen the scan window (CPERSONA_MAX_MEMORIES) — deep is about how weak a match may be, not how far back the search reaches.",
                "default": False,
            },
            "channel": {
                "type": "string",
                "description": "Filter memories by channel (e.g. 'chat', 'discord'). Default: '' (all channels).",
            },
            "exclude_contents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Normalized content strings to exclude from results (starts-with match). "
                "Used to prevent duplication with conversation context already known to the caller.",
            },
            "session_key": _SESSION_KEY_PROPERTY,
            "project_id": {
                "type": "string",
                "description": (
                    "v2.4.17 γ filter. Omit → no filter (all projects). "
                    "'' → global pool only. 'X' → 'X' bucket ∪ global pool. "
                    "Threaded through cascade / RRF / vector / FTS / keyword paths. "
                    + _AUTO_PROJECT_ID_CLAUSE
                ),
            },
            "source_id": {
                "type": "string",
                "description": (
                    "v2.4.20 per-user source filter. Empty (default) = no filter. "
                    "Non-empty = prefix match against json_extract(source, '$.id'), "
                    "e.g. 'discord:12345' to restrict to one Discord user, or "
                    "'discord:' to scope to all Discord-sourced memories. "
                    # C26 (e): the exception is real behaviour, not an edge case —
                    # the session-start grounding path relies on it.
                    "Episodes carry no per-user source tagging, so they are skipped "
                    "when this is set — UNLESS channel is also set, which scopes "
                    "episodes to one conversation and re-admits them."
                ),
                "default": "",
            },
            "full_content": {
                "type": "boolean",
                "default": False,
                "description": (
                    "v2.5.0 preview tier opt-out. By default message content longer than "
                    "the preview cap (CPERSONA_RECALL_PREVIEW_CHARS, default 500) is "
                    "returned as a pure prefix with content_truncated/content_len markers; "
                    "each message's `ref` expands via get_contents. true returns full text."
                ),
            },
        },
        "required": ["agent_id", "query"],
    },
    do_recall_boundary,
    [
        ("agent_id", str),
        ("query", str),
        ("limit", int, 10),
        ("deep", bool, False),
        ("channel", str, ""),
        ("exclude_contents", list, []),
        ("project_id", str, None),
        ("source_id", str, ""),
        ("full_content", bool, False),
        ("session_key", str, ""),
    ],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "recall_with_context",
    "Recall memories and merge with external conversation context. "
    "Automatically deduplicates, sorts chronologically, and returns a unified list. "
    "Replaces separate recall + manual merge in the caller. "
    "Content is preview-tiered by default — see recall's full_content / get_contents "
    "(full_content shares recall's 200k-char response budget, bug-211). "
    # audit C13: disclose the asymmetry instead of leaving it invisible.
    "Every external_context entry's content filters the recall (the caller already "
    "holds that text), but only role=user / role=assistant entries are merged into "
    "messages. When entries of other roles are present the response carries "
    # bug-172: the disclosure fires whenever such entries carry content, without
    # checking that a memory was in fact dropped — claiming suppression happened
    # overstated it. It reports what CAN filter invisibly, not what did.
    "context_filter_only={roles:[...]} — those entries filtered the recall without "
    "appearing in the output, whether or not they dropped a memory this time. "
    "gate_fallback=true (absent otherwise) is forwarded from the underlying recall: every "
    "candidate fell below the quality gate and the below-gate lexical matches were returned "
    "instead of an empty result — treat them as low-confidence.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID"},
            "query": {"type": "string", "description": "Search query"},
            "external_context": {
                "type": "array",
                # bug-163: the item shape was advertised in prose only, so a
                # caller had no schema-level statement that role / content are
                # strings. The server coerces either way (_ctx_role/_ctx_content);
                # this states the intent the handler assumes.
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
                "description": "Conversation history entries [{role, name?, user_id?, content, timestamp?}, ...]",
            },
            "limit": {
                "type": "integer",
                "description": "Per-retriever search depth for the underlying recall, not a pure response cap — same semantics as recall's limit: lowering it shrinks the candidate pool itself, not just the rows returned. (Agent-facing cap; the library layer accepts up to the scan window for direct callers.)",
                "default": 10,
                "minimum": 0,
                "maximum": 100,
            },
            "channel": {"type": "string", "description": "Memory channel filter"},
            "deep": {"type": "boolean", "description": "Deep recall — same semantics as in `recall`: halves the quality gate so weaker matches are admitted.", "default": False},
            "project_id": {
                "type": "string",
                "description": "v2.4.17 γ filter — passed through to recall. Same semantics as in `recall`. " + _AUTO_PROJECT_ID_CLAUSE,
            },
            "source_id": {
                "type": "string",
                "description": "v2.4.20 per-user source filter — passed through to recall. Same semantics as in `recall`.",
                "default": "",
            },
            "full_content": {
                "type": "boolean",
                "default": False,
                "description": "v2.5.0 preview tier opt-out — same semantics as in `recall`.",
            },
            "session_key": _SESSION_KEY_PROPERTY,
        },
        "required": ["agent_id", "query"],
    },
    do_recall_with_context_boundary,
    [
        ("agent_id", str),
        ("query", str),
        ("external_context", list, []),
        ("limit", int, 10),
        ("channel", str, ""),
        ("deep", bool, False),
        ("project_id", str, None),
        ("source_id", str, ""),
        ("full_content", bool, False),
        ("session_key", str, ""),
    ],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "get_contents",
    "Fetch full, untrimmed content for recall preview refs. Use after a preview-tier "
    "recall to expand only the rows that matter instead of opting the whole recall "
    "out with full_content=true. Bounded twice: at most 20 refs per call, and a "
    "40,000-character budget across the batch (2.5.4a2) that does not move when "
    "CPERSONA_MAX_CONTENT_LENGTH does. Rows are never cut to fit — when the budget "
    "is spent the remaining refs come back in `deferred` (absent otherwise) "
    "alongside `budget_chars`; re-fetch them in a second call. A single row larger "
    "than the budget is still returned in full, because this tool is the only path "
    "back to a row's complete text.",
    {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Agent identifier (ownership check — another agent's refs come back in `missing`)",
            },
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": "Refs from recall messages, e.g. ['mem:123', 'ep:45'] (max 20 per call)",
            },
        },
        "required": ["agent_id", "refs"],
    },
    do_get_contents,
    [("agent_id", str), ("refs", list, [])],
    annotations=ToolAnnotations(readOnlyHint=True),
)

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
    "Save a pre-computed agent profile to the database. "
    # The cap was invisible at this boundary while store / update_memory both
    # state theirs, and the profile row is the ONLY copy of that text (it is not
    # a memory row and has no ref), so an unannounced cut is unrecoverable.
    "The text passes through the same sanitizer as store, against the profile's own "
    f"ceiling: it is capped at {config.MAX_PROFILE_LENGTH} characters "
    "(CPERSONA_MAX_PROFILE_LENGTH) and the response carries truncated:true when the cap "
    "bit — branch on it, the discarded remainder is not stored anywhere else.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "profile": {
                "type": "string",
                "description": (
                    "Profile text to save (pre-computed by caller). Capped at "
                    f"{config.MAX_PROFILE_LENGTH} characters (CPERSONA_MAX_PROFILE_LENGTH); "
                    "the response says truncated:true when the cap cut it."
                ),
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["agent_id", "profile"],
    },
    do_update_profile_or_queue,
    [("agent_id", str), ("profile", str, ""), ("session_key", str, "")],
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
                "description": "Original conversation messages (used for start/end timestamp extraction; the episode embedding is computed from summary)",
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
            "project_id": {
                "type": "string",
                "description": "v2.4.17 isolation axis. Omit or pass '' for the global pool. " + _AUTO_PROJECT_ID_CLAUSE,
            },
            "channel": {
                "type": "string",
                "description": (
                    "v2.4.22 conversation-channel tag (e.g. a Discord channel id). "
                    "Default '' (= unscoped). Channel-scoped recall returns episodes "
                    "whose channel matches; this powers the per-channel episodic loop."
                ),
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["agent_id", "summary"],
    },
    do_archive_episode_boundary,
    [
        ("agent_id", str),
        ("history", list, []),
        ("summary", str, ""),
        ("keywords", str, ""),
        ("resolved", bool, None),
        ("project_id", str, ""),
        ("channel", str, ""),
        ("session_key", str, ""),
    ],
    # bug-064: NOT idempotent — do_archive_episode does a bare INSERT with no OR IGNORE and
    # no unique constraint, so every call appends a new episode. idempotentHint=True falsely
    # advertised retry-safety; a host retrying after a lost response would double-store the
    # episode (inflating recall + list_episodes). False is the safe, honest declaration.
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)

registry.auto_tool(
    "list_memories",
    (
        "List recent memories for an agent (for dashboard display). "
        "bug-255: the response holds a 1,000,000-character content budget. Rows are "
        "returned newest-first and none is dropped; once the budget is spent, later "
        "rows LONGER than the preview cap (CPERSONA_RECALL_PREVIEW_CHARS, default 500) "
        "degrade to a pure prefix with content_truncated/content_len and a `ref` that "
        "get_contents expands under the row's own agent_id (in an all-agents listing, "
        "pair the ref with the row's agent_id field). budget_chars appears iff at least "
        "one row was degraded. The effective ceiling is the budget plus one whole row "
        "plus the degraded rows' prefixes, so it scales with the preview cap; preview "
        "cap 0 disables trimming and the budget with it."
    ),
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier (empty for all agents)"},
            "limit": {"type": "integer", "description": "Max memories to return", "default": 100},
            "project_id": {
                "type": "string",
                "description": "v2.4.17 γ filter. Omit → no filter; '' → global pool only; 'X' → 'X' ∪ global pool. " + _AUTO_PROJECT_ID_CLAUSE,
            },
        },
        "required": [],
    },
    do_list_memories_boundary,
    [("agent_id", str), ("limit", int, 100), ("project_id", str, None)],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "list_episodes",
    (
        "List archived episodes for an agent (for dashboard display). "
        "bug-255: the response holds an 800,000-character budget across `summary` and "
        "`keywords` together, with the same degradation and ceiling semantics as "
        "list_memories — rows past the budget that exceed the preview cap carry pure "
        "prefixes plus summary_truncated/summary_len and keywords_truncated/"
        "keywords_len; budget_chars appears iff at least one row was degraded. Their "
        "`ref` expands the summary via get_contents (under the row's own agent_id); a "
        "full keywords string is only available through export_data."
    ),
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier (empty for all agents)"},
            "limit": {"type": "integer", "description": "Max episodes to return", "default": 50},
            "project_id": {
                "type": "string",
                "description": "v2.4.17 γ filter. Same semantics as list_memories. " + _AUTO_PROJECT_ID_CLAUSE,
            },
        },
        "required": [],
    },
    do_list_episodes_boundary,
    [("agent_id", str), ("limit", int, 50), ("project_id", str, None)],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "delete_agent_data",
    "Delete ALL data (memories, profiles, episodes) for a specific agent. Used by kernel during agent deletion.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID whose data should be purged"},
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["agent_id"],
    },
    do_delete_agent_data,
    [("agent_id", str), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
)

registry.auto_tool(
    "calibrate_threshold",
    "Auto-calibrate the vector search threshold from the null (random-pair) cosine "
    "distribution. Samples random memory pairs and places the threshold ABOVE the "
    "null mean so unrelated pairs are rejected. method='separation' (default) learns "
    "the operating point from two populations — null pairs vs temporally-adjacent "
    "same-session positives (nearest-neighbour fallback when too few exist); "
    "method='percentile' uses a quantile of the null distribution (robust to "
    "anisotropic models such as bge-m3); method='zscore' uses mean + z*std. No labels "
    "used, purely statistical. Adapts to both embedding model and corpus characteristics.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID whose memories to sample"},
            "sample_size": {"type": "integer", "description": "Number of embeddings to sample (default: 200)"},
            "z_factor": {"type": "number", "description": "Z-score multiplier for method='zscore' (default: 1.0, higher = stricter)"},
            # bug-231: the enum is rendered from config.CALIBRATE_METHODS, the same tuple
            # the handler validates against — an unknown spelling is now refused at the
            # boundary instead of silently taking the percentile branch.
            "method": {"type": "string", "enum": list(config.CALIBRATE_METHODS), "description": "'separation' (default; two-population — learns the operating point from null pairs vs temporally-adjacent same-session positives, falling back to nearest-neighbour when too few exist), 'percentile', or 'zscore'"},
            "percentile": {"type": "number", "description": "Null-distribution quantile for method='percentile' (default: 0.95, higher = stricter)"},
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["agent_id"],
    },
    do_calibrate_threshold,
    [
        ("agent_id", str),
        ("sample_size", int, 0),
        ("z_factor", float, 0),
        ("method", str, ""),
        ("percentile", float, 0),
        ("session_key", str, ""),
    ],
    # Mutates the agent's persisted calibration state; each run redraws the
    # sample, so repeating it is not idempotent (ACL_DESIGN.md §6 survey gap).
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)

registry.auto_tool(
    "set_recall_precision",
    "Set an agent's recall precision (knob 3) and recalibrate its quality gate. "
    "precision = strict | balanced | lenient maps to a specificity weight beta of "
    "2.0 / 1.0 / 0.5 in the gate separation objective (sensitivity + beta*specificity): "
    "strict sits the gate higher (fewer contaminants, more misses), lenient lower "
    "(fewer misses, more contaminants). A raw beta > 0 overrides the named level; an "
    "empty precision with beta <= 0 clears the per-agent override and returns the agent "
    "to the global CPERSONA_RECALL_PRECISION default. The gate is recalibrated at the new "
    "beta immediately and persisted, so the change is live without a restart. Precision is "
    "a per-agent setting, not a per-recall argument: the gate threshold is precomputed on "
    "the separation curve at a fixed beta, so this tool recalibrates once instead.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent whose precision to set"},
            "precision": {
                "type": "string",
                "description": "strict / balanced / lenient. Empty (with beta <= 0) clears the override.",
                "default": "",
            },
            "beta": {
                "type": "number",
                "description": "Raw specificity weight; overrides the named precision when > 0.",
                "default": 0,
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["agent_id"],
    },
    do_set_recall_precision,
    [
        ("agent_id", str),
        ("precision", str, ""),
        ("beta", float, 0),
        ("session_key", str, ""),
    ],
    # Writes the agent's beta override and recalibrates its gate; setting the
    # same precision twice lands in the same state (ACL_DESIGN.md §6 survey gap).
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "get_recall_precision",
    "Read an agent's effective recall precision (knob 3) — the read-back companion to "
    "set_recall_precision. Returns the resolved specificity weight (beta) and its named "
    "precision level (strict / balanced / lenient, or 'custom' for a raw beta), and flags "
    "whether the value is a per-agent override or the global CPERSONA_RECALL_PRECISION "
    "default (overridden + global_precision / global_beta). Read-only: it never "
    "recalibrates and never persists, so a UI can load the current setting, let the user "
    "edit it, and write it back instead of the control being write-only.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent whose precision to read"},
        },
        "required": ["agent_id"],
    },
    do_get_recall_precision,
    [
        ("agent_id", str),
    ],
    # bug-065: pure read (never recalibrates, never persists) — declare readOnlyHint like
    # every peer read tool (get_profile / list_memories / persistence_status / …) so a host
    # that auto-approves reads treats it consistently instead of prompting for a safe read.
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "delete_memory",
    "Delete a single memory by ID. Ownership is enforced when agent_id is provided.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID for ownership verification (injected by kernel)"},
            "memory_id": {"type": "integer", "description": "Memory ID to delete"},
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["memory_id"],
    },
    do_delete_memory,
    [("memory_id", int), ("agent_id", str), ("session_key", str, "")],
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
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["episode_id"],
    },
    do_delete_episode,
    [("episode_id", int), ("agent_id", str), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
)

registry.auto_tool(
    "update_memory",
    "Update memory content by ID. Rejects if memory is locked. Ownership enforced when "
    # bug-156 (C12): the edit path now applies the write path's content policy, so
    # state it here — the two seams used to disagree silently.
    "agent_id provided. The new content passes through the same sanitizer as store: "
    "it is capped at the content length limit (the response carries truncated:true "
    "when the cap bit) and [Memory from ...] annotations are stripped, so content "
    "consisting only of those is refused rather than written as an empty row.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID for ownership verification"},
            "memory_id": {"type": "integer", "description": "Memory ID to update"},
            "content": {"type": "string", "description": "New content for the memory"},
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["memory_id", "content"],
    },
    do_update_memory,
    [("memory_id", int), ("content", str), ("agent_id", str), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "lock_memory",
    "Lock a memory to prevent deletion and editing. Ownership enforced when agent_id provided.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID for ownership verification"},
            "memory_id": {"type": "integer", "description": "Memory ID to lock"},
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["memory_id"],
    },
    do_lock_memory,
    [("memory_id", int), ("agent_id", str), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "unlock_memory",
    "Unlock a memory to allow deletion and editing. Ownership enforced when agent_id provided.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID for ownership verification"},
            "memory_id": {"type": "integer", "description": "Memory ID to unlock"},
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["memory_id"],
    },
    do_unlock_memory,
    [("memory_id", int), ("agent_id", str), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
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
    # bug-054: export_memories WRITES/overwrites a caller-supplied filesystem path
    # (os.makedirs + open(path,'w') in do_export_memories), so it must NOT be
    # readOnlyHint=True — a host that auto-approves read-only tools would perform an
    # unconfirmed, environment-modifying (and potentially destructive) file write.
    # do_export_memories additionally confines output_path against traversal.
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)

registry.auto_tool(
    "import_memories",
    # bug-220: the claim now matches the code. Episodes were re-inserted on every run
    # while this said "idempotent" and the annotation below said idempotentHint=True,
    # which is what a host acts on when it retries a lost response.
    "Import memories, episodes, and profiles from a JSONL file. Idempotent: memories deduplicate on "
    "msg_id (and on content within a project/channel), episodes on their summary within a project/channel.",
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
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["input_path"],
    },
    do_import_memories,
    [("input_path", str), ("target_agent_id", str, ""), ("dry_run", bool, False), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
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
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
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
        ("session_key", str, ""),
    ],
    # bug-078: annotations must reflect the WORST reachable behavior. mode='move'
    # ends with do_delete_agent_data(source) — the same irreversible whole-agent wipe
    # the delete_agent_data tool declares destructiveHint=True for. Advertising
    # destructiveHint=False let that wipe bypass any host-side HITL approval gate
    # keyed on the hint (the bug-054 annotation-truthfulness class).
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
)

registry.auto_tool(
    "check_health",
    # C26 doc-drift class: the count is rendered from the registry, not typed
    # in prose (it said 20 while the registry held 23).
    f"Check memory database health ({len(HEALTH_CHECK_NAMES)}-check registry, each issue tagged with "
    "severity critical/warn/info). Detects contamination, duplicates, oversized "
    "content, embedding issues, FTS integrity (count + content-level), schema "
    "version/object drift (missing UNIQUE indexes or FTS triggers), SQLite file "
    "integrity, project_id naming drift, invalid JSON/timestamps, timestamp "
    "format drift, stale tasks, missing profiles, empty content, "
    "invalid/anonymous sources. Returns storage stats incl. project_id/channel "
    "distributions. Set fix=true to auto-repair (agent-scoped, locked-safe); "
    "critical file-integrity findings are report-only. Two repairs are lossy and "
    "irreversible, each against its own cap: oversized memories are cut to "
    "CPERSONA_MAX_CONTENT_LENGTH (default 16000 since 2.5.4a2) and the agent's "
    "profile row to CPERSONA_MAX_PROFILE_LENGTH (default 2000), keeping the "
    "start. Lower either cap and a fix run shortens rows that were within the "
    "old one. Some repairs are bounded per run (source canonicalisation "
    "classifies at most 1000 rows); a fix response carrying `remaining` > 0 "
    "with a re-run hint has NOT converged — run fix again until `remaining` "
    "stops decreasing. "
    "Use checks parameter to "
    # bug-230: an unrecognised name used to select nothing and answer 'healthy'.
    "run a subset — an unknown name is rejected (ok=false) rather than silently "
    "running nothing, and every response echoes `checks_run`. "
    # b1-3 (2.5.2b1, CONTRACT BREAK): one verdict, not two.
    "The verdict is `status`: healthy / degraded / unhealthy, "
    "derived from severity counts (info never degrades). The pre-2.5.2b1 "
    "`healthy` boolean (len(issues) == 0) is gone — it reported False for an "
    "info-only database that `status` called healthy; read `issues` / "
    "`severity_summary` for the underlying counts.",
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
            "checks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Registry check names to run (empty = all). See "
                "cpersona.checks.HEALTH_CHECK_NAMES.",
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
    },
    do_check_health,
    [("agent_id", str, ""), ("fix", bool, False), ("checks", list, []), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False),
)

registry.auto_tool(
    "get_session_findings",
    "Pull the storage-integrity findings on demand (SuperAuditor v1 pull contract, "
    "docs/SUPERAUDITOR_STANDARD.md) instead of reading them off check_health. Same "
    "detector as check_health(fix=false) over the WHOLE database, delivered as "
    "findings: each carries `kind` (the check registry name, so "
    "check_health(checks=[kind]) re-runs exactly that probe; escalation tiers are "
    "their own kinds, e.g. null_embedding_pipeline_down) and a static per-kind "
    "`severity` (critical = the read contract is broken now / warn = two stored facts "
    "contradict / info = an observation). check_health's own instance verdict rides "
    "along as `health_severity`; a probe that raised is reported as kind "
    "`check_crashed` instead of failing the pull, so a partial result says which "
    "probe is missing. Read-only, never repairs. NOT free, though: the registry runs "
    "unfiltered, which includes two whole-database reads (the FTS5 integrity-check over "
    "both indexes, and PRAGMA quick_check over the file), so every pull is O(database) "
    "on a channel meant to be pulled once a session — budget it by call frequency. There "
    "is deliberately no cheap subset: choosing which probes run would be choosing which "
    "forgotten state stays forgotten. Findings "
    "are NOT filtered by agent_id or project_id — the channel surfaces forgotten "
    "state, and slicing it by the caller's bucket would hide exactly the rows that "
    "were forgotten (scope a repair with check_health(agent_id=...)). Honest caps: "
    "`findings` holds at most per_kind_limit rows per kind, `capped_kinds` names every "
    "kind that had more (observed, not inferred from count == limit), `total` and "
    "the counts describe the RETURNED set only, and `per_kind_limit` echoes the limit "
    "applied. `summary` restates the same trimmed set in prose (pass "
    "include_summary=false to skip paying for it). On a shared remote transport with "
    "no session_key declared the response carries `identity_shared: true` — this "
    "server has no session-scoped probes, so the key is a partition hint, not a "
    "filter. `_meta.server_version` identifies the running instance.",
    {
        "type": "object",
        "properties": {
            # NOT _SESSION_KEY_PROPERTY_SHORT, and not an oversight. The shared
            # text says the key "selects which no-persist pause applies to this
            # call" — true of every tool that carries it except this one, which
            # consults no pause at all. Replacing this with the shared text to
            # make the surface uniform would make it uniformly false here. What
            # the key does on this tool is mark a keyless remote response
            # identity_shared, which is what it says.
            "session_key": {
                "type": "string",
                "description": "Opaque client-declared session identity (partition hint, not "
                "authentication). Empty on a non-stdio transport marks the response "
                "identity_shared.",
                "default": "",
            },
            "per_kind_limit": {
                "type": "integer",
                "description": "Maximum findings returned per kind (default 5, minimum 1). Kinds "
                "that hit it are listed in capped_kinds.",
                "default": 5,
            },
            "include_summary": {
                "type": "boolean",
                "description": "Include the human-readable `summary` rendering (default true). "
                "It restates `findings` in prose — set false when machine-reading.",
                "default": True,
            },
        },
    },
    do_get_session_findings,
    [("session_key", str, ""), ("per_kind_limit", int, 5), ("include_summary", bool, True)],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "deep_check",
    "Deep heuristic analysis of memory data quality. Detects issues requiring "
    "recovery or judgment (anonymous sources, short/trivial content, stale "
    "profiles, orphaned episodes, stale threshold calibration, embedding-space "
    "near-duplicate pairs as merge candidates). "
    # C26: rendered from checks.DEEP_FIX_CAPABLE — the prose named only two of
    # the four report-only checks, so fix=true silently no-opped on the others.
    f"fix=true applies repairs for: {', '.join(sorted(checks_module.DEEP_FIX_CAPABLE))}. "
    f"Report-only (fix is accepted and ignored): {', '.join(checks_module.DEEP_REPORT_ONLY)} "
    "— apply those decisions via merge_memories / delete_memory / "
    "calibrate_threshold / update_profile. Use checks parameter to select "
    "specific checks.",
    {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Agent ID to check (required)",
            },
            "fix": {
                "type": "boolean",
                "description": "Apply repairs (default: dry-run preview only)",
                "default": False,
            },
            "checks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Checks to run (empty = all). Options: anonymous_source, short_content, stale_profile, orphaned_episodes, calibration_staleness, near_duplicate",
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": ["agent_id"],
    },
    do_deep_check,
    [("agent_id", str), ("fix", bool, False), ("checks", list, []), ("session_key", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False),
)

registry.auto_tool(
    "migrate_channel_axis",
    "Re-channel bridge-type memories to their concrete channel (knob2 v2 default "
    "flip prep). Memories the kernel filed under the bridge type ('discord') are "
    "rewritten to the concrete channel recovered from the stored session_id "
    "('{channel_id}:{user_id}:{chunk}' | '{channel_id}:shared' → channel_id), so "
    "per-channel recall can match them. Non-destructive (only the channel column "
    "changes) and idempotent (re-running is a no-op once moved). dry_run=true "
    "(default) reports the recoverable count, the channels that would be recovered, "
    "and an unrecoverable bucket (channel='discord' rows with no snowflake "
    "session_id) without mutating. globalize_unrecoverable=true moves the "
    "unrecoverable bucket to channel='' (global, matched by every channel-scoped "
    "recall) so the flip orphans nothing; default false (report only).",
    {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Agent ID to migrate (empty = all agents)",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Preview counts only, no mutation (default: true)",
                "default": True,
            },
            "globalize_unrecoverable": {
                "type": "boolean",
                "description": "Also move channel='discord' rows with no snowflake session_id to channel='' (global). Default false.",
                "default": False,
            },
            "session_key": _SESSION_KEY_PROPERTY_SHORT,
        },
        "required": [],
    },
    do_migrate_channel_axis,
    [
        ("agent_id", str, ""),
        ("dry_run", bool, True),
        ("globalize_unrecoverable", bool, False),
        ("session_key", str, ""),
    ],
    annotations=ToolAnnotations(readOnlyHint=False),
)

# Capability guard (docs/ACL_DESIGN.md §5.2): wrap every handler registered
# above. Installed unconditionally — with no active ACL configuration the wrap
# passes straight through (legacy mode, zero decisions). This line must stay
# BELOW the last auto_tool registration; the §8 exhaustiveness test fails red
# if a tool is registered without a classification, and test_acl.py's
# wrap-coverage check fails if one is registered without the guard.
acl.install(registry)


# =============================================================================
# Streamable HTTP transport (Bearer auth, CORS)
# =============================================================================

# Peer names that belong to the local machine but are not IP literals, so
# _peer_is_remote cannot decide them arithmetically. Note what this does NOT
# mean: binding to loopback does not make the server unreachable from the
# network. A tunnel (cloudflared, ngrok), a reverse proxy, `kubectl
# port-forward`, or a published Docker port all forward to a loopback address.
# This is only used to tell a local peer from a remote one once a request has
# actually arrived.
_LOOPBACK_HTTP_HOSTS = frozenset({"localhost"})

# Headers a reverse proxy or tunnel adds on the way in. Their presence is
# evidence that the request did not originate on this machine, whatever the
# socket peer says. `via` is the RFC 7230 one and was missing; the
# x-forwarded-proto / -host pair is what a proxy configured to rewrite the URL
# but not the client address sends, which is a common nginx snippet.
_FORWARDED_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "forwarded",
    "via",
    "x-real-ip",
    "true-client-ip",
    "cf-connecting-ip",
)


def _peer_is_remote(peer: str) -> bool:
    """Is this socket peer somewhere other than this machine?

    Decided arithmetically rather than by string membership. The set-based
    version missed ``::ffff:127.0.0.1`` — the form a dual-stack listener
    (``CPERSONA_HTTP_HOST=::``) reports for an IPv4 client on the same host —
    and called it remote. That direction of error is the expensive one here:
    the warning latches after the first hit, so one false positive on a purely
    local request means the genuine remote arrival later in the same process is
    never reported. A detector whose noise silences its own signal is worse
    than no detector, and suppressing repeats is the reason this one exists.

    An unparseable peer is treated as remote. Erring loud is right for a
    warning: the cost is a line in the log, and the alternative is a silent
    miss of exactly the case nobody anticipated.
    """
    if not peer or peer in _LOOPBACK_HTTP_HOSTS:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return True
    # An IPv4-mapped IPv6 address is that IPv4 address; ask the mapped one.
    address = getattr(address, "ipv4_mapped", None) or address
    return not address.is_loopback


class _OAuthDiscovery(NamedTuple):
    """Resolved RFC 9728 discovery configuration (docs/OAUTH_DESIGN.md §7).

    Discovery only — nothing in this tuple verifies a token. It does decide
    *what* the other half accepts, though: ``resource`` is the audience a token
    must be minted for, and ``authorization_servers`` is the closed set of
    issuers whose keys will be trusted. See ``_oauth_verifier``.
    """

    #: The operator's strings, kept verbatim. RFC 8414 §3.3 and RFC 9728 §3.3
    #: both compare identifiers by identity: a client MUST NOT use metadata
    #: whose issuer / resource differs from the value it started with, and a
    #: normalised copy is a different value. AnyHttpUrl validates them below;
    #: it does not get to decide what is published (bug-266).
    resource: str
    authorization_servers: list[str]
    scopes: list[str]
    #: Exact request path the metadata document is served at. Matched
    #: exactly, never as a prefix — see BearerTokenMiddleware.__call__.
    metadata_path: str
    #: The full WWW-Authenticate value for a 401 while discovery is on.
    challenge: str


def _scope_tokens(raw: str) -> list[str]:
    """Split a configured scope string, dropping anything that is not a scope.

    RFC 6749 §3.3 defines a scope token as printable ASCII excluding the double
    quote and the backslash. That is not pedantry here: this value is
    interpolated into a quoted WWW-Authenticate parameter, so a quote arriving
    from configuration would end the parameter early and hand the client a
    malformed challenge — the one header discovery depends on. Filtering at the
    boundary keeps a configuration typo from becoming a protocol error, and
    warns rather than dropping silently.
    """
    kept, rejected = [], []
    for token in raw.replace(",", " ").split():
        if all("\x21" <= ch <= "\x7e" and ch not in '"\\' for ch in token):
            kept.append(token)
        else:
            rejected.append(token)
    if rejected:
        logger.warning(
            "ignoring %d scope value(s) that are not RFC 6749 scope tokens: %r",
            len(rejected),
            rejected,
        )
    return kept


def _oauth_discovery() -> "_OAuthDiscovery | None":
    """Resolve the discovery configuration, or None when the feature is off.

    Off is the default and off must be indistinguishable from the server that
    has never heard of OAuth: no route, no pass-through, and a 401 carrying the
    bare ``Bearer`` challenge it carries today. Both halves of the switch are
    required — a resource URI with no authorization server would publish a
    metadata document whose one mandatory field (RFC 9728 §2,
    ``authorization_servers``) is empty, which is worse than publishing
    nothing.

    A malformed URL warns and disables rather than raising. This mirrors the
    bug-133 handling of malformed numeric settings: a typo in an additive,
    optional setting must not stop the server from serving the callers that
    never asked for it. The warning is the part that keeps it from being a
    silent failure.
    """
    resource = (config.OAUTH_RESOURCE or "").strip()
    if not resource:
        return None
    # Whitespace or comma, so an operator can write either form.
    raw_servers = (config.OAUTH_AUTHORIZATION_SERVERS or "").replace(",", " ").split()
    if not raw_servers:
        logger.warning(
            "CPERSONA_OAUTH_RESOURCE is set but CPERSONA_OAUTH_AUTHORIZATION_SERVERS is "
            "empty; protected resource metadata is disabled (RFC 9728 requires at least "
            "one authorization server)."
        )
        return None
    from mcp.server.auth.routes import build_resource_metadata_url

    try:
        # Validated, then set aside: build_resource_metadata_url needs the
        # parsed form, the published document needs the written one.
        resource_url = AnyHttpUrl(resource)
        for server in raw_servers:
            AnyHttpUrl(server)
        metadata_url = build_resource_metadata_url(resource_url)
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "invalid OAuth discovery configuration (resource=%r, authorization_servers=%r): "
            "%s; protected resource metadata is disabled",
            resource,
            config.OAUTH_AUTHORIZATION_SERVERS,
            exc,
        )
        return None

    scopes = _scope_tokens(config.OAUTH_SCOPES or "")
    challenge = f'Bearer resource_metadata="{metadata_url}"'
    if scopes:
        # Measured (docs/OAUTH_DESIGN.md §2): the client adopts the scope the
        # 401 advertises, verbatim — so an advertised scope the issuer does
        # not define ends every authorization at invalid_scope. This branch is
        # for the operator who knows their issuer's scopes; the default
        # advertises none.
        challenge += f', scope="{" ".join(scopes)}"'
    return _OAuthDiscovery(
        resource=resource,
        authorization_servers=raw_servers,
        scopes=scopes,
        metadata_path=urlparse(str(metadata_url)).path,
        challenge=challenge,
    )


def _oauth_verifier(oauth: "_OAuthDiscovery | None", acl_config: "acl.AclConfig | None"):
    """Build the token verifier, or None when verification must stay off.

    Two conditions, and the second is the one worth reading twice.

    Discovery must be on. The same two settings enable both halves on purpose:
    the failure §7 set out to end was a client that could not find the door, and
    replacing it with a door that opens for nobody would be the same failure
    wearing a different status code.

    **ACL mode must be on.** The adopted design provisions grants per client
    (docs/OAUTH_DESIGN.md §8, §11). Without a grant table there is nothing to
    provision *against*: a verified token would authenticate, and with no
    enforcement layer behind it the holder would reach every tool — including
    delete_agent_data and the file-reading import/export. That is a fail-open,
    and it would arrive silently, so verification refuses to start instead and
    says why. Discovery is left on, because a client that can still find the
    issuer and is then refused has learned something true.
    """
    if oauth is None:
        return None
    if acl_config is None:
        logger.warning(
            "OAuth discovery is configured but no ACL file is active, so token "
            "verification stays OFF: a verified token would authenticate with no "
            "grant table to limit it, reaching every tool. Set CPERSONA_ACL_FILE "
            "and give the provider's clients grants (docs/ACL_DESIGN.md) to enable "
            "it. Discovery keeps working; clients will find the issuer and then be "
            "refused."
        )
        return None
    from cpersona.oauth import IdpTokenVerifier

    jwks_override = (config.OAUTH_JWKS_URI or "").strip()
    require_public_subject = bool(acl_config.per_subject_clients)
    if require_public_subject and jwks_override:
        # The override skips authorization-server metadata entirely, and the
        # metadata is where subject_types_supported would be read — so the
        # pairwise fail-closed check (docs/OAUTH_DESIGN.md §12) cannot run.
        # Said at startup, where the operator who set both is still looking.
        logger.warning(
            "CPERSONA_OAUTH_JWKS_URI bypasses authorization server metadata, so "
            "subject_types_supported cannot be checked; per-subject partitioning "
            "relies on the issuer using public subject identifiers — verify that "
            "yourself"
        )
    return IdpTokenVerifier(
        oauth.authorization_servers,
        oauth.resource,
        jwks_uri=jwks_override,
        require_public_subject=require_public_subject,
    )


def _preserve_empty_url_paths_in_metadata() -> None:
    """Stop the SDK's metadata model from rewriting a path-less identifier.

    ``AnyHttpUrl`` normalises ``https://host`` to ``https://host/``, and the
    document the SDK builds is typed with it — so an issuer written without a
    path is published with one no matter what this server passes in. That is
    not cosmetic: RFC 8414 §3.3 requires the ``issuer`` a client reads back to
    be *identical* to the value it started from and says the response MUST NOT
    be used otherwise, and RFC 9728 §3.3 says the same of ``resource``. An
    identifier we altered is a value no authorization server will ever return.

    Upstream fixed this by setting ``url_preserve_empty_path`` on the models
    (modelcontextprotocol/python-sdk#2925, closing #2883), and the fix ships
    in SDK 2.0 — a major upgrade this server has not taken yet. Rather than
    hand-writing the document to route around one config flag, the same flag
    is set here on the same models. Where the SDK already carries it this is a
    no-op, so the patch retires itself when the upgrade lands; both halves are
    needed until then, because a value this server normalised before handing
    it over arrives already altered (bug-266).
    """
    from pydantic import ConfigDict

    from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

    for model in (ProtectedResourceMetadata, OAuthMetadata):
        if model.model_config.get("url_preserve_empty_path"):
            continue
        try:
            model.model_config = ConfigDict(
                **{**dict(model.model_config), "url_preserve_empty_path": True}
            )
            model.model_rebuild(force=True)
        except Exception as exc:  # pragma: no cover - a pydantic that lacks the flag
            # Discovery still serves; identifiers keep the trailing slash the
            # specification objects to. Loud, because the alternative is a
            # conformance defect nobody is looking at.
            logger.warning(
                "could not preserve empty URL paths on %s (%s); published OAuth "
                "identifiers may differ from the configured ones",
                model.__name__,
                exc,
            )


def _bearer_credential(header: str) -> str:
    """The credential out of an ``Authorization: Bearer <token>`` header, or "".

    One implementation for both credential modes. They previously parsed the
    same header by different rules — the ACL branch case-insensitively, the
    static-token branch not — so a client sending the equally legal
    ``authorization: bearer <token>`` authenticated in ACL mode and got a bare
    401 from the same server, the same client and the same credential in
    ``CPERSONA_AUTH_TOKEN`` mode. RFC 7235 §2.1: the auth-scheme is
    case-insensitive. Divergence is prevented here rather than corrected in
    two places, because two spellings of one rule is what produced it
    (bug-265).
    """
    return header[7:] if header[:7].lower() == "bearer " else ""


class BearerTokenMiddleware:
    """Simple Bearer token authentication middleware.

    bug-134: module-level so the real class is unit-tested, not a hand-copied
    replica.
    """

    def __init__(
        self,
        app,
        auth_token: str = "",
        acl_config: "acl.AclConfig | None" = None,
        oauth: "_OAuthDiscovery | None" = None,
        oauth_verifier=None,
    ):
        self.app = app
        self.auth_token = auth_token
        # ACL mode (docs/ACL_DESIGN.md §5.1): token → Principal via the
        # identity seam; auth_token is not consulted while this is set (D3).
        self.acl_config = acl_config
        # RFC 9728 discovery, or None when the feature is off. Resolved once by
        # _build_http_app rather than per request: a malformed setting then
        # costs one warning at startup instead of a 500 on every arrival.
        self.oauth = oauth
        # The other half (docs/OAUTH_DESIGN.md §8): a verifier that turns a
        # provider-issued token into the same Principal the local table
        # produces. None whenever verification is off, which is the default and
        # is also what an operator who configured discovery without a grant
        # table gets — see _oauth_verifier.
        self.oauth_verifier = oauth_verifier
        self._exposure_warned = False

    def _unauthorized(self, scope, receive, send):
        """The 401 both credential modes return, with one challenge builder.

        The two modes had the header spelled out separately, so a change to one
        was a change to one. Discovery lives or dies on this header (RFC 9728
        §5.1 — it is one of the two ways a client can find the metadata), and a
        client that authenticates by static token would have been left with the
        bare challenge while the ACL path advertised discovery. One builder,
        both callers.
        """
        challenge = self.oauth.challenge if self.oauth is not None else "Bearer"
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )
        return response(scope, receive, send)

    def _is_public_metadata_request(self, scope) -> bool:
        """Is this the one unauthenticated request the server owes an answer?

        Protected resource metadata is a public document by construction: a
        client that cannot read it without a token cannot learn where to get a
        token, which is precisely today's failure. So this path — and only this
        path — is exempt from the credential check.

        Exact match, never a prefix. ``startswith`` would exempt
        ``/.well-known/oauth-protected-resource/mcp/../../mcp`` and anything
        else a caller can hang off the end of that string, turning one public
        document into an unauthenticated subtree. Reads only, too: the document
        is served by GET, and a POST to that path has no business skipping
        authentication.
        """
        if self.oauth is None:
            return False
        if scope.get("path") != self.oauth.metadata_path:
            return False
        return scope.get("method") in ("GET", "HEAD", "OPTIONS")

    async def _oauth_principal(self, presented: str) -> "acl.Principal | None":
        """The last resort of the composed resolver: a provider-issued token.

        Order is chosen for cost and attack surface rather than correctness
        (docs/OAUTH_DESIGN.md §8). The local comparisons are cheap and cannot be
        fooled by a remote token; the token parse is the only step that reads
        attacker-controlled structure, so it goes last. Every caller who
        authenticates the way they do today therefore reaches a verdict without
        this path running at all — which is what makes enabling OAuth additive
        for them.

        Rejection is silent here because the caller turns it into the same 401
        every other failed credential gets: a response that distinguished "your
        JWT was malformed" from "your token is not in the table" would hand a
        prober the shape of our configuration.
        """
        if self.oauth_verifier is None or not presented:
            return None
        verified = await self.oauth_verifier.verify_token(presented)
        if verified is None:
            return None
        # The seam carries the verified subject and its issuer onto the
        # principal (docs/OAUTH_DESIGN.md §12) — both were checked as signed
        # claims, which is what entitles enforcement to consume them. Static
        # resolvers leave these fields empty; only this one may fill them.
        claims = getattr(verified, "claims", None) or {}
        return acl.Principal(
            client_id=verified.client_id,
            issuer=str(claims.get("iss") or ""),
            subject=str(getattr(verified, "subject", "") or ""),
        )

    def _warn_once_if_remotely_reached(self, request) -> None:
        """Report observed reachability while running without authentication.

        The startup guard can only reason about the bind address, and a bind
        address does not determine who can reach the process. An arriving
        request is better evidence — a forwarding header, or a peer that is not
        this machine, proves something outside the host is talking to an
        unauthenticated server — but only in one direction. Read what this
        cannot do:

        A relay that egresses from loopback and adds no HTTP headers is
        indistinguishable, from inside this process, from a local client. That
        is ``ssh -L``, ``socat``, ``kubectl port-forward``, and a bare nginx
        ``proxy_pass`` with no ``proxy_set_header`` — measured with a real TCP
        relay: the request is served, and nothing here fires. The peer this
        code observes IS loopback and there are no headers to read, so the
        information needed to decide is not present at this layer at any price.
        Silence from this detector is therefore not evidence of a local-only
        deployment. The conditions below are sufficient, never necessary, and
        the opt-in warning in ``_assert_safe_http_bind`` says so where an
        operator reading it is still in a position to act.

        Warn once per process — this is a standing condition, not a per-request
        event, and one line per request would bury it in the very log an
        operator is scanning.
        """
        if self._exposure_warned:
            return
        via = next((h for h in _FORWARDED_HEADERS if h in request.headers), "")
        peer = (request.scope.get("client") or ("", 0))[0]
        remote_peer = _peer_is_remote(peer)
        if not via and not remote_peer:
            return
        self._exposure_warned = True
        logger.warning(
            "CPersona is serving requests that reach it from outside this machine "
            "while UNAUTHENTICATED (%s). Every tool is callable by whoever can reach "
            "this endpoint, including delete_agent_data and export/import file access. "
            "Set CPERSONA_AUTH_TOKEN and restart.",
            f"proxy header {via!r}" if via else f"peer {peer}",
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # Startup/shutdown, not a request: there is nobody to authenticate
            # and refusing it would stop the server from starting.
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Anything else — websocket today — used to be forwarded
            # unauthenticated, because "not http" was read as "not a request".
            # It is a request; it just carries its credentials somewhere this
            # middleware does not look. Reach is limited (no websockets/wsproto
            # is installed, so uvicorn never completes the upgrade) but the
            # contract "no unauthenticated request reaches a tool" has to hold
            # as written, not as far as the dependency list happens to allow.
            if self.auth_token or self.acl_config is not None:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if self._is_public_metadata_request(scope):
            # RFC 9728 metadata, and nothing else, is served without
            # credentials. Placed ahead of both credential modes because the
            # exemption has to hold in either; placed behind nothing else,
            # because every other path below must stay refused.
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if self.acl_config is not None:
            # ACL mode (docs/ACL_DESIGN.md §5.1): resolve the bearer token to a
            # Principal and carry it to the dispatch guard via contextvar. The
            # stateless HTTP transport executes the tool call inside this
            # request's task lineage, so the value set here is visible at
            # dispatch — pinned end-to-end by tests/test_acl.py.
            token = _bearer_credential(request.headers.get("authorization", ""))
            principal = acl.resolve_token(self.acl_config, token)
            if principal is None:
                principal = await self._oauth_principal(token)
            if principal is None:
                await self._unauthorized(scope, receive, send)
                return
            ctx_token = acl.set_principal(principal)
            try:
                await self.app(scope, receive, send)
            finally:
                acl.reset_principal(ctx_token)
            return
        if not self.auth_token:
            self._warn_once_if_remotely_reached(request)
        if self.auth_token:
            token = _bearer_credential(request.headers.get("authorization", ""))
            # A missing/malformed header yields an empty token, which must
            # be rejected — the earlier code let header-less requests fall
            # through to the app (auth bypass, bug-003). compare_digest keeps
            # the check constant-time against token-probing; it runs over
            # UTF-8 bytes because the str overload raises on non-ASCII input,
            # and a header a remote caller controls must never turn a 401
            # into a 500 (bug-259).
            if not token or not hmac.compare_digest(token.encode("utf-8"), self.auth_token.encode("utf-8")):
                await self._unauthorized(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _assert_safe_http_bind(
    auth_token: str, host: str, *, allow_unauthenticated: bool | None = None, warn: bool = True
) -> None:
    """Fail closed before the HTTP transport binds (bug-017, reworked in 2.5.3).

    ``auth_token`` defaults to '' and ``BearerTokenMiddleware`` only enforces
    credentials when it is truthy, so an unset token turns auth into a no-op,
    leaving every tool — including ``delete_agent_data`` and the
    file-reading/writing ``import``/``export`` — callable by anyone who can
    reach the port.

    Until 2.5.3 this guard decided using the bind address: a non-loopback bind
    without a token was refused, a loopback bind was allowed with a warning
    saying it was "bound to loopback only". That premise does not hold. A
    tunnel, a reverse proxy, ``kubectl port-forward``, or a published container
    port all forward to loopback, so a loopback bind can be world-reachable —
    and the reassuring wording is what let this deployment run publicly
    reachable and unauthenticated for 13 days without anyone noticing. The
    guard believed it was failing closed while it was failing open.

    So the bind address no longer decides anything. Without a token the server
    refuses to start, wherever it binds. Local development that genuinely wants
    no auth opts in explicitly via ``CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true``,
    which states the intent that an address never could. ``host`` is still taken
    so the error can name what was being attempted; it is not part of the
    decision. ``allow_unauthenticated`` is read from the environment when not
    passed — it is a parameter so tests can drive both branches directly.

    ``warn=False`` performs the refusal without logging the opt-in warning, so
    the pre-flight in ``main()`` can fail closed before the process opens the
    database or calls the embedding backend without printing the same paragraph
    twice.
    """
    if auth_token:
        return
    if allow_unauthenticated is None:
        allow_unauthenticated = (
            os.environ.get("CPERSONA_ALLOW_UNAUTHENTICATED_HTTP", "false").lower() == "true"
        )
    if not allow_unauthenticated:
        raise SystemExit(
            f"CPersona: refusing to start the HTTP transport on {host!r} without "
            "CPERSONA_AUTH_TOKEN. Every tool (delete_agent_data, export/import file "
            "access) would be callable without credentials by anything that can reach "
            "the port — and a loopback bind does not mean nothing can: tunnels, reverse "
            "proxies, port-forwards and published container ports all forward to it. "
            "Set CPERSONA_AUTH_TOKEN. If you really want no authentication (local "
            "development only), set CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true to say so."
        )
    if not warn:
        return
    logger.warning(
        "CPERSONA_ALLOW_UNAUTHENTICATED_HTTP is set — the HTTP transport on %s is "
        "UNAUTHENTICATED and every tool is callable without credentials. Binding to a "
        "loopback address does not contain this: tunnels, reverse proxies and "
        "port-forwards reach it. This server warns if it observes a request arriving "
        "from outside the host, but it CANNOT see a relay that forwards from loopback "
        "without adding headers (ssh -L, socat, a plain nginx proxy_pass), so silence "
        "is not evidence that nothing outside can reach this port. Use this for local "
        "development only; set CPERSONA_AUTH_TOKEN anywhere else.",
        host,
    )


def _resolve_http_port() -> int:
    """Resolve CPERSONA_HTTP_PORT via the bug-133 warn+fall-back-to-default parse.

    C10: a bare int() here raised an uncaught ValueError on a malformed value
    (unit suffix, stray quote, trailing junk), aborting the HTTP transport
    before it could bind. Route through config.parse_int so a bad value warns
    and falls back to the default instead of crashing startup.
    """
    return config.parse_int("CPERSONA_HTTP_PORT", 8402)


def _resolve_embedding_timeout() -> int:
    """Resolve CPERSONA_EMBEDDING_TIMEOUT_SECS via the bug-133 warn+fall-back parse.

    C10: this read runs in main() whenever embeddings are enabled, before the
    transport is selected, so a bare int() raising on a malformed value crashed
    BOTH the stdio and streamable-http transports. config.parse_int warns and
    falls back to the default instead.
    """
    return config.parse_int("CPERSONA_EMBEDDING_TIMEOUT_SECS", 30)


def _build_http_app(auth_token: str, mcp_endpoint, lifespan, acl_config: "acl.AclConfig | None" = None):
    """Assemble the Starlette app the HTTP transport serves, middleware included.

    Factored out of ``_run_http_server`` so the wiring itself is testable. A
    middleware that is written and unit-tested but never mounted is
    indistinguishable at runtime from one that was never written: dropping the
    ``BearerTokenMiddleware`` entry below serves every tool without credentials
    AND silences the bug-198 reachability warning, while every test that
    constructs the middleware directly keeps passing. tests/
    test_253_middleware_wiring.py therefore builds this app and drives real
    ASGI requests through it, so the mounting — not just the class — is pinned.

    Order is load-bearing: CORS is outermost so a browser preflight (which
    carries no Authorization header) is answered before authentication runs,
    and BearerTokenMiddleware sits in front of the MCP mounts so no
    unauthenticated request can reach a tool.
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Mount

    oauth = _oauth_discovery()
    routes = [Mount("/mcp", app=mcp_endpoint), Mount("/", app=mcp_endpoint)]
    if oauth is not None:
        # RFC 9728 (docs/OAUTH_DESIGN.md §7). The SDK builds the route and the
        # document; writing either by hand would be a second implementation of
        # a format whose whole value is that clients already parse it.
        #
        # Ahead of the mounts, and it has to be: Mount("/") matches every path,
        # so a metadata route registered after it is never reached. Off (the
        # default) the list is what it has always been.
        from mcp.server.auth.routes import create_protected_resource_routes

        # Before the document is built: the models normalise at validation.
        _preserve_empty_url_paths_in_metadata()

        routes = (
            create_protected_resource_routes(
                resource_url=oauth.resource,
                authorization_servers=oauth.authorization_servers,
                scopes_supported=oauth.scopes or None,
            )
            + routes
        )

    return Starlette(
        routes=routes,
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
            Middleware(
                BearerTokenMiddleware,
                auth_token=auth_token,
                acl_config=acl_config,
                oauth=oauth,
                oauth_verifier=_oauth_verifier(oauth, acl_config),
            ),
        ],
        lifespan=lifespan,
    )


async def _run_http_server():
    """Run CPersona as a Streamable HTTP MCP server with Bearer token auth."""
    import contextlib
    from collections.abc import AsyncIterator

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette

    auth_token = os.environ.get("CPERSONA_AUTH_TOKEN", "")
    acl_config = acl.active_config()
    if os.environ.get("CPERSONA_ACL_FILE", "") and acl_config is None:
        # The operator asked for ACL mode but nothing activated it — a wiring
        # regression (main() skipped, or activate dropped). Serving now would
        # fall back to the legacy token path, silently granting a lingering
        # CPERSONA_AUTH_TOKEN full capability: the exact fail-open D3 exists
        # to prevent. Refuse to serve (review finding on PR #112).
        raise RuntimeError(
            "CPERSONA_ACL_FILE is set but no ACL configuration is active; "
            "refusing to serve on the legacy authentication path"
        )
    if acl_config is not None:
        # D3 (docs/ACL_DESIGN.md §4.1): one credential authority at a time.
        # main() has already warned when both were set; here the token is
        # simply not handed to the middleware.
        auth_token = ""

    session_manager = StreamableHTTPSessionManager(
        app=registry.server,
        stateless=True,
    )

    async def mcp_endpoint(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("CPersona Streamable HTTP server ready")
            yield

    app = _build_http_app(auth_token, mcp_endpoint, lifespan, acl_config=acl_config)

    # The bind address is chosen here, but it is NOT what makes the port safe
    # (bug-198): tunnels, reverse proxies and published container ports all
    # forward to loopback. The guard below therefore requires a token wherever
    # this binds, and the old "a public bind additionally requires a token"
    # rule (bug-017) is withdrawn — do not reintroduce it.
    host = os.environ.get("CPERSONA_HTTP_HOST", "127.0.0.1")
    port = _resolve_http_port()
    # main() has already run this check (see _preflight_http_auth); it stays
    # here because _run_http_server is also entered directly by tests and by
    # anything embedding the transport, and a guard that only one caller
    # reaches is a guard one refactor away from being absent. ACL mode IS
    # authentication — every request must resolve to a principal — so the
    # empty-token refusal does not apply there.
    if acl_config is None:
        _assert_safe_http_bind(auth_token, host)
    logger.info("Starting Streamable HTTP on %s:%d", host, port)

    # Not named `config`: that is the module this file imports, and shadowing it
    # here is a loaded gun for the next edit — any config.* read added to this
    # function would raise UnboundLocalError before the assignment below and
    # take the whole HTTP transport down at startup.
    uvicorn_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    await server.serve()


def _preflight_http_auth() -> None:
    """Refuse an unauthenticated HTTP start before anything expensive happens.

    The guard's inputs are two environment variables; nothing it decides needs
    the database, the embedding client, or the task queue. Those were all
    initialised first anyway, because the only call site was inside
    ``_run_http_server`` at the end of ``main()``. Under the production unit —
    ``Restart=always``, ``RestartSec=10``, ``EnvironmentFile`` (verified on the
    deployment) — a token that fails to load turns every restart into: open and
    migrate the database, then, with CALIBRATE_ON_MODEL_CHANGE defaulting on,
    an HTTP round-trip to the embedding backend, then start and stop the queue,
    then exit 1. Every ten seconds. The security outcome was already right; the
    cost of reaching it was not.

    Runs for the HTTP transport only, and silently for stdio, which has no bind
    and no token.
    """
    if config.transport() != "streamable-http":
        return
    if os.environ.get("CPERSONA_ACL_FILE", ""):
        # ACL mode is authentication (every request must resolve to a
        # principal); the file itself is validated fail-closed in main().
        # The activation invariant lives here as well as in _run_http_server
        # so an env/activation mismatch dies BEFORE the database, the
        # embedding backend and the queue are initialised — under
        # Restart=always that difference is a cheap failure loop versus the
        # expensive one this preflight exists to avoid.
        if not acl.is_active():
            raise RuntimeError(
                "CPERSONA_ACL_FILE is set but no ACL configuration is active; "
                "refusing to serve on the legacy authentication path"
            )
        return
    _assert_safe_http_bind(
        os.environ.get("CPERSONA_AUTH_TOKEN", ""),
        os.environ.get("CPERSONA_HTTP_HOST", "127.0.0.1"),
        warn=False,
    )


# =============================================================================
# Entry point
# =============================================================================


def _schedule_startup_calibration() -> asyncio.Task:
    """Run the startup calibration guard as a background task (bug-258).

    The task owns nothing the serving path waits on: gates and thresholds land
    in vector's module state as each agent's calibration completes, and until
    then recall uses the heuristic fallback — the same degraded mode as a failed
    calibration, and infinitely better than the bound-nothing full outage that
    awaiting the guard inline produced. The done-callback exists because a task
    created and awaited only at shutdown would otherwise swallow its exception
    for the whole session (the same silent-death shape the stdio bridge's
    writer had): a failed guard must say so the moment it fails, and say what
    the operator can do about it.
    """

    async def _run():
        status = await ensure_calibrated_on_startup(AUTO_CALIBRATE, CALIBRATE_ON_MODEL_CHANGE)
        logger.info("Vector threshold startup calibration: %s", status)

    task = asyncio.create_task(_run())

    def _report(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "Startup calibration failed; recall gates stay on the heuristic "
                "fallback until calibrate_threshold is run manually: %r",
                exc,
            )

    task.add_done_callback(_report)
    return task


async def _run_stdio_server():
    """Run the stdio transport, entering the ACL "local" principal first.

    §5.4 (docs/ACL_DESIGN.md): the stdio peer is whoever spawned the process —
    it resolves to the reserved "local" principal, whose grants come from the
    same file (and default to none if unlisted). Set in this task's context so
    every handler inherits it. Factored out of ``main()`` so the principal
    entry is testable — a dropped set_principal here turns every stdio call in
    ACL mode into a "no principal resolved" denial, a wholesale outage no
    other test observes (review finding on PR #112).
    """
    if os.environ.get("CPERSONA_ACL_FILE", "") and not acl.is_active():
        # Same invariant as the HTTP transport, kept symmetric: the operator
        # asked for ACL mode but nothing activated a configuration, and
        # serving now would run every stdio call unrestricted (review finding
        # on PR #112, second pass).
        raise RuntimeError(
            "CPERSONA_ACL_FILE is set but no ACL configuration is active; "
            "refusing to serve the stdio transport unrestricted"
        )
    if acl.is_active():
        acl.set_principal(acl.Principal(acl.LOCAL_CLIENT_ID))
    async with stdio_server() as (read_stream, write_stream):
        await registry.server.run(read_stream, write_stream, registry.server.create_initialization_options())


async def _assert_no_reserved_agent_ids() -> None:
    """Refuse startup when per-subject reserved names are already in use.

    Runs only when some ACL row declared per_subject (docs/OAUTH_DESIGN.md
    §12), after the schema is ready. The names it guards: the literal ``@me``
    sentinel — a stored row under it could never be addressed again, because
    the guard rewrites the name before any query — and the ``u-`` alias
    prefix, where an agent the alias ledger does not record is
    indistinguishable from an issued alias and the boundary could hand one
    subject another tenant's data. Aliases the ledger records are exempt
    (bug-267): they are the server's own prior issuance, and refusing them
    meant every restart after the first mint failed here — measured in
    production, 2026-08-31. A collision is a configuration error, not a
    migration: the operator either renames the colliding agents
    (merge_memories) or does not enable per_subject on this database.
    """
    from cpersona import database
    from cpersona.isolation import isolation_where

    iso_all = isolation_where(agent_id=None)  # deliberate cross-agent enumeration
    agent_ids: set[str] = set()
    async with database.connection() as db:
        for table in ("memories", "profiles", "episodes", "pending_memory_tasks"):
            async with db.execute(
                f"SELECT DISTINCT agent_id FROM {table}{iso_all.where}", iso_all.params
            ) as cursor:
                agent_ids.update(row[0] for row in await cursor.fetchall())
    ledger = acl.active_ledger()
    known_aliases = ledger.issued_aliases() if ledger is not None else frozenset()
    collisions = acl.reserved_agent_id_collisions(agent_ids, known_aliases)
    if collisions:
        raise RuntimeError(
            "per_subject is configured but the database already uses reserved "
            f"agent ids: {', '.join(collisions)}. The @me sentinel and the "
            f"{acl.ALIAS_PREFIX!r} prefix belong to subject aliasing "
            "(docs/OAUTH_DESIGN.md §12), and the alias ledger records none of "
            "these ids as issued; rename those agents (merge_memories) "
            "or remove per_subject from the ACL file"
        )


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # ACL mode (docs/ACL_DESIGN.md): load and validate the grant table before
    # anything expensive, failing closed on any defect — the server refuses to
    # start rather than serve a policy other than the one written (§7).
    acl_path = os.environ.get("CPERSONA_ACL_FILE", "")
    if acl_path:
        acl.activate(acl.load_config(acl_path))
        logger.info(
            "ACL mode active: %d client(s) from %s",
            len(acl.active_config().grants_by_client),
            acl_path,
        )
        if acl.active_config().per_subject_clients:
            # Per-subject partitioning (docs/OAUTH_DESIGN.md §12). The ledger
            # loads with the same failure posture as the grant table: a file
            # that exists but cannot be parsed refuses startup — starting over
            # would re-issue every subject's alias and sever each person from
            # the memory space they already own.
            from cpersona.aliases import AliasLedger

            ledger_path = config.alias_ledger_path()
            acl.activate_ledger(AliasLedger(ledger_path))
            logger.info(
                "per-subject partitioning active: %d client(s); alias ledger at %s",
                len(acl.active_config().per_subject_clients),
                ledger_path,
            )
        if os.environ.get("CPERSONA_AUTH_TOKEN", ""):
            logger.warning(
                "CPERSONA_AUTH_TOKEN is IGNORED while CPERSONA_ACL_FILE is set: "
                "credentials come from the ACL file only (docs/ACL_DESIGN.md §4.1). "
                "To keep using that token, list it as a client in the ACL file."
            )

    # Before the database, the embedding backend and the queue — see the
    # docstring for what a restart loop costs when this runs last instead.
    _preflight_http_auth()

    # bug-275: before the client, because an unsupported mode is a startup
    # failure and not a per-call one. Constructing the client anyway produced a
    # server that ran, answered every recall in keyword/FTS-only mode, and
    # reported the embedding endpoint as unreachable although nothing was ever
    # contacted.
    config.assert_embedding_mode_supported()

    if EMBEDDING_MODE != "none":
        # _vendored_mcp_common.EmbeddingClient takes env-derived config via constructor
        # args (it does no env reading of its own), so cache size / TTL /
        # timeout are passed explicitly here to preserve CPERSONA_EMBEDDING_*
        # override behavior.
        vector._embedding_client = EmbeddingClient(
            mode=EMBEDDING_MODE,
            http_url=EMBEDDING_URL,
            api_key=EMBEDDING_API_KEY,
            api_url=EMBEDDING_API_URL,
            model=EMBEDDING_MODEL,
            cache_size=EMBEDDING_CACHE_SIZE,
            cache_ttl=EMBEDDING_CACHE_TTL,
            timeout=_resolve_embedding_timeout(),
        )
        await vector._embedding_client.initialize()
        logger.info("Embedding client ready (mode=%s)", EMBEDDING_MODE)
        if not local_blobs_stored(VECTOR_SEARCH_MODE, STORE_BLOB):
            # bug-180: state the trade at boot. In this configuration the local
            # cosine scan — the fallback for a remote /search outage — has no rows
            # to scan, and the degraded advisory watches the embed boundary only,
            # so the first symptom would otherwise be recall quietly returning
            # FTS/keyword hits alone. check_vector_fallback_config reports the same
            # thing on the maintenance surface.
            logger.info(
                "Vector search mode=%s with CPERSONA_STORE_BLOB=false: memories keep no "
                "local embedding, so a remote /search outage leaves recall with FTS/keyword only",
                VECTOR_SEARCH_MODE,
            )
    else:
        logger.info("Embedding disabled (mode=none), using FTS5 + keyword only")

    await init_db()

    # The vendored run_mcp_server installs this itself, but this server has its
    # own main loop (it also serves HTTP) and therefore never goes through it —
    # so the filter has to be installed here, exactly as mcp_utils documents for
    # custom loops. Without it every kernel handshake probe (cloto/*) logs a
    # 31-line pydantic ValidationError for a method the MCP schema does not know,
    # which is noise, not a fault: the probe is answered correctly either way.
    # The sibling servers in clotohub-servers got this in a1386b7; this repo was
    # extracted before that and never received the port. It sits outside the
    # bug-268 try below: installing a logging filter cannot fail, and the
    # structural test pins the call at function-body level, unnested.
    install_mgp_validation_filter()

    # bug-268: everything past init_db runs inside the try so the finally's
    # close_db() reaches every failure path. aiosqlite runs each connection on
    # a NON-daemon worker thread; a startup failure that skipped close_db left
    # that thread blocking interpreter exit — the process stayed alive with no
    # MCP port bound, systemd counted it active, and Restart=always never
    # fired (measured in production, 2026-08-31: the bug-267 guard raised and
    # the outage was invisible until a manual kill).
    calibration_task: asyncio.Task | None = None
    try:
        if acl.is_active() and acl.active_config().per_subject_clients:
            await _assert_no_reserved_agent_ids()

        # Vector-similarity threshold startup guard (v2.4.24): restore persisted
        # thresholds, or (re)calibrate on first run / embedding-dimension change even when
        # AUTO_CALIBRATE is off. A stale threshold from a prior embedding model (e.g. a
        # silent jina 768d -> bge-m3 1024d swap) is a known recall-contamination cause.
        #
        # bug-258: scheduled, not awaited. Awaiting here held the transport closed for
        # the whole guard — and a recalibration is minutes of real embedding calls
        # (median-of-K multiplied it by the draw count, and it runs once per agent), so
        # a scoring-version bump turned every deploy into a multi-minute full outage
        # (measured: 3.5 minutes for two agents on a 2400-row corpus). Binding first is
        # safe because the guard's values only ever ARRIVE through it: until it lands,
        # the in-memory gates are simply unset and recall runs on the same heuristic
        # fallback it uses when calibration fails or has never run. A stale sidecar is
        # never consulted at recall time — _restore_calibration_state is the only
        # reader — so serving during the window cannot apply a gate measured on the
        # wrong scoring function.
        if EMBEDDING_MODE != "none":
            calibration_task = _schedule_startup_calibration()

        if TASK_QUEUE_ENABLED:
            tasks._task_queue = tasks.MemoryTaskQueue()
            await tasks._task_queue.start()
        else:
            logger.info("Task queue disabled")

        transport = config.transport()
        if transport == "stdio":
            await _run_stdio_server()
        elif transport == "streamable-http":
            await _run_http_server()
        else:
            raise ValueError(f"Unknown transport: {transport}")
    finally:
        # bug-258: a calibration still in flight at shutdown is abandoned, not
        # awaited — its embedding round-trips would hold the shutdown open for
        # minutes, and an interrupted calibration leaves exactly the state it
        # started from (the sidecar write is the last step).
        if calibration_task is not None and not calibration_task.done():
            calibration_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await calibration_task
        if tasks._task_queue:
            await tasks._task_queue.stop()
        await close_db()
        if vector._embedding_client:
            await vector._embedding_client.close()


def run():
    """Synchronous entry point for the ``cpersona`` console script and
    ``python -m cpersona``."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
