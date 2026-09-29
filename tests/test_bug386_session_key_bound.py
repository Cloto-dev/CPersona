"""bug-386: session_key has a published length bound, refused at the tool boundary.

The key is a dictionary key in three process-global maps, each capped at 256
entries and not by bytes, so an unbounded key made retained memory 256 times
whatever a caller chose to send (measured: 400 distinct 64 KiB keys left each
advisory map holding 16.8 MB). Every schema that takes the key now declares
`maxLength`, and the MCP boundary validates arguments against the schema.
"""

import pytest
from mcp import types

from cpersona import health, server, update_check
from cpersona.session import SESSION_KEY_MAX_CHARS


def _tools_with_a_session_key():
    return [
        (tool.name, tool.inputSchema["properties"]["session_key"])
        for tool in server.registry._tools
        if "session_key" in tool.inputSchema.get("properties", {})
    ]


def test_every_tool_that_takes_the_key_declares_the_bound():
    tools = _tools_with_a_session_key()
    # The key is on most of the surface; an empty list would make this vacuous.
    assert len(tools) >= 20, [name for name, _ in tools]
    unbounded = [name for name, prop in tools if prop.get("maxLength") != SESSION_KEY_MAX_CHARS]
    assert unbounded == [], unbounded


async def _call(name: str, arguments: dict) -> types.CallToolResult:
    handler = server.registry.server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return (await handler(request)).root


@pytest.mark.asyncio
async def test_a_key_past_the_bound_is_refused_at_the_boundary():
    result = await _call("persistence_status", {"session_key": "k" * (SESSION_KEY_MAX_CHARS + 1)})
    # Before the fix the call succeeded and answered scope "session".
    assert result.isError is True
    text = result.content[0].text
    assert text.startswith("Input validation error") and text.endswith("is too long"), text[-80:]


@pytest.mark.asyncio
async def test_a_key_at_the_bound_is_accepted():
    result = await _call("persistence_status", {"session_key": "k" * SESSION_KEY_MAX_CHARS})
    assert result.isError is not True, result.content[0].text
    assert '"scope": "session"' in result.content[0].text


@pytest.mark.asyncio
async def test_a_refused_key_reaches_no_process_map(monkeypatch):
    """The point of the bound: a refused call leaves nothing behind in the maps."""
    health._reset()
    monkeypatch.setattr(update_check, "_told_sessions", type(update_check._told_sessions)())
    long_key = "x" * (SESSION_KEY_MAX_CHARS + 1)
    result = await _call("recall", {"agent_id": "bound.agent", "query": "q", "session_key": long_key})
    assert result.isError is True
    assert long_key not in health._told_sessions
    assert long_key not in update_check._told_sessions
