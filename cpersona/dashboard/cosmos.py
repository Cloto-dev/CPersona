"""The store as a nested sky (prototype): every level a mesh of a few dozen points.

A picture reads as a mesh while each group shows a few dozen points; one agent of
thousands does not. So the store is nested until no group holds more than ``FAN``:
the store holds its agents, an agent its projects, and a project its memories — or,
when it holds more than ``FAN``, groups of them made by content (spherical k-means
on the stored vectors, the clusters lens' method), split again until each holds at
most ``FAN``. Each group is named as the clusters lens names one: by quoting the
memory nearest its centre. No model is called and nothing is written.

The points of one group are joined to the ``NEAR`` siblings whose centres (or, for
memories, whose vectors) are nearest, so every level is drawn as a mesh of its own.
"""

import math
import os
import threading
import time

import numpy as np

from cpersona.dashboard import graph, overall, store

#: Most points one group shows before it is split by content.
FAN = 40
#: Memories a split aims at for each group it makes.
LEAF = 30
#: Siblings each point is joined to, and the cosine below which memories are not.
NEAR = 3
FLOOR = 0.55
#: Characters of the quotation that names a content group.
QUOTE_CHARS = 40

_lock = threading.Lock()
_cache: dict[str, tuple] = {}


def _signature(conn) -> tuple:
    return tuple(
        conn.execute(
            "SELECT count(*), max(id), total(id), total(length(embedding)) FROM memories"
        ).fetchone()
    )


def build(conn, db_path: str) -> dict:
    """The nested sky, from the cache while the store keeps the same rows."""
    key = os.path.abspath(db_path)
    sig = _signature(conn)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == sig and time.monotonic() - hit[1] < graph.CACHE_SECONDS:
            return hit[2]
    out = _build(conn)
    with _lock:
        _cache[key] = (sig, time.monotonic(), out)
    return out


def _build(conn) -> dict:
    rows = conn.execute(
        "SELECT id, agent_id, project_id, timestamp, embedding FROM memories ORDER BY id"
    ).fetchall()
    units = {r["id"]: graph._unit(r["embedding"]) for r in rows}
    widths: dict[int, int] = {}
    for u in units.values():
        if u is not None:
            widths[len(u)] = widths.get(len(u), 0) + 1
    # Only vectors of the most common width are compared, as in the clusters lens.
    width = max(widths, key=lambda w: (widths[w], w)) if widths else 0
    vec = {i: u for i, u in units.items() if u is not None and len(u) == width}
    quote_cache: dict[int, str] = {}

    def quote(mem_id: int) -> str:
        if mem_id not in quote_cache:
            row = conn.execute("SELECT content FROM memories WHERE id = ?", (mem_id,)).fetchone()
            text = store.snippet(row[0] if row else "")
            quote_cache[mem_id] = text[:QUOTE_CHARS] + ("…" if len(text) > QUOTE_CHARS else "")
        return quote_cache[mem_id]

    def centre(members: list) -> np.ndarray | None:
        got = [vec[r["id"]] for r in members if r["id"] in vec]
        if not got:
            return None
        m = np.sum(got, axis=0)
        n = float(np.linalg.norm(m))
        return m / n if n > 0 else None

    def joined(children: list, centres: list, floor: float | None) -> list:
        """Each child to its NEAR nearest siblings by centre, as [i, j, cosine]."""
        have = [i for i, c in enumerate(centres) if c is not None]
        if len(have) < 2:
            return []
        m = np.stack([centres[i] for i in have])
        sim = m @ m.T
        np.fill_diagonal(sim, -np.inf)
        pairs: dict[tuple[int, int], float] = {}
        for a in range(len(have)):
            for b in np.argsort(-sim[a], kind="stable")[:NEAR]:
                w = float(sim[a, b])
                if not np.isfinite(w) or (floor is not None and w < floor):
                    continue
                i, j = sorted((have[a], have[int(b)]))
                pairs[(i, j)] = max(pairs.get((i, j), -1.0), w)
        return [[i, j, round(w, 3)] for (i, j), w in sorted(pairs.items())]

    def memories(members: list) -> dict:
        kids = [{"id": f"m:{r['id']}", "kind": "memory", "mem": r["id"], "day": r["timestamp"][:10]} for r in members]
        return {"children": kids, "links": joined(kids, [vec.get(r["id"]) for r in members], FLOOR)}

    def split(members: list, prefix: str) -> dict:
        """Children for a set of memories: the memories, or content groups of them."""
        if len(members) <= FAN:
            return memories(members)
        with_vec = [r for r in members if r["id"] in vec]
        without = [r for r in members if r["id"] not in vec]
        parts: list[list] = []
        if len(with_vec) > FAN:
            x = np.stack([vec[r["id"]] for r in with_vec])
            k = min(FAN, math.ceil(len(with_vec) / LEAF))
            labels, _ = overall._kmeans(x, k)
            parts = [[with_vec[i] for i in np.flatnonzero(labels == j)] for j in range(k)]
            parts = [p for p in parts if p]
            if len(parts) < 2:  # nothing split: fall back to runs in stored order
                parts = []
        if not parts:
            ordered = with_vec + without
            without = []
            size = math.ceil(len(ordered) / math.ceil(len(ordered) / FAN))
            parts = [ordered[i : i + size] for i in range(0, len(ordered), size)]
        elif without:
            parts.append(without)
        kids, centres = [], []
        for n, part in enumerate(parts):
            c = centre(part)
            if c is not None:
                near = max(part, key=lambda r: (float(vec[r["id"]] @ c) if r["id"] in vec else -2.0, -r["id"]))
                label = quote(near["id"])
            else:
                label = ""
            node = {"id": f"{prefix}.{n}", "kind": "cluster", "label": label, "count": len(part)}
            node.update(split(part, node["id"]))
            kids.append(node)
            centres.append(c)
        return {"children": kids, "links": joined(kids, centres, None)}

    by_agent: dict[str, dict[str, list]] = {}
    for r in rows:
        by_agent.setdefault(r["agent_id"], {}).setdefault(r["project_id"], []).append(r)
    labels = store.distinct_labels(sorted(by_agent))
    agents, agent_centres = [], []
    for a in sorted(by_agent):
        projects, project_centres = [], []
        for p in sorted(by_agent[a]):
            members = by_agent[a][p]
            node = {"id": f"p:{a}/{p}", "kind": "project", "label": p, "count": len(members)}
            node.update(split(members, node["id"]))
            projects.append(node)
            project_centres.append(centre(members))
        everyone = [r for p in by_agent[a].values() for r in p]
        agents.append(
            {
                "id": f"a:{a}",
                "kind": "agent",
                "label": labels.get(a, a),
                "count": len(everyone),
                "children": projects,
                "links": joined(projects, project_centres, None),
            }
        )
        agent_centres.append(centre(everyone))
    return {
        "lens": "cosmos",
        "total": len(rows),
        "root": {
            "id": "root",
            "kind": "root",
            "count": len(rows),
            "children": agents,
            "links": joined(agents, agent_centres, None),
        },
    }


def clear_cache() -> None:
    with _lock:
        _cache.clear()
