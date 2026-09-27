"""Shared identity reaches the real stateless MCP dispatcher and existing ACL."""

import contextlib
import json

import httpx
import pytest
import pytest_asyncio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from cpersona import acl, database, server
from cpersona._vendored_mcp_common import identity


@pytest_asyncio.fixture
async def isolated_db(monkeypatch):
    await database.close_db()
    monkeypatch.setattr(database, "DB_PATH", ":memory:")
    await database.get_db()
    yield
    await database.close_db()


@pytest.mark.asyncio
async def test_real_mcp_store_uses_shared_principal_and_preserves_acl(tmp_path, monkeypatch, isolated_db):
    assert acl.Principal is identity.Principal
    path = tmp_path / "acl.json"
    path.write_text(json.dumps({"clients": [
        {"client_id": "writer", "token": "${TEST_SHARED_WRITER}", "grants": {"shared": "read-write"}},
        {"client_id": "reader", "token": "reader-token", "grants": {"shared": "read"}},
    ]}), encoding="utf-8")
    monkeypatch.setenv("TEST_SHARED_WRITER", "writer-token")
    config = acl.load_config(str(path))
    monkeypatch.setattr(acl, "_active_config", config)
    manager = StreamableHTTPSessionManager(server.registry.server, stateless=True, json_response=True)

    async def endpoint(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    app = server._build_http_app("", endpoint, lifespan, acl_config=config)
    async with manager.run(), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:

        async def store(token, content):
            response = await client.post(
                "/mcp/",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "store", "arguments": {"agent_id": "shared", "client_id": "writer", "message": {"content": content, "source": {"type": "Agent", "id": "test"}}}}},
            )
            assert response.status_code == 200, response.text
            return json.loads(response.json()["result"]["content"][0]["text"])

        stored = await store("writer-token", "shared identity real MCP write")
        assert stored["result"] == "stored", stored
        db = await database.get_db()
        rows = await db.execute_fetchall("SELECT agent_id, content FROM memories WHERE id = ?", (stored["id"],))
        assert rows[0][0:2] == ("shared", "shared identity real MCP write")
        denied = await store("reader-token", "must not be stored")
        assert denied["error"] == "permission_denied"
        assert denied["client_id"] == "reader", "body-supplied identity must not override the credential"
        assert (await db.execute_fetchall("SELECT count(*) FROM memories"))[0][0] == 1
        assert acl.current_principal() is None


def test_existing_principal_context_preserves_subject_fields_and_nested_restore():
    outer = acl.Principal("outer", issuer="issuer", subject="subject")
    token = acl.set_principal(outer)
    try:
        with acl._current_principal.bind(acl.Principal("inner")):
            assert acl.current_principal().client_id == "inner"
        assert acl.current_principal() == outer
    finally:
        acl.reset_principal(token)
    assert acl.current_principal() is None
