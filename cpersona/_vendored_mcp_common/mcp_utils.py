"""
Decorator-based MCP tool registration utility.
Eliminates boilerplate list_tools/call_tool patterns across all servers.
"""

import json
import logging
from collections.abc import Callable

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from cpersona._vendored_mcp_common.validation import (
    validate_bool,
    validate_dict,
    validate_float,
    validate_int,
    validate_list,
    validate_str,
)


class _MgpValidationFilter(logging.Filter):
    """Drop mcp.shared.session's bulk pydantic validation warnings.

    The Python MCP SDK's ``ClientRequest`` union doesn't include MGP
    extensions (``mgp/callback/respond``, ``notifications/mgp.*``). Every
    time the kernel sends one the SDK logs a 30+ line ``Failed to validate
    request`` warning against every known method, even though the SDK's
    own error-response path handles it cleanly. These warnings are pure
    noise and drown out genuine errors.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not msg.startswith("Failed to validate request:") and not msg.startswith(
            "Failed to validate notification:"
        )


_MGP_FILTER_INSTALLED = False


def install_mgp_validation_filter() -> None:
    """Install the MGP validation log filter on the root logger.

    Called automatically by ``run_mcp_server``. Servers with a custom
    main loop (e.g. ones that also serve HTTP) should call this
    explicitly before entering ``stdio_server``.
    """
    global _MGP_FILTER_INSTALLED
    if _MGP_FILTER_INSTALLED:
        return
    logging.getLogger().addFilter(_MgpValidationFilter())
    _MGP_FILTER_INSTALLED = True


_VALIDATORS: dict[type, Callable] = {
    bool: validate_bool,
    str: validate_str,
    int: validate_int,
    float: validate_float,
    dict: validate_dict,
    list: validate_list,
}


class ToolRegistry:
    """Decorator-based MCP tool registration.

    *instructions* is forwarded to the SDK ``Server`` and lands in the
    ``initialize`` response's ``instructions`` field — the MCP-native channel
    for serving operating guidance to every connected client.
    """

    def __init__(self, server_name: str, instructions: str | None = None):
        self.server = Server(server_name, instructions=instructions)
        self._tools: list[Tool] = []
        self._handlers: dict[str, Callable] = {}
        self._bind()

    def tool(
        self,
        name: str,
        description: str,
        schema: dict,
        annotations: ToolAnnotations | None = None,
    ):
        """Decorator: register a tool handler.

        The decorated function receives (arguments: dict) and returns a dict.
        JSON serialization and TextContent wrapping are handled automatically.

        *annotations* is forwarded to the MCP Tool schema. The kernel reads
        ``destructiveHint`` from annotations to trigger the HITL approval
        gate for destructive tools.
        """

        def decorator(fn):
            tool_kwargs = {"name": name, "description": description, "inputSchema": schema}
            if annotations is not None:
                tool_kwargs["annotations"] = annotations
            self._tools.append(Tool(**tool_kwargs))
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
                has_default = len(spec) > 2
                default = spec[2] if has_default else None
                validator = _VALIDATORS[typ]
                # bug-019: a spec that supplies an explicit default — including the
                # literal (key, typ, None) — must pass that default to the validator.
                # Keying on `default is not None` conflated "no default given" with
                # "default is None", so ("project_id", str, None) fell through to the
                # validator's hardcoded ""-default and an omitted project_id reached
                # the handler as "" (global pool only) instead of None (all projects).
                if has_default:
                    args.append(validator(arguments, key, default))
                else:
                    args.append(validator(arguments, key))
            return await handler(*args)

        self._tools.append(
            Tool(
                name=name,
                description=description,
                inputSchema=schema,
                annotations=annotations,
            )
        )
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
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": f"Unknown tool: {name}"}),
                    )
                ]
            try:
                result = await handler(arguments)
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(result, ensure_ascii=False),
                    )
                ]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def run_mcp_server(registry: ToolRegistry):
    """Standard MCP server main loop."""
    install_mgp_validation_filter()
    async with stdio_server() as (read_stream, write_stream):
        await registry.server.run(
            read_stream,
            write_stream,
            registry.server.create_initialization_options(),
        )
