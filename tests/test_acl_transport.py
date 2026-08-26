"""ACL through the REAL streamable-HTTP transport (design §5.1/§8-2).

tests/test_acl.py drives the assembled middleware stack with a sentinel
endpoint, which proves middleware → contextvar → guard — but not that the MCP
SDK's own dispatch preserves the request context. That is an implementation
detail of ``StreamableHTTPSessionManager(stateless=True)`` (the tool call runs
inside the request's task lineage), and it is exactly the detail a future
transport change (sessionful mode, task pools) would break silently: every
call would resolve to "no principal" and be denied. These tests run the real
session manager end-to-end so that regression is red, not silent.

The transport plumbing itself lives in ``tests/transport_harness.py`` — shared
with the bug-251 advisory tests, which need the same "one process, several
client sessions" setup for a different reason.
"""

import pytest
from transport_harness import (
    post_tool_call,
    run_with_real_transport,
    tool_result,
    write_acl_config,
)


def _acl_config(tmp_path):
    return write_acl_config(
        tmp_path,
        [
            {
                "client_id": "operator",
                "token": "op-token",
                "grants": {"*": "read-write"},
            },
            {"client_id": "reader", "token": "rd-token", "grants": {"beta": "read"}},
        ],
    )


@pytest.mark.asyncio
async def test_granted_call_executes_through_the_real_transport(tmp_path):
    async def drive(app):
        return await post_tool_call(app, "op-token", "persistence_status", {})

    status, raw = await run_with_real_transport(_acl_config(tmp_path), drive)
    assert status == 200
    result = tool_result(raw)
    assert "paused" in result, f"tool did not execute: {result}"


@pytest.mark.asyncio
async def test_denial_shape_travels_through_the_real_transport(tmp_path):
    async def drive(app):
        return await post_tool_call(
            app, "rd-token", "store", {"agent_id": "beta", "message": {"content": "x"}}
        )

    status, raw = await run_with_real_transport(_acl_config(tmp_path), drive)
    assert status == 200  # transport-level success; the refusal is the payload
    result = tool_result(raw)
    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    assert result["tool"] == "store"
    assert result["agent_id"] == "beta"
    assert result["required"] == "read-write"
    assert result["client_id"] == "reader"


@pytest.mark.asyncio
async def test_missing_token_is_401_before_the_transport(tmp_path):
    async def drive(app):
        return await post_tool_call(app, "", "persistence_status", {})

    status, _ = await run_with_real_transport(_acl_config(tmp_path), drive)
    assert status == 401
