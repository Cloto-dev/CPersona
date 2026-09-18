"""Associative memory — declaring the graph (docs/ASSOCIATIVE_MEMORY_DESIGN.md §2).

Entities, their aliases, the records that mention them, and
subject–predicate–object relations. Declarations come from an agent (through
`store` or `declare_associations`) or from an operator; the server records
which and adds nothing of its own: it does not extract an entity from stored
text and does not infer a relation from two that exist. Coverage is exactly
what was declared.

Normalization is the only processing a declaration receives, and it is
lossless on the stored form: `name`, `alias` and `predicate` are kept as
written, and their `normalized` twin — NFKC, case-folded, whitespace collapsed,
trimmed — is what lookups compare. No tokenizer and no named-entity
recognizer: a deterministic cut of a Japanese proper noun does not exist, so
none is attempted.

Two rules the schema cannot hold are held here (§1, §2):

* one normalized alias resolves to at most one entity within a scope —
  `entity_aliases` carries no scope columns, so the check is a query, not a
  constraint;
* a relation's record endpoint, and a mention's record, must exist in the
  declaring agent's store — this database declares no foreign keys.

A malformed item is reported in `dropped` and skipped, never a reason to
refuse the rest of the call, and never a reason to lose the memory a `store`
carried it on. The whole call is bounded (`MAX_ITEMS`) so a declaration cannot
become an unbounded write.

The second half of this module reads the graph for reconstructive recall (§3):
the lexical cue terms of stage 1, the record → record links of stage 2 and the
entity neighbourhood the walk of stage 3 follows. Every read is scoped the way
the call that reads it is scoped, and with nothing declared every read returns
empty, which is what keeps an empty graph a no-op (invariant 2).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cpersona._vendored_mcp_common.isolation import coerce_for_write
from cpersona.database import connection, transaction
from cpersona.isolation import isolation_where, source_id_where
from cpersona.utils import _try_parse_json, episode_timestamp

# Bounds on one call. A declaration that names more is cut at the bound and
# says so, item by item, in `dropped`.
MAX_ITEMS = 64
MAX_ALIASES = 32
MAX_TEXT = 256

RECORD_KINDS = ("mem", "ep")
DECLARED_BY = ("agent", "operator")

_WHITESPACE = re.compile(r"\s+")
_REF = re.compile(r"^(mem|ep):(\d+)$")
_TABLE = {"mem": "memories", "ep": "episodes"}


def normalize(text: str) -> str:
    """NFKC, case-folded, whitespace collapsed to one space, trimmed."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def parse_ref(ref) -> tuple[str, int] | None:
    """`mem:<id>` / `ep:<id>` → (kind, id); anything else → None."""
    if not isinstance(ref, str):
        return None
    m = _REF.match(ref.strip())
    return (m.group(1), int(m.group(2))) if m else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value) -> str | None:
    """A declared string within the length bound, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_TEXT:
        return None
    return value


class _Scope:
    __slots__ = ("agent_id", "project_id", "channel")

    def __init__(self, agent_id: str, project_id, channel) -> None:
        self.agent_id = agent_id
        self.project_id = coerce_for_write(project_id)
        self.channel = coerce_for_write(channel)


async def _record_exists(db, scope: _Scope, kind: str, row_id: int) -> bool:
    rows = await db.execute_fetchall(
        f"SELECT 1 FROM {_TABLE[kind]} WHERE id = ? AND agent_id = ?", (row_id, scope.agent_id)
    )
    return bool(rows)


async def _resolve_entity(db, scope: _Scope, normalized: str) -> int | None:
    """The entity a normalized name or alias resolves to, in this scope.

    The exact scope is searched first, then the global pool of the same agent
    (`project_id = ''`, `channel = ''`), so a project's declaration reuses an
    entity the agent already registered for everyone. Names and aliases are one
    namespace: two declarations that normalize alike are one entity.
    """
    for project_id, channel in ((scope.project_id, scope.channel), ("", "")):
        rows = await db.execute_fetchall(
            "SELECT e.id FROM entities e WHERE e.agent_id = ? AND e.project_id = ? AND e.channel = ? "
            "AND (e.normalized = ? OR EXISTS (SELECT 1 FROM entity_aliases a "
            "WHERE a.entity_id = e.id AND a.normalized = ?)) ORDER BY e.id LIMIT 1",
            (scope.agent_id, project_id, channel, normalized, normalized),
        )
        if rows:
            return rows[0][0]
    return None


async def _register_entity(db, scope: _Scope, name: str, declared_by: str, now: str) -> tuple[int, bool]:
    """The entity for `name`, registering it in the exact scope when new."""
    normalized = normalize(name)
    existing = await _resolve_entity(db, scope, normalized)
    if existing is not None:
        return existing, False
    cur = await db.execute(
        "INSERT INTO entities (agent_id, project_id, channel, name, normalized, declared_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope.agent_id, scope.project_id, scope.channel, name, normalized, declared_by, now),
    )
    return cur.lastrowid, True


async def _add_alias(db, scope: _Scope, entity_id: int, alias: str) -> str | None:
    """Attach an alias; the reason it could not be, or None."""
    normalized = normalize(alias)
    if not normalized:
        return "empty after normalization"
    owner = await _resolve_entity(db, scope, normalized)
    if owner is not None and owner != entity_id:
        return "already names another entity in this scope"
    await db.execute(
        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized) VALUES (?, ?, ?)",
        (entity_id, alias, normalized),
    )
    return None


async def _add_mention(db, entity_id: int, ref: str, declared_by: str, now: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, ?, ?)",
        (entity_id, ref, declared_by, now),
    )


async def _endpoint(db, scope: _Scope, value, declared_by: str, now: str) -> tuple[str, int] | str:
    """Resolve a relation endpoint: a record ref or an entity name (registered if new)."""
    parsed = parse_ref(value)
    if parsed is not None:
        kind, row_id = parsed
        if not await _record_exists(db, scope, kind, row_id):
            return f"{value.strip()} is not a record of this agent"
        return kind, row_id
    name = _text(value)
    if name is None:
        return "endpoint must be an entity name or a record ref"
    entity_id, _ = await _register_entity(db, scope, name, declared_by, now)
    return "entity", entity_id


async def declare(
    agent_id: str,
    associations: dict,
    *,
    project_id: str = "",
    channel: str = "",
    anchor_ref: str = "",
    declared_by: str = "agent",
) -> dict:
    """Record a declaration. Never raises for a malformed item; reports it.

    `anchor_ref` names the record the declaration is evidenced by. Every
    entity named in `entities` is recorded as mentioned by it, and every
    relation carries it as its `anchor_ref`. It must be a record of this agent;
    otherwise every item that needed it is dropped with that reason.
    """
    if declared_by not in DECLARED_BY:
        raise ValueError("declared_by must be one of " + ", ".join(DECLARED_BY))
    scope = _Scope(agent_id, project_id, channel)
    report: dict = {"entities": [], "mentions": 0, "relations": [], "dropped": []}
    dropped = report["dropped"]
    if not isinstance(associations, dict):
        dropped.append({"item": "associations", "reason": "must be an object"})
        return report
    entities = associations.get("entities", [])
    relations = associations.get("relations", [])
    if not isinstance(entities, list):
        dropped.append({"item": "entities", "reason": "must be a list"})
        entities = []
    if not isinstance(relations, list):
        dropped.append({"item": "relations", "reason": "must be a list"})
        relations = []
    now = _now()

    async with transaction() as db:
        anchor = anchor_ref.strip() if isinstance(anchor_ref, str) else ""
        if anchor:
            parsed = parse_ref(anchor)
            if parsed is None or not await _record_exists(db, scope, *parsed):
                dropped.append({"item": "anchor_ref", "reason": f"{anchor} is not a record of this agent"})
                anchor = ""

        for index, item in enumerate(entities):
            if index >= MAX_ITEMS:
                dropped.append({"item": f"entities[{index}]", "reason": f"beyond the {MAX_ITEMS}-item bound"})
                continue
            name = _text(item.get("name")) if isinstance(item, dict) else None
            if name is None:
                dropped.append({"item": f"entities[{index}]", "reason": "name must be a nonempty string"})
                continue
            entity_id, created = await _register_entity(db, scope, name, declared_by, now)
            entry = {"id": entity_id, "name": name, "created": created}
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list):
                dropped.append({"item": f"entities[{index}].aliases", "reason": "must be a list"})
                aliases = []
            for a_index, alias in enumerate(aliases):
                label = f"entities[{index}].aliases[{a_index}]"
                if a_index >= MAX_ALIASES:
                    dropped.append({"item": label, "reason": f"beyond the {MAX_ALIASES}-alias bound"})
                    continue
                text = _text(alias)
                reason = "must be a nonempty string" if text is None else await _add_alias(db, scope, entity_id, text)
                if reason:
                    dropped.append({"item": label, "reason": reason})
            if anchor:
                await _add_mention(db, entity_id, anchor, declared_by, now)
                report["mentions"] += 1
            report["entities"].append(entry)

        for index, item in enumerate(relations):
            label = f"relations[{index}]"
            if index >= MAX_ITEMS:
                dropped.append({"item": label, "reason": f"beyond the {MAX_ITEMS}-item bound"})
                continue
            if not isinstance(item, dict):
                dropped.append({"item": label, "reason": "must be an object"})
                continue
            predicate = _text(item.get("predicate"))
            if predicate is None or not normalize(predicate):
                dropped.append({"item": label, "reason": "predicate must be a nonempty string"})
                continue
            subject = await _endpoint(db, scope, item.get("subject"), declared_by, now)
            if isinstance(subject, str):
                dropped.append({"item": f"{label}.subject", "reason": subject})
                continue
            obj = await _endpoint(db, scope, item.get("object"), declared_by, now)
            if isinstance(obj, str):
                dropped.append({"item": f"{label}.object", "reason": obj})
                continue
            key = (scope.agent_id, scope.project_id, scope.channel, subject[0], subject[1],
                   normalize(predicate), obj[0], obj[1], anchor)
            await db.execute(
                "INSERT OR IGNORE INTO relations (agent_id, project_id, channel, subject_kind, subject_id, "
                "predicate, object_kind, object_id, anchor_ref, declared_by, declared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, declared_by, now),
            )
            rows = await db.execute_fetchall(
                "SELECT id FROM relations WHERE agent_id = ? AND project_id = ? AND channel = ? "
                "AND subject_kind = ? AND subject_id = ? AND predicate = ? AND object_kind = ? "
                "AND object_id = ? AND anchor_ref = ?",
                key,
            )
            report["relations"].append(rows[0][0])
    return report


async def retract(
    agent_id: str,
    *,
    relations: list | None = None,
    mentions: list | None = None,
) -> dict:
    """Remove declarations of this agent. Ids of other agents are left alone and counted as such."""
    report = {"relations": 0, "mentions": 0, "dropped": []}
    async with transaction() as db:
        for index, relation_id in enumerate(relations or []):
            if not isinstance(relation_id, int):
                report["dropped"].append({"item": f"retract.relations[{index}]", "reason": "must be an integer id"})
                continue
            cur = await db.execute("DELETE FROM relations WHERE id = ? AND agent_id = ?", (relation_id, agent_id))
            report["relations"] += cur.rowcount
        for index, item in enumerate(mentions or []):
            label = f"retract.mentions[{index}]"
            entity_id = item.get("entity") if isinstance(item, dict) else None
            ref = item.get("ref") if isinstance(item, dict) else None
            if not isinstance(entity_id, int) or parse_ref(ref) is None:
                report["dropped"].append({"item": label, "reason": "needs an integer entity and a record ref"})
                continue
            cur = await db.execute(
                "DELETE FROM entity_mentions WHERE entity_id = ? AND ref = ? "
                "AND entity_id IN (SELECT id FROM entities WHERE agent_id = ?)",
                (entity_id, ref.strip(), agent_id),
            )
            report["mentions"] += cur.rowcount
    return report


# ------------------------------------------------------------------------------------
# Reading the graph — reconstructive recall (§3)
# ------------------------------------------------------------------------------------
#
# Reads only: SQL here, and the walk itself is a pure function in reconstruct.py.
# Each read carries the scope of the call it serves -- the agent exactly, project_id
# and channel with the read semantics of recall -- so a relation is never followed
# into a scope the call could not read directly (invariant 7).

# Stage 1 bounds: declared terms one query may match, and terms the lexical arm is
# handed. The cut is by length, longest first, then by term and entity id.
MAX_QUERY_MATCHES = 64
MAX_EXPANSION_TERMS = 32


async def query_terms(
    agent_id: str, query: str, *, project_id: str | None = None, channel: str = ""
) -> tuple[list[str], dict]:
    """Stage 1 — the additional terms the lexical arm receives for `query`.

    The query is matched against the names and aliases in scope by normalized
    substring, longest first; a shorter term inside a span a longer one already
    claimed does not count, so `eye` inside `mizeye` does not name a second
    entity. For each entity the query names, its declared name and its other
    aliases are returned as written -- except those the query already contains.

    Returns `([], {})` when nothing matches, and in particular for an empty
    graph, so the caller hands retrieval exactly what it handed it before. The
    report names the entities matched and any terms the bound cut.
    """
    folded = normalize(query)
    if not folded:
        return [], {}
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")
    async with connection() as db:
        rows = await db.execute_fetchall(
            "SELECT id, term FROM ("
            f"SELECT e.id AS id, e.normalized AS term FROM entities e WHERE {iso.clause} "
            "AND e.normalized != '' AND instr(?, e.normalized) > 0 "
            "UNION "
            "SELECT e.id AS id, a.normalized AS term FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
            f"WHERE {iso.clause} AND a.normalized != '' AND instr(?, a.normalized) > 0"
            ") ORDER BY length(term) DESC, term, id LIMIT ?",
            (*iso.params, folded, *iso.params, folded, MAX_QUERY_MATCHES),
        )
        claimed: list[tuple[int, int]] = []
        found_terms: set[str] = set()
        matched: list[int] = []
        for entity_id, term in rows:
            found = term in found_terms
            start = folded.find(term)
            while not found and start != -1:
                end = start + len(term)
                if all(end <= s or start >= e for s, e in claimed):
                    claimed.append((start, end))
                    found = True
                start = folded.find(term, start + 1)
            if found:
                found_terms.add(term)
                if entity_id not in matched:
                    matched.append(entity_id)
        if not matched:
            return [], {}
        marks = ",".join("?" for _ in matched)
        names = {
            r[0]: (r[1], r[2])
            for r in await db.execute_fetchall(
                f"SELECT id, name, normalized FROM entities WHERE id IN ({marks})", matched
            )
        }
        aliases: dict[int, list[tuple[str, str]]] = {}
        for entity_id, alias, normalized in await db.execute_fetchall(
            f"SELECT entity_id, alias, normalized FROM entity_aliases WHERE entity_id IN ({marks}) "
            "ORDER BY entity_id, normalized",
            matched,
        ):
            aliases.setdefault(entity_id, []).append((alias, normalized))

    terms: list[str] = []
    seen: set[str] = set()
    cut = 0
    for entity_id in matched:
        for written, normalized in [names[entity_id], *aliases.get(entity_id, [])]:
            if normalized in seen or normalized in folded:
                continue
            seen.add(normalized)
            if len(terms) >= MAX_EXPANSION_TERMS:
                cut += 1
                continue
            terms.append(written)
    report: dict = {"entities": [names[e][0] for e in matched]}
    if terms:
        report["terms"] = terms
    if cut:
        report["terms_omitted"] = cut
    return terms, report


async def record_links(
    agent_id: str, refs: list[str], *, project_id: str | None = None, channel: str = ""
) -> list[tuple[str, str, str]]:
    """Stage 2 — declared record → record relations with BOTH endpoints in `refs`.

    `(subject_ref, predicate, object_ref)`, sorted, so the bundling that reads
    them is deterministic. A relation with one endpoint outside the pool is not
    returned: bundling joins candidates, it does not add rows.
    """
    pool = set(refs)
    by_kind: dict[str, list[int]] = {}
    for ref in refs:
        parsed = parse_ref(ref)
        if parsed is not None:
            by_kind.setdefault(parsed[0], []).append(parsed[1])
    if not by_kind:
        return []
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="r")
    links: set[tuple[str, str, str]] = set()
    async with connection() as db:
        for kind, ids in sorted(by_kind.items()):
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                marks = ",".join("?" for _ in batch)
                rows = await db.execute_fetchall(
                    "SELECT r.subject_kind, r.subject_id, r.predicate, r.object_kind, r.object_id "
                    f"FROM relations r WHERE {iso.clause} AND r.subject_kind = ? AND r.subject_id IN ({marks}) "
                    "AND r.object_kind IN ('mem', 'ep')",
                    (*iso.params, kind, *batch),
                )
                for s_kind, s_id, predicate, o_kind, o_id in rows:
                    subject, obj = f"{s_kind}:{s_id}", f"{o_kind}:{o_id}"
                    if obj in pool and obj != subject:
                        links.add((subject, predicate, obj))
    return sorted(links)


@dataclass
class WalkGraph:
    """The part of the graph one reconstruction's walk may follow.

    `mentions` maps a direct candidate's ref to the entities it mentions.
    `adjacency` maps an entity to its declared entity → entity relations in both
    directions, `(recency, relation id, other entity, predicate)`, where recency 0
    is the most recently declared relation of the whole set. `records` maps an
    entity reached through at least one relation to the refs of readable records
    that mention it, lowest id first, excluding the candidate pool; `rows` holds
    those records in the shape a recall row has, with `context`.
    """

    mentions: dict[str, list[int]] = field(default_factory=dict)
    adjacency: dict[int, list[tuple[int, int, int, str]]] = field(default_factory=dict)
    records: dict[int, list[str]] = field(default_factory=dict)
    rows: dict[str, dict] = field(default_factory=dict)
    # Entities whose `records` list was cut by the fetch bound.
    records_cut: set[int] = field(default_factory=set)


async def walk_graph(
    agent_id: str,
    start_refs: list[str],
    pool_refs: list[str],
    *,
    max_hops: int,
    per_entity: int,
    project_id: str | None = None,
    channel: str = "",
    source_id: str = "",
) -> WalkGraph:
    """Load what the walk from `start_refs` can follow within `max_hops`.

    Breadth first over the union of the starts: the adjacency of every entity
    within `max_hops` of any start is loaded, which is a superset of what the
    walk from one item needs, and the adjacency of entities one hop further is
    not. Records are fetched per reached entity, at most `per_entity + 1` --
    the `+ 1` is how a cut is known to have happened. A record is readable when
    the call could read it directly: this agent's, in the call's project and
    channel, matching `source_id`, and an episode only where recall would
    return one (no `source_id`, or a `channel`).
    """
    graph = WalkGraph()
    starts = sorted({r for r in start_refs if parse_ref(r) is not None})
    if not starts:
        return graph
    iso_e = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")
    iso_r = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="r")
    iso_s = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="s")
    iso_o = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="o")
    async with connection() as db:
        for offset in range(0, len(starts), 500):
            batch = starts[offset:offset + 500]
            marks = ",".join("?" for _ in batch)
            for ref, entity_id in await db.execute_fetchall(
                "SELECT m.ref, m.entity_id FROM entity_mentions m JOIN entities e ON e.id = m.entity_id "
                f"WHERE {iso_e.clause} AND m.ref IN ({marks}) ORDER BY m.ref, m.entity_id",
                (*iso_e.params, *batch),
            ):
                graph.mentions.setdefault(ref, []).append(entity_id)
        if not graph.mentions:
            return graph

        edges: dict[int, tuple[int, int, str, str]] = {}  # relation id -> (subject, object, predicate, declared_at)
        distance = {e: 0 for ids in graph.mentions.values() for e in ids}
        frontier = sorted(distance)
        for hop in range(max_hops + 1):
            if not frontier:
                break
            nxt: set[int] = set()
            for offset in range(0, len(frontier), 250):
                batch = frontier[offset:offset + 250]
                marks = ",".join("?" for _ in batch)
                for rel_id, subject, obj, predicate, declared_at in await db.execute_fetchall(
                    "SELECT r.id, r.subject_id, r.object_id, r.predicate, r.declared_at FROM relations r "
                    "JOIN entities s ON s.id = r.subject_id JOIN entities o ON o.id = r.object_id "
                    f"WHERE {iso_r.clause} AND {iso_s.clause} AND {iso_o.clause} "
                    "AND r.subject_kind = 'entity' AND r.object_kind = 'entity' "
                    f"AND (r.subject_id IN ({marks}) OR r.object_id IN ({marks}))",
                    (*iso_r.params, *iso_s.params, *iso_o.params, *batch, *batch),
                ):
                    edges[rel_id] = (subject, obj, predicate, declared_at)
                    for end in (subject, obj):
                        if end not in distance:
                            nxt.add(end)
            if hop == max_hops:
                break  # entities one hop past the bound are known, their edges are not needed
            for e in nxt:
                distance[e] = hop + 1
            frontier = sorted(nxt)

        # Recency is one written order over the loaded relations: most recently
        # declared first, then the lower relation id.
        newest_first = sorted(edges, key=lambda r: (edges[r][3], -r), reverse=True)
        recency = {rel_id: position for position, rel_id in enumerate(newest_first)}
        for rel_id, (subject, obj, predicate, _) in edges.items():
            if subject == obj:
                continue
            graph.adjacency.setdefault(subject, []).append((recency[rel_id], rel_id, obj, predicate))
            graph.adjacency.setdefault(obj, []).append((recency[rel_id], rel_id, subject, predicate))
        for entries in graph.adjacency.values():
            entries.sort()

        reached = sorted(e for e, d in distance.items() if d >= 1)
        excluded = sorted(set(pool_refs))
        excl_marks = ",".join("?" for _ in excluded) or "''"
        iso_m = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="t")
        src = source_id_where(source_id, alias="t")
        for entity_id in reached:
            found: list[tuple[int, str, str]] = []
            memory_rows = await db.execute_fetchall(
                "SELECT t.id, t.msg_id, t.content, t.source, t.timestamp, t.project_id, t.channel "
                "FROM entity_mentions m JOIN memories t ON t.id = CAST(substr(m.ref, 5) AS INTEGER) "
                f"WHERE m.entity_id = ? AND m.ref LIKE 'mem:%' AND m.ref NOT IN ({excl_marks}) "
                f"AND {iso_m.clause}{src.and_clause} ORDER BY t.id LIMIT ?",
                (entity_id, *excluded, *iso_m.params, *src.params, per_entity + 1),
            )
            for row_id, msg_id, content, source, stamp, project, chan in memory_rows:
                ref = f"mem:{row_id}"
                found.append((row_id, "mem", ref))
                row: dict = {"ref": ref, "content": content or "", "timestamp": stamp or "",
                             "context": (project or "", chan or "")}
                if source:
                    row["source"] = source if isinstance(source, dict) else _try_parse_json(source)
                if msg_id:
                    row["id"] = msg_id
                graph.rows[ref] = row
            if not source_id or channel:
                episode_rows = await db.execute_fetchall(
                    "SELECT t.id, t.summary, t.start_time, t.created_at, t.project_id, t.channel "
                    "FROM entity_mentions m JOIN episodes t ON t.id = CAST(substr(m.ref, 4) AS INTEGER) "
                    f"WHERE m.entity_id = ? AND m.ref LIKE 'ep:%' AND m.ref NOT IN ({excl_marks}) "
                    f"AND {iso_m.clause} ORDER BY t.id LIMIT ?",
                    (entity_id, *excluded, *iso_m.params, per_entity + 1),
                )
                for row_id, summary, start, created, project, chan in episode_rows:
                    ref = f"ep:{row_id}"
                    found.append((row_id, "ep", ref))
                    graph.rows[ref] = {
                        "ref": ref,
                        "content": f"[Episode] {summary}",
                        "source": {"System": "episode"},
                        "timestamp": episode_timestamp(start, created),
                        "context": (project or "", chan or ""),
                    }
            found.sort()
            if len(found) > per_entity:
                graph.records_cut.add(entity_id)
            if found:
                graph.records[entity_id] = [ref for _, _, ref in found[:per_entity + 1]]
    return graph
