"""The whole-store picture (2.6.15): the logical directory, entities and clusters.

The tree lens is pinned on counts (each point holds what its folder holds, a month
hangs from its channel, months are left out past the cap), the entities lens on
mention counts and the two kinds of line, and the clusters lens on vectors in
three known groups: the groups found, the member that names each group, the
determinism, the width it groups, the cap and the cache. Each point's own drawing
(a cluster's memories, an entity's memories) is pinned through the same rows.
"""

import json
import threading

import numpy as np
import pytest
import pytest_asyncio

from cpersona import database
from cpersona.dashboard import graph, overall, store, web
from cpersona.database import get_db

NOW = "2026-10-24T00:00:00+00:00"


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
    overall.clear_cache()
    yield conn
    graph.clear_cache()
    overall.clear_cache()


async def _add(conn, content, ts, agent="a", project="", channel="", embedding=None):
    cur = await conn.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, timestamp, embedding) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (agent, project, channel, content, ts, embedding),
    )
    await conn.commit()
    return cur.lastrowid


def _open():
    return store.open_store(database.DB_PATH)


@pytest.mark.asyncio
async def test_the_tree_counts_each_folder_and_hangs_months_from_channels(db, monkeypatch):
    for agent, project, channel, ts in (
        ("a", "p1", "", "2026-09-01"),
        ("a", "p1", "", "2026-10-01"),
        ("a", "p1", "", "2026-10-02"),
        ("a", "", "c1", "2026-10-03"),
        ("b", "p2", "", "2025-01-01"),
    ):
        await _add(db, f"note {ts}", ts + "T00:00:00+00:00", agent, project, channel)
    conn = _open()
    out = overall.tree(conn)
    counts = {tuple(n["path"]): n["count"] for n in out["nodes"]}
    assert counts == {
        ("a",): 4,
        ("a", ""): 1,
        ("a", "", "c1"): 1,
        ("a", "", "c1", "2026", "10"): 1,
        ("a", "p1"): 3,
        ("a", "p1", ""): 3,
        ("a", "p1", "", "2026", "09"): 1,
        ("a", "p1", "", "2026", "10"): 2,
        ("b",): 1,
        ("b", "p2"): 1,
        ("b", "p2", ""): 1,
        ("b", "p2", "", "2025", "01"): 1,
    }
    parent = {x["t"]: x["s"] for x in out["links"]}
    month = "t:" + json.dumps(["a", "p1", "", "2026", "10"])
    assert parent[month] == "t:" + json.dumps(["a", "p1", ""])
    assert len(out["links"]) == 10 and out["total"] == 5 and out["months_shown"]
    monkeypatch.setattr(overall, "TREE_MAX_NODES", 8)
    lean = overall.tree(conn)
    conn.close()
    assert not lean["months_shown"] and lean["months"] == 4
    assert {len(n["path"]) for n in lean["nodes"]} == {1, 2, 3}


@pytest.mark.asyncio
async def test_entities_are_sized_by_mentions_and_joined_two_ways(db):
    m1 = await _add(db, "one", NOW)
    m2 = await _add(db, "two", NOW)

    async def entity(name):
        cur = await db.execute(
            "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES ('a', ?, ?, 'a', ?)",
            (name, name.lower(), NOW),
        )
        return cur.lastrowid

    x, y, z = await entity("X"), await entity("Y"), await entity("Z")
    for ent, ref in ((x, f"mem:{m1}"), (x, f"mem:{m2}"), (y, f"mem:{m2}"), (z, "ep:1")):
        await db.execute(
            "INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'a', ?)",
            (ent, ref, NOW),
        )
    await db.execute(
        "INSERT INTO relations (agent_id, subject_kind, subject_id, predicate, object_kind, object_id, declared_by, declared_at) "
        "VALUES ('a', 'entity', ?, 'friend_of', 'entity', ?, 'a', ?)",
        (x, y, NOW),
    )
    await db.commit()
    conn = _open()
    out = overall.entities(conn)
    mine = graph.mentioning(conn, x)
    none = graph.mentioning(conn, 999_999)
    conn.close()
    # An episode's mention is not a memory's: Z is mentioned by none of the memories.
    assert {n["label"]: n["count"] for n in out["nodes"]} == {"X": 2, "Y": 1, "Z": 0}
    assert {(x_["s"], x_["t"], x_["kind"], x_.get("label") or x_.get("w")) for x_ in out["links"]} == {
        (f"e:{x}", f"e:{y}", "declared", "friend_of"),
        (f"e:{x}", f"e:{y}", "co", 1),
    }
    assert {n["mem"] for n in mine["nodes"] if n["kind"] == "memory"} == {m1, m2}
    assert mine["label"] == "X" and none is None


def _vec(*xs):
    v = np.zeros(6, dtype=np.float32)
    v[: len(xs)] = xs
    return v.tobytes()


def _axis(g, extra=()):
    v = [0.0] * 6
    v[g] = 1.0
    for i, w in extra:
        v[i] += w
    return _vec(*v)


async def _three_groups(db):
    """Three groups of six around axes 0, 1 and 2. In each, the bare axis is nearest the centre."""
    groups = []
    for g in range(3):
        o1, o2 = (g + 1) % 3, (g + 2) % 3
        shapes = [(), ((o1, 0.2),), ((o1, -0.2),), ((o2, 0.2),), ((o2, -0.2),), ((5, 0.3),)]
        ids = []
        for i, extra in enumerate(shapes):
            ids.append(await _add(db, f"group {g} item {i}", f"2026-10-{g * 6 + i + 1:02d}T00:00:00+00:00", embedding=_axis(g, extra)))
        groups.append(ids)
    return groups


@pytest.mark.asyncio
async def test_clusters_find_the_groups_and_name_each_by_its_central_memory(db):
    groups = await _three_groups(db)
    conn = _open()
    out = overall.clusters(conn, database.DB_PATH)
    found = {}
    for n in out["nodes"]:
        drawn = overall.cluster_of(conn, database.DB_PATH, n["mem"])
        found[n["mem"]] = {m["mem"] for m in drawn["nodes"] if m["kind"] == "memory"}
    conn.close()
    assert out["groups"] == 3 and out["grouped"] == 18 and out["not_grouped"] == 0
    assert "members" not in out
    assert found == {g[0]: set(g) for g in groups}
    assert {n["label"] for n in out["nodes"]} == {f"group {g} item 0" for g in range(3)}
    assert {n["count"] for n in out["nodes"]} == {6}
    assert len(out["links"]) == 3


@pytest.mark.asyncio
async def test_the_same_store_groups_the_same_way(db):
    await _three_groups(db)
    conn = _open()
    first = overall.clusters(conn, database.DB_PATH)
    overall.clear_cache()
    again = overall.clusters(conn, database.DB_PATH)
    conn.close()
    assert first == again


@pytest.mark.asyncio
async def test_only_the_common_width_is_grouped_and_a_large_store_is_not(db, monkeypatch):
    await _three_groups(db)
    other = await _add(db, "four wide", NOW, embedding=np.ones(4, dtype=np.float32).tobytes())
    conn = _open()
    out = overall.clusters(conn, database.DB_PATH)
    stray = overall.cluster_of(conn, database.DB_PATH, other)
    overall.clear_cache()
    monkeypatch.setattr(overall, "CLUSTER_MAX_ROWS", 5)
    big = overall.clusters(conn, database.DB_PATH)
    conn.close()
    assert out["grouped"] == 18 and out["not_grouped"] == 1 and stray is None
    assert big == {"lens": "clusters", "too_many": 19, "max_rows": 5}


@pytest.mark.asyncio
async def test_a_new_memory_regroups_at_once(db):
    await _three_groups(db)
    conn = _open()
    before = overall.clusters(conn, database.DB_PATH)
    await _add(db, "one more", NOW, embedding=_axis(0, ((1, 0.05),)))
    after = overall.clusters(conn, database.DB_PATH)
    conn.close()
    assert before["grouped"] == 18 and after["grouped"] == 19


@pytest.mark.asyncio
async def test_a_points_own_drawing_keeps_the_newest(db, monkeypatch):
    groups = await _three_groups(db)
    monkeypatch.setattr(graph, "MAX_NODES", 2)
    conn = _open()
    out = graph.members(conn, groups[1], "cluster")
    conn.close()
    assert out["total"] == 6 and out["shown"] == 2
    assert {n["mem"] for n in out["nodes"] if n["kind"] == "memory"} == set(groups[1][-2:])


@pytest.fixture
def served():
    server, _ = web.make_server(database.DB_PATH, 0, token="tok")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _get(server, path):
    import http.client

    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "Authorization": "Bearer tok"})
    r = conn.getresponse()
    out = (r.status, r.read())
    conn.close()
    return out


@pytest.mark.asyncio
async def test_the_endpoints_answer_each_lens_and_each_points_drawing(db, served):
    groups = await _three_groups(db)
    lone = await _add(db, "never embedded", NOW)
    assert json.loads(_get(served, "/api/map")[1])["lens"] == "tree"
    status, body = _get(served, "/api/map?lens=clusters")
    assert status == 200 and json.loads(body)["groups"] == 3
    assert b"members" not in body and b"embedding" not in body
    assert json.loads(_get(served, "/api/map?lens=entities")[1])["nodes"] == []
    assert _get(served, "/api/map?lens=other")[0] == 400
    status, body = _get(served, f"/api/graph?cluster_of={groups[2][3]}")
    assert status == 200 and json.loads(body)["cluster"] == groups[2][0]
    assert _get(served, f"/api/graph?cluster_of={lone}")[0] == 404
    assert _get(served, "/api/graph?entity=999999")[0] == 404
