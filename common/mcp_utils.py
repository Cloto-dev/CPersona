"""
Decorator-based MCP tool registration utility.
Eliminates boilerplate list_tools/call_tool patterns across all servers.
"""

import json
from collections.abc import Callable

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from common.validation import validate_bool, validate_dict, validate_int, validate_list, validate_str

_VALIDATORS: dict[type, Callable] = {
    bool: validate_bool,
    str: validate_str,
    int: validate_int,
    dict: validate_dict,
    list: validate_list,
}


class ToolRegistry:
    """Decorator-based MCP tool registration."""

    def __init__(self, server_name: str):
        self.server = Server(server_name)
        self._tools: list[Tool] = []
        self._handlers: dict[str, Callable] = {}
        self._bind()

    def tool(self, name: str, description: str, schema: dict):
        """Decorator: register a tool handler.

        The decorated function receives (arguments: dict) and returns a dict.
        JSON serialization and TextContent wrapping are handled automatically.
        """

        def decorator(fn):
            self._tools.append(Tool(name=name, description=description, inputSchema=schema))
            self._handlers[name] = fn
            return fn

        return decorator

    def auto_tool(
        self,
        name: str,
        description: str,
        schema: dict,
        handler: Callable,
        params: list[tuple],
        annotations: ToolAnnotations | None = None,
    ):
        """Register a tool with auto-validated parameter extraction.

        Each entry in *params* is ``(key, type)`` or ``(key, type, default)``.
        Supported types: ``str``, ``int``, ``dict``, ``list``.
        The extracted values are passed positionally to *handler*.
        """

        async def _handler(arguments: dict) -> dict:
            args = []
            for spec in params:
                key, typ = spec[0], spec[1]
                default = spec[2] if len(spec) > 2 else None
                validator = _VALIDATORS[typ]
                if default is not None:
                    args.append(validator(arguments, key, default))
                else:
                    args.append(validator(arguments, key))
            return await handler(*args)

        self._tools.append(Tool(name=name, description=description, inputSchema=schema, annotations=annotations))
        self._handlers[name] = _handler

    def _bind(self):
        registry = self

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return registry._tools

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            handler = registry._handlers.get(name)
            if handler is None:
                return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
            try:
                result = await handler(arguments)
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def run_mcp_server(registry: ToolRegistry):
    """Standard MCP server main loop."""
    async with stdio_server() as (read_stream, write_stream):
        await registry.server.run(read_stream, write_stream, registry.server.create_initialization_options())
