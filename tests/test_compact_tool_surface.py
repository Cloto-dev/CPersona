"""A client named in CPERSONA_COMPACT_TOOL_CLIENTS is answered with the compact tool list.

Codex in code mode prints the full definition of every tool an agent looks up, each
time it looks, and cuts a lookup at about 40,000 characters; all of CPersona's tools
take about 75,000. Such a client is answered with the tools a session uses. These
tests drive the real initialize and tools/list through the SDK's in-memory
transport, so the client name is the one a client sends in `initialize`.
"""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from cpersona import config, server

SESSION_TOOLS = {
    "reconstruct", "store", "get_contents", "archive_episode", "update_memory",
    "lock_memory", "unlock_memory", "pause_persistence", "resume_persistence",
}
#: Codex's cut lookups ended at 40,090 characters. The compact list has to fit with
#: room left for whatever else the same lookup prints.
LISTING_BUDGET = 32_000


async def _listed(client_name):
    async with create_connected_server_and_client_session(
        server.registry.server, client_info=Implementation(name=client_name, version="0")
    ) as session:
        return {t.name for t in (await session.list_tools()).tools}


def _every_tool():
    return {t.name for t in server.registry._tools}


@pytest.mark.asyncio
async def test_codex_is_answered_with_the_compact_list():
    assert await _listed("codex-mcp-client") == SESSION_TOOLS


@pytest.mark.asyncio
async def test_other_clients_get_every_tool():
    assert await _listed("claude-code") == _every_tool()
    assert len(_every_tool()) > len(SESSION_TOOLS)


@pytest.mark.asyncio
async def test_an_empty_client_list_turns_it_off(monkeypatch):
    monkeypatch.setattr(config, "COMPACT_TOOL_CLIENTS", frozenset())
    assert await _listed("codex-mcp-client") == _every_tool()


@pytest.mark.asyncio
async def test_the_sdk_tool_cache_keeps_every_tool_after_a_compact_answer():
    # One HTTP process serves several clients and the SDK validates every call
    # against this cache; narrowing it for one client would narrow it for all.
    await _listed("codex-mcp-client")
    assert set(server.registry.server._tool_cache) == _every_tool()


def test_the_default_compact_tools_are_the_session_tools_and_all_exist():
    assert config.COMPACT_TOOLS == SESSION_TOOLS
    assert SESSION_TOOLS <= _every_tool()


def test_the_compact_list_fits_under_the_lookup_cap():
    # Roughly what Codex prints per tool: the server instructions, the description,
    # and the input schema.
    size = sum(
        len(json.dumps({
            "name": t.name,
            "description": server.SERVER_INSTRUCTIONS + "\n\n" + (t.description or ""),
            "input_schema": t.inputSchema,
        }))
        for t in server.registry._tools
        if t.name in config.COMPACT_TOOLS
    )
    assert size <= LISTING_BUDGET, f"the compact list is about {size} characters, over {LISTING_BUDGET}"
