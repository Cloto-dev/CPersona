"""Regression tests for update_memory's refusals and the remote side-effect deadline.

Three defects, each reproduced on the unfixed tree first:

  bug-609  an edit into text another row of the same (agent, project, channel)
           bucket already holds raised sqlite3.IntegrityError out of the handler,
           so the wire answer was a bare {"error": "UNIQUE constraint failed: ..."}
           with no ok:false. It is now a refusal that names the row holding the text.
  bug-610  an UPDATE that matched no row was always answered "is locked", including
           when a concurrent delete or merge-move had removed the row.
  bug-608  the delete / update / purge / move de-index posts stated no deadline, so
           each one waited the embedding client's own timeout (30 s by default)
           while every sibling remote call states REMOTE_INDEX_TIMEOUT_SECS; the
           move de-index multiplied that wait by its chunk count.
"""

import asyncio
import sqlite3
import time

import httpx
import pytest
import pytest_asyncio

from cpersona import admin_handlers as admin
from cpersona import session, vector
from cpersona.database import connection, get_db, transaction

AGENT = "regress.x01"
OTHER = "regress.x01.other"


@pytest_asyncio.fixture
async def clean():
    session.reset_pauses_for_tests()
    db = await get_db()
    for agent in (AGENT, OTHER):
        for table in ("memories", "episodes", "profiles"):
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()
    yield db
    for agent in (AGENT, OTHER):
        for table in ("memories", "episodes", "profiles"):
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()


async def _insert(content: str, *, agent: str = AGENT, channel: str = "", project: str = "") -> int:
    async with transaction() as db:
        cursor = await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp)"
            " VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            (agent, project, channel, content),
        )
        return cursor.lastrowid


async def _contents() -> dict:
    async with connection() as db:
        rows = await db.execute_fetchall(
            "SELECT id, content FROM memories WHERE agent_id = ? ORDER BY id", (AGENT,)
        )
    return {row_id: text for row_id, text in rows}


# ==========================================================================
# bug-609 -- an edit into a same-bucket duplicate escaped as IntegrityError.
# ==========================================================================
@pytest.mark.asyncio
async def test_update_into_same_bucket_text_is_refused_and_names_the_holder(
    clean, fake_embedding_client, monkeypatch
):
    holder = await _insert("alpha content one")
    edited = await _insert("beta content two")
    embedded = []
    original_embed = fake_embedding_client.embed

    async def counting_embed(texts):
        embedded.append(list(texts))
        return await original_embed(texts)

    monkeypatch.setattr(fake_embedding_client, "embed", counting_embed)
    result = await admin.do_update_memory(edited, "alpha content one", AGENT)
    assert result["ok"] is False, result
    assert f"memory {holder} " in result["error"], result
    assert "UNIQUE" not in result["error"], result
    # Refused before the embedding round trip, not after it.
    assert embedded == [], embedded
    assert await _contents() == {holder: "alpha content one", edited: "beta content two"}


@pytest.mark.asyncio
async def test_update_into_text_of_another_bucket_or_itself_still_lands(clean, fake_embedding_client):
    # The unique index is per (agent, project, channel); the refusal must be too.
    await _insert("shared wording", channel="elsewhere")
    await _insert("shared wording", project="proj-x")
    await _insert("shared wording", agent=OTHER)
    edited = await _insert("to be rewritten")
    result = await admin.do_update_memory(edited, "shared wording", AGENT)
    assert (result["ok"], result.get("updated_id")) == (True, edited), result
    # Rewriting a row to the text it already holds collides with nothing.
    again = await admin.do_update_memory(edited, "shared wording", AGENT)
    assert (again["ok"], again.get("updated_id")) == (True, edited), again


@pytest.mark.asyncio
async def test_update_racing_a_store_of_the_same_text_is_refused_not_raised(
    clean, fake_embedding_client, monkeypatch
):
    """The duplicate lands during the embedding await, after any pre-check could see it."""
    edited = await _insert("old wording")
    holder = {}
    original_embed = fake_embedding_client.embed

    async def racing_embed(texts):
        holder["id"] = await _insert("contested wording")
        return await original_embed(texts)

    monkeypatch.setattr(fake_embedding_client, "embed", racing_embed)
    try:
        result = await admin.do_update_memory(edited, "contested wording", AGENT)
    except sqlite3.IntegrityError as exc:  # the defect: a raw constraint error
        pytest.fail(f"update_memory raised {type(exc).__name__}: {exc}")
    assert result["ok"] is False, result
    assert f"memory {holder['id']} " in result["error"], result
    assert await _contents() == {edited: "old wording", holder["id"]: "contested wording"}


@pytest.mark.asyncio
async def test_update_collision_reaches_the_wire_as_ok_false(clean):
    """Through the registered MCP handler, not just the coroutine."""
    import json

    import mcp.types as T

    from cpersona import server

    holder = await _insert("wire wording one")
    edited = await _insert("wire wording two")
    request = T.CallToolRequest(
        method="tools/call",
        params=T.CallToolRequestParams(
            name="update_memory",
            arguments={"memory_id": edited, "content": "wire wording one", "agent_id": AGENT},
        ),
    )
    response = await server.registry.server.request_handlers[T.CallToolRequest](request)
    body = json.loads(response.root.content[0].text)
    assert body.get("ok") is False, body
    assert f"memory {holder} " in body["error"], body


# ==========================================================================
# bug-610 -- a row removed under the edit was reported as locked.
# ==========================================================================
async def _update_while(action, *, scoped: bool, fake_embedding_client, monkeypatch):
    row_id = await _insert("edit target")
    original_embed = fake_embedding_client.embed

    async def interleaved_embed(texts):
        await action(row_id)
        return await original_embed(texts)

    monkeypatch.setattr(fake_embedding_client, "embed", interleaved_embed)
    return row_id, await admin.do_update_memory(row_id, "edited text", AGENT if scoped else "")


async def _delete_row(row_id):
    async with transaction() as db:
        await db.execute("DELETE FROM memories WHERE id = ?", (row_id,))


async def _lock_row(row_id):
    async with transaction() as db:
        await db.execute("UPDATE memories SET locked = 1 WHERE id = ?", (row_id,))


@pytest.mark.asyncio
async def test_update_of_a_row_deleted_meanwhile_says_not_found_not_locked(
    clean, fake_embedding_client, monkeypatch
):
    row_id, result = await _update_while(
        _delete_row, scoped=False, fake_embedding_client=fake_embedding_client, monkeypatch=monkeypatch
    )
    assert result == {"ok": False, "error": f"Memory {row_id} not found"}, result


@pytest.mark.asyncio
async def test_scoped_update_of_a_row_deleted_meanwhile_gets_the_ambiguous_refusal(
    clean, fake_embedding_client, monkeypatch
):
    row_id, result = await _update_while(
        _delete_row, scoped=True, fake_embedding_client=fake_embedding_client, monkeypatch=monkeypatch
    )
    assert result == {
        "ok": False,
        "error": f"Memory {row_id} not found, not owned by agent, or locked",
    }, result


@pytest.mark.asyncio
async def test_update_of_a_row_moved_by_a_merge_meanwhile_is_not_called_locked(
    clean, fake_embedding_client, monkeypatch
):
    async def move(_row_id):
        out = await admin.do_merge_memories(AGENT, OTHER, mode="move")
        assert out.get("ok") is not False, out

    row_id, result = await _update_while(
        move, scoped=False, fake_embedding_client=fake_embedding_client, monkeypatch=monkeypatch
    )
    assert result["ok"] is False, result
    assert "locked" not in result["error"], result


@pytest.mark.asyncio
async def test_update_of_a_row_locked_meanwhile_still_says_locked(
    clean, fake_embedding_client, monkeypatch
):
    row_id, result = await _update_while(
        _lock_row, scoped=False, fake_embedding_client=fake_embedding_client, monkeypatch=monkeypatch
    )
    assert result == {"ok": False, "error": f"Memory {row_id} is locked and cannot be edited"}, result
    assert await _contents() == {row_id: "edit target"}


# ==========================================================================
# bug-608 -- the remote side-effect posts named no deadline.
# ==========================================================================
class _Recorder:
    """Stands in for the embedding client's httpx client; records each post's kwargs."""

    def __init__(self, fail=None):
        self.posts = []
        self.fail = fail

    async def post(self, url, json=None, **kwargs):  # noqa: A002
        self.posts.append((url.rsplit("/", 1)[-1], json, kwargs))
        if self.fail is not None:
            raise self.fail
        return httpx.Response(200, json={})


@pytest.fixture
def remote(monkeypatch):
    def install(http):
        class _Client:
            _http_url = "http://remote.invalid/embed"
            _client = http
            mode = "remote"

            async def embed(self, texts):
                return [[0.1] * 8 for _ in texts]

        monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
        monkeypatch.setattr(admin, "VECTOR_SEARCH_MODE", "remote")
        monkeypatch.setattr(vector, "_embedding_client", _Client())
        monkeypatch.setattr(admin, "REMOTE_INDEX_TIMEOUT_SECS", 0.37, raising=False)
        return http

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete_memory", "update_memory", "delete_agent", "delete_episode"])
async def test_every_side_effect_post_states_the_index_deadline(clean, remote, operation):
    http = remote(_Recorder())
    row_id = await _insert("deadline probe")
    if operation == "delete_memory":
        result = await admin.do_delete_memory(row_id, AGENT)
    elif operation == "update_memory":
        result = await admin.do_update_memory(row_id, "deadline probe edited", AGENT)
    elif operation == "delete_agent":
        result = await admin.do_delete_agent_data(AGENT)
    else:
        async with transaction() as db:
            cursor = await db.execute(
                "INSERT INTO episodes (agent_id, summary) VALUES (?, ?)", (AGENT, "an episode")
            )
            episode_id = cursor.lastrowid
        result = await admin.do_delete_episode(episode_id, AGENT)
    assert result["ok"] is True, result
    assert http.posts, http.posts
    assert all(kwargs.get("timeout") == 0.37 for _path, _body, kwargs in http.posts), http.posts


@pytest.mark.asyncio
async def test_move_de_index_stops_after_a_timed_out_chunk(clean, remote, caplog):
    http = remote(_Recorder(fail=httpx.ReadTimeout("index service stopped answering")))
    tally = admin._MergeTally(dry_run=False)
    tally.copied_memory_ids = list(range(1000, 1300))  # 300 ids: three chunks of up to 128
    tally.copied_episode_ids = []
    await admin._remove_moved_source_vectors(AGENT, tally)
    # One hung chunk is one deadline, not one deadline per chunk.
    assert len(http.posts) == 1, [(p, len(b["ids"])) for p, b, _k in http.posts]
    assert http.posts[0][2].get("timeout") == 0.37


@pytest.mark.asyncio
async def test_move_de_index_keeps_going_past_a_refused_chunk(clean, remote):
    """A fast refusal costs nothing to retry on the next chunk, so it does not stop the loop."""

    class _Refusing(_Recorder):
        async def post(self, url, json=None, **kwargs):  # noqa: A002
            await super().post(url, json=json, **kwargs)
            return httpx.Response(500, json={})

    http = remote(_Refusing())
    tally = admin._MergeTally(dry_run=False)
    tally.copied_memory_ids = list(range(1000, 1300))
    tally.copied_episode_ids = []
    await admin._remove_moved_source_vectors(AGENT, tally)
    assert [len(body["ids"]) for _p, body, _k in http.posts] == [128, 128, 44]


@pytest.mark.asyncio
async def test_side_effect_post_against_a_hung_service_returns_at_the_index_deadline(
    clean, monkeypatch
):
    """End to end over a real socket: a service that accepts and never answers."""
    from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient

    async def hung(reader, writer):
        try:
            await reader.read(65536)
            await asyncio.sleep(3600)
        except Exception:  # noqa: BLE001 - the probe server is torn down mid-sleep
            pass

    server = await asyncio.start_server(hung, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = EmbeddingClient(mode="http", http_url=f"http://127.0.0.1:{port}/embed", timeout=20)
    await client.initialize()
    try:
        row_id = await _insert("hung service probe")
        monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
        monkeypatch.setattr(admin, "VECTOR_SEARCH_MODE", "remote")
        monkeypatch.setattr(vector, "_embedding_client", client)
        monkeypatch.setattr(admin, "REMOTE_INDEX_TIMEOUT_SECS", 0.5, raising=False)
        started = time.monotonic()
        result = await asyncio.wait_for(admin.do_delete_memory(row_id, AGENT), 25)
        elapsed = time.monotonic() - started
    finally:
        await client.close()
        server.close()
    assert result == {"ok": True, "deleted_id": row_id}, result
    assert elapsed < 5, elapsed
