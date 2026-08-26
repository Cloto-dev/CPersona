"""Drive the production ASGI app through the REAL streamable-HTTP transport.

Two test modules need the same thing: the assembled Starlette app, mounted on a
real ``StreamableHTTPSessionManager``, answering real JSON-RPC POSTs. Building
that twice would give the suite two theories of how a request reaches a tool,
and the point of these tests is that the wiring — not the classes — is what
production runs. A second copy would keep passing after the first went stale.

Not a ``test_*`` module, so pytest collects nothing here; ``tests/`` is on
``sys.path`` during collection (same route ``behaviour_252`` is imported by).
"""

import asyncio
import contextlib
import json

from cpersona import acl, server


def write_acl_config(tmp_path, clients):
    """Write an ACL file from ``clients`` and load it the way the server does."""
    path = tmp_path / "acl.json"
    path.write_text(json.dumps({"clients": clients}), encoding="utf-8")
    return acl.load_config(str(path))


async def run_with_real_transport(acl_config, drive):
    """Build the production app over a real session manager and call *drive*.

    Everything happens in the current task: the anyio task group inside
    ``session_manager.run()`` must be entered and exited by the same task, so
    this is a helper rather than a yielding fixture.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    acl.activate(acl_config)
    try:
        session_manager = StreamableHTTPSessionManager(
            app=server.registry.server, stateless=True
        )

        async def mcp_endpoint(scope, receive, send):
            await session_manager.handle_request(scope, receive, send)

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            yield

        app = server._build_http_app("", mcp_endpoint, lifespan, acl_config=acl_config)
        async with session_manager.run():
            return await drive(app)
    finally:
        acl.activate(None)


async def post_tool_call(app, token, tool, arguments):
    """One real JSON-RPC tools/call POST through the full ASGI stack."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    if token:
        headers.append((b"authorization", b"Bearer " + token.encode()))

    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8402),
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            # Never signal a disconnect: the stateless server closes the SSE
            # response itself, and an early disconnect aborts the write.
            await asyncio.Event().wait()
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], raw


def tool_result(raw: bytes) -> dict:
    """Extract the tool's JSON payload from an SSE or JSON response body."""
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:].strip()
            break
    rpc = json.loads(text)
    return json.loads(rpc["result"]["content"][0]["text"])
