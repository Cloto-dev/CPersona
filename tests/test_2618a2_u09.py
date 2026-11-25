"""The dashboard store reads stored times as instants, folds case beyond ASCII,
answers the [project:x] property, and bounds what one search request reads.
"""

import tracemalloc

import pytest
import pytest_asyncio

from cpersona import admin_handlers, database
from cpersona.dashboard import store, web
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


async def _add(conn, content, ts, agent="a", project="", channel="", **extra):
    cols = {"agent_id": agent, "project_id": project, "channel": channel, "content": content, "timestamp": ts, **extra}
    cur = await conn.execute(
        f"INSERT INTO memories ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values())
    )
    await conn.commit()
    return cur.lastrowid


def _open():
    return store.open_store(database.DB_PATH)


# ---- bug-821: group, filter and order by the instant, not the text


@pytest.mark.asyncio
async def test_bug_821_folders_and_month_filters_follow_the_utc_time(db):
    # Both are January 2026 in UTC; their text starts with 2026-02 and 2025-12.
    tokyo = await _add(db, "tokyo note on the first", "2026-02-01T08:00:00+09:00", agent="z")
    ny = await _add(db, "new york note on the eve", "2025-12-31T20:00:00-05:00", agent="z")
    listed = await admin_handlers.do_list_directory("z", 100, ["z", "", ""])
    agent_years = [(c["name"], c["memory_count"]) for c in listed["children"]]
    listed = await admin_handlers.do_list_directory("z", 100, ["z", "", "", "2026"])
    agent_months = [(c["name"], c["memory_count"]) for c in listed["children"]]
    assert agent_years == [("2026", 2)] and agent_months == [("01", 2)]
    conn = _open()
    try:
        years = [(k["key"], k["count"]) for k in store.tree(conn, ["z", "", ""])]
        months = [(k["key"], k["count"]) for k in store.tree(conn, ["z", "", "", "2026"])]
        in_folder = store.memories(conn, path=["z", "", "", "2026", "01"])
        jan = store.memories(conn, path=["z"], month="2026-01")
        dec = store.memories(conn, path=["z"], month="2025-12")
        over = store.overview(conn, "a.db")
        by_op = {r["id"] for r in store.search(conn, "note month:2026-01")["rows"]}
    finally:
        conn.close()
    assert years == agent_years, f"dashboard years {years} != list_memories {agent_years}"
    assert months == agent_months, f"dashboard months {months} != list_memories {agent_months}"
    assert {r["id"] for r in in_folder["rows"]} == {tokyo, ny}
    assert jan["total"] == 2 and dec["total"] == 0, (jan["total"], dec["total"])
    assert [(m["month"], m["count"]) for m in over["months"]] == [("2026-01", 2)], over["months"]
    assert by_op == {tokyo, ny}, f"month:2026-01 found {by_op}"


@pytest.mark.asyncio
async def test_bug_821_newest_first_is_by_the_instant(db):
    a = await _add(db, "one a", "2026-07-23T08:00:00+09:00", agent="d")  # 07-22T23:00Z, the oldest
    b = await _add(db, "one b", "2026-07-23T00:30:00+00:00", agent="d")
    c = await _add(db, "one c", "2026-07-23 01:00:00", agent="d")  # naive is UTC: the newest
    await db.executemany(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, start_time, end_time, created_at) "
        "VALUES ('d', '', '', ?, '', NULL, ?, '2026-07-23 00:00:00')",
        [("ep old", "2026-07-23T08:00:00+09:00"), ("ep new", "2026-07-23T01:00:00+00:00")],
    )
    await db.commit()
    conn = _open()
    try:
        listed = [r["id"] for r in store.memories(conn, path=["d"])["rows"]]
        over = store.overview(conn, "a.db")
        new = [r["id"] for r in store.search(conn, "one", sort="new")["rows"]]
        old = [r["id"] for r in store.search(conn, "one", sort="old")["rows"]]
        eps = [r["snippet"] for r in store.episodes(conn)["rows"]]
    finally:
        conn.close()
    assert listed == [c, b, a], f"memories newest first {listed}"
    assert [r["id"] for r in over["recent"]] == [c, b, a], over["recent"]
    assert new == [c, b, a] and old == [a, b, c], (new, old)
    assert over["span"] == ["2026-07-23T08:00:00+09:00", "2026-07-23 01:00:00"], over["span"]
    assert over["agents"][0]["last"] == "2026-07-23 01:00:00", over["agents"]
    assert eps == ["ep new", "ep old"], eps


# ---- bug-822: case-insensitive search folds non-ASCII letters


@pytest.mark.asyncio
async def test_bug_822_case_insensitive_filters_fold_beyond_ascii(db):
    upper = await _add(db, "Émile wrote this in München", "2026-10-01T00:00:00+00:00", agent="Ünal")
    lower = await _add(db, "émile wrote that in münchen", "2026-10-02T00:00:00+00:00")
    cyr = await _add(db, "Москва note", "2026-10-03T00:00:00+00:00")
    cur = await db.execute(
        "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES ('a', 'Émile', 'émile', 'a', 'x')"
    )
    eid = cur.lastrowid
    await db.execute(
        "INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'a', 'x')", (eid, f"mem:{upper}")
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, created_at) "
        "VALUES ('a', '', '', 'Ég talk', '', '2026-10-01 00:00:00')"
    )
    await db.commit()
    conn = _open()
    try:
        panel = {r["id"] for r in store.search(conn, "émile")["rows"]}
        exact = {r["id"] for r in store.search(conn, "émile", case=True)["rows"]}
        kept_out = {r["id"] for r in store.search(conn, "wrote -MÜNCHEN")["rows"]}
        moscow = {r["id"] for r in store.search(conn, "москва")["rows"]}
        tagged = {r["id"] for r in store.search(conn, "tag:émile")["rows"]}
        by_agent = {r["id"] for r in store.search(conn, "agent:ünal")["rows"]}
        short = {r["id"] for r in store.memories(conn, "éM")["rows"]}
        ents = store.entities(conn, "émile")["total"]
        eps = store.episodes(conn, "éG")["total"]
    finally:
        conn.close()
    assert panel == {upper, lower}, f"'émile' found {panel}, missing the 'Émile' row {upper}"
    assert exact == {lower}  # Aa on still matches case
    assert kept_out == set(), kept_out
    assert moscow == {cyr}, moscow
    assert tagged == {upper}, tagged
    assert by_agent == {upper}, by_agent
    assert short == {upper, lower}, f"the short-query arm found {short}"
    assert ents == 1 and eps == 1, (ents, eps)


# ---- bug-823: the [project:x] property reads the project column


@pytest.mark.asyncio
async def test_bug_823_project_property_keeps_that_project(db):
    p1 = await _add(db, "note one", "2026-10-01T00:00:00+00:00", project="p1", channel="c1")
    p2 = await _add(db, "note two", "2026-10-02T00:00:00+00:00", project="p2", channel="c1")
    code, out = web.answer(database.DB_PATH, "/api/search", {"q": "[project:p1]"}, "a.db")
    assert code == 200, f"[project:p1] answered {code} {out}"
    assert [r["id"] for r in out["rows"]] == [p1]
    code, out = web.answer(database.DB_PATH, "/api/search", {"q": "note [project:p2] [channel:c1]"}, "a.db")
    assert code == 200 and [r["id"] for r in out["rows"]] == [p2], (code, out)


# ---- bug-824: a search reads one page of text, not every match


@pytest.mark.asyncio
async def test_bug_824_search_pages_in_sql_and_reads_one_page(db):
    body = "x" * 20000
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, timestamp, locked) VALUES ('a','','',?,?,1)",
        [(f"n{i:03d} {body}\nalpha beta n{i:03d}", f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00") for i in range(300)],
    )
    await db.commit()
    conn = _open()
    try:
        seen = []
        conn.set_trace_callback(seen.append)
        tracemalloc.start()
        out = store.search(conn, "[locked]", limit=1, offset=2)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        conn.set_trace_callback(None)
        tracemalloc.start()
        lined = store.search(conn, "line:(alpha beta)", limit=2, offset=3)
        _, peak_lined = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        none_lined = store.search(conn, "line:(alpha xxx)")
    finally:
        conn.close()
    # Newest first: offset 2 is the third newest.
    assert out["total"] == 300 and [r["id"] for r in out["rows"]] == [out["rows"][0]["id"]]
    assert out["rows"][0]["lines"][0].startswith("n297"), out["rows"][0]["lines"]
    content_reads = [s for s in seen if "m.content" in s]
    assert content_reads and all("LIMIT" in s for s in content_reads), f"search read every match's text: {content_reads}"
    assert peak < 1_000_000, f"a one-row page peaked at {peak} bytes"
    assert lined["total"] == 300 and len(lined["rows"]) == 2, lined["total"]
    assert [r["lines"][0] for r in lined["rows"]] == ["alpha beta n296", "alpha beta n295"], [r["lines"] for r in lined["rows"]]
    assert peak_lined < 1_000_000, f"a line: search peaked at {peak_lined} bytes"
    assert none_lined["total"] == 0 and none_lined["rows"] == []
