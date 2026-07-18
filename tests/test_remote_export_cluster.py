"""Regressions for the deferred remote-mode and streaming-export cluster."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from cpersona import admin_handlers, database, memory_handlers, vector
from cpersona.database import get_db, transaction


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


class _RemoteResponse:
    def __init__(self, results):
        self._results = results

    def raise_for_status(self):
        return None

    def json(self):
        return {"results": self._results}


class _RemoteSearchHttpClient:
    def __init__(self, results):
        self._results = results

    async def post(self, url, **kwargs):
        assert url == "http://embedding.test/search"
        return _RemoteResponse(self._results)


class _RemoteSearchClient:
    def __init__(self, results):
        self._http_url = "http://embedding.test/embed"
        self._client = _RemoteSearchHttpClient(results)


@pytest.mark.asyncio
async def test_bug_046_remote_episode_fetch_rejects_other_channel(clean_db, monkeypatch):
    cur = await clean_db.execute(
        "INSERT INTO episodes (agent_id, channel, summary) VALUES ('remote-a', 'room-b', 'private')"
    )
    await clean_db.commit()
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(
        vector,
        "_embedding_client",
        _RemoteSearchClient([{"id": f"ep:{cur.lastrowid}", "score": 0.9}]),
    )

    results = await vector._search_vector(
        clean_db, "remote-a", "private", limit=10, channel="room-a"
    )

    assert results == []


@pytest.mark.asyncio
async def test_bug_075_remote_episode_source_gate_matches_channel_scoped_local_contract(
    clean_db, monkeypatch
):
    cur = await clean_db.execute(
        "INSERT INTO episodes (agent_id, channel, summary) VALUES ('remote-a', 'room-a', 'grounding')"
    )
    await clean_db.commit()
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(
        vector,
        "_embedding_client",
        _RemoteSearchClient([{"id": f"ep:{cur.lastrowid}", "score": 0.9}]),
    )

    scoped = await vector._search_vector(
        clean_db,
        "remote-a",
        "grounding",
        limit=10,
        channel="room-a",
        source_id="discord:user",
    )
    unscoped = await vector._search_vector(
        clean_db, "remote-a", "grounding", limit=10, source_id="discord:user"
    )

    assert [row["content"] for row in scoped] == ["[Episode] grounding"]
    assert unscoped == []


@pytest.mark.asyncio
async def test_bug_049_archive_episode_syncs_remote_index_after_commit(clean_db, monkeypatch):
    remote_upsert = AsyncMock()
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "remote_index_upsert", remote_upsert)

    result = await memory_handlers.do_archive_episode(
        "archive-agent", [], summary="remote archive summary"
    )

    assert result["ok"] is True
    remote_upsert.assert_awaited_once_with(
        "archive-agent",
        [{"id": f"ep:{result['episode_id']}", "text": "remote archive summary"}],
    )


@pytest.mark.asyncio
async def test_bug_050_import_and_merge_sync_episode_ids(clean_db, monkeypatch, tmp_path):
    remote_upsert = AsyncMock()
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "remote_index_upsert", remote_upsert)
    path = tmp_path / "episodes.jsonl"
    path.write_text(
        '{"_type":"episode","agent_id":"import-agent","summary":"imported episode"}\n'
    )

    imported = await admin_handlers.do_import_memories(str(path))
    import_id = (
        await clean_db.execute_fetchall(
            "SELECT id FROM episodes WHERE agent_id = 'import-agent'"
        )
    )[0][0]
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary) VALUES ('merge-source', 'merged episode')"
    )
    await clean_db.commit()
    merged = await admin_handlers.do_merge_memories("merge-source", "merge-target")
    merge_id = (
        await clean_db.execute_fetchall(
            "SELECT id FROM episodes WHERE agent_id = 'merge-target'"
        )
    )[0][0]

    assert imported["ok"] is True
    assert merged["ok"] is True
    assert remote_upsert.await_args_list[0].args == (
        "import-agent",
        [{"id": f"ep:{import_id}", "text": "imported episode"}],
    )
    assert remote_upsert.await_args_list[1].args == (
        "merge-target",
        [{"id": f"ep:{merge_id}", "text": "merged episode"}],
    )


class _RecordingCursor:
    def __init__(self, cursor, state, table):
        self._cursor = cursor
        self._state = state
        self._table = table

    async def fetchone(self):
        return await self._cursor.fetchone()

    async def fetchmany(self, size):
        self._state["fetchmany_calls"].append((self._table, size))
        if not self._state["writer_injected"]:
            self._state["writer_injected"] = True
            async with transaction() as db:
                await db.execute(
                    "INSERT INTO memories (agent_id, content, timestamp)"
                    " VALUES ('export-agent', 'concurrent row', '')"
                )
        return await self._cursor.fetchmany(size)

    async def fetchall(self):
        return await self._cursor.fetchall()


class _RecordingConnection:
    def __init__(self, db, state):
        self._connection = db
        self._state = state

    async def execute(self, sql, params=None):
        if params is None:
            cursor = await self._connection.execute(sql)
        else:
            cursor = await self._connection.execute(sql, params)
        table = next(
            (name for name in ("memories", "episodes", "profiles") if f"FROM {name}" in sql),
            None,
        )
        return _RecordingCursor(cursor, self._state, table)

    async def execute_fetchall(self, sql, params=None):
        if params is None:
            return await self._connection.execute_fetchall(sql)
        return await self._connection.execute_fetchall(sql, params)


class _ReaderScopeCursor:
    def __init__(self, cursor, state, table):
        self._cursor = cursor
        self._state = state
        self._table = table

    async def fetchone(self):
        return await self._cursor.fetchone()

    async def fetchmany(self, size):
        self._state["fetchmany_calls"].append((self._table, size))
        if not self._state["reader_injected"]:
            self._state["reader_injected"] = True
            async with database.connection() as d2:
                await d2.execute("SELECT 1")
            async with transaction() as db:
                await db.execute(
                    "INSERT INTO episodes (agent_id, summary)"
                    " VALUES ('export-agent', 'after reader scope')"
                )
        return await self._cursor.fetchmany(size)


class _ReaderScopeConnection(_RecordingConnection):
    async def execute(self, sql, params=None):
        if params is None:
            cursor = await self._connection.execute(sql)
        else:
            cursor = await self._connection.execute(sql, params)
        table = next(
            (name for name in ("memories", "episodes", "profiles") if f"FROM {name}" in sql),
            None,
        )
        return _ReaderScopeCursor(cursor, self._state, table)


@pytest.mark.asyncio
async def test_bug_073_export_streams_snapshot_and_roundtrips(clean_db, monkeypatch, tmp_path):
    memory_count = 1001
    await clean_db.executemany(
        "INSERT INTO memories (agent_id, msg_id, content, timestamp) VALUES (?, ?, ?, '')",
        [("export-agent", f"m-{i}", f"body {i}") for i in range(memory_count)],
    )
    await clean_db.executemany(
        "INSERT INTO episodes (agent_id, summary) VALUES (?, ?)",
        [("export-agent", f"episode {i}") for i in range(3)],
    )
    await clean_db.executemany(
        "INSERT INTO profiles (agent_id, user_id, content) VALUES (?, ?, ?)",
        [("export-agent", f"u-{i}", f"profile {i}") for i in range(2)],
    )
    await clean_db.commit()

    state = {"fetchmany_calls": [], "writer_injected": False}
    original_read_snapshot = admin_handlers.read_snapshot

    @asynccontextmanager
    async def recording_snapshot():
        async with original_read_snapshot() as db:
            yield _RecordingConnection(db, state)

    monkeypatch.setattr(admin_handlers, "read_snapshot", recording_snapshot)
    path = tmp_path / "streamed.jsonl"
    exported = await admin_handlers.do_export_memories("export-agent", str(path))

    records = [json.loads(line) for line in path.read_text().splitlines()]
    header = records[0]
    body_counts = {
        kind: sum(record.get("_type") == kind for record in records[1:])
        for kind in ("memory", "episode", "profile")
    }
    streamed_tables = {table for table, _ in state["fetchmany_calls"] if table}
    assert streamed_tables == {"memories", "episodes", "profiles"}
    assert {size for _, size in state["fetchmany_calls"]} == {500}
    assert sum(table == "memories" for table, _ in state["fetchmany_calls"]) >= 3
    assert state["writer_injected"] is True
    assert exported == {
        "ok": True,
        "path": str(path),
        "memories": memory_count,
        "episodes": 3,
        "profiles": 2,
    }
    assert header["memory_count"] == body_counts["memory"] == memory_count
    assert header["episode_count"] == body_counts["episode"] == 3
    assert header["profile_count"] == body_counts["profile"] == 2

    imported = await admin_handlers.do_import_memories(
        str(path), target_agent_id="restored-agent"
    )
    assert imported["ok"] is True
    assert imported["imported_memories"] == memory_count
    assert imported["imported_episodes"] == 3


@pytest.mark.asyncio
async def test_bug_073_export_survives_concurrent_reader_scope(
    clean_db, monkeypatch, tmp_path
):
    await clean_db.executemany(
        "INSERT INTO memories (agent_id, msg_id, content, timestamp) VALUES (?, ?, ?, '')",
        [("export-agent", f"r-{i}", f"reader body {i}") for i in range(3)],
    )
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary) VALUES ('export-agent', 'original episode')"
    )
    await clean_db.execute(
        "INSERT INTO profiles (agent_id, user_id, content)"
        " VALUES ('export-agent', '', 'reader profile')"
    )
    await clean_db.commit()

    state = {"fetchmany_calls": [], "reader_injected": False}
    original_read_snapshot = admin_handlers.read_snapshot

    @asynccontextmanager
    async def recording_snapshot():
        async with original_read_snapshot() as db:
            yield _ReaderScopeConnection(db, state)

    monkeypatch.setattr(admin_handlers, "read_snapshot", recording_snapshot)
    path = tmp_path / "reader-scope.jsonl"
    exported = await admin_handlers.do_export_memories("export-agent", str(path))

    records = [json.loads(line) for line in path.read_text().splitlines()]
    header = records[0]
    body_counts = {
        kind: sum(record.get("_type") == kind for record in records[1:])
        for kind in ("memory", "episode", "profile")
    }
    assert state["reader_injected"] is True
    assert exported == {
        "ok": True,
        "path": str(path),
        "memories": 3,
        "episodes": 1,
        "profiles": 1,
    }
    assert header["memory_count"] == body_counts["memory"] == 3
    assert header["episode_count"] == body_counts["episode"] == 1
    assert header["profile_count"] == body_counts["profile"] == 1

    imported = await admin_handlers.do_import_memories(
        str(path), target_agent_id="reader-restored-agent"
    )
    assert imported["ok"] is True
    assert imported["imported_memories"] == 3
    assert imported["imported_episodes"] == 1
