"""The vector index files read through a change log of the rows they hold (bug-514).

The contiguous index of each table and the coarse index of records copy every
row's vector, axes and times at the build. The watermark sends rows written
since to the live table, but a row the build held can change in place: its
vector replaced (`update_memory`, the NULL-embedding repair), cleared, its axes
rewritten, its time rewritten, or the row deleted. Each such change is logged by
a trigger, and a read leaves the logged rows out of the file's selection and
reads them live.

Every claim below is the index answer measured against the same query with the
index files removed, next to a counter showing which path produced it: every
failure of this design ends in a silent fallback, so "the answers match" is only
worth anything next to "and the index answered".

Every path that writes, copies or deletes a record, and what each does to the log:
store / import / a merge's copy insert above every watermark and log nothing;
update_memory, the NULL-embedding repair, migrate_channel_axis, the timestamp
repairs and a merge's move (its delete) are logged by the triggers; export reads
and carries no log; delete_agent_data's deletes are logged, and under a grant on
every agent it drops the files as well; a build turns the log on and prunes it.
"""

from __future__ import annotations

import json
import os
import random

import numpy as np
import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, coarse_index, coarse_search, database, vector, vector_index
from cpersona import maintenance_handlers
from cpersona.database import get_db
from tests.conftest import fake_embed_one

AGENT = "b514.agent"
OTHER = "b514.other"
QUERY = "orchard telescope lantern harbour"
OLD = "apples and pears orchard harvest"
NEW = "telescope astronomy stars night sky"


def _paths() -> list[str]:
    out = []
    for table in vector_index.INDEXED_TABLES:
        out += [vector_index.index_path(table)]
    for table in coarse_index.COARSE_TABLES:
        out += [coarse_index.index_path(table)]
    return out


def _drop_files() -> None:
    for path in _paths():
        if os.path.exists(path):
            os.unlink(path)


def _blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


async def _memory(db, content, *, second, agent_id=AGENT, project_id="", channel="", embedding=None) -> int:
    vec = fake_embed_one(content) if embedding is None else embedding
    cur = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            agent_id, project_id, channel, content, '{"type": "User", "id": "user-1"}',
            f"2026-03-01T00:{second // 60:02d}:{second % 60:02d}+00:00",
            f"2026-03-01 00:{second // 60:02d}:{second % 60:02d}", _blob(vec),
        ),
    )
    return cur.lastrowid


async def _episode(db, summary, *, second, embedding=None) -> int:
    vec = fake_embed_one(summary) if embedding is None else embedding
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, embedding,"
        " start_time, resolved, created_at) VALUES (?, '', '', ?, '', ?, NULL, 0, ?)",
        (AGENT, summary, _blob(vec), f"2026-03-01 00:00:{second:02d}"),
    )
    return cur.lastrowid


async def _log_rows(db) -> list[tuple]:
    return [tuple(r) for r in await db.execute_fetchall(
        "SELECT kind, record_id FROM record_vector_changes ORDER BY seq"
    )]


@pytest_asyncio.fixture
async def clean(fake_embedding_client):
    """An empty store, no index file, and the change log off, as a deployment starts."""
    db = await get_db()
    _drop_files()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await vector_index.stop_logging(db)
    await db.commit()
    yield db
    await vector_index.stop_logging(db)
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    _drop_files()


#: Off while the oracle runs: with the files set aside every phase 1 falls back,
#: and those are the scan's own answers, not the index's.
_COUNTING = [True]


@pytest.fixture
def taken(monkeypatch):
    """Phase-1 answers per table: the index's, and the scan's (`<table>_fallback`)."""
    counter = {"memories": 0, "episodes": 0, "memories_fallback": 0, "episodes_fallback": 0}
    real = vector._index_phase1

    async def counting(*args, **kwargs):
        result = await real(*args, **kwargs)
        table = kwargs.get("table", "memories")
        if _COUNTING[0]:
            counter[table if result is not None else f"{table}_fallback"] += 1
        return result

    monkeypatch.setattr(vector, "_index_phase1", counting)
    return counter


async def _build(db, *tables: str) -> None:
    """What the operator command does: the log on, the rows read, the log pruned."""
    await vector_index.start_logging()
    for table in tables or ("memories", "episodes"):
        assert (await vector_index.build_index(db, table))["built"]
        if table in coarse_index.COARSE_TABLES:
            assert (await coarse_index.build_coarse_index(db, table))["built"]
    await vector_index.prune_log()


async def _search(db, query=QUERY, **kw):
    return [
        (row["_rid"], round(float(row["_cosine"]), 6))
        for row in await vector._search_vector(db, kw.pop("agent_id", AGENT), query, 10, min_similarity=-1.0, **kw)
    ]


async def _search_without_index(db, query=QUERY, **kw):
    """The same query with every index file set aside: the scan's own answer."""
    saved = {}
    for path in _paths():
        if os.path.exists(path):
            with open(path, "rb") as fh:
                saved[path] = fh.read()
            os.unlink(path)
    _COUNTING[0] = False
    try:
        return await _search(db, query, **kw)
    finally:
        _COUNTING[0] = True
        for path, blob in saved.items():
            with open(path, "wb") as fh:
                fh.write(blob)


async def _corpus(db, n: int = 12) -> list[int]:
    ids = [await _memory(db, f"note {k} about {QUERY if k % 3 == 0 else 'filler'} {k}", second=k + 1)
           for k in range(n)]
    await db.commit()
    return ids


# --------------------------------------------------------------------------------------
# The record paths that change a row the files hold.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_edited_memory_is_scored_by_its_new_vector(clean, taken):
    """bug-514 as reported: update_memory replaces the vector in place."""
    db = clean
    target = await _memory(db, OLD, second=1)
    await _corpus(db)
    await _build(db, "memories")

    assert (await admin_handlers.do_update_memory(target, NEW, agent_id=AGENT))["ok"]

    for query in (NEW, OLD):
        scan = await _search_without_index(db, query)
        got = await _search(db, query)
        assert got == scan, query
        assert dict(scan)[("mem", target)] == pytest.approx(
            float(np.dot(fake_embed_one(query), fake_embed_one(NEW))), abs=1e-5
        ), "the oracle must score the edited row by its new text"
    assert taken["memories"] == 2 and taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_a_refilled_embedding_is_scored_by_its_new_vector(clean, taken):
    """The NULL-embedding repair writes a vector in place inside the same check run."""
    db = clean
    target = await _memory(db, OLD, second=1)
    await _corpus(db)
    await _build(db, "memories")

    # What the content sanitiser does: new text, the vector cleared for the repair.
    await db.execute("UPDATE memories SET content = ?, embedding = NULL WHERE id = ?", (NEW, target))
    await db.commit()
    result = await maintenance_handlers.do_check_health(AGENT, fix=True, checks=["null_embedding"])
    assert [i.get("re_embedded") for i in result["issues"] if i["type"] == "null_embedding"] == [1], result
    assert (await db.execute_fetchall("SELECT embedding IS NOT NULL FROM memories WHERE id = ?", (target,)))[0][0]

    scan = await _search_without_index(db, NEW)
    assert await _search(db, NEW) == scan
    assert dict(scan)[("mem", target)] == pytest.approx(1.0, abs=1e-5)
    assert taken["memories"] == 1 and taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_a_retagged_memory_moves_with_its_tag(clean, taken):
    """Re-tagging after a build: design §7 used to accept that the strict direction misses the row."""
    db = clean
    ids = await _corpus(db)
    await _build(db, "memories")

    await db.execute("UPDATE memories SET channel = 'c2' WHERE id = ?", (ids[0],))
    await db.execute("UPDATE memories SET project_id = 'p2' WHERE id = ?", (ids[3],))
    await db.execute("UPDATE memories SET agent_id = ? WHERE id = ?", (OTHER, ids[6]))
    await db.commit()

    for kw in ({}, {"channel": "c2"}, {"project_id": "p2"}, {"project_id": ""}, {"agent_id": OTHER}):
        assert await _search(db, **kw) == await _search_without_index(db, **kw), kw
    assert taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_migrate_channel_axis_is_followed(clean, taken):
    db = clean
    ids = await _corpus(db)
    await db.execute(
        "UPDATE memories SET channel = 'discord', metadata = ? WHERE id = ?",
        (json.dumps({"session_id": "123456789012345678:42:0"}), ids[0]),
    )
    await db.commit()
    await _build(db, "memories")

    result = await maintenance_handlers.do_migrate_channel_axis(AGENT, dry_run=False)
    assert result.get("recoverable_total") == 1, result
    moved = (await db.execute_fetchall("SELECT channel FROM memories WHERE id = ?", (ids[0],)))[0][0]
    assert moved == "123456789012345678", result
    assert ("mem", ids[0]) in await _log_rows(db)

    for kw in ({"channel": "discord"}, {"channel": moved}):
        assert await _search(db, **kw) == await _search_without_index(db, **kw), kw
    assert taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_a_retimed_memory_takes_its_new_place(clean, taken, monkeypatch):
    """created_at orders the window; a rewritten one moves the row across the window edge."""
    db = clean
    monkeypatch.setattr(vector, "MAX_MEMORIES", 4)
    ids = await _corpus(db)
    await _build(db, "memories")
    await db.execute("UPDATE memories SET created_at = '2026-03-02 00:00:00' WHERE id = ?", (ids[0],))
    await db.commit()

    assert await _search(db) == await _search_without_index(db)
    assert ("mem", ids[0]) in [rid for rid, _ in await _search(db)]
    assert taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_a_deleted_memory_keeps_the_index_answering(clean, taken, monkeypatch):
    """bug-588 handed such a query to the scan until a rebuild; the log keeps it on the index."""
    db = clean
    monkeypatch.setattr(vector, "MAX_MEMORIES", 4)
    ids = await _corpus(db)
    await _build(db, "memories")
    assert (await admin_handlers.do_delete_memory(ids[-1], agent_id=AGENT))["ok"]

    assert await _search(db) == await _search_without_index(db)
    assert taken["memories"] == 1 and taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_a_merge_move_is_followed(clean, taken):
    """A move inserts copies above the watermark and deletes the sources, which the log names."""
    db = clean
    ids = [await _memory(db, f"moving {k} {QUERY}", second=k + 1, agent_id=OTHER) for k in range(4)]
    await _corpus(db)
    await _build(db, "memories")

    result = await admin_handlers.do_merge_memories(OTHER, AGENT, mode="move")
    assert result.get("ok"), result
    assert {("mem", i) for i in ids} <= set(await _log_rows(db))

    for agent in (AGENT, OTHER):
        assert await _search(db, agent_id=agent) == await _search_without_index(db, agent_id=agent)
    assert taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_an_edited_episode_is_scored_by_its_new_vector(clean, taken):
    db = clean
    target = await _episode(db, OLD, second=1)
    for k in range(2, 8):
        await _episode(db, f"episode filler {k}", second=k)
    await db.commit()
    await _build(db, "episodes")
    await db.execute("UPDATE episodes SET summary = ?, embedding = ? WHERE id = ?", (NEW, _blob(fake_embed_one(NEW)), target))
    await db.commit()

    scan = await _search_without_index(db, NEW)
    assert await _search(db, NEW) == scan
    assert dict(scan)[("ep", target)] == pytest.approx(1.0, abs=1e-5)
    assert taken["episodes"] == 1 and taken["episodes_fallback"] == 0


@pytest.mark.asyncio
async def test_the_coarse_index_reads_an_edited_record_live(clean):
    db = clean
    ids = await _corpus(db, 30)
    await _build(db, "memories")
    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(fake_embed_one(QUERY)), ids[5]))
    await db.commit()

    query = np.asarray(fake_embed_one(QUERY), dtype=np.float32)
    got = await coarse_search.coarse_candidates(db, query, agent_id=AGENT, k=5)
    _drop_files()
    live = await coarse_search.coarse_candidates(db, query, agent_id=AGENT, k=5)
    assert got.source == "index" and live.source == "live"
    assert got.rows() == live.rows()
    assert ids[5] == got.ids[0], "the record now carries the query's own vector"


@pytest.mark.asyncio
async def test_the_coarse_index_places_a_rewritten_timestamp_in_its_new_period(clean):
    """The file copies datetime(timestamp) for a period; a rewrite moves the record between periods."""
    db = clean
    ids = await _corpus(db, 30)
    await _build(db, "memories")
    await db.execute("UPDATE memories SET timestamp = '2026-04-01T00:00:00+00:00' WHERE id = ?", (ids[2],))
    await db.commit()

    query = np.asarray(fake_embed_one(QUERY), dtype=np.float32)
    answers = {}
    for name, period in (("march", ("2026-03-01 00:00:00", "2026-03-02 00:00:00")),
                         ("april", ("2026-04-01 00:00:00", "2026-04-02 00:00:00"))):
        got = await coarse_search.coarse_candidates(db, query, agent_id=AGENT, k=50, period=period)
        assert got.source == "index", name
        answers[name] = got
    _drop_files()
    for name, period in (("march", ("2026-03-01 00:00:00", "2026-03-02 00:00:00")),
                         ("april", ("2026-04-01 00:00:00", "2026-04-02 00:00:00"))):
        live = await coarse_search.coarse_candidates(db, query, agent_id=AGENT, k=50, period=period)
        assert answers[name].rows() == live.rows(), name
    assert answers["april"].ids == (ids[2],)
    assert ids[2] not in answers["march"].ids


# --------------------------------------------------------------------------------------
# What the triggers log.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_a_change_to_what_the_files_copy_is_logged(clean):
    db = clean
    a = await _memory(db, "first", second=1)
    b = await _memory(db, "second", second=2)
    ep = await _episode(db, "an episode", second=3)
    await db.commit()
    assert await vector_index.start_logging() is not None

    await _memory(db, "an insert", second=4)  # above every watermark: never logged
    await db.execute("UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = datetime('now')")
    await db.execute("UPDATE memories SET content = 'reworded', metadata = '{}' WHERE id = ?", (a,))
    await db.execute("UPDATE memories SET channel = channel, embedding = embedding WHERE id = ?", (a,))
    await db.commit()
    assert await _log_rows(db) == [], "neither the counters, the text nor a no-op set is copied by a file"

    head = (await vector_index.log_clock(db))[2]
    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(fake_embed_one("x")), a))
    await db.execute("UPDATE memories SET source = '{\"id\": \"other\"}' WHERE id = ?", (b,))
    await db.execute("UPDATE memories SET timestamp = '2026-05-01T00:00:00+00:00' WHERE id = ?", (b,))
    await db.execute("UPDATE episodes SET channel = 'c' WHERE id = ?", (ep,))
    await db.execute("DELETE FROM memories WHERE id = ?", (a,))
    await db.commit()
    assert await _log_rows(db) == [("mem", a), ("mem", b), ("mem", b), ("ep", ep), ("mem", a)]
    assert (await vector_index.log_clock(db))[2] == head + 5, "the clock counts every logged change"


def test_an_id_change_logs_the_id_a_row_leaves_and_the_one_it_takes(tmp_path):
    """The server never moves an id, but another writer could; both ids are then stale.

    On a database of its own: moving a row's id under the shared test database
    would leave its full-text and node rows pointing at an id that no longer exists.
    """
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "ids.db"))
    try:
        conn.executescript(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, agent_id, project_id, channel, source,"
            " timestamp, created_at, embedding);"
            "CREATE TABLE episodes (id INTEGER PRIMARY KEY, agent_id, project_id, channel, created_at, embedding);"
            + database.VECTOR_LOG_SQL
        )
        conn.execute("INSERT INTO memories (id) VALUES (7)")
        conn.execute("INSERT INTO episodes (id) VALUES (3)")
        conn.execute("UPDATE vector_log_clock SET logging = 1 WHERE id = 0")
        conn.execute("UPDATE memories SET id = 70 WHERE id = 7")
        conn.execute("UPDATE episodes SET id = 30 WHERE id = 3")
        rows = conn.execute("SELECT seq, kind, record_id FROM record_vector_changes ORDER BY seq").fetchall()
        assert rows == [(1, "mem", 7), (2, "mem", 70), (3, "ep", 3), (4, "ep", 30)]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_nothing_is_logged_while_the_log_is_off(clean):
    db = clean
    a = await _memory(db, "first", second=1)
    await db.commit()
    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(fake_embed_one("x")), a))
    await db.execute("DELETE FROM memories WHERE id = ?", (a,))
    await db.commit()
    assert await _log_rows(db) == []
    assert (await vector_index.log_clock(db))[0] == 0


@pytest.mark.asyncio
async def test_the_schema_check_knows_the_six_triggers(clean):
    db = clean
    assert not [i for i in await checks.check_schema_objects(db, "", False)
                if i["object"] in vector_index.LOG_TRIGGERS], "the boot SQL and the check must agree"
    await db.execute("DROP TRIGGER episodes_vector_log_ad")
    await db.commit()
    try:
        issues = await checks.check_schema_objects(db, "", True)
        assert [(i["object"], i["state"], i.get("fixed")) for i in issues] == [
            ("episodes_vector_log_ad", "missing", True)
        ]
        await db.commit()
    finally:
        await db.executescript(database.VECTOR_LOG_SQL)
        await db.commit()
    assert await vector_index.log_triggers_differ(db) == []


# --------------------------------------------------------------------------------------
# Builds.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_build_declines_while_the_log_is_off(clean):
    db = clean
    await _corpus(db)
    for build in (vector_index.build_index, coarse_index.build_coarse_index):
        result = await build(db, "memories")
        assert result == {"built": False, "reason": vector_index.LOG_OFF_REASON}
    assert not any(os.path.exists(p) for p in _paths())


@pytest.mark.asyncio
async def test_a_build_declines_while_a_logging_trigger_is_missing(clean):
    db = clean
    await _corpus(db)
    assert await vector_index.start_logging() is not None
    await db.execute("DROP TRIGGER memories_vector_log_au")
    await db.commit()
    try:
        result = await vector_index.build_index(db, "memories")
        assert not result["built"] and "memories_vector_log_au" in result["reason"]
    finally:
        await db.executescript(database.VECTOR_LOG_SQL)
        await db.commit()


@pytest.mark.asyncio
async def test_the_header_records_where_the_log_stood(clean):
    db = clean
    a = await _memory(db, "first", second=1)
    await db.commit()
    generation = await vector_index.start_logging()
    head = (await vector_index.log_clock(db))[2]
    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(fake_embed_one("x")), a))
    await db.commit()
    built = await vector_index.build_index(db, "memories")
    index = vector_index.load_index("memories")
    cookie = (await db.execute_fetchall("PRAGMA schema_version"))[0][0]
    assert (built["generation"], built["built_seq"]) == (generation, head + 1)
    assert (index.generation, index.built_seq, index.schema_cookie) == (generation, head + 1, cookie)


@pytest.mark.asyncio
async def test_a_write_while_a_build_reads_is_read_live(clean, taken, monkeypatch):
    """The build reads the log's head before its first row, so a write that lands
    while it reads carries a higher number and is taken from the live table."""
    import sqlite3

    from cpersona import config

    db = clean
    ids = await _corpus(db)
    # From one channel to another: the file then excludes the row under the new one,
    # the direction the hydrate cannot repair.
    await db.execute("UPDATE memories SET channel = 'before' WHERE id = ?", (ids[3],))
    await db.commit()
    real = vector_index.plan_rows

    async def racing(conn, table="memories", **kw):
        plan = await real(conn, table, **kw)
        w = sqlite3.connect(config.DB_PATH)
        try:
            w.execute("UPDATE memories SET channel = 'raced' WHERE id = ?", (ids[3],))
            w.commit()
        finally:
            w.close()
        return plan

    monkeypatch.setattr(vector_index, "plan_rows", racing)
    await vector_index.start_logging()
    assert (await vector_index.build_index(db, "memories"))["built"]
    monkeypatch.setattr(vector_index, "plan_rows", real)

    scan = await _search_without_index(db, channel="raced")
    assert ("mem", ids[3]) in [rid for rid, _ in scan]
    assert await _search(db, channel="raced") == scan
    assert taken["memories"] == 1 and taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_a_prune_keeps_what_the_oldest_file_still_needs(clean):
    db = clean
    ids = await _corpus(db)
    await vector_index.start_logging()
    assert (await vector_index.build_index(db, "memories"))["built"]
    first = vector_index.load_index("memories").built_seq
    for k in range(3):
        await db.execute("UPDATE memories SET channel = ? WHERE id = ?", (f"c{k}", ids[k]))
    await db.commit()
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    assert await vector_index.prune_log() == first, "the contiguous file still needs all three"
    assert len(await _log_rows(db)) == 3

    assert (await vector_index.build_index(db, "memories"))["built"]
    assert await vector_index.prune_log() == first + 3
    assert await _log_rows(db) == []
    # The mark only rises: a prune computed from an older head never lowers it.
    await db.execute("UPDATE vector_log_clock SET pruned_through = ? WHERE id = 0", (first + 9,))
    await db.commit()
    await vector_index.prune_log()
    assert (await vector_index.log_clock(db))[3] == first + 9


@pytest.mark.asyncio
async def test_a_file_of_another_generation_does_not_hold_the_log_back(clean):
    db = clean
    ids = await _corpus(db)
    await _build(db, "memories")
    stale = vector_index.load_index("memories").generation
    async with database.transaction() as tdb:
        await vector_index.stop_logging(tdb)
    await vector_index.start_logging()
    await db.execute("UPDATE memories SET channel = 'x' WHERE id = ?", (ids[0],))
    await db.commit()
    os.unlink(coarse_index.index_path("memories"))
    assert vector_index.load_index("memories").generation == stale
    assert await vector_index.prune_log() is None, "no file of the running generation"


@pytest.mark.asyncio
async def test_startup_stops_a_log_no_file_reads(clean):
    db = clean
    await _corpus(db)
    await _build(db, "memories")
    await vector_index.on_startup()
    assert (await vector_index.log_clock(db))[0] == 1, "a file is present: the log stays"
    _drop_files()
    await vector_index.on_startup()
    assert (await vector_index.log_clock(db))[0] == 0


# --------------------------------------------------------------------------------------
# A log that cannot vouch for a file sends every read to the live table.
# --------------------------------------------------------------------------------------


async def _logging_off(db, index):
    await vector_index.stop_logging(db)


async def _restarted(db, index):
    await vector_index.stop_logging(db)
    await vector_index.start_logging_on(db)


async def _pruned_past(db, index):
    await db.execute("UPDATE vector_log_clock SET pruned_through = ? WHERE id = 0", (index.built_seq + 1,))


async def _head_behind(db, index):
    await db.execute("UPDATE vector_log_clock SET head = ? WHERE id = 0", (index.built_seq - 1,))


async def _schema_changed(db, index):
    await db.execute("CREATE TABLE b514_probe (x)")
    await db.execute("DROP TABLE b514_probe")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "break_it, reason",
    [
        (_logging_off, "logging_off"),
        (_restarted, "generation"),
        (_pruned_past, "pruned"),
        (_head_behind, "newer_than_snapshot"),
        (_schema_changed, "schema_changed"),
    ],
)
async def test_a_log_that_cannot_vouch_sends_the_query_to_the_scan(clean, taken, break_it, reason):
    db = clean
    ids = await _corpus(db)
    await _build(db, "memories")
    await db.execute("UPDATE memories SET channel = 'c' WHERE id = ?", (ids[1],))
    await db.commit()
    index = vector_index.load_index("memories")
    await break_it(db, index)
    await db.commit()

    assert (await vector_index.changed_since_build(db, index, "memories"))[1] == reason
    assert await _search(db) == await _search_without_index(db)
    assert taken["memories"] == 0 and taken["memories_fallback"] == 1

    query = np.asarray(fake_embed_one(QUERY), dtype=np.float32)
    assert (await coarse_search.coarse_candidates(db, query, agent_id=AGENT, k=3)).source == "live"
    issues = await checks.check_vector_index(db, AGENT, False)
    assert [(i["type"], i["reason"]) for i in issues if i.get("table") == "memories"] == [
        ("vector_index_log_refused", reason)
    ]


@pytest.mark.asyncio
async def test_more_changes_than_the_bound_send_the_query_to_the_scan(clean, taken, monkeypatch):
    db = clean
    ids = await _corpus(db)
    await _build(db, "memories")
    monkeypatch.setattr(vector_index, "MAX_CHANGED_IDS", 2)
    for k in range(2):
        await db.execute("UPDATE memories SET channel = 'c' WHERE id = ?", (ids[k],))
    await db.commit()
    assert await _search(db) == await _search_without_index(db)
    assert taken["memories"] == 1, "two changes are within the bound"
    await db.execute("UPDATE memories SET channel = 'c' WHERE id = ?", (ids[2],))
    await db.commit()
    assert await _search(db) == await _search_without_index(db)
    assert taken["memories_fallback"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["replaced", "cleared", "deleted"])
async def test_an_edit_inside_the_window_keeps_the_probes_in_range_form(clean, taken, monkeypatch, edit):
    """A logged hole in a run of ids must not turn every later query into the chunked probes,
    and the range forms must skip it: a cleared or deleted row there is the log's to answer for,
    not a reason to hand the query to the scan."""
    db = clean
    ids = await _corpus(db)
    await _build(db, "memories")
    if edit == "replaced":
        await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(fake_embed_one("moved")), ids[4]))
    elif edit == "cleared":
        await db.execute("UPDATE memories SET embedding = NULL WHERE id = ?", (ids[4],))
    else:
        await db.execute("DELETE FROM memories WHERE id = ?", (ids[4],))
    await db.commit()

    def refuse(*_a, **_k):
        raise AssertionError("the scattered form was used for a run with a named hole")

    monkeypatch.setattr(vector, "_lost_embedding_chunk_sql", refuse)
    monkeypatch.setattr(vector, "_gone_scattered_sql", refuse)
    assert await _search(db) == await _search_without_index(db)
    assert taken["memories"] == 1 and taken["memories_fallback"] == 0


@pytest.mark.asyncio
async def test_the_range_probes_with_holes_still_catch_an_unlogged_change(clean, taken):
    """The probes are the backstop for a change the log does not name; skipping the
    named holes must not skip the rest of the run."""
    db = clean
    ids = await _corpus(db)
    await _build(db, "memories")
    await db.execute("UPDATE memories SET channel = 'c' WHERE id = ?", (ids[4],))  # logged
    await db.execute("UPDATE memories SET embedding = NULL WHERE id = ?", (ids[6],))
    await db.execute("DELETE FROM record_vector_changes WHERE record_id = ?", (ids[6],))  # unlogged
    await db.commit()
    assert await _search(db) == await _search_without_index(db)
    assert taken["memories_fallback"] == 1

    # Put ids[6] back as the file holds it, then delete ids[7] where only the gone
    # probe can see it.
    content = (await db.execute_fetchall("SELECT content FROM memories WHERE id = ?", (ids[6],)))[0][0]
    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(fake_embed_one(content)), ids[6]))
    await db.execute("DELETE FROM memories WHERE id = ?", (ids[7],))
    await db.execute("DELETE FROM record_vector_changes WHERE record_id IN (?, ?)", (ids[6], ids[7]))
    await db.commit()
    assert await _search(db) == await _search_without_index(db)
    assert taken["memories_fallback"] == 2, "the gone probe caught the unlogged delete"


# --------------------------------------------------------------------------------------
# Export, import and the repair that builds the coarse file.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_and_import_carry_no_log(clean, tmp_path):
    db = clean
    await _corpus(db, 4)
    await _build(db, "memories")
    out = str(tmp_path / "export.jsonl")
    assert (await admin_handlers.do_export_memories(AGENT, out, include_embeddings=True))["ok"]
    text = open(out, encoding="utf-8").read()
    assert "record_vector_changes" not in text and "vector_log" not in text
    before = await _log_rows(db)
    result = await admin_handlers.do_import_memories(out, target_agent_id=OTHER)
    assert result.get("ok"), result
    assert await _log_rows(db) == before, "an import inserts above every watermark and logs nothing"


@pytest.mark.asyncio
async def test_the_coarse_repair_builds_a_file_the_log_vouches_for(clean, monkeypatch):
    """check_health(fix) builds the coarse file after its run commits, the log turned on first."""
    from cpersona import config

    db = clean
    monkeypatch.setattr(vector, "MAX_MEMORIES", 5)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 1)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")
    await _corpus(db, 30)
    result = await maintenance_handlers.do_check_health(AGENT, fix=True, checks=["coarse_index"])
    issue = [i for i in result["issues"] if i["type"].startswith("coarse_index")]
    assert issue and issue[0].get("rebuilt") is True, result

    index = coarse_index.load_coarse_index()
    changed, refusal = await vector_index.changed_since_build(db, index, "memories")
    assert refusal == "" and changed.tolist() == []
    assert (await vector_index.log_clock(db))[0] == 1


@pytest.mark.asyncio
async def test_a_repair_build_that_declines_leaves_no_log_running(clean, monkeypatch):
    """The repair turns the log on for its build; with no file built, nothing reads it."""
    from cpersona import config

    db = clean
    monkeypatch.setattr(vector, "MAX_MEMORIES", 5)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 1)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")
    await _corpus(db, 30)
    await db.execute("DROP TRIGGER memories_vector_log_ad")
    await db.commit()
    try:
        result = await maintenance_handlers.do_check_health(AGENT, fix=True, checks=["coarse_index"])
        issue = [i for i in result["issues"] if i["type"].startswith("coarse_index")]
        assert issue and issue[0]["rebuilt"] is False, result
        assert "memories_vector_log_ad" in issue[0]["build_declined"]
        assert (await vector_index.log_clock(db))[0] == 0
    finally:
        await db.executescript(database.VECTOR_LOG_SQL)
        await db.commit()


# --------------------------------------------------------------------------------------
# Random writes after a build, against the scan.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_random_writes_after_a_build_answer_as_the_scan_does(clean, taken, monkeypatch, seed):
    db = clean
    rng = random.Random(seed)
    monkeypatch.setattr(vector, "MAX_MEMORIES", 8)
    words = ["orchard", "telescope", "lantern", "harbour", "filler", "river", "stone", "glass"]
    second = 0
    ids = []
    for n in range(30):
        second += 1
        text = " ".join(rng.sample(words, 3)) + f" #{n}"
        ids.append(await _memory(
            db, text, second=second, agent_id=rng.choice([AGENT, AGENT, OTHER]),
            project_id=rng.choice(["", "", "p1"]), channel=rng.choice(["", "c1"]),
        ))
    await db.commit()
    await _build(db, "memories")

    scopes = [{}, {"project_id": "p1"}, {"project_id": ""}, {"channel": "c1"}, {"agent_id": OTHER}]
    for step in range(25):
        live = [i for i in ids if (await db.execute_fetchall("SELECT 1 FROM memories WHERE id = ?", (i,)))]
        target = rng.choice(live)
        op = rng.choice(["embed", "clear", "channel", "project", "agent", "time", "delete", "insert"])
        if op == "embed":
            vec = fake_embed_one(" ".join(rng.sample(words, 3)) + f" @{step}")
            await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_blob(vec), target))
        elif op == "clear":
            await db.execute("UPDATE memories SET embedding = NULL WHERE id = ?", (target,))
        elif op == "channel":
            await db.execute("UPDATE memories SET channel = ? WHERE id = ?", (rng.choice(["", "c1", "c2"]), target))
        elif op == "project":
            await db.execute("UPDATE memories SET project_id = ? WHERE id = ?", (rng.choice(["", "p1", "p2"]), target))
        elif op == "agent":
            await db.execute("UPDATE memories SET agent_id = ? WHERE id = ?", (rng.choice([AGENT, OTHER]), target))
        elif op == "time":
            await db.execute(
                "UPDATE memories SET created_at = ? WHERE id = ?",
                (f"2026-03-01 00:{rng.randrange(60):02d}:{rng.randrange(60):02d}", target),
            )
        elif op == "delete":
            await db.execute("DELETE FROM memories WHERE id = ?", (target,))
        else:
            second += 1
            ids.append(await _memory(db, " ".join(rng.sample(words, 3)) + f" +{step}", second=second))
        await db.commit()
        for kw in scopes:
            assert await _search(db, **kw) == await _search_without_index(db, **kw), (seed, step, op, kw)
    assert taken["memories_fallback"] == 0, "every answer came from the index"
