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

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cpersona._vendored_mcp_common.isolation import coerce_for_write
from cpersona.database import connection, transaction
from cpersona.isolation import isolation_where, source_id_where
from cpersona.utils import episode_timestamp, fits_sqlite_integer

logger = logging.getLogger(__name__)

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
    if not m:
        return None
    row_id = int(m.group(2))
    # A digit run past SQLite's INTEGER range names no record, and binding it
    # raised OverflowError out of the whole declaration -- after store had
    # already committed the memory the declaration rides on.
    return (m.group(1), row_id) if fits_sqlite_integer(row_id) else None


def canonical_ref(ref) -> str | None:
    """The one spelling of a record ref that is stored and compared: `mem:001` → `mem:1`.

    parse_ref accepts leading zeros, but the delete triggers and the walk build refs
    as `'mem:' || id`. A ref kept as the caller spelled it matched neither, so the
    walk never reached it and it outlived its record (bug-449).
    """
    parsed = parse_ref(ref)
    return f"{parsed[0]}:{parsed[1]}" if parsed else None


def _is_id(value) -> bool:
    """An integer id. JSON true / false arrive as bool, which Python counts as an int (bug-447).

    One SQLite cannot hold is no id either: binding it raises instead of matching nothing.
    """
    return isinstance(value, int) and not isinstance(value, bool) and fits_sqlite_integer(value)


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


def _visible_scopes(scope: _Scope) -> list[tuple[str, str]]:
    """The scopes whose entities a reader of `scope` sees, most specific first.

    Per axis the reader sees its own value and the empty one, so a declaration in
    (P, C) can reuse an entity of (P, C), (P, ''), ('', C) or ('', '') in that order.
    Searching only the exact scope and the global pool (bug-450) missed (P, '') and
    ('', C): the declaration registered a second entity under a name its reader
    already resolved, and an alias could name two entities that reader sees.
    """
    projects = [scope.project_id, ""] if scope.project_id else [""]
    channels = [scope.channel, ""] if scope.channel else [""]
    return [(p, c) for p in projects for c in channels]


async def _resolve_entity(db, scope: _Scope, normalized: str, *, exact: bool = False) -> int | None:
    """The entity a normalized name or alias resolves to, in this scope.

    Searched over `_visible_scopes`, so a project's declaration reuses an entity
    the agent already registered for everyone. `exact` searches the declaring
    scope alone. Names and aliases are one namespace: two declarations that
    normalize alike are one entity.
    """
    scopes = _visible_scopes(scope)
    for project_id, channel in scopes[:1] if exact else scopes:
        rows = await db.execute_fetchall(
            "SELECT e.id FROM entities e WHERE e.agent_id = ? AND e.project_id = ? AND e.channel = ? "
            "AND (e.normalized = ? OR EXISTS (SELECT 1 FROM entity_aliases a "
            "WHERE a.entity_id = e.id AND a.normalized = ?)) ORDER BY e.id LIMIT 1",
            (scope.agent_id, project_id, channel, normalized, normalized),
        )
        if rows:
            return rows[0][0]
    return None


async def _register_entity(
    db, scope: _Scope, name: str, declared_by: str, now: str, *, exact: bool = False
) -> tuple[int, bool]:
    """The entity for `name`, registering it in the exact scope when new.

    `exact` reuses only an entity of the declaring scope itself. An alias is
    attached to the entity it is declared with and entity_aliases carries no
    scope, so an alias added to an entity of a wider scope would be read in every
    scope that entity is (bug-448): a declaration that carries aliases gets an
    entity of its own scope, which its reader sees beside the wider one.
    """
    normalized = normalize(name)
    existing = await _resolve_entity(db, scope, normalized, exact=exact)
    if existing is not None:
        return existing, False
    cur = await db.execute(
        "INSERT INTO entities (agent_id, project_id, channel, name, normalized, declared_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope.agent_id, scope.project_id, scope.channel, name, normalized, declared_by, now),
    )
    return cur.lastrowid, True


async def _alias_refusal(db, scope: _Scope, entity_id: int, alias: str) -> str | None:
    """Why `alias` cannot be attached to `entity_id` in this scope, or None."""
    normalized = normalize(alias)
    if not normalized:
        return "empty after normalization"
    owner = await _resolve_entity(db, scope, normalized)
    if owner is not None and owner != entity_id:
        return "already names another entity in this scope"
    return None


async def _add_alias(db, scope: _Scope, entity_id: int, alias: str) -> str | None:
    """Attach an alias; the reason it could not be, or None."""
    reason = await _alias_refusal(db, scope, entity_id, alias)
    if reason is None:
        await db.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized) VALUES (?, ?, ?)",
            (entity_id, alias, normalize(alias)),
        )
    return reason


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
    if isinstance(value, str) and _REF.match(value.strip()):
        # Shaped as a record ref, but its id is past SQLite's range: no record has
        # it, and registering the string as an entity name would invent one.
        return f"{value.strip()} is not a record of this agent"
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
            else:
                anchor = canonical_ref(anchor)

        for index, item in enumerate(entities):
            if index >= MAX_ITEMS:
                dropped.append({"item": f"entities[{index}]", "reason": f"beyond the {MAX_ITEMS}-item bound"})
                continue
            name = _text(item.get("name")) if isinstance(item, dict) else None
            if name is None:
                dropped.append({"item": f"entities[{index}]", "reason": "name must be a nonempty string"})
                continue
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list):
                dropped.append({"item": f"entities[{index}].aliases", "reason": "must be a list"})
                aliases = []
            carries_aliases = any(_text(a) is not None for a in aliases[:MAX_ALIASES])
            entity_id, created = await _register_entity(
                db, scope, name, declared_by, now, exact=carries_aliases
            )
            entry = {"id": entity_id, "name": name, "created": created}
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


def _retract_list(items, name: str, dropped: list) -> tuple[list, list[dict]]:
    """The items of one retract list within MAX_ITEMS, and the report of those past it."""
    if items is None:
        return [], []
    if not isinstance(items, list):
        dropped.append({"item": f"retract.{name}", "reason": "must be a list"})
        return [], []
    excess = [
        {"item": f"retract.{name}[{index}]", "reason": f"beyond the {MAX_ITEMS}-item bound"}
        for index in range(MAX_ITEMS, len(items))
    ]
    return items[:MAX_ITEMS], excess


async def retract(
    agent_id: str,
    *,
    relations: list | None = None,
    mentions: list | None = None,
) -> dict:
    """Remove declarations of this agent. Ids of other agents are left alone and counted as such."""
    report = {"relations": 0, "mentions": 0, "dropped": []}
    # Bounded like a declaration: each item is its own DELETE under the process-wide
    # write lock, so an unbounded list held every writer of every agent for as long
    # as the payload was long. Items past the bound are reported, never deleted, and
    # the report is built before the lock is taken.
    relations, relations_excess = _retract_list(relations, "relations", report["dropped"])
    mentions, mentions_excess = _retract_list(mentions, "mentions", report["dropped"])
    async with transaction() as db:
        for index, relation_id in enumerate(relations):
            if not _is_id(relation_id):
                report["dropped"].append({"item": f"retract.relations[{index}]", "reason": "must be an integer id"})
                continue
            cur = await db.execute("DELETE FROM relations WHERE id = ? AND agent_id = ?", (relation_id, agent_id))
            report["relations"] += cur.rowcount
        report["dropped"].extend(relations_excess)
        for index, item in enumerate(mentions):
            label = f"retract.mentions[{index}]"
            entity_id = item.get("entity") if isinstance(item, dict) else None
            ref = item.get("ref") if isinstance(item, dict) else None
            if not _is_id(entity_id) or parse_ref(ref) is None:
                report["dropped"].append({"item": label, "reason": "needs an integer entity and a record ref"})
                continue
            cur = await db.execute(
                "DELETE FROM entity_mentions WHERE entity_id = ? AND ref = ? "
                "AND entity_id IN (SELECT id FROM entities WHERE agent_id = ?)",
                (entity_id, canonical_ref(ref), agent_id),
            )
            report["mentions"] += cur.rowcount
        report["dropped"].extend(mentions_excess)
    return report


# ------------------------------------------------------------------------------------
# Carrying the graph with its records
# ------------------------------------------------------------------------------------
#
# A declaration names its records by `mem:<id>` / `ep:<id>`, it is filed in the scope
# columns of its entity or relation, and the delete triggers take it with any record
# it names. So every path that gives a record a new identity -- a new id when merge or
# import copies it, the survivor's id when a dedup repair deletes it, a new channel
# when migrate_channel_axis re-files it -- has to bring the declarations along through
# one map from the record's old ref to its new one. Without that the declaration is
# deleted with the old row, or left where no reader of the record can see it, and
# retraction stops being the only way a declaration leaves the store (§2).
#
# The record shapes below are the export file's (`_type` aside), so a declaration
# read from the store and one read back from a backup go through the same writer.

ENTITY_COLUMNS = "e.id, e.agent_id, e.project_id, e.channel, e.name, e.declared_by, e.created_at"
MENTION_COLUMNS = "m.entity_id, m.ref, m.declared_by, m.created_at"
RELATION_COLUMNS = (
    "r.id, r.agent_id, r.project_id, r.channel, r.subject_kind, r.subject_id, r.predicate, "
    "r.object_kind, r.object_id, r.anchor_ref, r.declared_by, r.declared_at"
)
_ENTITY_KEYS = ("id", "agent_id", "project_id", "channel", "name", "declared_by", "created_at")
_MENTION_KEYS = ("entity", "ref", "declared_by", "created_at")
_RELATION_KEYS = (
    "id", "agent_id", "project_id", "channel", "subject_kind", "subject_id", "predicate",
    "object_kind", "object_id", "anchor_ref", "declared_by", "declared_at",
)


def entity_record(row, aliases: list[str]) -> dict:
    """An ENTITY_COLUMNS row and its aliases, as a record."""
    return {**dict(zip(_ENTITY_KEYS, row)), "aliases": aliases}


def mention_record(row) -> dict:
    """A MENTION_COLUMNS row as a record."""
    return dict(zip(_MENTION_KEYS, row))


def relation_record(row) -> dict:
    """A RELATION_COLUMNS row as a record."""
    return dict(zip(_RELATION_KEYS, row))


def names_memory_sql(column: str) -> str:
    """SQL that is true when some declaration names the memory whose id is `column`."""
    ref = f"'mem:' || {column}"
    return (
        f"(EXISTS (SELECT 1 FROM entity_mentions WHERE ref = {ref}) "
        f"OR EXISTS (SELECT 1 FROM relations WHERE anchor_ref = {ref} "
        f"OR (subject_kind = 'mem' AND subject_id = {column}) "
        f"OR (object_kind = 'mem' AND object_id = {column})))"
    )


async def aliases_of(db, entity_ids: list[int]) -> dict[int, list[str]]:
    """The aliases of each entity as written, in normalized order."""
    out: dict[int, list[str]] = {}
    for offset in range(0, len(entity_ids), 500):
        batch = entity_ids[offset:offset + 500]
        marks = ",".join("?" for _ in batch)
        for entity_id, alias in await db.execute_fetchall(
            f"SELECT entity_id, alias FROM entity_aliases WHERE entity_id IN ({marks}) "
            "ORDER BY entity_id, normalized",
            batch,
        ):
            out.setdefault(entity_id, []).append(alias)
    return out


@dataclass
class Graph:
    """Declarations keyed on the ids they had where they were read.

    `entities` maps an entity id to its record; mentions and relations name
    entities by those ids and records by refs of the same origin.
    """

    entities: dict[int, dict] = field(default_factory=dict)
    mentions: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.entities or self.mentions or self.relations)


async def read_graph(db, agent_id: str, *, channel: str | None = None) -> Graph:
    """Every declaration of `agent_id`; with `channel`, the entities and relations filed there."""
    graph = Graph()
    e_where, r_where, params = "e.agent_id = ?", "r.agent_id = ?", [agent_id]
    if channel is not None:
        e_where += " AND e.channel = ?"
        r_where += " AND r.channel = ?"
        params.append(channel)
    rows = await db.execute_fetchall(f"SELECT {ENTITY_COLUMNS} FROM entities e WHERE {e_where} ORDER BY e.id", params)
    aliases = await aliases_of(db, [r[0] for r in rows])
    for row in rows:
        graph.entities[row[0]] = entity_record(row, aliases.get(row[0], []))
    graph.mentions = [
        mention_record(row)
        for row in await db.execute_fetchall(
            f"SELECT {MENTION_COLUMNS} FROM entity_mentions m JOIN entities e ON e.id = m.entity_id "
            f"WHERE {e_where} ORDER BY m.entity_id, m.ref",
            params,
        )
    ]
    graph.relations = [
        relation_record(row)
        for row in await db.execute_fetchall(
            f"SELECT {RELATION_COLUMNS} FROM relations r WHERE {r_where} ORDER BY r.id", params
        )
    ]
    return graph


@dataclass
class Carried:
    """What `carry` wrote, and what it could not."""

    entities: int = 0
    mentions: int = 0
    relations: int = 0
    # Entities, mentions and relations not written: malformed, or naming a record
    # or entity that did not come along.
    dropped: int = 0
    # Aliases refused because they already name another entity the reader sees (§1),
    # and the source ids of the entities that lost one.
    aliases_dropped: int = 0
    incomplete: set = field(default_factory=set)
    entity_map: dict = field(default_factory=dict)

    def counts(self) -> dict:
        return {"entities": self.entities, "mentions": self.mentions, "relations": self.relations}


def _carried_ref(ref, ref_map) -> str | None:
    """The ref a record has here: `ref_map`'s value, or the ref itself when there is no map."""
    canonical = canonical_ref(ref)
    if canonical is None or ref_map is None:
        return canonical
    return ref_map.get(canonical)


def _stamp(value, now: str) -> str:
    return value if isinstance(value, str) and value else now


async def carry(
    db,
    graph: Graph,
    ref_map: dict | None,
    *,
    agent_id: str | None = None,
    entity_map: dict | None = None,
    dry_run: bool = False,
) -> Carried:
    """Write `graph` into the store with every record ref remapped through `ref_map`.

    `ref_map` maps the ref a record had where the graph was read to the ref of the
    same record here; None means refs are unchanged (a re-channelled record keeps
    its id). A mention or relation naming a record the map does not hold is not
    written -- the record it is about did not come along -- and neither is one
    whose record is not its agent's (a declaration's records are its own agent's).

    Each entity lands in its own scope, under `agent_id` when one is given: on the
    entity of that exact scope its name already resolves to, or registered there
    with its name, provenance and time. Its aliases follow, except one that already
    names another entity its reader sees (§1). `entity_map` seeds entities that are
    already in place and are referenced, not copied.

    Mentions and relations go in with INSERT OR IGNORE, so carrying a graph twice
    writes it once. `dry_run` writes nothing and counts what a real run would carry.
    """
    out = Carried(entity_map=dict(entity_map or {}))
    owners: dict[int, str] = {}
    now = _now()
    for old_id, record in graph.entities.items():
        name = _text(record.get("name"))
        declared_by = record.get("declared_by")
        owner = agent_id or record.get("agent_id")
        if not _is_id(old_id) or name is None or declared_by not in DECLARED_BY or not isinstance(owner, str) \
                or not owner:
            out.dropped += 1
            continue
        scope = _Scope(owner, record.get("project_id"), record.get("channel"))
        entity_id = await _resolve_entity(db, scope, normalize(name), exact=True)
        if entity_id is None:
            if dry_run:
                entity_id = -old_id  # stands in for the row a real run would register
            else:
                cur = await db.execute(
                    "INSERT INTO entities (agent_id, project_id, channel, name, normalized, declared_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (owner, scope.project_id, scope.channel, name, normalize(name), declared_by,
                     _stamp(record.get("created_at"), now)),
                )
                entity_id = cur.lastrowid
        out.entity_map[old_id] = entity_id
        owners[old_id] = owner
        out.entities += 1
        aliases = record.get("aliases") or []
        for alias in aliases if isinstance(aliases, list) else [None]:
            text = _text(alias)
            if text is None:
                reason = "must be a nonempty string"
            elif dry_run:
                reason = await _alias_refusal(db, scope, entity_id, text)
            else:
                reason = await _add_alias(db, scope, entity_id, text)
            if reason:
                out.aliases_dropped += 1
                out.incomplete.add(old_id)

    async def record_here(owner: str, ref: str) -> str | None:
        here = _carried_ref(ref, ref_map)
        if here is None:
            return None
        if not dry_run and not await _record_exists(db, _Scope(owner, "", ""), *parse_ref(here)):
            return None
        return here

    for record in graph.mentions:
        source_entity = record.get("entity")
        entity_id = out.entity_map.get(source_entity) if _is_id(source_entity) else None
        owner = (agent_id or owners.get(source_entity)) if entity_id is not None else None
        declared_by = record.get("declared_by")
        ref = await record_here(owner, record.get("ref")) if entity_id is not None and owner else None
        if ref is None or declared_by not in DECLARED_BY:
            out.dropped += 1
            continue
        if not dry_run:
            await _add_mention(db, entity_id, ref, declared_by, _stamp(record.get("created_at"), now))
        out.mentions += 1

    for record in graph.relations:
        owner = agent_id or record.get("agent_id")
        predicate = _text(record.get("predicate"))
        declared_by = record.get("declared_by")
        if not isinstance(owner, str) or not owner or predicate is None or not normalize(predicate) \
                or declared_by not in DECLARED_BY:
            out.dropped += 1
            continue
        ends = []
        for kind, end_id in ((record.get("subject_kind"), record.get("subject_id")),
                             (record.get("object_kind"), record.get("object_id"))):
            if kind == "entity":
                mapped = out.entity_map.get(end_id) if _is_id(end_id) else None
                ends.append(("entity", mapped) if mapped is not None else None)
            elif kind in RECORD_KINDS and _is_id(end_id):
                here = await record_here(owner, f"{kind}:{end_id}")
                ends.append(parse_ref(here) if here else None)
            else:
                ends.append(None)
        anchor = record.get("anchor_ref") or ""
        if anchor:
            anchor = await record_here(owner, anchor)
        if None in ends or anchor is None:
            out.dropped += 1
            continue
        if not dry_run:
            scope = _Scope(owner, record.get("project_id"), record.get("channel"))
            (s_kind, s_id), (o_kind, o_id) = ends
            await db.execute(
                "INSERT OR IGNORE INTO relations (agent_id, project_id, channel, subject_kind, subject_id, "
                "predicate, object_kind, object_id, anchor_ref, declared_by, declared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner, scope.project_id, scope.channel, s_kind, s_id, normalize(predicate), o_kind, o_id,
                 anchor, declared_by, _stamp(record.get("declared_at"), now)),
            )
        out.relations += 1
    return out


async def repoint(db, agent_id: str, ref_map: dict) -> None:
    """Point the declarations naming each old record at its new one, in place.

    For a record about to be deleted in favour of an equal record of the same agent
    (a dedup repair): its mentions are added to the survivor and its relations are
    re-pointed there, keeping their ids. A declaration the survivor already carries
    is left on the old record, and the delete trigger takes it with that record.
    """
    for old, new in ref_map.items():
        before, after = parse_ref(old), parse_ref(new)
        if before is None or after is None or before[0] != after[0] or before == after:
            continue
        kind = before[0]
        old_ref, new_ref = f"{kind}:{before[1]}", f"{kind}:{after[1]}"
        await db.execute(
            "INSERT OR IGNORE INTO entity_mentions (entity_id, ref, declared_by, created_at) "
            "SELECT m.entity_id, ?, m.declared_by, m.created_at FROM entity_mentions m "
            "JOIN entities e ON e.id = m.entity_id WHERE m.ref = ? AND e.agent_id = ?",
            (new_ref, old_ref, agent_id),
        )
        await db.execute(
            "UPDATE OR IGNORE relations SET "
            "subject_id = CASE WHEN subject_kind = ? AND subject_id = ? THEN ? ELSE subject_id END, "
            "object_id = CASE WHEN object_kind = ? AND object_id = ? THEN ? ELSE object_id END, "
            "anchor_ref = CASE WHEN anchor_ref = ? THEN ? ELSE anchor_ref END "
            "WHERE agent_id = ? AND ((subject_kind = ? AND subject_id = ?) "
            "OR (object_kind = ? AND object_id = ?) OR anchor_ref = ?)",
            (kind, before[1], after[1], kind, before[1], after[1], old_ref, new_ref,
             agent_id, kind, before[1], kind, before[1], old_ref),
        )


def _landing(relation: dict, landing: dict[str, str]) -> str | None:
    """The channel a relation follows: its anchor's, else that of a record it names."""
    if relation["anchor_ref"]:
        return landing.get(relation["anchor_ref"])
    for kind, end_id in ((relation["subject_kind"], relation["subject_id"]),
                         (relation["object_kind"], relation["object_id"])):
        if kind in RECORD_KINDS and f"{kind}:{end_id}" in landing:
            return landing[f"{kind}:{end_id}"]
    return None


async def follow_rechannelled(db, moved: dict[int, tuple[str, str]], *, from_channel: str) -> None:
    """Take the `from_channel` declarations about re-channelled memories to their new channel.

    `moved` maps a memory id to (its agent, the channel it now has). A reader sees
    its own channel and the global one, so a mention of a moved memory and a
    relation anchored on it (or, unanchored, naming it) stop being visible to the
    memory's readers while they stay filed in `from_channel`. Each moves to the
    memory's channel, and the `from_channel` entities they name are carried there
    with their aliases -- into every channel their records went to, when those
    differ. A relation between two entities with no record behind it is copied to
    every channel one of them went to where the other is visible too.

    An original entity nothing in `from_channel` names any more is removed; one that
    lost an alias on the way, or that something left behind still names, stays.
    """
    by_agent: dict[str, dict[str, str]] = {}
    for mem_id, (agent_id, channel) in moved.items():
        if channel != from_channel:
            by_agent.setdefault(agent_id, {})[f"mem:{mem_id}"] = channel
    for agent_id, landing in sorted(by_agent.items()):
        graph = await read_graph(db, agent_id, channel=from_channel)
        per_channel: dict[str, Graph] = {}
        gone_mentions: list[tuple[int, str]] = []
        gone_relations: list[int] = []

        def take(channel: str, entity_id) -> Graph:
            into = per_channel.setdefault(channel, Graph())
            if entity_id in graph.entities:
                into.entities[entity_id] = {**graph.entities[entity_id], "channel": channel}
            return into

        for mention in graph.mentions:
            channel = landing.get(mention["ref"])
            if channel is not None:
                take(channel, mention["entity"]).mentions.append(mention)
                gone_mentions.append((mention["entity"], mention["ref"]))
        for relation in graph.relations:
            channel = _landing(relation, landing)
            if channel is None:
                continue
            for kind, end_id in ((relation["subject_kind"], relation["subject_id"]),
                                 (relation["object_kind"], relation["object_id"])):
                if kind == "entity":
                    take(channel, end_id)
            per_channel[channel].relations.append({**relation, "channel": channel})
            gone_relations.append(relation["id"])
        if not per_channel:
            continue
        copied = set(gone_relations)
        for relation in graph.relations:
            if relation["anchor_ref"] or relation["subject_kind"] != "entity" or relation["object_kind"] != "entity":
                continue
            ends = (relation["subject_id"], relation["object_id"])
            for channel, into in per_channel.items():
                # Copied where one end was carried and the other is there too, or is
                # filed outside `from_channel` (the global pool) and so already seen.
                if any(e in into.entities for e in ends) and all(
                    e in into.entities or e not in graph.entities for e in ends
                ):
                    into.relations.append({**relation, "channel": channel})
                    copied.add(relation["id"])

        incomplete: set[int] = set()
        for channel, into in sorted(per_channel.items()):
            # An endpoint filed outside `from_channel` (the global pool) is already
            # visible in the new channel; the copy names it as it is.
            in_place = {
                end_id
                for relation in into.relations
                for kind, end_id in ((relation["subject_kind"], relation["subject_id"]),
                                     (relation["object_kind"], relation["object_id"]))
                if kind == "entity" and end_id not in into.entities
            }
            done = await carry(db, into, None, entity_map={e: e for e in in_place})
            if done.dropped:
                # Something could not be written: keep every original, a duplicate
                # costs less than a declaration lost.
                logger.warning(
                    "migrate_channel_axis: %d declarations of %s could not follow their records to %r; "
                    "the originals stay in %r", done.dropped, agent_id, channel, from_channel,
                )
                gone_mentions, gone_relations, incomplete = [], [], set(graph.entities)
            incomplete |= done.incomplete

        for entity_id, ref in gone_mentions:
            await db.execute("DELETE FROM entity_mentions WHERE entity_id = ? AND ref = ?", (entity_id, ref))
        for relation_id in gone_relations:
            await db.execute("DELETE FROM relations WHERE id = ?", (relation_id,))
        carried_entities = {e for into in per_channel.values() for e in into.entities}
        for entity_id in sorted(carried_entities - incomplete):
            if await db.execute_fetchall("SELECT 1 FROM entity_mentions WHERE entity_id = ? LIMIT 1", (entity_id,)):
                continue
            touching = {
                r[0]
                for r in await db.execute_fetchall(
                    "SELECT id FROM relations WHERE (subject_kind = 'entity' AND subject_id = ?) "
                    "OR (object_kind = 'entity' AND object_id = ?)",
                    (entity_id, entity_id),
                )
            }
            if touching <= copied:
                await db.execute("DELETE FROM entities WHERE id = ?", (entity_id,))


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


async def _neighbourhood(
    db, agent_id: str, starts: list[int], max_hops: int, project_id: str | None, channel: str
) -> tuple[dict[int, tuple], dict[int, int], dict[int, int]]:
    """Breadth first from `starts` over entity → entity relations in scope, either direction.

    Returns `(edges, distance, recency)`:

    * `edges` — relation id → (subject, object, predicate, declared_at, anchor_ref,
      declared_by), for every relation touching an entity within `max_hops`. The
      relations of entities AT the bound are loaded too, so a caller can tell one
      was left unfollowed; the entities they lead to get no distance.
    * `distance` — entity → hops, for every entity within `max_hops`.
    * `recency` — relation id → position in one written order over `edges`: most
      recently declared first, then the lower relation id.

    Both endpoints must be entities the call could read, and so must the relation.
    """
    iso_r = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="r")
    iso_s = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="s")
    iso_o = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="o")
    edges: dict[int, tuple] = {}
    distance = {e: 0 for e in starts}
    frontier = sorted(distance)
    for hop in range(max_hops + 1):
        if not frontier:
            break
        nxt: set[int] = set()
        for offset in range(0, len(frontier), 250):
            batch = frontier[offset:offset + 250]
            marks = ",".join("?" for _ in batch)
            for rel_id, subject, obj, predicate, declared_at, anchor, declared_by in await db.execute_fetchall(
                "SELECT r.id, r.subject_id, r.object_id, r.predicate, r.declared_at, r.anchor_ref, r.declared_by "
                "FROM relations r JOIN entities s ON s.id = r.subject_id JOIN entities o ON o.id = r.object_id "
                f"WHERE {iso_r.clause} AND {iso_s.clause} AND {iso_o.clause} "
                "AND r.subject_kind = 'entity' AND r.object_kind = 'entity' "
                f"AND (r.subject_id IN ({marks}) OR r.object_id IN ({marks}))",
                (*iso_r.params, *iso_s.params, *iso_o.params, *batch, *batch),
            ):
                edges[rel_id] = (subject, obj, predicate, declared_at, anchor, declared_by)
                for end in (subject, obj):
                    if end not in distance:
                        nxt.add(end)
        if hop == max_hops:
            break  # entities one hop past the bound are known, their edges are not needed
        for e in nxt:
            distance[e] = hop + 1
        frontier = sorted(nxt)
    newest_first = sorted(edges, key=lambda r: (edges[r][3], -r), reverse=True)
    recency = {rel_id: position for position, rel_id in enumerate(newest_first)}
    return edges, distance, recency


async def _mentioning_records(
    db,
    entity_id: int,
    *,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
    excluded: list[str],
    limit: int,
    count: bool = False,
    refs_only: bool = False,
) -> tuple[list[tuple[int, str, str, dict | None]], int | None]:
    """The readable records that mention `entity_id`, lowest id first, at most `limit`.

    Readable means the call could read the record directly: this agent's, in the
    call's project and channel, matching `source_id`, and an episode only where
    recall would return one (no `source_id`, or a `channel`). Returns
    `[(id, kind, ref, row)]` -- `row` carries what a walked claim reads (ref,
    content, timestamp, `context`) -- and, when `count` is set, how many readable
    records there are in all. No message id: versions of one record cannot share
    a project (the dedup index), so it would never order a walked row.

    `refs_only` reads the ids alone and returns None for `row`: traverse keeps only
    the refs, and read every memory's content and episode's summary to drop them
    (bug-501). The count is a query only for a table whose read came back full --
    one that returned fewer than `limit` rows has already counted itself.
    """
    excl_marks = ",".join("?" for _ in excluded) or "''"
    iso_m = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="t")
    src = source_id_where(source_id, alias="t")
    mem_where = (
        "FROM entity_mentions m JOIN memories t ON t.id = CAST(substr(m.ref, 5) AS INTEGER) "
        f"WHERE m.entity_id = ? AND m.ref LIKE 'mem:%' AND m.ref NOT IN ({excl_marks}) "
        f"AND {iso_m.clause}{src.and_clause}"
    )
    mem_params = (entity_id, *excluded, *iso_m.params, *src.params)
    ep_where = (
        "FROM entity_mentions m JOIN episodes t ON t.id = CAST(substr(m.ref, 4) AS INTEGER) "
        f"WHERE m.entity_id = ? AND m.ref LIKE 'ep:%' AND m.ref NOT IN ({excl_marks}) "
        f"AND {iso_m.clause}"
    )
    ep_params = (entity_id, *excluded, *iso_m.params)
    episodes_readable = not source_id or bool(channel)

    found: list[tuple[int, str, str, dict | None]] = []
    mem_cols = "t.id" if refs_only else "t.id, t.content, t.timestamp, t.project_id, t.channel"
    mem_rows = await db.execute_fetchall(
        f"SELECT {mem_cols} {mem_where} ORDER BY t.id LIMIT ?", (*mem_params, limit)
    )
    for fields in mem_rows:
        row_id, ref = fields[0], f"mem:{fields[0]}"
        row = None
        if not refs_only:
            _, content, stamp, project, chan = fields
            row = {"ref": ref, "content": content or "", "timestamp": stamp or "",
                   "context": (project or "", chan or "")}
        found.append((row_id, "mem", ref, row))
    ep_rows: list = []
    if episodes_readable:
        ep_cols = "t.id" if refs_only else "t.id, t.summary, t.start_time, t.created_at, t.project_id, t.channel"
        ep_rows = await db.execute_fetchall(
            f"SELECT {ep_cols} {ep_where} ORDER BY t.id LIMIT ?", (*ep_params, limit)
        )
        for fields in ep_rows:
            row_id, ref = fields[0], f"ep:{fields[0]}"
            row = None
            if not refs_only:
                _, summary, start, created, project, chan = fields
                row = {"ref": ref, "content": f"[Episode] {summary}",
                       "timestamp": episode_timestamp(start, created), "context": (project or "", chan or "")}
            found.append((row_id, "ep", ref, row))
    found.sort(key=lambda f: (f[0], f[1]))
    total = None
    if count:
        total = len(mem_rows)
        if len(mem_rows) >= limit:
            total = (await db.execute_fetchall(f"SELECT COUNT(*) {mem_where}", mem_params))[0][0]
        if episodes_readable:
            ep_total = len(ep_rows)
            if len(ep_rows) >= limit:
                ep_total = (await db.execute_fetchall(f"SELECT COUNT(*) {ep_where}", ep_params))[0][0]
            total += ep_total
    return found[:limit], total


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
    the `+ 1` is how a cut is known to have happened -- and only readable ones
    (see `_mentioning_records`).
    """
    graph = WalkGraph()
    starts = sorted({r for r in start_refs if parse_ref(r) is not None})
    if not starts:
        return graph
    iso_e = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")
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

        mentioned = sorted({e for ids in graph.mentions.values() for e in ids})
        edges, distance, recency = await _neighbourhood(db, agent_id, mentioned, max_hops, project_id, channel)
        for rel_id, (subject, obj, predicate, *_) in edges.items():
            if subject == obj:
                continue
            graph.adjacency.setdefault(subject, []).append((recency[rel_id], rel_id, obj, predicate))
            graph.adjacency.setdefault(obj, []).append((recency[rel_id], rel_id, subject, predicate))
        for entries in graph.adjacency.values():
            entries.sort()

        # Records are read for every entity some start reaches through at least one
        # relation. Distance from the union of the starts is not that: an entity one
        # start mentions is at distance 0 there, yet another start may reach it
        # through a relation, and the walk (which runs per item) then found no
        # records for it -- so bringing a second item into the window took walked
        # evidence away from the first. Each start's own reach is followed over the
        # adjacency just loaded, which holds every edge within max_hops of any start.
        reached = {e for e, d in distance.items() if d >= 1}
        for own in graph.mentions.values():
            seen = set(own)
            frontier = seen
            for _ in range(max_hops):
                frontier = {
                    other for e in frontier for _, _, other, _ in graph.adjacency.get(e, ())
                    if other not in seen and other in distance
                }
                if not frontier:
                    break
                seen |= frontier
                reached |= frontier

        excluded = sorted(set(pool_refs))
        for entity_id in sorted(reached):
            found, _ = await _mentioning_records(
                db, entity_id, agent_id=agent_id, project_id=project_id, channel=channel,
                source_id=source_id, excluded=excluded, limit=per_entity + 1,
            )
            if len(found) > per_entity:
                graph.records_cut.add(entity_id)
            if found:
                graph.records[entity_id] = [ref for _, _, ref, _ in found]
                for _, _, ref, row in found:
                    graph.rows[ref] = row
    return graph


# ------------------------------------------------------------------------------------
# traverse (§4) — the entity's neighbourhood, as a graph
# ------------------------------------------------------------------------------------

TRAVERSE_MAX_HOPS = 5
TRAVERSE_MAX_LIMIT = 100


async def traverse(
    agent_id: str,
    entity: str,
    *,
    max_hops: int = 1,
    limit: int = 20,
    project_id: str | None = None,
    channel: str = "",
    source_id: str = "",
) -> dict:
    """The neighbourhood of a declared entity: aliases, relations to the hop bound,
    the entities reached, and the refs of the readable records that mention each.

    `entity` is a name or an alias, compared after normalization. When it names
    more than one entity the call can read (a project's and the global pool's),
    all of them are starts. Entities are ordered by hops, then by the most
    recently declared relation that reached them, then by id -- the order the
    reconstruct walk cuts by -- and `limit` bounds both how many are returned
    and how many mentioning refs each lists. No record text is returned; a ref
    expands through get_contents.
    """
    max_hops = max(0, min(int(max_hops), TRAVERSE_MAX_HOPS))
    limit = max(1, min(int(limit), TRAVERSE_MAX_LIMIT))
    response: dict = {"entity": entity, "max_hops": max_hops, "limit": limit, "entities": [], "relations": []}
    normalized = normalize(entity) if isinstance(entity, str) else ""
    if not normalized:
        response["reason"] = "no_such_entity"
        return response
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")
    async with connection() as db:
        starts = [
            r[0]
            for r in await db.execute_fetchall(
                f"SELECT e.id FROM entities e WHERE {iso.clause} AND (e.normalized = ? OR EXISTS "
                "(SELECT 1 FROM entity_aliases a WHERE a.entity_id = e.id AND a.normalized = ?)) ORDER BY e.id",
                (*iso.params, normalized, normalized),
            )
        ]
        if not starts:
            response["reason"] = "no_such_entity"
            return response
        edges, distance, recency = await _neighbourhood(db, agent_id, starts, max_hops, project_id, channel)

        # The relation that reached an entity: from one hop closer, most recent first.
        reached_by: dict[int, int] = {}
        for rel_id, (subject, obj, *_) in edges.items():
            for near, far in ((subject, obj), (obj, subject)):
                if near in distance and far in distance and distance[far] == distance[near] + 1:
                    reached_by[far] = min(reached_by.get(far, recency[rel_id]), recency[rel_id])
        order = sorted(distance, key=lambda e: (distance[e], reached_by.get(e, -1), e))
        kept = order[:limit]
        kept_set = set(kept)

        marks = ",".join("?" for _ in kept)
        names = dict(await db.execute_fetchall(f"SELECT id, name FROM entities WHERE id IN ({marks})", kept))
        aliases: dict[int, list[str]] = {}
        for entity_id, alias in await db.execute_fetchall(
            f"SELECT entity_id, alias FROM entity_aliases WHERE entity_id IN ({marks}) ORDER BY entity_id, normalized",
            kept,
        ):
            aliases.setdefault(entity_id, []).append(alias)

        mentions_cut = False
        for entity_id in kept:
            entry: dict = {"id": entity_id, "name": names[entity_id], "hops": distance[entity_id]}
            if aliases.get(entity_id):
                entry["aliases"] = aliases[entity_id]
            found, total = await _mentioning_records(
                db, entity_id, agent_id=agent_id, project_id=project_id, channel=channel,
                source_id=source_id, excluded=[], limit=limit, count=True, refs_only=True,
            )
            if found:
                entry["mentions"] = [ref for _, _, ref, _ in found]
            if total > len(found):
                entry["mentions_omitted"] = total - len(found)
                mentions_cut = True
            response["entities"].append(entry)

    for rel_id in sorted(edges, key=lambda r: recency[r]):
        subject, obj, predicate, declared_at, anchor, declared_by = edges[rel_id]
        if subject in kept_set and obj in kept_set:
            relation: dict = {"id": rel_id, "subject": subject, "predicate": predicate, "object": obj,
                              "declared_by": declared_by, "declared_at": declared_at}
            if anchor:
                relation["anchor_ref"] = anchor
            response["relations"].append(relation)

    omitted = []
    if any(end not in distance for subject, obj, *_ in edges.values() for end in (subject, obj)):
        omitted.append("max_hops")
    if len(order) > len(kept) or mentions_cut:
        omitted.append("limit")
    if len(order) > len(kept):
        response["entities_omitted"] = len(order) - len(kept)
    if omitted:
        response["bounds"] = {"omitted": omitted}
    return response
