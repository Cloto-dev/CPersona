"""The whole store in one picture: memories grouped, never drawn one by one.

A store of thousands of memories cannot be drawn a point per memory, so the whole
picture draws groups, through one of three lenses:

- **tree** (the default): the logical directory as points — agent, project,
  channel, month — each as large as the memories it holds, joined parent to child.
  Nothing is computed beyond the counts.
- **entities**: the names agents declared, each as large as the memories that
  mention it, joined by the relations declared between them and by being mentioned
  in the same memory.
- **clusters**: memories grouped by their stored vectors (spherical k-means, seeded,
  so the same store groups the same way). A group is named by quoting the start of
  the memory nearest its centre — a quotation, not a summary: no model is called.
  Groups are joined to the groups whose centres are nearest. Only vectors of the
  most common width are grouped; a store with more than ``CLUSTER_MAX_ROWS`` of
  them is not grouped until the cost at that size has been measured.

Each point opens a drawing of its own memories (``graph``), and each memory there
opens the walk around it. Nothing is written to the store.
"""

import json
import math
import os
import threading
import time

import numpy as np

from cpersona.dashboard import graph, store

LENSES = ("tree", "entities", "clusters")

#: Points the tree lens draws before it leaves the months out.
TREE_MAX_NODES = 1_500
#: Entities the entities lens draws, the most mentioned first.
ENTITY_MAX_NODES = 800
#: Vectors the clusters lens groups. Above it the lens says so instead.
CLUSTER_MAX_ROWS = 20_000
#: Groups at most, whatever the store's size.
MAX_GROUPS = 300
ITERATIONS = 25
SEED = 0
#: Starts tried. One unlucky start can put two seeds in one group and merge two
#: others, which no number of iterations undoes; the tightest of several is kept.
STARTS = 4
#: Groups each group is joined to.
NEAR = 3
#: Characters of the quotation that names a group.
QUOTE_CHARS = 90

_lock = threading.Lock()
# absolute path -> (signature, born, result with its membership)
_cache: dict[str, tuple] = {}


def tree(conn) -> dict:
    """The logical directory as points, down to the month."""
    rows = conn.execute(
        "SELECT agent_id, project_id, channel, substr(timestamp, 1, 4) AS y, "
        "substr(timestamp, 6, 2) AS m, count(*) AS n FROM memories "
        "GROUP BY agent_id, project_id, channel, y, m"
    ).fetchall()
    counts: dict[tuple, int] = {}
    for r in rows:
        full = (r["agent_id"], r["project_id"], r["channel"], r["y"], r["m"])
        # A month point stands for its year and month together: [agent, project, channel, year, month].
        for depth in (1, 2, 3, 5):
            counts[full[:depth]] = counts.get(full[:depth], 0) + r["n"]
    months = sum(len(p) == 5 for p in counts)
    with_months = len(counts) <= TREE_MAX_NODES
    labels = store.distinct_labels(sorted({p[0] for p in counts}))
    nodes, links = [], []
    level = {1: "agent", 2: "project", 3: "channel", 5: "month"}
    for path in sorted(counts):
        if len(path) == 5 and not with_months:
            continue
        nodes.append(
            {
                "id": "t:" + json.dumps(list(path)),
                "kind": "group",
                "level": level[len(path)],
                "key": f"{path[3]}-{path[4]}" if len(path) == 5 else path[-1],
                "label": labels.get(path[0], path[0]) if len(path) == 1 else None,
                "path": list(path),
                "count": counts[path],
                "agent": path[0],
            }
        )
        if len(path) > 1:
            parent = path[:3] if len(path) == 5 else path[:-1]
            links.append(
                {
                    "s": "t:" + json.dumps(list(parent)),
                    "t": "t:" + json.dumps(list(path)),
                    "kind": "part",
                }
            )
    return {
        "lens": "tree",
        "total": sum(r["n"] for r in rows),
        "months": months,
        "months_shown": with_months,
        "nodes": nodes,
        "links": links,
    }


def entities(conn) -> dict:
    """Declared entities, joined by declared relations and by shared mentions."""
    found = conn.execute(
        "SELECT e.id, e.name, e.agent_id, count(m.ref) AS n FROM entities e "
        "LEFT JOIN entity_mentions m ON m.entity_id = e.id AND m.ref LIKE 'mem:%' "
        "GROUP BY e.id ORDER BY n DESC, e.id LIMIT ?",
        (ENTITY_MAX_NODES,),
    ).fetchall()
    shown = {r["id"] for r in found}
    nodes = [
        {
            "id": f"e:{r['id']}",
            "kind": "entity",
            "entity": r["id"],
            "label": r["name"],
            "agent": r["agent_id"],
            "count": r["n"],
        }
        for r in found
    ]
    links = [
        {"s": f"e:{r[0]}", "t": f"e:{r[1]}", "kind": "declared", "label": r[2]}
        for r in conn.execute(
            "SELECT subject_id, object_id, predicate FROM relations "
            "WHERE subject_kind = 'entity' AND object_kind = 'entity' ORDER BY id"
        )
        if r[0] in shown and r[1] in shown
    ]
    links += [
        {"s": f"e:{r[0]}", "t": f"e:{r[1]}", "kind": "co", "w": r[2]}
        for r in conn.execute(
            "SELECT a.entity_id, b.entity_id, count(*) FROM entity_mentions a "
            "JOIN entity_mentions b ON a.ref = b.ref AND a.entity_id < b.entity_id "
            "WHERE a.ref LIKE 'mem:%' GROUP BY a.entity_id, b.entity_id "
            "ORDER BY a.entity_id, b.entity_id"
        )
        if r[0] in shown and r[1] in shown
    ]
    total = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
    return {"lens": "entities", "total": total, "nodes": nodes, "links": links}


def _kmeans(x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Spherical k-means on unit rows: (label of each row, unit centres).

    Seeded k-means++, so the same rows in the same order give the same groups. Of
    ``STARTS`` starts, the one whose rows sit closest to their centres is kept.
    """
    rng = np.random.default_rng(SEED)
    best, best_score = None, -np.inf
    for _ in range(STARTS):
        labels, c = _kmeans_once(x, k, rng)
        score = float((x @ c.T)[np.arange(len(x)), labels].sum())
        if score > best_score:
            best, best_score = (labels, c), score
    return best


def _kmeans_once(x: np.ndarray, k: int, rng) -> tuple[np.ndarray, np.ndarray]:
    n = len(x)
    centres = [x[int(rng.integers(n))]]
    gap = 1.0 - x @ centres[0]
    for _ in range(1, k):
        weights = np.clip(gap, 0.0, None)
        total = float(weights.sum())
        pick = int(rng.choice(n, p=weights / total)) if total > 0 else int(rng.integers(n))
        centres.append(x[pick])
        gap = np.minimum(gap, 1.0 - x @ x[pick])
    c = np.stack(centres)
    labels = np.full(n, -1)
    for _ in range(ITERATIONS):
        new = np.argmax(x @ c.T, axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            members = x[labels == j]
            if len(members):
                mean = members.sum(axis=0)
                norm = float(np.linalg.norm(mean))
                if norm > 0:
                    c[j] = mean / norm
    return labels, c


def _grouped(conn, db_path: str) -> dict:
    """The clusters of the store, from the cache while the store keeps its rows."""
    key = os.path.abspath(db_path)
    sig = graph._signature(conn)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == sig[:3] and time.monotonic() - hit[1] < graph.CACHE_SECONDS:
            return hit[2]
    if sig[0] > CLUSTER_MAX_ROWS:
        out = {"lens": "clusters", "too_many": sig[0], "max_rows": CLUSTER_MAX_ROWS}
    else:
        out = _cluster(conn)
    with _lock:
        _cache[key] = (sig[:3], time.monotonic(), out)
    return out


def _cluster(conn) -> dict:
    groups = graph._group(
        conn.execute(
            "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL ORDER BY id"
        )
    )
    embedded = conn.execute(
        "SELECT count(*) FROM memories WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    if not groups:
        return {"lens": "clusters", "total": 0, "grouped": 0, "not_grouped": embedded, "nodes": [], "links": [], "members": {}}
    # The most common width; ties go to the wider vector.
    width = max(groups, key=lambda w: (len(groups[w][0]), w))
    ids, x = groups[width]
    k = max(1, min(MAX_GROUPS, len(ids), round(math.sqrt(len(ids) / 2))))
    labels, centres = _kmeans(x, k)
    agent_of = dict(
        conn.execute("SELECT id, agent_id FROM memories WHERE embedding IS NOT NULL")
    )
    nodes, members, kept = [], {}, []
    for j in range(k):
        idx = np.flatnonzero(labels == j)
        if not len(idx):
            continue
        # The member nearest the centre names the group; equal scores go to the lower id.
        near = int(idx[int(np.argmax(x[idx] @ centres[j]))])
        head = int(ids[near])
        row = conn.execute(
            "SELECT content FROM memories WHERE id = ?", (head,)
        ).fetchone()
        quote = store.snippet(row[0] if row else "")
        agents: dict[str, int] = {}
        for i in idx:
            a = agent_of.get(int(ids[i]), "")
            agents[a] = agents.get(a, 0) + 1
        top = sorted(agents.items(), key=lambda kv: (-kv[1], kv[0]))
        nodes.append(
            {
                "id": f"c:{head}",
                "kind": "cluster",
                "mem": head,
                "label": quote[:QUOTE_CHARS] + ("…" if len(quote) > QUOTE_CHARS else ""),
                "quote": quote,
                "count": len(idx),
                "agent": top[0][0],
                "agents": dict(top),
            }
        )
        members[head] = [int(ids[i]) for i in idx]
        kept.append(j)
    sims = centres[kept] @ centres[kept].T
    np.fill_diagonal(sims, -np.inf)
    pairs: dict[tuple[str, str], float] = {}
    for a in range(len(kept)):
        for b in np.argsort(-sims[a], kind="stable")[:NEAR]:
            if not np.isfinite(sims[a, b]):
                continue
            key = tuple(sorted((nodes[a]["id"], nodes[int(b)]["id"])))
            pairs[key] = max(pairs.get(key, -1.0), float(sims[a, b]))
    links = [
        {"s": s, "t": t, "kind": "near", "w": round(w, 3)}
        for (s, t), w in sorted(pairs.items())
    ]
    return {
        "lens": "clusters",
        "total": embedded,
        "grouped": len(ids),
        "not_grouped": embedded - len(ids),
        "groups": len(nodes),
        "nodes": nodes,
        "links": links,
        "members": members,
    }


def clusters(conn, db_path: str) -> dict:
    """The clusters lens, without the membership the page does not need."""
    out = _grouped(conn, db_path)
    return {k: v for k, v in out.items() if k != "members"}


def cluster_of(conn, db_path: str, memory_id: int) -> dict | None:
    """The memories of the group that holds ``memory_id``, drawn as a folder is."""
    out = _grouped(conn, db_path)
    for head, ids in out.get("members", {}).items():
        if memory_id in ids:
            node = next(n for n in out["nodes"] if n["mem"] == head)
            return graph.members(
                conn, ids, "cluster", cluster=head, label=node["label"]
            )
    return None


def clear_cache() -> None:
    with _lock:
        _cache.clear()
