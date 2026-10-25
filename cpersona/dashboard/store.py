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

import os
import re
import sqlite3
from pathlib import Path

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


def overview(conn: sqlite3.Connection, name: str) -> dict:
    """What the store holds and when it was written."""
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
        },
        "span": [span[0], span[1]],
        "months": [{"month": r["m"], "count": r["n"]} for r in months],
        "agents": [
            {"id": r["agent_id"], "count": r["n"], "last": r["last"]} for r in agents
        ],
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
