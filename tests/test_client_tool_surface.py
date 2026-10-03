"""What a client sees of CPersona before it calls anything: tool definitions and instructions.

Claude Code defers MCP tool definitions and shows an agent only the tool names and the server
instructions at session start; it cuts each tool description and the instructions at 2,048
characters, and loads a tool with the session when its _meta carries `anthropic/alwaysLoad`
(code.claude.com/docs/en/mcp). A description longer than the cut is a contract the agent never
reads, so the limit is a gate, not a style rule.
"""

import re

from mcp.types import Tool

from cpersona import server

DESCRIPTION_LIMIT = 2048
#: Room left in the 2,048 characters for an operator's summary, which the operating context
#: design asks to keep at or under 1,500 characters.
OWN_INSTRUCTIONS_LIMIT = 548


def _over_limit(tools) -> list[str]:
    return sorted(t.name for t in tools if len(t.description or "") > DESCRIPTION_LIMIT)


def _always_loaded(tools) -> set[str]:
    return {
        t.name
        for t in tools
        if (t.model_dump(by_alias=True, exclude_none=True).get("_meta") or {}).get("anthropic/alwaysLoad") is True
    }


def test_every_tool_description_fits_the_client_limit():
    over = _over_limit(server.registry._tools)
    assert not over, f"descriptions an agent sees only the first {DESCRIPTION_LIMIT} characters of: {over}"


def test_the_limit_check_sees_a_long_description():
    long = Tool(name="long", description="a" * (DESCRIPTION_LIMIT + 1), inputSchema={"type": "object"})
    fits = Tool(name="fits", description="a" * DESCRIPTION_LIMIT, inputSchema={"type": "object"})
    assert _over_limit([long, fits]) == ["long"]


def test_the_session_tools_are_loaded_with_the_session_on_the_wire():
    assert _always_loaded(server.registry._tools) == {"reconstruct", "store", "archive_episode"}
    assert set(server.ALWAYS_LOADED_TOOLS) == {"reconstruct", "store", "archive_episode"}


def test_the_instructions_open_with_cpersonas_guidance():
    text = server.registry.server.create_initialization_options().instructions
    assert text and text.startswith(server.SERVER_INSTRUCTIONS)
    assert len(server.SERVER_INSTRUCTIONS) <= OWN_INSTRUCTIONS_LIMIT
    for name in server.ALWAYS_LOADED_TOOLS:
        assert name in server.SERVER_INSTRUCTIONS, f"the instructions do not say when to use {name}"


def test_every_tool_the_instructions_name_exists():
    registered = {t.name for t in server.registry._tools}
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", server.SERVER_INSTRUCTIONS)) | {"reconstruct", "store"}
    assert named <= registered, f"the instructions name tools that are not registered: {sorted(named - registered)}"


def test_an_operator_summary_follows_cpersonas_guidance():
    assert server.server_instructions(None) == server.SERVER_INSTRUCTIONS
    assert server.server_instructions("") == server.SERVER_INSTRUCTIONS
    combined = server.server_instructions("operator doctrine")
    assert combined.startswith(server.SERVER_INSTRUCTIONS) and combined.endswith("operator doctrine")
