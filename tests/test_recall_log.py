"""The recall log (2.6.17): each real recall's trace, kept beside the store.

Off, nothing is written. On, every recall and reconstruct writes one row, the
response the caller receives is exactly the one it receives with the log off
(the log runs the call traced and takes the trace out), a paused session writes
nothing, the log keeps the newest CPERSONA_RECALL_LOG_SIZE rows, and no stored
text reaches it.
"""

import json
import os
import sqlite3

import pytest
import pytest_asyncio

from cpersona import config, fileperms, recall_log, server, session
from cpersona.database import get_db

TEXT = "the zebracorn ledger moved to the north vault on tuesday"
# Shares every word with TEXT but the marker, so the bag-of-words test embedding
# (conftest) passes the gate and the record comes back, while the marker is never
# in the query and can only reach the log from the stored text.
QUERY = "the ledger moved to the north vault on tuesday"
AGENT = "agent.recall-log"


@pytest_asyncio.fixture
async def store(fake_embedding_client):
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    path = recall_log.log_path()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    for i, text in enumerate([TEXT, "the weekly sync is on monday", "lunch order: noodles", "the ledger audit is due friday"]):
        r = await server.do_store_boundary(AGENT, None, content=text)
        assert r.get("result") == "stored", r
    return db


@pytest.fixture
def log_on(monkeypatch):
    monkeypatch.setattr(config, "RECALL_LOG", True)
    monkeypatch.setattr(config, "RECALL_LOG_SIZE", 200)


def _rows() -> list[dict]:
    path = recall_log.log_path()
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM recall_log ORDER BY id")]
    finally:
        conn.close()


async def _forget_recalls(db) -> None:
    # A recall raises recall_count, which the confidence order reads; reset it so two
    # calls compared for equality start from the same store.
    await db.execute("UPDATE memories SET recall_count = 0, last_recalled_at = NULL")
    await db.commit()


async def _recall(**kw):
    return await server.do_recall_boundary(
        AGENT, QUERY, kw.pop("limit", 5), kw.pop("deep", False), "", [], None, "", **kw
    )


async def _reconstruct(**kw):
    return await server.do_reconstruct_boundary(
        AGENT, QUERY, kw.pop("count", 3), None, None, None, False, "", None, "", **kw
    )


@pytest.mark.asyncio
async def test_off_writes_nothing(store):
    await _recall()
    await _reconstruct()
    assert not os.path.exists(recall_log.log_path())


@pytest.mark.asyncio
async def test_each_call_writes_one_row_with_its_trace(store, log_on):
    r = await _recall()
    assert "trace" not in r and r["messages"]  # something was returned, so `returned` is not trivially empty
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "recall" and row["agent_id"] == AGENT and row["query"] == QUERY and row["deep"] == 0
    assert json.loads(row["returned"]) == [m["ref"] for m in r["messages"]]
    trace = recall_log.read_trace(row["trace"])
    assert "arms" in trace and trace["scope"]["agent_id"] == AGENT

    c = await _reconstruct()
    assert "trace" not in c and c["items"]
    rows = _rows()
    assert [x["tool"] for x in rows] == ["recall", "reconstruct"]
    assert json.loads(rows[1]["returned"]) == [i["head_ref"] for i in c["items"]]
    assert "recall" in recall_log.read_trace(rows[1]["trace"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "lite"])
async def test_the_response_is_the_same_with_the_log_on(store, monkeypatch, mode):
    db = store
    await _forget_recalls(db)
    off_recall = await _recall()
    await _forget_recalls(db)
    off_reconstruct = await _reconstruct(mode=mode)
    monkeypatch.setattr(config, "RECALL_LOG", True)
    await _forget_recalls(db)
    on_recall = await _recall()
    await _forget_recalls(db)
    on_reconstruct = await _reconstruct(mode=mode)
    assert len(_rows()) == 2 and off_recall["messages"] and off_reconstruct["items"]
    assert json.dumps(on_recall, sort_keys=True) == json.dumps(off_recall, sort_keys=True)
    assert json.dumps(on_reconstruct, sort_keys=True) == json.dumps(off_reconstruct, sort_keys=True)


@pytest.mark.asyncio
async def test_a_caller_that_asks_for_the_trace_still_gets_it(store, log_on):
    r = await _recall(trace=True)
    c = await _reconstruct(trace=True)
    assert "trace" in r and "trace" in c
    rows = _rows()
    assert recall_log.read_trace(rows[0]["trace"]) == json.loads(json.dumps(r["trace"], default=str))
    assert recall_log.read_trace(rows[1]["trace"]) == json.loads(json.dumps(c["trace"], default=str))


@pytest.mark.asyncio
async def test_a_paused_session_writes_nothing(store, log_on):
    session.pause_for("paused-session", True, 600)
    await _recall(session_key="paused-session")
    await _reconstruct(session_key="paused-session")
    assert _rows() == []
    await _recall(session_key="another-session")
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_the_log_keeps_the_newest_rows(store, log_on, monkeypatch):
    monkeypatch.setattr(config, "RECALL_LOG_SIZE", 3)
    for _ in range(5):
        await _recall()
    rows = _rows()
    assert len(rows) == 3
    assert [r["id"] for r in rows] == [3, 4, 5]


@pytest.mark.asyncio
async def test_no_stored_text_reaches_the_log(store, log_on):
    r = await _recall(limit=10)
    assert any("zebracorn" in m.get("content", "") for m in r["messages"])  # the record was returned
    await _reconstruct(count=5)
    for row in _rows():
        written = json.dumps(recall_log.read_trace(row["trace"])) + row["args"] + row["returned"] + row["query"]
        assert "zebracorn" not in written


@pytest.mark.asyncio
async def test_a_deep_recall_is_marked(store, log_on):
    await _recall(deep=True)
    assert _rows()[0]["deep"] == 1


@pytest.mark.asyncio
async def test_a_failed_write_does_not_fail_the_recall(store, log_on, monkeypatch):
    def broken(path):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(recall_log, "_connect", broken)
    r = await _recall()
    assert r["messages"]
    assert not os.path.exists(recall_log.log_path())


def test_the_log_is_a_private_file_the_health_check_knows():
    path = recall_log.log_path()
    assert path == f"{config.DB_PATH}.recall-log.db"
    assert {path, f"{path}-wal", f"{path}-shm"} <= {p.path for p in fileperms.owned_paths()}
