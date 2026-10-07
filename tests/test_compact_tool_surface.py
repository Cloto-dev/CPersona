"""A client named in CPERSONA_COMPACT_TOOL_CLIENTS is answered with the compact tool list.

Codex in code mode prints the full definition of every tool an agent looks up, each
time it looks, and cuts a lookup at about 40,000 characters; all of CPersona's tools
take about 75,000. Such a client is answered with the tools a session uses. These
tests drive the real initialize and tools/list through the SDK's in-memory
transport, so the client name is the one a client sends in `initialize`, and
through the real stateless HTTP app, where no session remembers `initialize` and
the client is named by the User-Agent it sends with every request.
"""

import contextlib
import json

import httpx
import pytest
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
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


async def _listed_over_http(user_agent):
    # The production HTTP path: a stateless session manager behind the server's own
    # app, so tools/list arrives in a session that never saw initialize.
    manager = StreamableHTTPSessionManager(server.registry.server, stateless=True, json_response=True)

    async def endpoint(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    app = server._build_http_app("", endpoint, lifespan)
    async with manager.run(), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/mcp/",
            headers={"User-Agent": user_agent, "Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 200, response.text
    return {t["name"] for t in response.json()["result"]["tools"]}


@pytest.mark.asyncio
async def test_codex_over_stateless_http_is_named_by_its_user_agent():
    # Measured: Codex 0.160.1 sends "codex-mcp-client/0.160.1" with each request.
    assert await _listed_over_http("codex-mcp-client/0.160.1") == SESSION_TOOLS


@pytest.mark.asyncio
async def test_another_user_agent_over_http_gets_every_tool():
    assert await _listed_over_http("claude-code/2.1.288") == _every_tool()


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
