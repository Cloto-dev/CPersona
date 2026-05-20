"""Cloto MCP Server: CPersona Memory.

Thin orchestration shell. Tool implementations live in module siblings:

  - config.py             — env var configuration
  - utils.py              — stateless helpers
  - database.py           — connection, schema, migrations
  - tasks.py              — MemoryTaskQueue + _task_queue singleton
  - vector.py             — _embedding_client singleton + _search_vector (EmbeddingClient from mcp_common)
  - memory_handlers.py    — store / recall / recall_with_context / archive_episode
  - admin_handlers.py     — profile / list / delete / update / lock / agent_data / threshold / export / import / merge / queue_status
  - maintenance_handlers.py — check_health / deep_check

This shell:
  1. Imports do_* handlers
  2. Defines orchestration wrappers (do_update_profile_or_queue / do_archive_episode_or_queue)
  3. Registers 24 MCP tools
  4. Wires HTTP/stdio transport
  5. main() initializes singletons (vector._embedding_client, tasks._task_queue) and runs the server
"""

import asyncio
import logging
import os

from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations
from mcp_common import no_persist
from mcp_common.embedding_client import EmbeddingClient
from mcp_common.mcp_utils import ToolRegistry

import tasks
import vector
from admin_handlers import (
    do_calibrate_threshold,
    do_delete_agent_data,
    do_delete_episode,
    do_delete_memory,
    do_export_memories,
    do_get_profile,
    do_get_queue_status,
    do_import_memories,
    do_list_episodes,
    do_list_memories,
    do_lock_memory,
    do_merge_memories,
    do_unlock_memory,
    do_update_memory,
    do_update_profile,
)
from config import (
    AUTO_CALIBRATE,
    EMBEDDING_API_KEY,
    EMBEDDING_API_URL,
    EMBEDDING_CACHE_SIZE,
    EMBEDDING_CACHE_TTL,
    EMBEDDING_MODE,
    EMBEDDING_MODEL,
    EMBEDDING_URL,
    TASK_QUEUE_ENABLED,
)
from database import close_db, get_db
from maintenance_handlers import do_check_health, do_deep_check
from memory_handlers import (
    do_archive_episode,
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
            agent_id, history, summary=summary, keywords=keywords, resolved=resolved, project_id=project_id
        )
    if tasks._task_queue and TASK_QUEUE_ENABLED:
        # NOTE: the queue path does not yet propagate project_id — the
        # LLM-driven branch is not expected from project-tagged callers
        # (which always pre-compute summary). Tracked for follow-up if needed.
        task_id = await tasks._task_queue.enqueue("archive_episode", agent_id, history)
        return {"ok": True, "queued": True, "task_id": task_id}
    return await do_archive_episode(agent_id, history)


# =============================================================================
# MCP Tool Registry — 24 tools
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
        },
        "required": ["agent_id", "query"],
    },
    do_recall,
    [
        ("agent_id", str),
        ("query", str),
        ("limit", int, 10),
        ("deep", bool, False),
        ("channel", str, ""),
        ("exclude_contents", list, []),
        ("project_id", str, None),
        ("source_id", str, ""),
    ],
    annotations=ToolAnnotations(readOnlyHint=True),
)

registry.auto_tool(
    "recall_with_context",
    "Recall memories and merge with external conversation context. "
    "Automatically deduplicates, sorts chronologically, and returns a unified list. "
    "Replaces separate recall + manual merge in the caller.",
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
            "limit": {"type": "integer", "description": "Max recalled memories", "default": 10},
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
        },
        "required": ["agent_id", "query"],
    },
    do_recall_with_context,
    [
        ("agent_id", str),
        ("query", str),
        ("external_context", list, []),
        ("limit", int, 10),
        ("channel", str, ""),
        ("deep", bool, False),
        ("project_id", str, None),
        ("source_id", str, ""),
    ],
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
            "project_id": {
                "type": "string",
                "description": "v2.4.17 isolation axis. Omit or pass '' for the global pool.",
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
    ],
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

registry.auto_tool(
    "check_health",
    "Check memory database health (16 checks). Detects contamination, duplicates, "
    "oversized content, embedding issues, FTS desync, invalid JSON/timestamps, "
    "stale tasks, missing profiles, empty content, invalid/anonymous sources. "
    "Returns storage stats. Set fix=true to auto-repair.",
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

registry.auto_tool(
    "deep_check",
    "Deep semantic analysis of memory data quality. Detects issues requiring "
    "heuristic recovery (anonymous sources, short/trivial content, stale profiles, "
    "orphaned episodes). Set fix=true to apply repairs. Use checks parameter to "
    "select specific checks.",
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
                "description": "Checks to run (empty = all). Options: anonymous_source, short_content, stale_profile, orphaned_episodes",
            },
        },
        "required": ["agent_id"],
    },
    do_deep_check,
    [("agent_id", str), ("fix", bool, False), ("checks", list, [])],
    annotations=ToolAnnotations(readOnlyHint=False),
)


# =============================================================================
# Streamable HTTP transport (Bearer auth, CORS)
# =============================================================================


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
            header = request.headers.get("authorization", "")
            if auth_token and header:
                if not header.startswith("Bearer ") or header[7:] != auth_token:
                    response = JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await response(scope, receive, send)
                    return
            elif auth_token and not header:
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


# =============================================================================
# Entry point
# =============================================================================


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if EMBEDDING_MODE != "none":
        # mcp_common.EmbeddingClient takes env-derived config via constructor
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

    await get_db()

    # Auto-calibrate the vector-similarity threshold on startup if enabled (v2.4.15).
    if AUTO_CALIBRATE and EMBEDDING_MODE != "none":
        db = await get_db()
        # Phase 1: global threshold from the all-agents corpus
        global_result = await do_calibrate_threshold(agent_id="")
        if global_result.get("ok"):
            logger.info(
                "Auto-calibrate global: %.4f → %.4f",
                global_result["old_threshold"],
                global_result["new_threshold"],
            )
        # Phase 2: per-agent thresholds for each agent with sufficient embeddings
        agent_rows = await db.execute_fetchall(
            "SELECT DISTINCT agent_id FROM memories WHERE embedding IS NOT NULL"
        )
        for (aid,) in agent_rows:
            result = await do_calibrate_threshold(agent_id=aid)
            if result.get("ok"):
                logger.info(
                    "Auto-calibrate %s: %.4f → %.4f",
                    aid,
                    result["old_threshold"],
                    result["new_threshold"],
                )
            # agents with < 10 embeddings are silently skipped (result["ok"] is False)

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


if __name__ == "__main__":
    asyncio.run(main())
