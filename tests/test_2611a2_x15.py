"""Regression tests for the task queue and the costs of blocks and associations.

  pause gate      A no-persist pause drops the queued writes of the session that
                  armed it. Derived work is not such a write: the backfill sweep
                  and the block index build belong to no session, and a record's
                  node or block build follows a record that is already stored.
                  Dropping either left records without their derived layers, and
                  the sweep, queued once per process start, never came back.
  requeue         A failed build moved to the tail carries its session with it.
                  When its row was deleted underneath the queue, the move matched
                  nothing, and the tail row it then read was another session's.
  span            A bare number passes SQLite's datetime() as a Julian day; the
                  span must still not end on a stamp its caller cannot parse.
  index build     The build's per-row columns are held as typed arrays, not as
                  lists of Python ints.
  missing_blocks  Dividing every candidate record runs off the event loop.
  walk fan-out    The neighbourhood a walk or traverse expands is bounded by a
                  count of entities, and a cut it makes is reported.
"""

import asyncio
import os
import tempfile
import time
import tracemalloc

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x15.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import (  # noqa: E402
    admin_handlers,
    block_index,
    blocks,
    memory_handlers,
    nodes,
    session,
    tasks,
)
from cpersona.database import get_db  # noqa: E402

A = "session-a"
B = "session-b"


@pytest_asyncio.fixture
async def queue_db():
    saved = tasks._task_queue
    session.reset_pauses_for_tests()
    db = await get_db()
    await db.execute("DELETE FROM pending_memory_tasks")
    await db.commit()
    try:
        yield db
    finally:
        session.reset_pauses_for_tests()
        await db.execute("DELETE FROM pending_memory_tasks")
        await db.commit()
        tasks._task_queue = saved


async def _drain(queue) -> None:
    queue._running = True
    try:
        await queue._drain(admin_handlers, memory_handlers, nodes)
    finally:
        queue._running = False


# ---------------------------------------------------------------------------
# pause gate: derived work survives a pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_keyless_pause_does_not_delete_the_servers_own_derived_work(queue_db, monkeypatch):
    """The sweep and the index build are enqueued with no session, so they were
    judged against the shared bucket, and any keyless pause deleted them."""
    ran: list[str] = []

    async def fake_backfill(payload):
        ran.append(blocks.BACKFILL_TASK_TYPE)
        return "swept"

    async def fake_index_build(payload):
        ran.append(block_index.TASK_TYPE)
        return "built"

    async def no_index_build():
        return False

    monkeypatch.setattr(blocks, "backfill", fake_backfill)
    monkeypatch.setattr(block_index, "build_task", fake_index_build)
    monkeypatch.setattr(block_index, "queue_build_if_needed", no_index_build)

    queue = tasks.MemoryTaskQueue()
    tasks._task_queue = queue
    # The two server paths, as the server takes them: no session key.
    assert await blocks._enqueue_backfill({})
    await queue.enqueue(block_index.TASK_TYPE, "", {})
    session.pause_for(session.TRANSPORT_KEY, False, 600)

    await _drain(queue)

    assert ran == [blocks.BACKFILL_TASK_TYPE, block_index.TASK_TYPE], (
        "a keyless no-persist pause deleted server-owned derived work: " + repr(ran)
    )
    rows = await queue_db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_a_pause_does_not_drop_the_build_of_a_record_already_stored(queue_db, monkeypatch):
    """The record was committed before the pause; dropping its build protects no
    write and leaves it without blocks. The session's own queued write is still
    dropped -- that is what the pause is for."""
    built: list[str] = []
    profiles: list[str] = []

    async def fake_build_blocks(payload):
        built.append(blocks.TASK_TYPE)
        return "built"

    async def fake_build_nodes(payload):
        built.append(nodes.TASK_TYPE)
        return "built"

    async def no_index_build():
        return False

    async def spy_update_profile(agent_id, payload, session_key=""):
        profiles.append(agent_id)
        return {"ok": True, "profiles_updated": 1}

    monkeypatch.setattr(blocks, "build_blocks", fake_build_blocks)
    monkeypatch.setattr(nodes, "build_nodes", fake_build_nodes)
    monkeypatch.setattr(block_index, "queue_build_if_needed", no_index_build)
    monkeypatch.setattr(admin_handlers, "do_update_profile", spy_update_profile)

    queue = tasks.MemoryTaskQueue()
    tasks._task_queue = queue
    await queue.enqueue(nodes.TASK_TYPE, "agent-A", {"kind": "mem", "id": 1}, session_key=A)
    await queue.enqueue(blocks.TASK_TYPE, "agent-A", {"kind": "mem", "id": 1}, session_key=A)
    await queue.enqueue("update_profile", "agent-A", [{"content": "x"}], session_key=A)
    session.pause_for(A, True, 600)

    await _drain(queue)

    assert built == [nodes.TASK_TYPE, blocks.TASK_TYPE], (
        "the pause deleted the derived builds of a record stored before it: " + repr(built)
    )
    assert profiles == [], "the pause no longer drops the session's own queued write"


# ---------------------------------------------------------------------------
# requeue: a vanished row does not lend its session to another row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeueing_a_vanished_row_does_not_reattribute_another_sessions_task(queue_db):
    queue = tasks.MemoryTaskQueue()
    build_id = await queue.enqueue(nodes.TASK_TYPE, "agent-A", {"kind": "mem", "id": 1}, session_key=A)
    other_id = await queue.enqueue("update_profile", "agent-B", [{"content": "y"}], session_key=B)
    # Deleted underneath the queue, as delete_agent_data or a move-mode merge does.
    await queue_db.execute("DELETE FROM pending_memory_tasks WHERE id = ?", (build_id,))
    await queue_db.commit()

    await queue._requeue_at_tail(build_id)

    assert queue._session_for(other_id) == B, (
        "the vanished row's session was written over another session's task: "
        + repr(queue._task_sessions)
    )
    assert build_id not in queue._task_sessions
    rows = await queue_db.execute_fetchall("SELECT id, retries FROM pending_memory_tasks")
    assert rows == [(other_id, 0)], "a requeue of a vanished row moved or charged another row"
    session.pause_for(B, True, 600)
    assert session.is_paused_for(queue._session_for(other_id))


# ---------------------------------------------------------------------------
# span: a bare number is a Julian day to SQLite and nothing to the parser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", ["2025", "0", "1e3"])
async def test_a_bare_number_stored_through_store_is_not_the_span_s_oldest_end(stamp):
    from cpersona import scope_stats
    from cpersona.utils import _parse_timestamp_utc

    agent = f"agent.x15.span.{stamp}"
    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
    await db.commit()
    for ts in ("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"):
        stored = await memory_handlers.do_store(agent, {"content": f"orchard note {ts}", "timestamp": ts})
        assert stored.get("id"), stored
    stored = await memory_handlers.do_store(agent, {"content": "kettle reminder", "timestamp": stamp})
    assert stored.get("id"), stored
    rows = await db.execute_fetchall(
        "SELECT datetime(timestamp) IS NOT NULL FROM memories WHERE agent_id = ? AND timestamp = ?",
        (agent, stamp),
    )
    assert rows == [(1,)], "the stamp no longer passes SQLite's datetime(); the case is gone"
    scope_stats.clear()

    lo, hi = await scope_stats.get_span(db, agent, project_id="", channel="")

    assert (lo, hi) == ("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"), (lo, hi)
    assert _parse_timestamp_utc(lo) is not None and _parse_timestamp_utc(hi) is not None


# ---------------------------------------------------------------------------
# index build: the per-row columns are typed, not lists of Python ints
# ---------------------------------------------------------------------------

_BLOCK_INSERT = (
    "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id,"
    " channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model)"
    " VALUES (?, ?, ?, ?, ?, ?, 0, 1, 0, ?, ?)"
)
_INDEX_ROWS = 60_000
_INDEX_WIDTH = 8


@pytest_asyncio.fixture
async def fresh_db(monkeypatch):
    """A database of its own: the build reads every agent's block rows."""
    from cpersona import config, database

    saved = (database._db, database.DB_PATH, config.DB_PATH)
    directory = tempfile.mkdtemp()
    database._db = None
    database.DB_PATH = config.DB_PATH = os.path.join(directory, "x15.db")
    block_index._cache.clear()
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        database._db, database.DB_PATH, config.DB_PATH = saved
        block_index._cache.clear()


def _index_rows(n: int) -> list[tuple]:
    rows = []
    for i in range(n):
        # Parent ids above the small-int cache, as a real corpus has them.
        rows.append((
            "mem" if i % 5 else "ep",
            100_000 + i // 4,
            i % 4,
            f"agent-{i % 3}",
            "" if i % 2 else "proj",
            "",
            (i % 251).to_bytes(1, "little") * _INDEX_WIDTH,
            "model-a",
        ))
    return rows


@pytest.mark.asyncio
async def test_the_index_build_holds_its_columns_compactly(fresh_db, monkeypatch):
    """About 100 bytes a row as lists of ints, against 22 bytes a row in the file.

    The bound is over everything the build allocates. Measured here: 125 bytes a
    row with lists, 57 with typed columns (30 for the columns, the rest the
    column numpy copies while writing and an array's regrowth). 90 sits between.
    """
    monkeypatch.setattr(block_index, "_BUILD_PAGE", 512)
    rows = _index_rows(_INDEX_ROWS)
    await fresh_db.executemany(_BLOCK_INSERT, rows)
    await fresh_db.commit()
    out = os.path.join(tempfile.mkdtemp(), "blocks.idx")
    # A first build imports what a build imports lazily (the schema checks module
    # among it), which would otherwise be counted against the rows.
    assert (await block_index.build_block_index(path=out, width=_INDEX_WIDTH))["built"]

    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        built = await block_index.build_block_index(path=out, width=_INDEX_WIDTH)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()

    assert built["built"], built
    per_row = peak / _INDEX_ROWS
    assert per_row < 90, f"the build held {per_row:.0f} bytes a row ({peak} bytes for {_INDEX_ROWS} rows)"

    # The file still says what the rows say, column by column.
    index = block_index.load_block_index(out)
    assert index.count == _INDEX_ROWS
    expected = sorted(rows, key=lambda r: (r[0], r[1], r[2]))
    assert index.parent_id.tolist() == [r[1] for r in expected]
    assert index.block_index.tolist() == [r[2] for r in expected]
    assert [index.agents[c] for c in index.agent_code.tolist()] == [r[3] for r in expected]
    assert [index.projects[c] for c in index.project_code.tolist()] == [r[4] for r in expected]
    assert [index.models[c] for c in index.model_code.tolist()] == [r[7] for r in expected]
    assert [block_index.KINDS[c] for c in index.kind.tolist()] == [r[0] for r in expected]
    assert set(index.measurable.tolist()) == {1}
    assert bytes(index.bits[7]) == expected[7][6]


# ---------------------------------------------------------------------------
# missing_blocks: dividing the candidates does not hold the event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_missing_blocks_scan_divides_records_off_the_event_loop(monkeypatch):
    """The divider is pure Python and runs once per record without current blocks,
    which includes every one-block record on every run. In the loop, a check_health
    held every other request for the whole pass."""
    from cpersona.isolation import isolation_where

    agent = "agent.x15.scan"
    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
    for i in range(20):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
            (agent, f"First sentence {i}. Second sentence {i}.", "2026-10-01T00:00:00+00:00"),
        )
    await db.commit()

    real_segment = blocks.segment
    divided: list[int] = []

    def slow_segment(text, **kwargs):
        # Stands in for the divider's per-character work: it holds whichever
        # thread runs it, and nothing else.
        time.sleep(0.05)
        divided.append(1)
        return real_segment(text, **kwargs)

    monkeypatch.setattr(blocks, "segment", slow_segment)

    ticks: list[float] = []
    done = asyncio.Event()

    async def ticker():
        while not done.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    tick_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    started = time.monotonic()
    try:
        found = await blocks.records_without_current_blocks(db, isolation_where(agent_id=agent))
    finally:
        finished = time.monotonic()
        done.set()
        await tick_task

    assert len(divided) == 20 and len(found) == 20, (len(divided), found)
    inside = [t for t in ticks if started <= t <= finished]
    gaps = [b - a for a, b in zip([started, *inside], [*inside, finished])]
    assert max(gaps) < 0.3, (
        f"the event loop was held for {max(gaps):.2f}s of a {finished - started:.2f}s scan"
    )


# ---------------------------------------------------------------------------
# walk fan-out: the neighbourhood is bounded by a count of entities
# ---------------------------------------------------------------------------

WALK_AGENT = "x15-walk"
_TOPICS = 10


@pytest_asyncio.fixture
async def graph_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("entities", "entity_aliases", "entity_mentions", "relations"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (WALK_AGENT,))
    await db.commit()
    yield db
    for table in ("entities", "entity_aliases", "entity_mentions", "relations"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (WALK_AGENT,))
    await db.commit()


async def _walk_mem(content: str) -> str:
    out = await memory_handlers.do_store(WALK_AGENT, {"content": content})
    assert out["result"] == "stored", out
    return f"mem:{out['id']}"


async def _hub() -> dict:
    """A seed record mentions Alpha; Alpha relates to Topic0..Topic9, Topic9 the most
    recently declared; each topic is mentioned by one record of its own."""
    from cpersona import associations

    seed = await _walk_mem("rollback of the hub deploy")
    rep = await associations.declare(WALK_AGENT, {"entities": [{"name": "Alpha"}]}, anchor_ref=seed)
    ids = {"Alpha": rep["entities"][0]["id"]}
    refs = {}
    for i in range(_TOPICS):
        name = f"Topic{i}"
        refs[name] = await _walk_mem(f"topic {i} evidence note")
        rep = await associations.declare(WALK_AGENT, {"entities": [{"name": name}]}, anchor_ref=refs[name])
        ids[name] = rep["entities"][0]["id"]
        rep = await associations.declare(
            WALK_AGENT, {"relations": [{"subject": "Alpha", "predicate": "relates_to", "object": name}]}
        )
        assert rep["relations"], rep
        await _execute(
            "UPDATE relations SET declared_at = ? WHERE id = ?",
            (f"2026-01-{i + 1:02d}T00:00:00+00:00", rep["relations"][0]),
        )
    return {"seed": seed, "ids": ids, "refs": refs}


async def _execute(sql: str, params: tuple) -> None:
    db = await get_db()
    await db.execute(sql, params)
    await db.commit()


@pytest.mark.asyncio
async def test_the_walk_reads_no_more_entities_than_the_bound_and_says_it_cut(graph_db, monkeypatch):
    from cpersona import associations
    from cpersona import reconstruct as R

    hub = await _hub()
    monkeypatch.setattr(associations, "NEIGHBOURHOOD_MAX_ENTITIES", 4, raising=False)

    graph = await associations.walk_graph(WALK_AGENT, [hub["seed"]], [hub["seed"]], max_hops=2, per_entity=40)

    newest = [hub["ids"][f"Topic{i}"] for i in (9, 8, 7)]
    assert sorted(graph.records) == sorted(newest), (
        f"the walk read the records of {sorted(graph.records)} under a bound of 4 entities, "
        "not the three the most recent relations reached"
    )
    walked, cuts = R.walk([[0]], [R._Candidate({"ref": hub["seed"], "content": "x"}, rank=0)], graph, 2)
    # The written order still decides what is kept: the most recent relations first.
    assert [ref for ref, _, _ in walked[0]] == [hub["refs"][f"Topic{i}"] for i in (9, 8, 7)]
    # The graph is one relation deep and max_hops is 2, so only the entity bound
    # can have cut it -- and the item must say a relation was left unfollowed.
    assert R.BOUND_HOPS in cuts[0], cuts


@pytest.mark.asyncio
async def test_a_walk_inside_the_bound_is_unchanged(graph_db, monkeypatch):
    from cpersona import associations
    from cpersona import reconstruct as R

    hub = await _hub()
    graph = await associations.walk_graph(WALK_AGENT, [hub["seed"]], [hub["seed"]], max_hops=2, per_entity=40)
    walked, cuts = R.walk([[0]], [R._Candidate({"ref": hub["seed"], "content": "x"}, rank=0)], graph, 2)
    assert [ref for ref, _, _ in walked[0]] == [hub["refs"][f"Topic{i}"] for i in range(_TOPICS - 1, -1, -1)]
    assert cuts == [set()], cuts


@pytest.mark.asyncio
async def test_traverse_expands_no_more_entities_than_the_bound(graph_db, monkeypatch):
    from cpersona import associations

    hub = await _hub()
    monkeypatch.setattr(associations, "NEIGHBOURHOOD_MAX_ENTITIES", 4, raising=False)

    _, distance, _ = await associations._neighbourhood(
        graph_db, WALK_AGENT, [hub["ids"]["Alpha"]], 5, None, ""
    )
    assert len(distance) <= 4, f"one hop past a hub expanded {len(distance)} entities"

    out = await associations.traverse(WALK_AGENT, "Alpha", max_hops=5, limit=3)
    assert [e["name"] for e in out["entities"]] == ["Alpha", "Topic9", "Topic8"], out["entities"]
    assert "max_hops" in out.get("bounds", {}).get("omitted", []), out.get("bounds")


@pytest.mark.asyncio
async def test_traverse_returns_the_same_entities_under_a_bound_wider_than_its_limit(graph_db, monkeypatch):
    from cpersona import associations

    await _hub()
    wide = await associations.traverse(WALK_AGENT, "Alpha", max_hops=5, limit=3)
    monkeypatch.setattr(associations, "NEIGHBOURHOOD_MAX_ENTITIES", 4, raising=False)
    narrow = await associations.traverse(WALK_AGENT, "Alpha", max_hops=5, limit=3)
    assert narrow["entities"] == wide["entities"]
    assert narrow["relations"] == wide["relations"]
