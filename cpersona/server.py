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
import hmac
import logging
import os

from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations
from cpersona._vendored_mcp_common import no_persist
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona._vendored_mcp_common.mcp_utils import ToolRegistry

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
    TASK_QUEUE_ENABLED,
)
from cpersona import config
from cpersona.database import close_db, init_db
from cpersona.maintenance_handlers import do_check_health, do_deep_check, do_migrate_channel_axis
from cpersona.memory_handlers import (
    do_archive_episode,
    do_get_contents,
    do_recall,
    do_recall_with_context,
    do_store,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Queue dispatch wrappers (thin orchestration over handlers + _task_queue)
# =============================================================================


async def do_update_profile_or_queue(agent_id: str, profile: str = "") -> dict:
    """Save pre-computed profile. Queue is bypassed since no LLM processing is needed."""
    return await do_update_profile(agent_id, profile=profile)


async def do_archive_episode_or_queue(
    agent_id: str,
    history: list,
    summary: str = "",
    keywords: str = "",
    resolved: bool | None = None,
    project_id: str = "",
    channel: str = "",
) -> dict:
    """Enqueue episode archival if task queue is enabled, otherwise run synchronously.

    When summary/keywords are pre-computed, bypass the queue and store directly
    (no LLM call needed, so queuing for retry is unnecessary).
    """
    # Gate the wrapper too: enqueue() itself writes to pending_memory_tasks,
    # so guarding only the synchronous do_archive_episode would still let the
    # queue path leak rows into SQLite.
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {"ok": True, "queued": False, "task_id": None, "episode_id": None, "id": 0},
            "archive_episode",
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
# Recall preview boundary (2.5.0, Task #193)
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
) -> dict:
    result = await do_recall(
        agent_id,
        query,
        limit,
        deep=deep,
        channel=channel,
        exclude_contents=exclude_contents,
        project_id=project_id,
        source_id=source_id,
    )
    return result if full_content else _apply_preview(result)


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
) -> dict:
    result = await do_recall_with_context(
        agent_id,
        query,
        external_context=external_context,
        limit=limit,
        channel=channel,
        deep=deep,
        project_id=project_id,
        source_id=source_id,
    )
    return result if full_content else _apply_preview(result)


# =============================================================================
# MCP Tool Registry — 28 tools
# =============================================================================

registry = ToolRegistry("cloto-mcp-cpersona")


# Session no-persist controls — registered first for discoverability.
async def do_pause_persistence(ttl_seconds: int = no_persist.DEFAULT_TTL_SECONDS) -> dict:
    """Pause persistence for this MCP server process for a TTL window."""
    try:
        return no_persist.pause(ttl_seconds=ttl_seconds)
    except ValueError as e:
        return {"error": str(e)}


async def do_resume_persistence() -> dict:
    """Re-enable persistence immediately, clearing any active TTL."""
    return no_persist.resume()


async def do_persistence_status() -> dict:
    """Report whether write tools are currently being skipped, and the TTL remaining."""
    return no_persist.status()


registry.auto_tool(
    "pause_persistence",
    "Pause write operations on this MCP server for an opt-in TTL window. While "
    "paused, all write tools (store, archive_episode, update/delete/lock/unlock_memory, "
    "update_profile, import_memories, merge_memories, calibrate_threshold) return "
    'no-op responses with `persisted: false` and `id: "no-persist"` instead of '
    "writing to the database. Read tools (recall, list_*, get_profile, etc.) are "
    "unaffected. **This affects only this MCP server (cpersona). Call cscheduler's "
    "pause_persistence too if you want both paused.** Use for benchmarking, AB "
    "testing, or ephemeral exploration where memory contamination must be avoided. "
    "Default TTL: 1800 seconds (30 minutes); upper bound: 86400 seconds (1 day).",
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
        },
    },
    do_pause_persistence,
    [("ttl_seconds", int, no_persist.DEFAULT_TTL_SECONDS)],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "resume_persistence",
    "Re-enable persistence immediately, clearing any active no-persist TTL. "
    "Returns was_active=true if persistence was paused before this call.",
    {"type": "object", "properties": {}},
    do_resume_persistence,
    [],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "persistence_status",
    "Report whether persistence is currently paused and the TTL remaining (in seconds).",
    {"type": "object", "properties": {}},
    do_persistence_status,
    [],
    annotations=ToolAnnotations(readOnlyHint=True),
)

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
            "project_id": {
                "type": "string",
                "description": (
                    "v2.4.17 isolation axis. Optional — omit or pass '' to "
                    "store in the global pool. Reads via γ semantics: a "
                    "recall with project_id='X' returns 'X' rows + global pool."
                ),
            },
        },
        "required": ["agent_id", "message"],
    },
    do_store,
    [("agent_id", str), ("message", dict), ("channel", str, ""), ("project_id", str, "")],
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

registry.auto_tool(
    "recall",
    "Recall relevant memories using multi-strategy search (vector + FTS5 + keyword). "
    "Message content is returned as a preview tier by default — expand selected rows "
    "with get_contents(refs), or opt out wholesale with full_content=true.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier"},
            "query": {"type": "string", "description": "Search query (empty returns recent memories)"},
            "limit": {
                "type": "integer",
                "description": "Max memories to return (agent-facing cap; the library layer accepts up to the scan window for direct callers)",
                "default": 10,
                "minimum": 0,
                "maximum": 100,
            },
            "deep": {
                "type": "boolean",
                "description": "Deep recall — disable time and completion decay for exhaustive search",
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
            "project_id": {
                "type": "string",
                "description": (
                    "v2.4.17 γ filter. Omit → no filter (all projects). "
                    "'' → global pool only. 'X' → 'X' bucket ∪ global pool. "
                    "Threaded through cascade / RRF / vector / FTS / keyword paths."
                ),
            },
            "source_id": {
                "type": "string",
                "description": (
                    "v2.4.20 per-user source filter. Empty (default) = no filter. "
                    "Non-empty = prefix match against json_extract(source, '$.id'), "
                    "e.g. 'discord:12345' to restrict to one Discord user, or "
                    "'discord:' to scope to all Discord-sourced memories. "
                    "Episodes are skipped when set (no per-user source tagging)."
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
    ],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "recall_with_context",
    "Recall memories and merge with external conversation context. "
    "Automatically deduplicates, sorts chronologically, and returns a unified list. "
    "Replaces separate recall + manual merge in the caller. "
    "Content is preview-tiered by default — see recall's full_content / get_contents.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID"},
            "query": {"type": "string", "description": "Search query"},
            "external_context": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Conversation history entries [{role, name?, user_id?, content, timestamp?}, ...]",
            },
            "limit": {
                "type": "integer",
                "description": "Max recalled memories (agent-facing cap; the library layer accepts up to the scan window for direct callers)",
                "default": 10,
                "minimum": 0,
                "maximum": 100,
            },
            "channel": {"type": "string", "description": "Memory channel filter"},
            "deep": {"type": "boolean", "description": "Disable time decay", "default": False},
            "project_id": {
                "type": "string",
                "description": "v2.4.17 γ filter — passed through to recall. Same semantics as in `recall`.",
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
    ],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "get_contents",
    "Fetch full, untrimmed content for recall preview refs. Use after a preview-tier "
    "recall to expand only the rows that matter instead of opting the whole recall "
    "out with full_content=true.",
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
                "description": "v2.4.17 isolation axis. Omit or pass '' for the global pool.",
            },
            "channel": {
                "type": "string",
                "description": (
                    "v2.4.22 conversation-channel tag (e.g. a Discord channel id). "
                    "Default '' (= unscoped). Channel-scoped recall returns episodes "
                    "whose channel matches; this powers the per-channel episodic loop."
                ),
            },
        },
        "required": ["agent_id", "summary"],
    },
    do_archive_episode_or_queue,
    [
        ("agent_id", str),
        ("history", list, []),
        ("summary", str, ""),
        ("keywords", str, ""),
        ("resolved", bool, None),
        ("project_id", str, ""),
        ("channel", str, ""),
    ],
    # bug-064: NOT idempotent — do_archive_episode does a bare INSERT with no OR IGNORE and
    # no unique constraint, so every call appends a new episode. idempotentHint=True falsely
    # advertised retry-safety; a host retrying after a lost response would double-store the
    # episode (inflating recall + list_episodes). False is the safe, honest declaration.
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)

registry.auto_tool(
    "list_memories",
    "List recent memories for an agent (for dashboard display).",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent identifier (empty for all agents)"},
            "limit": {"type": "integer", "description": "Max memories to return", "default": 100},
            "project_id": {
                "type": "string",
                "description": "v2.4.17 γ filter. Omit → no filter; '' → global pool only; 'X' → 'X' ∪ global pool.",
            },
        },
        "required": [],
    },
    do_list_memories,
    [("agent_id", str), ("limit", int, 100), ("project_id", str, None)],
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
            "project_id": {
                "type": "string",
                "description": "v2.4.17 γ filter. Same semantics as list_memories.",
            },
        },
        "required": [],
    },
    do_list_episodes,
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
        },
        "required": ["agent_id"],
    },
    do_delete_agent_data,
    [("agent_id", str)],
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
            "method": {"type": "string", "description": "'separation' (default; two-population — learns the operating point from null pairs vs temporally-adjacent same-session positives, falling back to nearest-neighbour when too few exist), 'percentile', or 'zscore'"},
            "percentile": {"type": "number", "description": "Null-distribution quantile for method='percentile' (default: 0.95, higher = stricter)"},
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
    ],
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
        },
        "required": ["agent_id"],
    },
    do_set_recall_precision,
    [
        ("agent_id", str),
        ("precision", str, ""),
        ("beta", float, 0),
    ],
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
    "update_memory",
    "Update memory content by ID. Rejects if memory is locked. Ownership enforced when agent_id provided.",
    {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID for ownership verification"},
            "memory_id": {"type": "integer", "description": "Memory ID to update"},
            "content": {"type": "string", "description": "New content for the memory"},
        },
        "required": ["memory_id", "content"],
    },
    do_update_memory,
    [("memory_id", int), ("content", str), ("agent_id", str)],
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
        },
        "required": ["memory_id"],
    },
    do_lock_memory,
    [("memory_id", int), ("agent_id", str)],
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
        },
        "required": ["memory_id"],
    },
    do_unlock_memory,
    [("memory_id", int), ("agent_id", str)],
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
    # bug-078: annotations must reflect the WORST reachable behavior. mode='move'
    # ends with do_delete_agent_data(source) — the same irreversible whole-agent wipe
    # the delete_agent_data tool declares destructiveHint=True for. Advertising
    # destructiveHint=False let that wipe bypass any host-side HITL approval gate
    # keyed on the hint (the bug-054 annotation-truthfulness class).
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
)

registry.auto_tool(
    "check_health",
    "Check memory database health (20-check registry, each issue tagged with "
    "severity critical/warn/info). Detects contamination, duplicates, oversized "
    "content, embedding issues, FTS integrity (count + content-level), schema "
    "version/object drift (missing UNIQUE indexes or FTS triggers), SQLite file "
    "integrity, project_id naming drift, invalid JSON/timestamps, timestamp "
    "format drift, stale tasks, missing profiles, empty content, "
    "invalid/anonymous sources. Returns storage stats incl. project_id/channel "
    "distributions. Set fix=true to auto-repair (agent-scoped, locked-safe); "
    "critical file-integrity findings are report-only. Use checks parameter to "
    "run a subset.",
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
        },
    },
    do_check_health,
    [("agent_id", str, ""), ("fix", bool, False), ("checks", list, [])],
    annotations=ToolAnnotations(readOnlyHint=False),
)

registry.auto_tool(
    "deep_check",
    "Deep heuristic analysis of memory data quality. Detects issues requiring "
    "recovery or judgment (anonymous sources, short/trivial content, stale "
    "profiles, orphaned episodes, stale threshold calibration, embedding-space "
    "near-duplicate pairs as merge candidates). near_duplicate and "
    "calibration_staleness are report-only: apply decisions via merge_memories / "
    "delete_memory / calibrate_threshold. Set fix=true to apply repairs. Use "
    "checks parameter to select specific checks.",
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
        },
        "required": ["agent_id"],
    },
    do_deep_check,
    [("agent_id", str), ("fix", bool, False), ("checks", list, [])],
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
        },
        "required": [],
    },
    do_migrate_channel_axis,
    [("agent_id", str, ""), ("dry_run", bool, True), ("globalize_unrecoverable", bool, False)],
    annotations=ToolAnnotations(readOnlyHint=False),
)


# =============================================================================
# Streamable HTTP transport (Bearer auth, CORS)
# =============================================================================

# Hosts that only accept connections from the local machine. An unauthenticated
# bind to one of these is a local-dev convenience, not a network exposure.
_LOOPBACK_HTTP_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _assert_safe_http_bind(auth_token: str, host: str) -> None:
    """Fail closed before the HTTP transport binds (bug-017).

    ``auth_token`` defaults to '' and ``BearerTokenMiddleware`` only enforces
    credentials when it is truthy, so an unset token turns auth into a no-op.
    Combined with a public bind that silently exposes every tool — including
    ``delete_agent_data`` and the file-reading/writing ``import``/``export`` —
    to the whole network. Refuse to start an unauthenticated server on a
    non-loopback interface; a loopback bind (the default) stays usable for local
    development but is logged loudly so the missing auth is never a surprise.
    """
    if auth_token:
        return
    if host not in _LOOPBACK_HTTP_HOSTS:
        raise SystemExit(
            f"CPersona: refusing to start the HTTP transport on {host!r} without "
            "CPERSONA_AUTH_TOKEN. An unauthenticated non-loopback bind exposes every "
            "tool (delete_agent_data, export/import file access) to the network. Set "
            "CPERSONA_AUTH_TOKEN, or set CPERSONA_HTTP_HOST to a loopback address "
            "(127.0.0.1) for local-only use."
        )
    logger.warning(
        "CPERSONA_AUTH_TOKEN is unset — the HTTP transport is UNAUTHENTICATED "
        "(bound to loopback %s only). Set CPERSONA_AUTH_TOKEN to require a bearer token.",
        host,
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
            if request.method == "OPTIONS":
                await self.app(scope, receive, send)
                return
            if auth_token:
                header = request.headers.get("authorization", "")
                token = header[7:] if header.startswith("Bearer ") else ""
                # A missing/malformed header yields an empty token, which must
                # be rejected — the earlier code let header-less requests fall
                # through to the app (auth bypass, bug-003). compare_digest keeps
                # the check constant-time against token-probing.
                if not token or not hmac.compare_digest(token, auth_token):
                    response = JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await response(scope, receive, send)
                    return
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

    # Secure by default: bind loopback unless an operator opts into a wider
    # interface. A public bind additionally requires CPERSONA_AUTH_TOKEN (bug-017).
    host = os.environ.get("CPERSONA_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("CPERSONA_HTTP_PORT", "8402"))
    _assert_safe_http_bind(auth_token, host)
    logger.info("Starting Streamable HTTP on %s:%d", host, port)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


# =============================================================================
# Entry point
# =============================================================================


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

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
            timeout=int(os.environ.get("CPERSONA_EMBEDDING_TIMEOUT_SECS", "30")),
        )
        await vector._embedding_client.initialize()
        logger.info("Embedding client ready (mode=%s)", EMBEDDING_MODE)
    else:
        logger.info("Embedding disabled (mode=none), using FTS5 + keyword only")

    await init_db()

    # Vector-similarity threshold startup guard (v2.4.24): restore persisted
    # thresholds, or (re)calibrate on first run / embedding-dimension change even when
    # AUTO_CALIBRATE is off. A stale threshold from a prior embedding model (e.g. a
    # silent jina 768d -> bge-m3 1024d swap) is a known recall-contamination cause.
    if EMBEDDING_MODE != "none":
        status = await ensure_calibrated_on_startup(AUTO_CALIBRATE, CALIBRATE_ON_MODEL_CHANGE)
        logger.info("Vector threshold startup calibration: %s", status)

    if TASK_QUEUE_ENABLED:
        tasks._task_queue = tasks.MemoryTaskQueue()
        await tasks._task_queue.start()
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
