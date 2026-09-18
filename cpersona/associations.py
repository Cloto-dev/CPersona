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
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from cpersona._vendored_mcp_common.isolation import coerce_for_write
from cpersona.database import transaction

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
