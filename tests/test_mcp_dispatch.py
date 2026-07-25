"""bug-177: the MCP dispatch layer had no test at all.

Every response-shape assertion in the 2.5.2 contract work calls a handler
directly, so nothing exercised ``ToolRegistry._bind``'s ``call_tool`` — the
wrapper that actually turns a handler's dict into what a client receives. A
contract can therefore be correct in ``memory_handlers`` and still never reach a
client: the schema the tool advertises is a separate artifact from the arguments
the handler reads, and the outermost failure path is a separate code path from
the ones bug-161 unified.

These tests drive a real client session over the SDK's in-memory transport, so
the assertions are about what a client sees after a JSON-RPC round-trip.
"""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from cpersona import server as cpersona_server


def _body(result):
    """The tool's JSON payload, as a client would parse it."""
    assert result.content, "a tool call must answer with content"
    return json.loads(result.content[0].text)


@pytest.fixture
def registry_server():
    return cpersona_server.registry.server


@pytest.mark.asyncio
async def test_a_successful_call_survives_the_transport(registry_server):
    async with create_connected_server_and_client_session(registry_server) as session:
        result = await session.call_tool(
            "store", {"agent_id": "b177", "message": {"content": "a real memory"}}
        )
    body = _body(result)
    assert body["ok"] is True, body
    assert body["result"] == "stored", body


@pytest.mark.asyncio
async def test_the_b1_refusal_contract_survives_the_transport(registry_server):
    """store's rejection is the contract b1-1 introduced. Reaching a client with
    ok:false AND result:'rejected' is the claim; a handler-level assertion cannot
    make it."""
    async with create_connected_server_and_client_session(registry_server) as session:
        result = await session.call_tool(
            "store", {"agent_id": "b177", "message": {"content": ""}}
        )
    body = _body(result)
    assert body["ok"] is False, body
    assert body["result"] == "rejected", body
    assert "reason" in body, body


@pytest.mark.asyncio
async def test_the_advertised_schema_matches_what_the_handler_reads(registry_server):
    """The inert-in-production class: a handler can accept an argument the tool
    never advertises, so a client has no way to send it. Pin the two schemas the
    2.5.2 work depends on."""
    async with create_connected_server_and_client_session(registry_server) as session:
        listed = await session.list_tools()
    tools = {t.name: t for t in listed.tools}

    assert "store" in tools and "recall_with_context" in tools, sorted(tools)

    store_props = tools["store"].inputSchema["properties"]
    assert "agent_id" in store_props and "message" in store_props, store_props

    ctx = tools["recall_with_context"].inputSchema["properties"]["external_context"]
    assert ctx["type"] == "array", ctx
    # bug-163: role/content are advertised as strings, not prose-only.
    assert ctx["items"]["properties"]["role"]["type"] == "string", ctx
    assert ctx["items"]["properties"]["content"]["type"] == "string", ctx


@pytest.mark.asyncio
async def test_an_unknown_tool_answers_the_outermost_shape(registry_server):
    """CURRENT behaviour, pinned so bug-164 is visible rather than theoretical.

    The dispatch layer answers an unknown tool with a bare {error} carrying no
    `ok` key, so a client branching on `ok` — the field bug-161 made universal
    across the tool surface — reads None here. This assertion documents the gap;
    it must be INVERTED (not deleted) when bug-164 closes it upstream, since the
    wrapper lives in the vendored tree and is a cross-consumer change.
    """
    async with create_connected_server_and_client_session(registry_server) as session:
        result = await session.call_tool("no_such_tool_exists", {})
    body = _body(result)
    assert "error" in body, body
    assert "ok" not in body, (
        "bug-164 closed upstream — invert this assertion and update the registry entry"
    )


@pytest.mark.asyncio
async def test_an_escaped_exception_answers_the_outermost_shape(registry_server, monkeypatch):
    """The other half of bug-164: anything a handler lets escape is flattened to
    the same ok-less {error}. Pinned for the same reason, and it also proves the
    dispatch layer never propagates an exception to the transport."""

    async def exploding_handler(arguments):
        raise RuntimeError("handler blew up")

    monkeypatch.setitem(
        cpersona_server.registry._handlers, "get_recall_precision", exploding_handler
    )
    async with create_connected_server_and_client_session(registry_server) as session:
        result = await session.call_tool("get_recall_precision", {"agent_id": "b177"})
    body = _body(result)
    assert body == {"error": "handler blew up"}, body
