"""store's top-level form (2.6.6): content at the top, defaults from the connection, lock.

Measured on a model writing memories in a real session: one store in six was refused
by the client before it reached the server, every time for the same reason — the
closing braces of the three-deep message.source object miscounted — and the model
wrote the same source and id on nearly every call. These tests pin the shape that
removes those keys from the call, and the two rules that keep it from guessing:

* a default is applied only where the call left the field out (an explicit {} source
  or an explicit agent_id is the caller's, never replaced), and
* agent_id is filled in only where the connection can write to exactly one agent.
"""

import json

import pytest
import pytest_asyncio
from mcp.shared.memory import create_connected_server_and_client_session

from cpersona import acl, aliases, server, session
from cpersona.database import get_db

AGENT = "flat-form-agent"


@pytest_asyncio.fixture
async def db():
    handle = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await handle.execute(f"DELETE FROM {table}")
    await handle.commit()
    session.reset_pauses_for_tests()
    yield handle
    acl.activate(None)
    acl.activate_ledger(None)
    session.reset_pauses_for_tests()


async def _rows(handle):
    return await handle.execute_fetchall(
        "SELECT agent_id, msg_id, content, source, timestamp, metadata, locked FROM memories ORDER BY id"
    )


def _store_handler():
    """The served handler: argument extraction, the ACL guard and the boundary."""
    return server.registry._handlers["store"]


def _body(result):
    assert result.content, "a tool call must answer with content"
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# The top-level form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_level_fields_are_stored_with_this_agent_as_the_source(db):
    result = await _store_handler()(
        {
            "agent_id": AGENT,
            "content": "the flat form",
            "id": "flat-1",
            "timestamp": "2026-10-06T10:00:00+00:00",
            "metadata": {"k": "v"},
        }
    )
    assert result["ok"] is True and result["result"] == "stored", result
    [(agent_id, msg_id, content, source, timestamp, metadata, locked)] = await _rows(db)
    assert (agent_id, msg_id, content) == (AGENT, "flat-1", "the flat form")
    assert json.loads(source) == {"type": "Agent", "id": AGENT, "name": ""}
    assert timestamp == "2026-10-06T10:00:00+00:00"
    assert json.loads(metadata) == {"k": "v"}
    assert locked == 0 and "locked" not in result


@pytest.mark.asyncio
async def test_an_explicit_empty_source_is_kept_not_replaced_by_the_default(db):
    await _store_handler()({"agent_id": AGENT, "content": "producer unknown", "source": {}})
    await _store_handler()(
        {"agent_id": AGENT, "content": "a user said it", "source": {"type": "User", "id": "u-9"}}
    )
    sources = [json.loads(row[3]) for row in await _rows(db)]
    assert sources[0] == {}, "an explicit {} means 'unknown' and must not become this agent"
    assert sources[1] == {"type": "User", "id": "u-9"}


@pytest.mark.asyncio
async def test_the_legacy_message_form_keeps_its_anonymous_default(db):
    result = await _store_handler()({"agent_id": AGENT, "message": {"content": "legacy", "id": "old-1"}})
    assert result["result"] == "stored", result
    [(agent_id, msg_id, content, source, *_rest)] = await _rows(db)
    assert (agent_id, msg_id, content) == (AGENT, "old-1", "legacy")
    assert json.loads(source) == {}, "a legacy caller's attribution must not move"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"content": "top"}, {"id": "x"}, {"source": {}}, {"timestamp": "2026-10-06T10:00:00+00:00"}, {"metadata": {"a": 1}}],
)
async def test_mixing_the_two_forms_is_refused_and_writes_nothing(db, extra):
    result = await _store_handler()({"agent_id": AGENT, "message": {"content": "inside"}, **extra})
    assert result == {
        "ok": False,
        "result": "rejected",
        "reason": "send content, id, source, timestamp and metadata either at the top level "
        "or inside message, not both",
    }
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_missing_content_in_the_top_level_form_is_the_empty_content_refusal(db):
    result = await _store_handler()({"agent_id": AGENT})
    assert result["ok"] is False and result["result"] == "rejected" and result["reason"] == "empty content"
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_without_acl_an_omitted_agent_id_is_refused_not_written_to_the_empty_bucket(db):
    result = await _store_handler()({"content": "whose is this"})
    assert result["ok"] is False and result["result"] == "rejected", result
    assert "agent_id is required" in result["reason"]
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_an_explicit_empty_agent_id_still_addresses_the_empty_bucket(db):
    """The documented '' bucket (test_docs_behavioural_claims) is not an omission."""
    result = await _store_handler()({"agent_id": "", "content": "empty bucket"})
    assert result["result"] == "stored", result
    assert [row[0] for row in await _rows(db)] == [""]


@pytest.mark.asyncio
async def test_over_mcp_the_schema_admits_the_top_level_form_and_refuses_a_string_source(db):
    registry_server = server.registry.server
    async with create_connected_server_and_client_session(registry_server) as client:
        listed = await client.list_tools()
        stored = await client.call_tool("store", {"agent_id": AGENT, "content": "over the wire", "lock": True})
        omitted = await client.call_tool("store", {"content": "no agent named"})
        refused = await client.call_tool("store", {"agent_id": AGENT, "content": "x", "source": "user"})
    schema = next(t for t in listed.tools if t.name == "store").inputSchema
    assert "agent_id" not in schema.get("required", []), "agent_id can no longer be schema-required"
    assert _body(stored)["result"] == "stored" and _body(stored)["locked"] is True
    # Omission reaches the server, which answers in the store contract.
    assert _body(omitted)["result"] == "rejected"
    # A top-level string source would be read as "omitted" by the dict extractor, so the
    # schema refuses it where the caller can see it (the bug-398 class).
    assert refused.isError is True
    assert refused.content[0].text.startswith("Input validation error"), refused.content[0].text
    assert [row[2] for row in await _rows(db)] == ["over the wire"]


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_locks_the_stored_row(db):
    result = await _store_handler()({"agent_id": AGENT, "content": "keep this", "lock": True})
    assert result["result"] == "stored" and result["locked"] is True, result
    [row] = await _rows(db)
    assert row[6] == 1


@pytest.mark.asyncio
async def test_lock_on_a_duplicate_locks_the_existing_row(db):
    first = await _store_handler()({"agent_id": AGENT, "content": "said twice"})
    again = await _store_handler()({"agent_id": AGENT, "content": "said twice", "lock": True})
    assert again["result"] == "skipped" and again["id"] == first["id"] and again["locked"] is True, again
    [row] = await _rows(db)
    assert row[6] == 1


@pytest.mark.asyncio
async def test_lock_works_in_the_legacy_form_too(db):
    result = await _store_handler()({"agent_id": AGENT, "message": {"content": "legacy lock"}, "lock": True})
    assert result["locked"] is True
    assert [row[6] for row in await _rows(db)] == [1]


@pytest.mark.asyncio
async def test_lock_with_no_row_says_so(db):
    refused = await _store_handler()({"agent_id": AGENT, "content": "   ", "lock": True})
    assert refused["result"] == "rejected" and refused["locked"] is False, refused
    session.pause_for(session.TRANSPORT_KEY, False, 60)
    paused = await _store_handler()({"agent_id": AGENT, "content": "not now", "lock": True})
    assert paused["persisted"] is False and paused["locked"] is False, paused
    assert await _rows(db) == []


# ---------------------------------------------------------------------------
# agent_id from the connection (ACL)
# ---------------------------------------------------------------------------


def _activate(tmp_path, clients):
    path = tmp_path / "acl.json"
    path.write_text(json.dumps({"clients": clients}), encoding="utf-8")
    acl.activate(acl.load_config(str(path)))


def test_connection_agent_is_the_single_writable_agent_or_nothing():
    rw, r, none = acl.PERM_WRITE, acl.PERM_READ, acl.PERM_NONE
    assert acl.connection_agent({"alpha": rw}) == "alpha"
    assert acl.connection_agent({"alpha": rw, "*": r}) == "alpha", "reading elsewhere leaves one writable agent"
    assert acl.connection_agent({"alpha": rw, "beta": rw}) == ""
    assert acl.connection_agent({"*": rw}) == ""
    assert acl.connection_agent({"*": rw, "alpha": none}) == "", "the wildcard can still write elsewhere"
    assert acl.connection_agent({"alpha": rw, "*": rw}) == ""
    assert acl.connection_agent({"alpha": r}) == ""
    assert acl.connection_agent({}) == ""


@pytest.mark.asyncio
async def test_an_omitted_agent_id_lands_in_the_one_agent_the_connection_can_write(db, tmp_path):
    _activate(tmp_path, [{"client_id": "solo", "token": "t-solo", "grants": {"alpha": "read-write", "*": "read"}}])
    token = acl.set_principal(acl.Principal("solo"))
    try:
        result = await _store_handler()({"content": "from the connection"})
        named = await _store_handler()({"agent_id": "alpha", "content": "named explicitly"})
    finally:
        acl.reset_principal(token)
    assert result["result"] == "stored" and result["resolved_agent_id"] == "alpha", result
    assert "resolved_agent_id" not in named, "nothing was resolved for a caller that named the agent"
    rows = await _rows(db)
    assert [row[0] for row in rows] == ["alpha", "alpha"]
    assert json.loads(rows[0][3]) == {"type": "Agent", "id": "alpha", "name": ""}


@pytest.mark.asyncio
async def test_a_connection_that_can_write_two_agents_gets_no_default(db, tmp_path):
    _activate(tmp_path, [{"client_id": "duo", "token": "t-duo", "grants": {"alpha": "read-write", "beta": "read-write"}}])
    token = acl.set_principal(acl.Principal("duo"))
    try:
        result = await _store_handler()({"content": "which one"})
    finally:
        acl.reset_principal(token)
    assert result["ok"] is False and result["error"] == "permission_denied", result
    assert "pass agent_id" in result["detail"]
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_an_explicit_agent_id_is_never_replaced_by_the_connection(db, tmp_path):
    _activate(tmp_path, [{"client_id": "solo", "token": "t-solo", "grants": {"alpha": "read-write"}}])
    token = acl.set_principal(acl.Principal("solo"))
    try:
        result = await _store_handler()({"agent_id": "beta", "content": "not mine to write"})
    finally:
        acl.reset_principal(token)
    assert result["error"] == "permission_denied" and result["agent_id"] == "beta", result
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_an_explicit_empty_agent_id_is_not_an_omission_under_acl(db, tmp_path):
    """'' names the empty bucket; filling it in would move a write the caller addressed."""
    _activate(tmp_path, [{"client_id": "solo", "token": "t-solo", "grants": {"alpha": "read-write"}}])
    token = acl.set_principal(acl.Principal("solo"))
    try:
        result = await _store_handler()({"agent_id": "", "content": "the empty bucket"})
    finally:
        acl.reset_principal(token)
    assert result["error"] == "permission_denied" and "resolved_agent_id" not in result, result
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_only_store_takes_its_agent_from_the_connection(tmp_path):
    _activate(tmp_path, [{"client_id": "solo", "token": "t-solo", "grants": {"alpha": "read-write"}}])

    async def echo(arguments):
        return {"ok": True, "echo": arguments}

    token = acl.set_principal(acl.Principal("solo"))
    try:
        stored = await acl._wrap("store", echo)({"content": "x"})
        recalled = await acl._wrap("recall", echo)({"query": "x"})
    finally:
        acl.reset_principal(token)
    assert stored["echo"]["agent_id"] == "alpha"
    assert recalled["error"] == "permission_denied", "a read must name the agent it reads"


@pytest.mark.asyncio
async def test_a_per_subject_connection_writes_to_its_own_alias(db, tmp_path):
    _activate(
        tmp_path,
        [{"client_id": "oauth:https://idp.example:web", "token": None, "grants": {"*": "read-write"}, "per_subject": True}],
    )
    acl.activate_ledger(aliases.AliasLedger(str(tmp_path / "alias_ledger.json")))
    principal = acl.Principal(client_id="oauth:https://idp.example:web", issuer="https://idp.example", subject="user-1")
    token = acl.set_principal(principal)
    try:
        result = await _store_handler()({"content": "my own space"})
    finally:
        acl.reset_principal(token)
    alias = result["resolved_agent_id"]
    assert result["result"] == "stored" and alias.startswith(acl.ALIAS_PREFIX), result
    rows = await _rows(db)
    assert [row[0] for row in rows] == [alias]
    assert json.loads(rows[0][3])["id"] == alias
