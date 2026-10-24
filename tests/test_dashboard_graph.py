"""The dashboard's memory network (2.6.15): which lines are drawn, and from what.

The similar lines are pinned on vectors of known geometry (each memory to its K
nearest at or above the floor, only within one vector width, never from a vector that
cannot be compared), the folder view's cap on how many it draws, the "around" walk
step by step (and that reading the vectors in chunks gives the same answer as holding
them), the declared lines (mentions and relations, only between points that are
shown), the cache seeing a new row at once, and the endpoint never sending a vector.
"""

import json
import math
import threading

import numpy as np
import pytest
import pytest_asyncio

from cpersona import database
from cpersona.dashboard import graph, store, web
from cpersona.database import get_db


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in (
        "memories",
        "episodes",
        "record_nodes",
        "record_blocks",
        "pending_memory_tasks",
        "entities",
        "entity_aliases",
        "entity_mentions",
        "relations",
    ):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    graph.clear_cache()
    yield conn
    graph.clear_cache()


def _vec(*xs) -> bytes:
    return np.asarray(xs, dtype=np.float32).tobytes()


def _at(degrees) -> bytes:
    r = math.radians(degrees)
    return _vec(math.cos(r), math.sin(r), 0.0, 0.0)


async def _add(conn, content, embedding, ts, agent="a"):
    cur = await conn.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
        (agent, content, ts, embedding),
    )
    await conn.commit()
    return cur.lastrowid


def _ts(i):
    return f"2026-10-{i + 1:02d}T00:00:00+00:00"


def _open():
    return store.open_store(database.DB_PATH)


def _similar(out):
    return {
        tuple(sorted((int(x["s"][2:]), int(x["t"][2:]))))
        for x in out["links"]
        if x["kind"] == "similar"
    }


async def _five(db):
    """a, b, c lean together; d and e lean together; a and e are at cosine 0.5."""
    vecs = [
        _vec(1, 0, 0, 0),
        _vec(0.9, 0.1, 0, 0),
        _vec(0.8, 0.6, 0, 0),
        _vec(0, 0, 1, 0),
        _vec(0.5, 0, 0.866, 0),
    ]
    return [await _add(db, f"note {n}", v, _ts(i)) for i, (n, v) in enumerate(zip("abcde", vecs))]


@pytest.mark.asyncio
async def test_a_folder_joins_each_memory_to_its_nearest_above_the_floor(db):
    a, b, c, d, e = await _five(db)
    conn = _open()
    out = graph.folder(conn)
    conn.close()
    # a–e is at cosine 0.5, under the floor, though e is among a's three nearest.
    assert _similar(out) == {(a, b), (a, c), (b, c), (d, e)}
    assert out["total"] == out["shown"] == 5 and out["without_vector"] == 0
    assert {x["t"] for x in out["links"] if x["kind"] == "structure"} == {
        f"m:{i}" for i in (a, b, c, d, e)
    }


@pytest.mark.asyncio
async def test_each_memory_keeps_only_its_k_nearest(db, monkeypatch):
    a, b, c, d, e = await _five(db)
    monkeypatch.setattr(graph, "K", 1)
    conn = _open()
    out = graph.folder(conn)
    conn.close()
    # a→b, b→a, c→b, d→e, e→d: a–c is no one's single nearest.
    assert _similar(out) == {(a, b), (b, c), (d, e)}


@pytest.mark.asyncio
async def test_a_vector_that_cannot_be_compared_draws_no_line(db):
    a = await _add(db, "four wide", _vec(1, 0, 0, 0), _ts(0))
    b = await _add(db, "four wide too", _vec(1, 0.01, 0, 0), _ts(1))
    await _add(db, "three wide, same direction", _vec(1, 0, 0), _ts(2))
    await _add(db, "torn bytes", b"\x00\x00\x80?\x00", _ts(3))
    await _add(db, "not finite", _vec(float("nan"), 0, 0, 0), _ts(4))
    await _add(db, "zero", _vec(0, 0, 0, 0), _ts(5))
    await _add(db, "never embedded", None, _ts(6))
    conn = _open()
    out = graph.folder(conn)
    conn.close()
    assert _similar(out) == {(a, b)}
    # The three-wide vector is compared, but only with vectors three wide.
    assert out["shown"] == 7 and out["without_vector"] == 4
    assert sum(n["kind"] == "memory" for n in out["nodes"]) == 7


@pytest.mark.asyncio
async def test_a_large_folder_draws_its_newest_and_says_how_many_it_left_out(db, monkeypatch):
    ids = await _five(db)
    monkeypatch.setattr(graph, "MAX_NODES", 2)
    conn = _open()
    out = graph.folder(conn)
    conn.close()
    assert out["total"] == 5 and out["shown"] == 2
    assert {n["mem"] for n in out["nodes"] if n["kind"] == "memory"} == set(ids[-2:])


@pytest.mark.asyncio
async def test_a_folder_is_the_lists_folder_and_month(db):
    await _add(db, "mine", _vec(1, 0, 0, 0), "2026-09-01T00:00:00+00:00", agent="a")
    keep = await _add(db, "mine later", _vec(1, 0, 0, 0), "2026-10-01T00:00:00+00:00", agent="a")
    await _add(db, "theirs", _vec(1, 0, 0, 0), "2026-10-01T00:00:00+00:00", agent="b")
    conn = _open()
    out = graph.folder(conn, ["a"], "2026-10")
    conn.close()
    assert [n["mem"] for n in out["nodes"] if n["kind"] == "memory"] == [keep]
    assert out["total"] == 1


async def _arc(db):
    """Points on a circle at 0, 10, 30, 55 and 95 degrees, and one far away."""
    ids = [await _add(db, f"at {d}", _at(d), _ts(i)) for i, d in enumerate((0, 10, 30, 55, 95))]
    ids.append(await _add(db, "elsewhere", _vec(0, 0, 0, 1), _ts(9)))
    return ids


def _reached(out):
    return {n["mem"]: n["hop"] for n in out["nodes"] if n["kind"] == "memory"}


@pytest.mark.asyncio
async def test_around_walks_one_step_at_a_time(db, monkeypatch):
    p0, p10, p30, p55, p95, far = await _arc(db)
    monkeypatch.setattr(graph, "K", 2)
    conn = _open()
    one = graph.around(conn, database.DB_PATH, p0, hops=1)
    two = graph.around(conn, database.DB_PATH, p0, hops=2)
    three = graph.around(conn, database.DB_PATH, p0, hops=3)
    beyond = graph.around(conn, database.DB_PATH, p0, hops=9)
    conn.close()
    assert _reached(one) == {p0: 0, p10: 1, p30: 1}
    assert _reached(two) == {p0: 0, p10: 1, p30: 1, p55: 2}
    # 95° is 40° from 55° (cosine 0.77), so the third step reaches it; nothing reaches "elsewhere".
    assert _reached(three) == {p0: 0, p10: 1, p30: 1, p55: 2, p95: 3}
    assert beyond["hops"] == graph.MAX_HOPS and _reached(beyond) == _reached(three)
    assert _similar(one) == {(p0, p10), (p0, p30)}
    assert _similar(two) == {(p0, p10), (p0, p30), (p10, p30), (p30, p55)}


@pytest.mark.asyncio
async def test_reading_in_chunks_gives_the_same_walk_as_holding(db, monkeypatch):
    p0 = (await _arc(db))[0]
    monkeypatch.setattr(graph, "K", 2)
    conn = _open()
    held = graph.around(conn, database.DB_PATH, p0, hops=3)
    graph.clear_cache()
    monkeypatch.setattr(graph, "CACHE_MAX_BYTES", 0)
    monkeypatch.setattr(graph, "SCAN_CHUNK", 2)
    chunked = graph.around(conn, database.DB_PATH, p0, hops=3)
    conn.close()
    assert graph._cache == {}
    assert _reached(chunked) == _reached(held) and _similar(chunked) == _similar(held)


@pytest.mark.asyncio
async def test_around_a_memory_without_a_vector_is_just_that_memory(db):
    await _arc(db)
    lone = await _add(db, "never embedded", None, _ts(20))
    conn = _open()
    out = graph.around(conn, database.DB_PATH, lone)
    missing = graph.around(conn, database.DB_PATH, 999_999)
    conn.close()
    assert _reached(out) == {lone: 0} and out["has_vector"] is False
    assert missing is None


@pytest.mark.asyncio
async def test_a_new_row_is_seen_at_once_through_the_cache(db):
    p0 = (await _arc(db))[0]
    conn = _open()
    before = graph.around(conn, database.DB_PATH, p0, hops=1)
    close = await _add(db, "almost the same", _at(1), _ts(21))
    after = graph.around(conn, database.DB_PATH, p0, hops=1)
    conn.close()
    assert close not in _reached(before) and _reached(after)[close] == 1


@pytest.mark.asyncio
async def test_declared_lines_join_only_points_that_are_shown(db):
    a, b, c, d, e = await _five(db)
    now = "2026-10-24T00:00:00+00:00"

    async def entity(name):
        cur = await db.execute(
            "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES ('a', ?, ?, 'a', ?)",
            (name, name.lower(), now),
        )
        return cur.lastrowid

    caroline, melanie, unseen = await entity("Caroline"), await entity("Melanie"), await entity("Unseen")
    for ent, mem in ((caroline, a), (caroline, c), (melanie, d)):
        await db.execute(
            "INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'a', ?)",
            (ent, f"mem:{mem}", now),
        )
    for s_kind, s_id, pred, o_kind, o_id in (
        ("entity", caroline, "friend_of", "entity", melanie),
        ("mem", a, "supports", "mem", b),
        ("entity", caroline, "knows", "entity", unseen),
    ):
        await db.execute(
            "INSERT INTO relations (agent_id, subject_kind, subject_id, predicate, object_kind, object_id, declared_by, declared_at) "
            "VALUES ('a', ?, ?, ?, ?, ?, 'a', ?)",
            (s_kind, s_id, pred, o_kind, o_id, now),
        )
    await db.commit()
    conn = _open()
    out = graph.folder(conn)
    conn.close()
    declared = {(x["s"], x["t"], x["label"]) for x in out["links"] if x["kind"] == "declared"}
    assert declared == {
        (f"e:{caroline}", f"m:{a}", "mentions"),
        (f"e:{caroline}", f"m:{c}", "mentions"),
        (f"e:{melanie}", f"m:{d}", "mentions"),
        (f"e:{caroline}", f"e:{melanie}", "friend_of"),
        (f"m:{a}", f"m:{b}", "supports"),
    }
    assert {n["label"] for n in out["nodes"] if n["kind"] == "entity"} == {"Caroline", "Melanie"}


@pytest.fixture
def served():
    server, _ = web.make_server(database.DB_PATH, 0, token="tok")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _get(server, path, token="tok"):
    import http.client

    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    out = (r.status, r.read())
    conn.close()
    return out


@pytest.mark.asyncio
async def test_the_endpoint_needs_the_token_and_never_sends_a_vector(db, served):
    a = (await _five(db))[0]
    assert _get(served, "/api/graph", token=None)[0] == 401
    status, body = _get(served, "/api/graph")
    folder = json.loads(body)
    assert status == 200 and folder["view"] == "folder" and folder["shown"] == 5
    status, body = _get(served, f"/api/graph?around={a}&hops=1")
    assert status == 200 and json.loads(body)["center"] == a
    assert b"embedding" not in body
    assert _get(served, "/api/graph?around=999999")[0] == 404
    assert _get(served, "/api/graph?around=x")[0] == 400
