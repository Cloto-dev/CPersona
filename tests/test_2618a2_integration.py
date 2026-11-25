"""bug-821, the dashboard graph and map: they group, filter and order by the UTC
instant, as the memory list does, so the two views agree on a record's month."""

import pytest
import pytest_asyncio

from cpersona import database
from cpersona.dashboard import graph, overall, store
from cpersona.database import get_db


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in (
        "memories", "episodes", "entities", "entity_aliases", "entity_mentions",
        "record_nodes", "record_blocks", "pending_memory_tasks",
    ):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _add(conn, content, ts, agent="g"):
    cur = await conn.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, timestamp) VALUES (?, '', '', ?, ?)",
        (agent, content, ts),
    )
    await conn.commit()
    return cur.lastrowid


def _memory_ids(picture):
    return [int(n["id"][2:]) for n in picture["nodes"] if n["id"].startswith("m:")]


@pytest.mark.asyncio
async def test_bug_821_graph_folder_month_is_the_utc_month(db):
    # Both are January 2026 in UTC; their text starts with 2026-02 and 2025-12.
    tokyo = await _add(db, "tokyo note", "2026-02-01T08:00:00+09:00")
    ny = await _add(db, "new york note", "2025-12-31T20:00:00-05:00")
    conn = store.open_store(database.DB_PATH)
    try:
        jan = graph.folder(conn, ["g"], "2026-01")
        dec = graph.folder(conn, ["g"], "2025-12")
        feb = graph.folder(conn, ["g"], "2026-02")
        listed = store.memories(conn, path=["g"], month="2026-01")
    finally:
        conn.close()
    assert (jan["total"], dec["total"], feb["total"]) == (2, 0, 0), (jan["total"], dec["total"], feb["total"])
    assert set(_memory_ids(jan)) == {tokyo, ny} == {r["id"] for r in listed["rows"]}


@pytest.mark.asyncio
async def test_bug_821_graph_keeps_the_newest_instant_under_a_cap(db, monkeypatch):
    # By text "2026-07-23T08:00..." sorts after "2026-07-23T00:30...", but it is
    # 07-22T23:00Z, the older of the two.
    older = await _add(db, "one", "2026-07-23T08:00:00+09:00")
    newer = await _add(db, "two", "2026-07-23T00:30:00+00:00")
    monkeypatch.setattr(graph, "MAX_NODES", 1)
    conn = store.open_store(database.DB_PATH)
    try:
        in_folder = graph.folder(conn, ["g"])
        in_group = graph.members(conn, [older, newer], "group")
    finally:
        conn.close()
    assert _memory_ids(in_folder) == [newer], f"folder kept {_memory_ids(in_folder)}, the older instant"
    assert _memory_ids(in_group) == [newer], f"members kept {_memory_ids(in_group)}, the older instant"


@pytest.mark.asyncio
async def test_bug_821_map_months_match_the_memory_list(db):
    await _add(db, "tokyo note", "2026-02-01T08:00:00+09:00")
    await _add(db, "new york note", "2025-12-31T20:00:00-05:00")
    conn = store.open_store(database.DB_PATH)
    try:
        map_months = sorted(
            (n["key"], n["count"]) for n in overall.tree(conn)["nodes"] if n.get("level") == "month"
        )
        list_months = [(f"2026-{k['key']}", k["count"]) for k in store.tree(conn, ["g", "", "", "2026"])]
    finally:
        conn.close()
    assert map_months == list_months == [("2026-01", 2)], (map_months, list_months)
