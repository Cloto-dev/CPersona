"""Regression tests: data that must not outlive its source.

  bug-813  import_memories sanitises content and episode summaries (annotation
           strip, trim, cap) but kept the file's embedding, which was computed
           from the original text. A row whose text changed must be stored
           without a vector so the null-embedding repair re-embeds it.
  bug-913  delete_agent_data, and a move-mode merge that empties its source,
           left the agent's rows in the recall log (query text, arguments,
           returned references).
"""

import base64
import json
import os
import sqlite3
import struct

import pytest
import pytest_asyncio

from cpersona import admin_handlers as admin
from cpersona import config, recall_log, session, utils
from cpersona.database import get_db

AGENT = "regress.u02"
OTHER = "regress.u02.other"


# --- bug-813 ---------------------------------------------------------------


def _b64(values):
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode()


VEC = _b64([0.5, 0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8])


def _jsonl(tmp_path, records):
    path = tmp_path / "u02.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


@pytest_asyncio.fixture
async def clean_import(monkeypatch):
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", 20)
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT,))
    await db.commit()
    yield db
    for table in ("memories", "episodes"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT,))
    await db.commit()


@pytest.mark.asyncio
async def test_bug_813_import_drops_the_vector_of_text_it_changed(clean_import, tmp_path):
    records = [
        # Unchanged text: the file's vector still describes it and is kept.
        {"_type": "memory", "agent_id": AGENT, "content": "short ok",
         "embedding_b64": VEC, "embedding_model": "m"},
        # Cut at the 20-character cap.
        {"_type": "memory", "agent_id": AGENT, "content": "AAAAAAAAAA tail that is cut off here",
         "embedding_b64": VEC, "embedding_model": "m"},
        # Annotation stripped.
        {"_type": "memory", "agent_id": AGENT, "content": "[Memory from x] keep this",
         "embedding_b64": VEC, "embedding_model": "m"},
        # Episodes: one unchanged, one cut.
        {"_type": "episode", "agent_id": AGENT, "summary": "ep kept", "keywords": "k",
         "embedding_b64": VEC, "embedding_model": "m"},
        {"_type": "episode", "agent_id": AGENT, "summary": "BBBBBBBBBB episode tail that is cut off",
         "keywords": "k", "embedding_b64": VEC, "embedding_model": "m"},
    ]
    result = await admin.do_import_memories(_jsonl(tmp_path, records), target_agent_id=AGENT)
    assert result.get("ok") is True, result
    assert result["imported_memories"] == 3 and result["imported_episodes"] == 2, result

    mems = await clean_import.execute_fetchall(
        "SELECT content, embedding IS NOT NULL, embedding_model FROM memories WHERE agent_id = ? ORDER BY id",
        (AGENT,),
    )
    eps = await clean_import.execute_fetchall(
        "SELECT summary, embedding IS NOT NULL, embedding_model FROM episodes WHERE agent_id = ? ORDER BY id",
        (AGENT,),
    )
    assert mems == [
        ("short ok", 1, "m"),
        ("AAAAAAAAAA tail that", 0, ""),
        ("keep this", 0, ""),
    ], mems
    assert eps == [
        ("ep kept", 1, "m"),
        ("BBBBBBBBBB episode t", 0, ""),
    ], eps
    # Each dropped vector is reported on its own line, and only those.
    dropped = sorted(e.split(":")[0] for e in result["errors"] if "embedding not restored" in e)
    assert dropped == ["Line 2", "Line 3", "Line 5"], result["errors"]


# --- bug-913 ---------------------------------------------------------------


@pytest.fixture
def log_db(tmp_path, monkeypatch):
    """A store path of this test's own, so the recall log sits beside it."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "u02.db"))
    monkeypatch.setattr(config, "RECALL_LOG", True)
    monkeypatch.setattr(config, "RECALL_LOG_SIZE", 200)
    return recall_log.log_path()


def _log(agent_id: str, query: str) -> None:
    assert recall_log.write(
        tool="recall", agent_id=agent_id, project_id="", channel="", deep=False,
        query=query, args={}, returned=["mem:1"], ms=1.0, trace={},
    )


def _log_rows(path: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT agent_id, query FROM recall_log ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def clean_agents():
    session.reset_pauses_for_tests()
    db = await get_db()
    for agent in (AGENT, OTHER):
        for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()
    yield db
    for agent in (AGENT, OTHER):
        for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()


@pytest.mark.asyncio
async def test_bug_913_delete_agent_data_purges_the_agents_recall_log_rows(clean_agents, log_db):
    _log(AGENT, "private question one")
    _log(OTHER, "someone else's question")
    _log(AGENT, "private question two")

    res = await admin.do_delete_agent_data(AGENT)
    assert res["ok"] is True, res
    assert _log_rows(log_db) == [(OTHER, "someone else's question")]


@pytest.mark.asyncio
async def test_bug_913_purge_applies_to_a_log_written_while_logging_was_on(
    clean_agents, log_db, monkeypatch
):
    # The log outlives the setting: turning logging off later does not delete it.
    _log(AGENT, "asked while logging was on")
    monkeypatch.setattr(config, "RECALL_LOG", False)
    res = await admin.do_delete_agent_data(AGENT)
    assert res["ok"] is True, res
    assert _log_rows(log_db) == []


@pytest.mark.asyncio
async def test_bug_913_delete_without_a_log_creates_none(clean_agents, log_db):
    res = await admin.do_delete_agent_data(AGENT)
    assert res["ok"] is True, res
    assert not os.path.exists(log_db)


@pytest.mark.asyncio
async def test_bug_913_emptying_move_purges_the_source_recall_log(clean_agents, log_db):
    db = clean_agents
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, 'moves', '{}', 't')",
        (AGENT,),
    )
    await db.commit()
    _log(AGENT, "source question")
    _log(OTHER, "target question")

    res = await admin.do_merge_memories(AGENT, OTHER, mode="move")
    assert res["ok"] is True and not any(res["left_at_source"].values()), res
    assert _log_rows(log_db) == [(OTHER, "target question")]


@pytest.mark.asyncio
async def test_bug_913_partial_move_keeps_the_live_source_recall_log(clean_agents, log_db):
    db = clean_agents
    # The locked source row collides with a target row, so the move leaves it.
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked)"
        " VALUES (?, 'shared content', '{}', 't', 1)",
        (AGENT,),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, 'shared content', '{}', 't')",
        (OTHER,),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, 'only in source', '{}', 't')",
        (AGENT,),
    )
    await db.commit()
    _log(AGENT, "source question")

    res = await admin.do_merge_memories(AGENT, OTHER, mode="move")
    assert res["ok"] is True and res["left_at_source"]["memories"] == 1, res
    assert _log_rows(log_db) == [(AGENT, "source question")]
