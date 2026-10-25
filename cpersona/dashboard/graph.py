"""The memory network the dashboard draws: computed from the store, never written to it.

Three kinds of line join the points:

- **declared**: what an agent declared — the entities a memory mentions, and the
  relations between entities and memories. Drawn on top.
- **similar**: each memory to its ``K`` nearest memories by stored vector, when the
  cosine is at least ``FLOOR``. Nothing declared these; they are computed here from
  the vectors the server stored, and the page says so. A vector is compared only with
  vectors of the same width, the rule recall follows. Vectors are read to compare and
  are never sent to the page.
- **structure**: each agent to its memories.

Two views:

- **A folder** (the same path and month as the list): every memory in it, with the
  similar lines among them, as a notes app draws every note. ``MAX_NODES`` can bound
  it to the newest few; unset, nothing is left out. The neighbours are found a block
  of ``NEIGHBOUR_BLOCK`` rows at a time, so the similarities held at once grow with
  the folder, not with its square.
- **Around one memory**: the memory, its ``K`` nearest across the whole store, their
  nearest, and so on for ``hops`` steps. Each step compares the step's memories with
  every vector of that width once.

The vectors read for "around" are kept in this process for ``CACHE_SECONDS`` while
the store keeps the same rows (count, highest id and the sum of ids), so walking from
memory to memory does not read every vector again. A vector replaced in place keeps
those three numbers, so it can be seen at most ``CACHE_SECONDS`` late. A store whose
vectors are larger than ``CACHE_MAX_BYTES`` is not held: each step reads it in chunks.
"""

import json
import os
import threading
import time

import numpy as np

from cpersona.dashboard import store

#: Nearest memories each memory is joined to.
K = 3
#: Cosine below which two memories are not called similar.
FLOOR = 0.55
#: Memories a folder view draws, the newest first; None draws every one.
MAX_NODES: int | None = None
#: Rows whose neighbours are found at once: bounds the similarities held to this many
#: rows by the folder's size.
NEIGHBOUR_BLOCK = 1024
#: Steps the "around" view walks by default, and at most.
DEFAULT_HOPS = 2
MAX_HOPS = 3

CACHE_SECONDS = 300
CACHE_MAX_BYTES = 128 * 1024 * 1024
#: Rows read at a time when the vectors are not held.
SCAN_CHUNK = 4096

_lock = threading.Lock()
# absolute path -> (signature, born, {width: (ids, unit vectors)})
_cache: dict[str, tuple] = {}


def _unit(blob) -> np.ndarray | None:
    """A stored vector scaled to length 1, or None for one that cannot be compared."""
    if not blob or len(blob) % 4:
        return None
    vec = np.frombuffer(blob, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if not np.isfinite(vec).all() or norm == 0.0:
        return None
    return vec / norm


def _group(rows) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """``(id, blob)`` rows grouped by vector width, as (ids, unit vectors)."""
    by_width: dict[int, tuple[list, list]] = {}
    for rid, blob in rows:
        vec = _unit(blob)
        if vec is None:
            continue
        ids, vecs = by_width.setdefault(len(vec), ([], []))
        ids.append(rid)
        vecs.append(vec)
    return {
        w: (np.asarray(ids, dtype=np.int64), np.stack(vecs))
        for w, (ids, vecs) in by_width.items()
    }


def _signature(conn) -> tuple:
    return tuple(
        conn.execute(
            "SELECT count(*), max(id), total(id), total(length(embedding)) "
            "FROM memories WHERE embedding IS NOT NULL"
        ).fetchone()
    )


def _held(conn, db_path: str) -> dict | None:
    """Every vector of the store by width, from the cache when it is still the same store."""
    key = os.path.abspath(db_path)
    sig = _signature(conn)
    if sig[3] > CACHE_MAX_BYTES:
        return None
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == sig[:3] and time.monotonic() - hit[1] < CACHE_SECONDS:
            return hit[2]
        groups = _group(
            conn.execute("SELECT id, embedding FROM memories WHERE embedding IS NOT NULL")
        )
        _cache[key] = (sig[:3], time.monotonic(), groups)
        return groups


def _chunks(conn, db_path: str, width: int):
    """(ids, unit vectors) of one width, held or read a chunk at a time."""
    held = _held(conn, db_path)
    if held is not None:
        if width in held:
            yield held[width]
        return
    after = -1
    while True:
        rows = conn.execute(
            "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL "
            "AND length(embedding) = ? AND id > ? ORDER BY id LIMIT ?",
            (width * 4, after, SCAN_CHUNK),
        ).fetchall()
        if not rows:
            return
        after = rows[-1][0]
        got = _group(rows).get(width)
        if got is not None:
            yield got


def _nearest_within(
    ids: np.ndarray, vecs: np.ndarray, mutual: bool = False
) -> dict[tuple[int, int], float]:
    """Similar pairs inside one set: each row to its K nearest at or above FLOOR.

    Equal similarities go to the earlier row, whatever the block size. ``mutual``
    (prototype) keeps a pair only when each of the two chose the other.
    """
    pairs: dict[tuple[int, int], float] = {}
    chose: dict[tuple[int, int], int] = {}
    n = len(ids)
    take = min(K, n - 1)
    if take <= 0:
        return pairs
    for at in range(0, n, NEIGHBOUR_BLOCK):
        sim = vecs[at : at + NEIGHBOUR_BLOCK] @ vecs.T
        here = np.arange(len(sim))
        sim[here, at + here] = -np.inf
        # Every column as similar as the K-th best is a candidate, so a tie at the cut
        # is settled by position below, not by how the partition fell.
        cut = -np.partition(-sim, take - 1, axis=1)[:, take - 1]
        for r in here:
            cand = np.flatnonzero(sim[r] >= cut[r])
            for j in sorted(cand, key=lambda c: (-sim[r, c], c))[:K]:
                w = float(sim[r, j])
                if w < FLOOR:
                    break
                a, b = sorted((int(ids[at + r]), int(ids[j])))
                pairs[(a, b)] = max(pairs.get((a, b), -1.0), w)
                chose[(a, b)] = chose.get((a, b), 0) + 1
    if mutual:
        return {k: w for k, w in pairs.items() if chose[k] == 2}
    return pairs


def _nearest_across(conn, db_path, seeds: list[int], width: int, seed_vecs) -> dict:
    """For each seed, its K nearest memories of the whole store at or above FLOOR."""
    best: dict[int, list[tuple[float, int]]] = {s: [] for s in seeds}
    for ids, vecs in _chunks(conn, db_path, width):
        sim = vecs @ seed_vecs.T
        for col, seed in enumerate(seeds):
            column = sim[:, col]
            take = min(K + 1, len(column))
            for j in np.argpartition(-column, take - 1)[:take]:
                if int(ids[j]) != seed and column[j] >= FLOOR:
                    best[seed].append((float(column[j]), int(ids[j])))
    # Highest first; equal scores by the lower id, so the answer does not depend on chunks.
    return {
        s: sorted(c, key=lambda x: (-x[0], x[1]))[:K] for s, c in best.items()
    }


def _declared(conn, mem_ids: set[int]) -> tuple[list[dict], list[dict]]:
    """Entity points and declared lines that touch the shown memories."""
    if not mem_ids or not store._has_table(conn, "entity_mentions"):
        return [], []
    refs = [f"mem:{i}" for i in sorted(mem_ids)]
    marks = ",".join("?" * len(refs))
    mentions = conn.execute(
        f"SELECT entity_id, ref FROM entity_mentions WHERE ref IN ({marks})", refs
    ).fetchall()
    ent_ids = sorted({m[0] for m in mentions})
    entities = []
    if ent_ids:
        emarks = ",".join("?" * len(ent_ids))
        entities = conn.execute(
            f"SELECT id, name, agent_id FROM entities WHERE id IN ({emarks}) ORDER BY id",
            ent_ids,
        ).fetchall()
    known = {e[0] for e in entities}
    links = [
        {"s": f"e:{m[0]}", "t": "m:" + m[1][4:], "kind": "declared", "label": "mentions"}
        for m in mentions
        if m[0] in known
    ]
    ends = {("entity", i) for i in known} | {("mem", i) for i in mem_ids}
    # Through the subject index: a relation is drawn only when both of its ends are.
    ent_list, mem_list = sorted(known), sorted(mem_ids)
    for r in conn.execute(
        "SELECT subject_kind, subject_id, predicate, object_kind, object_id FROM relations "
        f"WHERE (subject_kind = 'entity' AND subject_id IN ({','.join('?' * len(ent_list))})) "
        f"OR (subject_kind = 'mem' AND subject_id IN ({','.join('?' * len(mem_list))})) "
        "ORDER BY id",
        [*ent_list, *mem_list],
    ):
        a, b = (r[0], r[1]), (r[3], r[4])
        if a in ends and b in ends:
            name = {"entity": "e", "mem": "m"}
            links.append(
                {
                    "s": f"{name[a[0]]}:{a[1]}",
                    "t": f"{name[b[0]]}:{b[1]}",
                    "kind": "declared",
                    "label": r[2],
                }
            )
    nodes = [
        {"id": f"e:{e[0]}", "kind": "entity", "entity": e[0], "label": e[1], "agent": e[2]}
        for e in entities
    ]
    return nodes, links


def _points(conn, rows, similar: dict, hops: dict | None = None, hubs: bool = False) -> dict:
    """The page's nodes and links for these memory rows and their similar pairs.

    ``hubs`` (prototype) puts each agent's projects between the agent and its
    memories, as a notes app's index notes gather the notes they link to.
    """
    agents = sorted({r["agent_id"] for r in rows})
    labels = store.distinct_labels(agents)
    nodes = [
        {"id": f"a:{a}", "kind": "agent", "agent": a, "label": labels.get(a, a)}
        for a in agents
    ]
    project_of = {}
    if hubs:
        got = conn.execute(
            "SELECT id, project_id FROM memories WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps([r["id"] for r in rows]),),
        ).fetchall()
        project_of = {g[0]: g[1] for g in got}
        counts: dict[tuple[str, str], int] = {}
        for r in rows:
            k = (r["agent_id"], project_of.get(r["id"], ""))
            counts[k] = counts.get(k, 0) + 1
        nodes += [
            {"id": f"p:{a}/{p}", "kind": "project", "agent": a, "label": p, "count": n}
            for (a, p), n in sorted(counts.items())
        ]
    for r in rows:
        node = {
            "id": f"m:{r['id']}",
            "kind": "memory",
            "mem": r["id"],
            "agent": r["agent_id"],
            "timestamp": r["timestamp"],
        }
        if hops is not None:
            node["hop"] = hops[r["id"]]
        nodes.append(node)
    if hubs:
        links = [
            {"s": f"a:{a}", "t": f"p:{a}/{p}", "kind": "structure"}
            for (a, p) in sorted({(r["agent_id"], project_of.get(r["id"], "")) for r in rows})
        ] + [
            {"s": f"p:{r['agent_id']}/{project_of.get(r['id'], '')}", "t": f"m:{r['id']}", "kind": "structure"}
            for r in rows
        ]
    else:
        links = [
            {"s": f"a:{r['agent_id']}", "t": f"m:{r['id']}", "kind": "structure"}
            for r in rows
        ]
    links += [
        {"s": f"m:{a}", "t": f"m:{b}", "kind": "similar", "w": round(w, 3)}
        for (a, b), w in sorted(similar.items())
    ]
    ent_nodes, declared = _declared(conn, {r["id"] for r in rows})
    return {"nodes": nodes + ent_nodes, "links": links + declared}


def folder(conn, path: list[str] | None = None, month: str = "", **proto) -> dict:
    """The memories of a folder and month (the newest MAX_NODES, when set), joined as above."""
    where, args = store._path_where(path or [])
    if month:
        where.append("substr(timestamp, 1, 7) = ?")
        args.append(month)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT count(*) FROM memories{clause}", args).fetchone()[0]
    rows = conn.execute(
        "SELECT id, agent_id, timestamp, embedding FROM memories"
        f"{clause} ORDER BY timestamp DESC, id DESC LIMIT ?",
        [*args, -1 if MAX_NODES is None else MAX_NODES],
    ).fetchall()
    return _drawn(conn, rows, total, "folder", **proto)


def _drawn(conn, rows, total: int, view: str, mutual: bool = False, hubs: bool = False, **extra) -> dict:
    """A set of memory rows drawn with the similar lines among them."""
    similar: dict[tuple[int, int], float] = {}
    groups = _group((r["id"], r["embedding"]) for r in rows)
    for ids, vecs in groups.values():
        similar.update(_nearest_within(ids, vecs, mutual))
    compared = sum(len(ids) for ids, _ in groups.values())
    return {
        "view": view,
        "total": total,
        "shown": len(rows),
        "without_vector": len(rows) - compared,
        "max_nodes": MAX_NODES,
        "k": K,
        "floor": FLOOR,
        **extra,
        **_points(conn, rows, similar, hubs=hubs),
    }


#: Ids asked for at a time, under SQLite's oldest limit on bound parameters.
_ID_BATCH = 900


def members(conn, ids: list[int], view: str, **extra) -> dict:
    """These memories (the newest MAX_NODES, when set), drawn as a folder is."""
    dated = []
    for at in range(0, len(ids), _ID_BATCH):
        part = ids[at : at + _ID_BATCH]
        dated += conn.execute(
            f"SELECT id, timestamp FROM memories WHERE id IN ({','.join('?' * len(part))})",
            part,
        ).fetchall()
    dated.sort(key=lambda r: (r[1], r[0]), reverse=True)
    keep = [r[0] for r in dated[:MAX_NODES]]
    rows = []
    # Batched like the ids above: undrawn by a cap, a group can hold thousands.
    for at in range(0, len(keep), _ID_BATCH):
        part = keep[at : at + _ID_BATCH]
        rows += conn.execute(
            "SELECT id, agent_id, timestamp, embedding FROM memories "
            f"WHERE id IN ({','.join('?' * len(part))})",
            part,
        ).fetchall()
    rows.sort(key=lambda r: (r["timestamp"], r["id"]), reverse=True)
    return _drawn(conn, rows, len(dated), view, **extra)


def mentioning(conn, entity_id: int) -> dict | None:
    """The memories that mention one entity, with the entity among them."""
    entity = conn.execute(
        "SELECT id, name FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if entity is None:
        return None
    ids = [
        int(r[0][4:])
        for r in conn.execute(
            "SELECT ref FROM entity_mentions WHERE entity_id = ? AND ref LIKE 'mem:%'",
            (entity_id,),
        )
        if r[0][4:].isdigit()
    ]
    return members(conn, ids, "entity", entity=entity_id, label=entity["name"])


def around(conn, db_path: str, memory_id: int, hops: int = DEFAULT_HOPS) -> dict | None:
    """One memory and the memories reached from it in ``hops`` nearest-neighbour steps."""
    hops = max(1, min(int(hops), MAX_HOPS))
    center = conn.execute(
        "SELECT id, embedding FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if center is None:
        return None
    reached = {memory_id: 0}
    similar: dict[tuple[int, int], float] = {}
    start = _unit(center["embedding"])
    if start is not None:
        width, frontier, frontier_vecs = len(start), [memory_id], start[None, :]
        for step in range(1, hops + 1):
            found = _nearest_across(conn, db_path, frontier, width, frontier_vecs)
            nxt = []
            for seed, near in found.items():
                for w, other in near:
                    a, b = sorted((seed, other))
                    similar[(a, b)] = max(similar.get((a, b), -1.0), w)
                    if other not in reached:
                        reached[other] = step
                        nxt.append(other)
            if not nxt:
                break
            marks = ",".join("?" * len(nxt))
            got = {
                r[0]: r[1]
                for r in conn.execute(
                    f"SELECT id, embedding FROM memories WHERE id IN ({marks})", nxt
                )
            }
            frontier = [i for i in nxt if _unit(got.get(i)) is not None]
            if not frontier:
                break
            frontier_vecs = np.stack([_unit(got[i]) for i in frontier])
    marks = ",".join("?" * len(reached))
    rows = conn.execute(
        f"SELECT id, agent_id, timestamp FROM memories WHERE id IN ({marks}) "
        "ORDER BY timestamp DESC, id DESC",
        list(reached),
    ).fetchall()
    return {
        "view": "around",
        "center": memory_id,
        "hops": hops,
        "has_vector": start is not None,
        "k": K,
        "floor": FLOOR,
        **_points(conn, rows, similar, reached),
    }


def clear_cache() -> None:
    """Drop the held vectors (tests, and a store that was replaced on disk)."""
    with _lock:
        _cache.clear()
