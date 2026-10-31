"""Read a CPersona database for the dashboard, and never write it.

The read-only contract:

- The file is opened with SQLite's ``mode=ro``, so a write is refused by SQLite
  itself ("attempt to write a readonly database"). Nothing here checkpoints,
  vacuums or sets a writing pragma, and a missing file is reported, never
  created.
- A database whose schema this reader was not built for is refused before
  anything is read: a newer one may have moved the columns read here, and an
  older one may lack them. Guessing would show wrong numbers without saying so.
- Embedding vectors are not read here, only whether a row has one. The network
  view (`graph.py`) reads them to compare memories and never sends them to the page.

A reader running beside a live server sees each committed write: in WAL mode a
read-only connection reads the last commit, and each request opens its own
connection, so a schema migration made while the dashboard is open is seen (and
refused, if it leaves the supported range) on the next request.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cpersona.config import MAX_PROFILE_LENGTH
from cpersona.database import SCHEMA_VERSION

#: The oldest schema this reader has been run against. Every column it reads
#: exists from here on.
MIN_SCHEMA = 19
#: The newest is the one this build writes; a newer database comes from a later
#: release, whose layout this reader cannot know.
MAX_SCHEMA = SCHEMA_VERSION

#: The levels of the logical directory, in order, and the expression each groups by.
#: The same projection `list_memories` gives an agent with a ``path``.
LEVELS = ("agent", "project", "channel", "year", "month")
_LEVEL_SQL = (
    "agent_id",
    "project_id",
    "channel",
    "substr(timestamp, 1, 4)",
    "substr(timestamp, 6, 2)",
)

#: A page of the list. The page asks for less; the ceiling bounds what one request reads.
MAX_LIMIT = 200
#: Characters of a record shown in the list.
SNIPPET_CHARS = 220
#: Characters kept before a match when the list shows where a search matched.
SNIPPET_LEAD = 70
#: The trigram index cannot match a query shorter than three characters (a
#: two-character Japanese word, for one), so those are searched by substring.
FTS_MIN_CHARS = 3

# Lines that only stamp a record ("Session date: ..."). Skipped at the start of a
# preview, so rows that all begin with the same stamp can be told apart.
_STAMP_LINE = re.compile(r"^\s*(session date|date|time|timestamp)\s*:", re.IGNORECASE)


class StoreError(Exception):
    """The database cannot be read safely; the message says why, for a person."""


def open_store(path: str) -> sqlite3.Connection:
    """Open ``path`` read-only and check that this reader understands its schema."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise StoreError(f"No database at {file}.")
    conn = sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0]
    except sqlite3.DatabaseError as e:
        conn.close()
        raise StoreError(f"{file} is not a CPersona database ({e}).") from None
    if version is None or version < MIN_SCHEMA:
        conn.close()
        raise StoreError(
            f"{file} uses schema {version}, older than this dashboard reads ({MIN_SCHEMA}). "
            "Start the CPersona server on it once to bring it up to date."
        )
    if version > MAX_SCHEMA:
        conn.close()
        raise StoreError(
            f"{file} uses schema {version}, newer than this dashboard reads ({MAX_SCHEMA}). "
            "Update cpersona to read it."
        )
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    return (
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if _has_table(conn, table)
        else 0
    )


def _path_where(path: list[str], prefix: str = "") -> tuple[list[str], list[str]]:
    if len(path) > len(LEVELS) or not all(isinstance(p, str) for p in path):
        raise ValueError(f"a path is up to {len(LEVELS)} strings: {', '.join(LEVELS)}")
    where, args = [], []
    for expr, value in zip(_LEVEL_SQL, path):
        where.append(
            expr.replace("timestamp", f"{prefix}timestamp")
            if "timestamp" in expr
            else f"{prefix}{expr}"
        )
        where[-1] += " = ?"
        args.append(value)
    return where, args


def distinct_labels(keys: list[str]) -> dict[str, str]:
    """Short labels that still tell sibling keys apart.

    Ids made by one tool often share a long prefix and suffix
    (``locomo_exp_user_0_speaker_a_x2611``, ``..._user_1_...``), and cutting them
    to a width keeps the shared part and drops the part that differs. This keeps
    the differing part with one word of context on each side, cut at ``_``, and
    leaves keys alone when they share little.
    """
    if len(keys) < 2:
        return {k: k for k in keys}
    pre = os.path.commonprefix(keys)
    suf = os.path.commonprefix([k[::-1] for k in keys])[::-1]
    if len(pre) + len(suf) < 12:
        return {k: k for k in keys}
    # Back the prefix up past one "_" boundary, and the suffix forward past one.
    cut_pre = pre.rfind("_", 0, max(pre.rfind("_"), 0)) + 1
    after = suf.find("_", suf.find("_") + 1)
    cut_suf = len(suf) if after < 0 else len(suf) - after
    out = {}
    for k in keys:
        core = k[cut_pre : len(k) - cut_suf]
        if not core:
            out[k] = k
            continue
        out[k] = ("…" if cut_pre else "") + core + ("…" if cut_suf else "")
    return out


def snippet(content: str, query: str = "") -> str:
    """The part of a record the list shows: around the match, else its start."""
    lines = content.splitlines()
    while lines and (not lines[0].strip() or _STAMP_LINE.match(lines[0])):
        lines.pop(0)
    text = " ".join(" ".join(lines).split())
    if query:
        at = text.lower().find(query.lower())
        if at >= 0:
            start = max(0, at - SNIPPET_LEAD)
            if start:
                space = text.find(" ", start)
                start = space + 1 if 0 <= space < at else start
            end = min(len(text), at + len(query) + SNIPPET_CHARS - SNIPPET_LEAD)
            return (
                ("… " if start else "")
                + text[start:end]
                + (" …" if end < len(text) else "")
            )
    return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")


#: The files a recall reads beside the store, by what they index.
INDEX_FILES = (("memories", ".memories.vecindex"), ("episodes", ".episodes.vecindex"), ("blocks", ".blocks.blockindex"))
#: What the store-status page shows of the calibration file's values for every agent.
CALIBRATION_KEYS = (
    "embedding_model", "embedding_dim", "scoring_version", "global_threshold",
    "global_fused_gate", "fused_gate_signal", "calibrated_at",
)


def _size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _calibration(db_path: str, scope) -> dict | None:
    """The calibration file beside the store, or None when nothing was measured.

    Its values for every agent are shown only to a viewer who reads every agent;
    each agent's own values only to a viewer who reads that agent."""
    try:
        with open(db_path + ".calibration.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    if not isinstance(data, dict):
        return {"unreadable": True}
    readable = (lambda a: True) if scope is None else scope.readable
    thresholds, gates = data.get("agent_thresholds") or {}, data.get("agent_fused_gates") or {}
    out = {k: data.get(k) for k in CALIBRATION_KEYS} if scope is None or scope.everything else {}
    out["agents"] = [
        {"agent": a, "threshold": thresholds.get(a), "fused_gate": gates.get(a)}
        for a in sorted(set(thresholds) | set(gates)) if readable(a)
    ]
    return out


def state(conn: sqlite3.Connection, db_path: str, scope=None) -> dict:
    """What the store says about itself, read without the server: the records a
    vector search cannot reach (no embedding), the files beside it, and the
    calibration it keeps. The counts follow the viewer's narrowing; the files, which
    describe every agent's records at once, are shown only to a viewer who reads
    every agent."""

    def missing(table: str) -> dict:
        total, without = conn.execute(f"SELECT count(*), count(*) - count(embedding) FROM {table}").fetchone()
        return {"total": total, "without_embedding": without}

    out = {
        "memories": missing("memories"),
        "episodes": missing("episodes"),
        "schema": conn.execute("SELECT max(version) FROM schema_version").fetchone()[0],
        "calibration": _calibration(db_path, scope),
    }
    if scope is None or scope.everything:
        written = [os.path.getmtime(p) for p in (db_path, db_path + "-wal") if os.path.exists(p)]
        out["files"] = {
            "size": _size(db_path),
            "wal": _size(db_path + "-wal"),
            "last_write": datetime.fromtimestamp(max(written), timezone.utc).isoformat() if written else None,
            "indexes": {kind: _size(db_path + suffix) for kind, suffix in INDEX_FILES},
        }
    return out


#: How many memories the overview lists as the latest stored and the most recalled.
OVERVIEW_ROWS = 5


def _overview_rows(conn: sqlite3.Connection, where: str, order: str) -> list[dict]:
    """A few memories for the overview. The ids are chosen first and only theirs are
    read in full, so a large store does not load every row's text to keep five."""
    outer = ", ".join("m." + part.strip() for part in order.split(","))
    rows = conn.execute(
        "SELECT m.id, m.agent_id, m.timestamp, m.content, m.recall_count FROM memories m"
        f" JOIN (SELECT id FROM memories {where} ORDER BY {order} LIMIT ?) t ON t.id = m.id ORDER BY {outer}",
        (OVERVIEW_ROWS,),
    ).fetchall()
    return [
        {"id": r["id"], "agent_id": r["agent_id"], "timestamp": r["timestamp"], "recall_count": r["recall_count"], "snippet": snippet(r["content"])}
        for r in rows
    ]


def overview(conn: sqlite3.Connection, name: str) -> dict:
    """What the store holds and when it was written, with the latest memories and
    the ones recalled most."""
    span = conn.execute(
        "SELECT min(timestamp), max(timestamp) FROM memories"
    ).fetchone()
    months = conn.execute(
        "SELECT substr(timestamp, 1, 7) AS m, count(*) AS n FROM memories GROUP BY m ORDER BY m"
    ).fetchall()
    agents = conn.execute(
        "SELECT agent_id, count(*) AS n, max(timestamp) AS last FROM memories GROUP BY agent_id ORDER BY n DESC, agent_id"
    ).fetchall()
    return {
        "db": name,
        "schema": conn.execute("SELECT max(version) FROM schema_version").fetchone()[0],
        "counts": {
            "memories": _count(conn, "memories"),
            "episodes": _count(conn, "episodes"),
            "entities": _count(conn, "entities"),
            "profiles": _count(conn, "profiles"),
        },
        "span": [span[0], span[1]],
        "months": [{"month": r["m"], "count": r["n"]} for r in months],
        "agents": [
            {"id": r["agent_id"], "count": r["n"], "last": r["last"]} for r in agents
        ],
        "recent": _overview_rows(conn, "", "timestamp DESC, id DESC"),
        # A memory never recalled is not "recalled most", however few have been.
        "recalled": _overview_rows(conn, "WHERE recall_count > 0", "recall_count DESC, timestamp DESC, id DESC"),
    }


def tree(conn: sqlite3.Connection, path: list[str]) -> list[dict]:
    """The children of one folder of the logical directory, with their counts."""
    depth = len(path)
    if depth >= len(LEVELS):
        return []
    where, args = _path_where(path)
    sql = f"SELECT {_LEVEL_SQL[depth]} AS k, count(*) AS n FROM memories"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Agents, projects and channels by size; years and months newest first.
    sql += " GROUP BY k ORDER BY " + ("k DESC" if depth >= 3 else "n DESC, k")
    rows = conn.execute(sql, args).fetchall()
    labels = distinct_labels([r["k"] for r in rows if r["k"]])
    return [
        {
            "key": r["k"],
            "label": labels.get(r["k"], r["k"]),
            "count": r["n"],
            "level": LEVELS[depth],
        }
        for r in rows
    ]


def memories(
    conn: sqlite3.Connection,
    query: str = "",
    path: list[str] | None = None,
    month: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """One page of memories, newest first, within a folder, a month and a search."""
    where, args = _path_where(path or [], "m.")
    if month:
        where.append("substr(m.timestamp, 1, 7) = ?")
        args.append(month)
    sql = "SELECT m.id, m.agent_id, m.project_id, m.channel, m.timestamp, m.content, m.recall_count, m.locked FROM memories m"
    query = query.strip()
    if query and len(query) >= FTS_MIN_CHARS:
        sql += " JOIN memories_fts f ON f.rowid = m.id"
        where.append("memories_fts MATCH ?")
        args.append('"' + query.replace('"', '""') + '"')
    elif query:
        where.append("instr(lower(m.content), lower(?)) > 0")
        args.append(query)
    if where:
        sql += " WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT count(*) FROM ({sql})", args).fetchone()[0]
    limit = max(1, min(int(limit), MAX_LIMIT))
    rows = conn.execute(
        sql + " ORDER BY m.timestamp DESC, m.id DESC LIMIT ? OFFSET ?",
        [*args, limit, max(0, int(offset))],
    )
    return {
        "total": total,
        "rows": [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "project_id": r["project_id"],
                "channel": r["channel"],
                "timestamp": r["timestamp"],
                "snippet": snippet(r["content"], query),
                "recall_count": r["recall_count"],
                "locked": bool(r["locked"]),
            }
            for r in rows
        ],
    }


def memory(conn: sqlite3.Connection, memory_id: int) -> dict | None:
    """One memory in full, with where it is split, but never its vector."""
    r = conn.execute(
        "SELECT id, agent_id, project_id, channel, msg_id, content, source, timestamp, metadata, recall_count, "
        "last_recalled_at, locked, embedding_model, created_at, embedding IS NOT NULL AS has_embedding "
        "FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["locked"] = bool(out["locked"])
    out["has_embedding"] = bool(out["has_embedding"])
    out["nodes"] = [
        dict(n)
        for n in conn.execute(
            "SELECT node_index, start_char, end_char, token_count FROM record_nodes "
            "WHERE parent_kind = 'mem' AND parent_id = ? ORDER BY node_index",
            (memory_id,),
        )
    ]
    out["blocks"] = [
        dict(b)
        for b in conn.execute(
            "SELECT block_index, start_char, end_char FROM record_blocks "
            "WHERE parent_kind = 'mem' AND parent_id = ? ORDER BY block_index",
            (memory_id,),
        )
    ]
    return out


# ---- Search in the manner of a notes app's search panel
# Words are all required, "quoted phrases" stay together and -word excludes. Operators
# narrow by what a memory carries instead of a file: path: (agent/project/channel),
# agent:, tag: (an entity it mentions), line: (all the words on one line), month:, and
# [locked] / [recalled] / [vector] / [project:x] / [channel:x] for its properties.
SEARCH_OPERATORS = ("path", "agent", "tag", "line", "month")
SEARCH_PROPERTIES = ("locked", "recalled", "vector", "project", "channel")
SEARCH_SORTS = ("new", "old", "recalled")
#: Lines quoted for each result, and the characters kept of each.
SEARCH_LINES = 3
SEARCH_LINE_CHARS = 160
_SEARCH_TOKEN = re.compile(r'(-)?(?:(\w+):)?(?:"([^"]*)"|\(([^)]*)\)|\[([^\]]*)\]|(\S+))')


def parse_search(text: str) -> dict:
    """The words, phrases, exclusions, operators and properties of a search."""
    out: dict = {"terms": [], "exclude": [], "ops": [], "props": []}
    for m in _SEARCH_TOKEN.finditer(text or ""):
        neg, op, quoted, group, prop, word = m.groups()
        if prop is not None:
            name, _, value = prop.partition(":")
            if name.strip().lower() in SEARCH_PROPERTIES:
                out["props"].append((name.strip().lower(), value.strip()))
            continue
        value = quoted if quoted is not None else group if group is not None else word
        if op and op.lower() in SEARCH_OPERATORS:
            parts = value.split() if op.lower() == "line" else [value]
            out["ops"].append((op.lower(), [p for p in parts if p]))
            continue
        if op:  # an unknown "x:" stays a word, as typed
            value = f"{op}:{value}"
        if not value:
            continue
        (out["exclude"] if neg else out["terms"]).append(value)
    return out


def search(
    conn: sqlite3.Connection,
    text: str,
    case: bool = False,
    sort: str = "new",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Memories matching a search, with the lines that matched and how often."""
    p = parse_search(text)
    if sort not in SEARCH_SORTS:
        raise ValueError(f"sort is one of {', '.join(SEARCH_SORTS)}")
    has = (lambda col: f"instr({col}, ?) > 0") if case else (lambda col: f"instr(lower({col}), lower(?)) > 0")
    where, args = [], []
    words: list[str] = list(p["terms"])
    for t in p["terms"]:
        where.append(has("m.content"))
        args.append(t)
    for t in p["exclude"]:
        where.append("NOT " + has("m.content"))
        args.append(t)
    lines: list[list[str]] = []
    for op, vals in p["ops"]:
        for v in vals if op != "line" else []:
            if op == "path":
                where.append("instr(lower(m.agent_id || '/' || m.project_id || '/' || m.channel), lower(?)) > 0")
            elif op == "agent":
                where.append("instr(lower(m.agent_id), lower(?)) > 0")
            elif op == "month":
                where.append("substr(m.timestamp, 1, length(?)) = ?")
                args.append(v)
            elif op == "tag":
                if not _has_table(conn, "entity_mentions"):
                    where.append("0")
                    continue
                where.append(
                    "('mem:' || m.id) IN (SELECT em.ref FROM entity_mentions em JOIN entities e ON e.id = em.entity_id "
                    "WHERE instr(lower(e.name), lower(?)) > 0 OR em.entity_id IN "
                    "(SELECT entity_id FROM entity_aliases WHERE instr(lower(alias), lower(?)) > 0))"
                )
                args.append(v)
            args.append(v)
        if op == "line" and vals:
            lines.append(vals)
            words += vals
            for v in vals:
                where.append(has("m.content"))
                args.append(v)
    for name, value in p["props"]:
        if name == "locked":
            where.append("m.locked = 1")
        elif name == "recalled":
            where.append("m.recall_count > 0")
        elif name == "vector":
            where.append("m.embedding IS NOT NULL")
        elif name in ("project", "channel"):
            where.append(f"m.{name} = ?")
            args.append(value)
    order = {"new": "m.timestamp DESC, m.id DESC", "old": "m.timestamp, m.id", "recalled": "m.recall_count DESC, m.timestamp DESC, m.id DESC"}[sort]
    sql = (
        "SELECT m.id, m.agent_id, m.project_id, m.channel, m.timestamp, m.content, m.recall_count, m.locked FROM memories m"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY {order}"
    )
    fold = (lambda s: s) if case else (lambda s: s.lower())

    def on_one_line(content: str) -> bool:
        rows = [fold(x) for x in content.splitlines()]
        return all(any(all(fold(w) in r for w in group) for r in rows) for group in lines)

    if not where and not lines:
        return {"total": 0, "rows": []}
    found = conn.execute(sql, args).fetchall()
    if lines:
        found = [r for r in found if on_one_line(r["content"])]
    limit = max(1, min(int(limit), MAX_LIMIT))
    page = found[max(0, int(offset)) : max(0, int(offset)) + limit]
    needles = [fold(w) for w in words if w]

    def matched(content: str) -> tuple[int, list[str]]:
        count, quoted = 0, []
        for line in content.splitlines():
            hits = sum(fold(line).count(n) for n in needles)
            if not hits:
                continue
            count += hits
            if len(quoted) < SEARCH_LINES:
                text_ = " ".join(line.split())
                at = min((fold(text_).find(n) for n in needles if n in fold(text_)), default=0)
                start = max(0, at - SEARCH_LINE_CHARS // 3)
                cut = text_[start : start + SEARCH_LINE_CHARS]
                quoted.append(("… " if start else "") + cut + (" …" if start + SEARCH_LINE_CHARS < len(text_) else ""))
        return count, quoted

    rows = []
    for r in page:
        count, quoted = matched(r["content"])
        rows.append(
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "project_id": r["project_id"],
                "channel": r["channel"],
                "timestamp": r["timestamp"],
                "matches": count,
                "lines": quoted or [snippet(r["content"])],
                "recall_count": r["recall_count"],
                "locked": bool(r["locked"]),
            }
        )
    return {"total": len(found), "rows": rows}


# ---- Episodes: the summary an agent stores when a conversation ends
def _fts_or_substring(query: str, fts: str, alias: str, columns: tuple[str, ...]) -> tuple[str, list[str], list]:
    """A join and a condition that find ``query``: the trigram index from three
    characters, else a substring of any of ``columns`` (as the memory list does)."""
    query = query.strip()
    if not query:
        return "", [], []
    if len(query) >= FTS_MIN_CHARS:
        return f" JOIN {fts} f ON f.rowid = {alias}.id", [f"{fts} MATCH ?"], ['"' + query.replace('"', '""') + '"']
    either = " OR ".join(f"instr(lower({alias}.{c}), lower(?)) > 0" for c in columns)
    return "", [f"({either})"], [query] * len(columns)


def episodes(conn: sqlite3.Connection, query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """One page of episodes, the latest conversation first, within a search of
    their summaries and keywords."""
    if not _has_table(conn, "episodes"):
        return {"total": 0, "rows": []}
    join, where, args = _fts_or_substring(query, "episodes_fts", "e", ("summary", "keywords"))
    sql = (
        "SELECT e.id, e.agent_id, e.project_id, e.channel, e.summary, e.keywords, e.start_time, "
        "e.end_time, e.resolved, e.created_at FROM episodes e" + join
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT count(*) FROM ({sql})", args).fetchone()[0]
    limit = max(1, min(int(limit), MAX_LIMIT))
    # When a conversation took place, else when it was stored.
    rows = conn.execute(
        sql + " ORDER BY coalesce(e.end_time, e.start_time, e.created_at) DESC, e.id DESC LIMIT ? OFFSET ?",
        [*args, limit, max(0, int(offset))],
    )
    return {
        "total": total,
        "rows": [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "project_id": r["project_id"],
                "channel": r["channel"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "created_at": r["created_at"],
                "resolved": bool(r["resolved"]),
                "keywords": r["keywords"],
                "snippet": snippet(r["summary"], query.strip()),
            }
            for r in rows
        ],
    }


def episode(conn: sqlite3.Connection, episode_id: int) -> dict | None:
    """One episode in full, with the entities declared as mentioned in it, but never its vector."""
    r = conn.execute(
        "SELECT id, agent_id, project_id, channel, summary, keywords, start_time, end_time, resolved, "
        "embedding_model, created_at, embedding IS NOT NULL AS has_embedding FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["resolved"] = bool(out["resolved"])
    out["has_embedding"] = bool(out["has_embedding"])
    out["entities"] = (
        [
            dict(e)
            for e in conn.execute(
                "SELECT e.id, e.name FROM entity_mentions em JOIN entities e ON e.id = em.entity_id "
                "WHERE em.ref = ? ORDER BY e.name, e.id",
                (f"ep:{episode_id}",),
            )
        ]
        if _has_table(conn, "entity_mentions")
        else []
    )
    return out


# ---- Entities: names an agent declares, with their aliases, mentions and relations
#: Characters of a memory or an episode shown where an entity's page names it.
REF_CHARS = 120


def entities(conn: sqlite3.Connection, query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """One page of entities, the most mentioned first, matched on their names and aliases."""
    if not _has_table(conn, "entities"):
        return {"total": 0, "rows": []}
    where, args = [], []
    query = query.strip()
    if query:
        where.append(
            "(instr(lower(e.name), lower(?)) > 0 OR e.id IN "
            "(SELECT entity_id FROM entity_aliases WHERE instr(lower(alias), lower(?)) > 0))"
        )
        args += [query, query]
    sql = (
        "SELECT e.id, e.name, e.agent_id, e.project_id, e.channel, e.declared_by, e.created_at, "
        "(SELECT count(*) FROM entity_mentions m WHERE m.entity_id = e.id) AS mentions, "
        "(SELECT count(*) FROM relations r WHERE (r.subject_kind = 'entity' AND r.subject_id = e.id) "
        "OR (r.object_kind = 'entity' AND r.object_id = e.id)) AS relations, "
        "(SELECT group_concat(alias, char(31)) FROM (SELECT alias FROM entity_aliases a "
        "WHERE a.entity_id = e.id ORDER BY alias)) AS aliases FROM entities e"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT count(*) FROM ({sql})", args).fetchone()[0]
    limit = max(1, min(int(limit), MAX_LIMIT))
    rows = conn.execute(
        sql + " ORDER BY mentions DESC, relations DESC, lower(e.name), e.id LIMIT ? OFFSET ?",
        [*args, limit, max(0, int(offset))],
    )
    return {
        "total": total,
        "rows": [
            {
                **{k: r[k] for k in ("id", "name", "agent_id", "project_id", "channel", "declared_by", "created_at")},
                "mentions": r["mentions"],
                "relations": r["relations"],
                # Aliases equal to the name only repeat it.
                "aliases": [a for a in (r["aliases"] or "").split("\x1f") if a and a != r["name"]],
            }
            for r in rows
        ],
    }


def _short(text: str) -> str:
    """The start of a record, cut to REF_CHARS with a mark where it was cut."""
    text = snippet(text).removesuffix("…")
    return text if len(text) <= REF_CHARS else text[:REF_CHARS].rstrip() + "…"


def _describe(conn: sqlite3.Connection, kind: str, record_id: int) -> dict:
    """What a relation or a mention points at, for a link: an entity's name, or the
    start of a memory or an episode. ``missing`` when the row is gone or not visible."""
    if kind == "entity":
        r = conn.execute("SELECT name FROM entities WHERE id = ?", (record_id,)).fetchone()
        text = r["name"] if r else None
    elif kind == "mem":
        r = conn.execute("SELECT content, timestamp FROM memories WHERE id = ?", (record_id,)).fetchone()
        text = _short(r["content"]) if r else None
    elif kind == "ep":
        r = conn.execute("SELECT summary FROM episodes WHERE id = ?", (record_id,)).fetchone()
        text = _short(r["summary"]) if r else None
    else:
        text = None
    return {"kind": kind, "id": record_id, "text": text, "missing": text is None}


def _ref(ref: str) -> tuple[str, int] | None:
    kind, _, rid = ref.partition(":")
    return (kind, int(rid)) if rid.isdigit() else None


def entity(conn: sqlite3.Connection, entity_id: int) -> dict | None:
    """One entity: its aliases, the records declared to mention it, and its relations,
    each end described so the page can link to it."""
    r = conn.execute(
        "SELECT id, name, normalized, agent_id, project_id, channel, declared_by, created_at "
        "FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["aliases"] = [
        a["alias"]
        for a in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (entity_id,)
        )
        if a["alias"] != r["name"]
    ]
    mentions = []
    for m in conn.execute(
        "SELECT ref, declared_by, created_at FROM entity_mentions WHERE entity_id = ? ORDER BY created_at DESC, ref",
        (entity_id,),
    ):
        parsed = _ref(m["ref"])
        if parsed:
            mentions.append({**_describe(conn, *parsed), "declared_by": m["declared_by"], "created_at": m["created_at"]})
    out["mentions"] = mentions
    relations = []
    for rel in conn.execute(
        "SELECT id, subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref, declared_by, declared_at "
        "FROM relations WHERE (subject_kind = 'entity' AND subject_id = ?) OR (object_kind = 'entity' AND object_id = ?) "
        "ORDER BY declared_at DESC, id DESC",
        (entity_id, entity_id),
    ):
        relations.append(
            {
                "id": rel["id"],
                "predicate": rel["predicate"],
                "subject": _describe(conn, rel["subject_kind"], rel["subject_id"]),
                "object": _describe(conn, rel["object_kind"], rel["object_id"]),
                "anchor": _describe(conn, *_ref(rel["anchor_ref"])) if _ref(rel["anchor_ref"]) else None,
                "declared_by": rel["declared_by"],
                "declared_at": rel["declared_at"],
            }
        )
    out["relations"] = relations
    return out


# ---- Profiles: the summary an agent keeps of its user, sent with every recall
def profiles(conn: sqlite3.Connection, agent: str = "") -> dict:
    """Each agent's profile in full, the latest written first; one agent's when ``agent`` is given.

    ``in_recall`` marks the rows a recall attaches (the agent's own profile, with
    no user named); the length is set against the cap a write is cut to.
    """
    if not _has_table(conn, "profiles"):
        return {"max_length": MAX_PROFILE_LENGTH, "rows": []}
    sql = "SELECT agent_id, project_id, user_id, content, updated_at FROM profiles"
    args: list = []
    if agent:
        sql += " WHERE agent_id = ?"
        args.append(agent)
    rows = conn.execute(sql + " ORDER BY updated_at DESC, agent_id, user_id", args)
    return {
        "max_length": MAX_PROFILE_LENGTH,
        "rows": [
            {
                **dict(r),
                "length": len(r["content"]),
                "in_recall": r["user_id"] == "",
            }
            for r in rows
        ],
    }
