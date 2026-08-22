"""ACL through the REAL streamable-HTTP transport (design §5.1/§8-2).

tests/test_acl.py drives the assembled middleware stack with a sentinel
endpoint, which proves middleware → contextvar → guard — but not that the MCP
SDK's own dispatch preserves the request context. That is an implementation
detail of ``StreamableHTTPSessionManager(stateless=True)`` (the tool call runs
inside the request's task lineage), and it is exactly the detail a future
transport change (sessionful mode, task pools) would break silently: every
call would resolve to "no principal" and be denied. These tests run the real
session manager end-to-end so that regression is red, not silent.
"""

import asyncio
import contextlib
import json

import pytest

from cpersona import acl, server


def _acl_config(tmp_path):
    payload = {
        "clients": [
            {
                "client_id": "operator",
                "token": "op-token",
                "grants": {"*": "read-write"},
            },
            {"client_id": "reader", "token": "rd-token", "grants": {"beta": "read"}},
        ]
    }
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return acl.load_config(str(path))


async def _run_with_real_transport(tmp_path, drive):
    """Build the production app over a real session manager and call *drive*.

    Everything happens in the current task: the anyio task group inside
    ``session_manager.run()`` must be entered and exited by the same task, so
    this is a helper rather than a yielding fixture.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    config = _acl_config(tmp_path)
    acl.activate(config)
    try:
        session_manager = StreamableHTTPSessionManager(
            app=server.registry.server, stateless=True
        )

        async def mcp_endpoint(scope, receive, send):
            await session_manager.handle_request(scope, receive, send)

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            yield

        app = server._build_http_app("", mcp_endpoint, lifespan, acl_config=config)
        async with session_manager.run():
            return await drive(app)
    finally:
        acl.activate(None)


async def _post_tool_call(app, token, tool, arguments):
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


def _tool_result(raw: bytes) -> dict:
    """Extract the tool's JSON payload from an SSE or JSON response body."""
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:].strip()
            break
    rpc = json.loads(text)
    return json.loads(rpc["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_granted_call_executes_through_the_real_transport(tmp_path):
    async def drive(app):
        return await _post_tool_call(app, "op-token", "persistence_status", {})

    status, raw = await _run_with_real_transport(tmp_path, drive)
    assert status == 200
    result = _tool_result(raw)
    assert "paused" in result, f"tool did not execute: {result}"


@pytest.mark.asyncio
async def test_denial_shape_travels_through_the_real_transport(tmp_path):
    async def drive(app):
        return await _post_tool_call(
            app, "rd-token", "store", {"agent_id": "beta", "message": {"content": "x"}}
        )

    status, raw = await _run_with_real_transport(tmp_path, drive)
    assert status == 200  # transport-level success; the refusal is the payload
    result = _tool_result(raw)
    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    assert result["tool"] == "store"
    assert result["agent_id"] == "beta"
    assert result["required"] == "read-write"
    assert result["client_id"] == "reader"


@pytest.mark.asyncio
async def test_missing_token_is_401_before_the_transport(tmp_path):
    async def drive(app):
        return await _post_tool_call(app, "", "persistence_status", {})

    status, _ = await _run_with_real_transport(tmp_path, drive)
    assert status == 401
