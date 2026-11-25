"""store's answers when a write races, and when the folder rider fails.

* bug-825: when another writer's identical text commits in the writer's own
  bucket between the dedup probe and the insert, the unique index absorbs the
  insert. That branch answered ``skipped`` without looking at the row it
  collided with, so a copy under a different source id -- which the sequential
  probe refuses -- was reported as harmlessly done. It is now decided the same
  way the probe decides: a different-source copy is ``rejected``, a same-source
  copy (or a msg_id collision) stays the documented id-less skip.
* bug-834: with declared folders on, the folder rider runs after the memory has
  committed. An exception from it replaced the whole store response, so the
  caller was told the store failed for a row that exists. A failure is now
  reported in the rider's own ``folder`` field, next to ``result`` and ``id``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

from cpersona import database, folders, memory_handlers, server, session
from cpersona.database import get_db

AGENT = "u06.agent"
TABLES = ("memories", "episodes", "folders", "folder_members", "pending_memory_tasks")


@pytest_asyncio.fixture
async def clean_db(monkeypatch):
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in TABLES:
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    yield db
    for table in TABLES:
        await db.execute(f"DELETE FROM {table}")
    await db.commit()


def _racing(monkeypatch, *, content: str, source_id: str, msg_id: str = "", project_id: str = ""):
    """Commit a copy of ``content`` just before do_store's write transaction."""
    real_transaction = memory_handlers.transaction
    armed = {"n": 0}

    @contextlib.asynccontextmanager
    async def racing_transaction():
        if armed["n"] == 0:
            armed["n"] = 1
            async with real_transaction() as racer:
                await racer.execute(
                    "INSERT INTO memories (agent_id, project_id, msg_id, content, source,"
                    " timestamp, metadata, channel) VALUES (?, ?, ?, ?, ?, 't', '{}', '')",
                    (AGENT, project_id, msg_id, content, json.dumps({"type": "User", "id": source_id})),
                )
        async with real_transaction() as inner:
            yield inner

    monkeypatch.setattr(memory_handlers, "transaction", racing_transaction)
    return armed


async def _sources(db, content: str) -> list:
    rows = await db.execute_fetchall(
        "SELECT json_extract(source, '$.id') FROM memories WHERE agent_id = ? AND content = ?",
        (AGENT, content),
    )
    return sorted(r[0] for r in rows)


def _user(source_id: str) -> dict:
    return {"type": "User", "id": source_id}


# --- bug-825 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_825_race_loser_under_another_source_is_rejected(clean_db, monkeypatch):
    text = "u06 race text"
    # The sequential answer the race must match.
    first = await memory_handlers.do_store(AGENT, {"content": text + " seq", "source": _user("alice")})
    seq = await memory_handlers.do_store(AGENT, {"content": text + " seq", "source": _user("bob")})
    assert first["result"] == "stored" and seq["result"] == "rejected", (first, seq)

    armed = _racing(monkeypatch, content=text, source_id="alice", project_id="X")
    res = await memory_handlers.do_store(AGENT, {"content": text, "source": _user("bob")}, project_id="X")
    assert armed["n"] == 1, "the race was never armed"
    assert await _sources(clean_db, text) == ["alice"], "bob's copy must not have been written"
    assert res.get("ok") is False and res.get("result") == "rejected", (
        f"a different-source race loser answered {res}, the sequential store answers {seq}"
    )
    assert res.get("reason") == seq.get("reason"), (res, seq)
    assert "id" not in res, res


@pytest.mark.asyncio
async def test_bug_825_same_source_race_stays_the_id_less_skip(clean_db, monkeypatch):
    text = "u06 same source race"
    armed = _racing(monkeypatch, content=text, source_id="alice")
    res = await memory_handlers.do_store(AGENT, {"content": text, "source": _user("alice")})
    assert armed["n"] == 1
    assert res.get("ok") is True and res.get("result") == "skipped", res
    assert res.get("reason") == "duplicate (unique index)", res
    assert "id" not in res, res
    assert await _sources(clean_db, text) == ["alice"]


@pytest.mark.asyncio
async def test_bug_825_msg_id_race_is_skipped_whatever_the_source(clean_db, monkeypatch):
    """The probe answers a msg_id collision before it looks at content or source,
    so a racing row with the same msg_id is a skip even under another source."""
    text = "u06 msg id race"
    first = await memory_handlers.do_store(AGENT, {"id": "m-seq", "content": text + " seq", "source": _user("alice")})
    seq = await memory_handlers.do_store(AGENT, {"id": "m-seq", "content": text + " seq", "source": _user("bob")})
    assert first["result"] == "stored" and seq["result"] == "skipped", (first, seq)

    armed = _racing(monkeypatch, content=text, source_id="alice", msg_id="m-race")
    res = await memory_handlers.do_store(AGENT, {"id": "m-race", "content": text, "source": _user("bob")})
    assert armed["n"] == 1
    assert res.get("ok") is True and res.get("result") == "skipped", res
    assert res.get("reason") == "duplicate (unique index)", res
    assert await _sources(clean_db, text) == ["alice"]


# --- bug-834 ---------------------------------------------------------------------


def _wrapped_store():
    stub = SimpleNamespace(_tools=list(server.registry._tools), _handlers=dict(server.registry._handlers))
    stub.auto_tool = lambda *a, **k: None
    original = stub._handlers["store"]
    stub._handlers["store"] = getattr(original, "__wrapped__", original)
    server._enable_declared_folders(stub)
    return stub._handlers["store"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["_file", "file_stored"])
async def test_bug_834_folder_rider_failure_is_reported_in_its_own_field(clean_db, monkeypatch, target):
    await folders.organize("fa", [{"op": "create", "path": ["design"]}], project_id="p1")
    store = _wrapped_store()

    async def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(folders, target, locked)
    args = {"agent_id": "fa", "project_id": "p1", "content": "u06 filed memory", "folder": ["design"], "lock": True}
    try:
        result = await store(dict(args))
    except Exception as exc:  # the transport turns this into a bare {"error": ...}
        pytest.fail(f"store raised {exc!r} for a row that committed")
    rows = await clean_db.execute_fetchall("SELECT id, locked FROM memories WHERE agent_id = 'fa'")
    assert len(rows) == 1 and rows[0][1] == 1, rows
    assert result.get("ok") is True and result.get("result") == "stored", result
    assert result.get("id") == rows[0][0], result
    assert result.get("locked") is True, result
    assert result.get("folder", {}).get("filed") is False, result
    assert "database is locked" in result["folder"].get("reason", ""), result
    assert await clean_db.execute_fetchall("SELECT COUNT(*) FROM folder_members") == [(0,)]


@pytest.mark.asyncio
async def test_bug_834_folder_rider_still_files_when_nothing_fails(clean_db):
    await folders.organize("fa", [{"op": "create", "path": ["design"]}], project_id="p1")
    store = _wrapped_store()
    result = await store({"agent_id": "fa", "project_id": "p1", "content": "u06 filed ok", "folder": ["design"]})
    assert result.get("result") == "stored", result
    assert result.get("folder") == {"filed": True, "folder": ["design"]}, result
    assert await clean_db.execute_fetchall("SELECT COUNT(*) FROM folder_members") == [(1,)]
