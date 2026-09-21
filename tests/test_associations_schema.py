"""SCHEMA_VERSION 15: the declared graph's tables and the triggers that keep them true.

docs/ASSOCIATIVE_MEMORY_DESIGN.md §1 (schema) and invariant 8 (no declaration
outlives its endpoints). Nothing here declares anything through a tool -- that
is the write path's job. These tests pin the storage layer: the four tables
have the designed shape, an existing database reaches v15 without any other
object changing, and no alias, mention or relation survives the deletion of
an entity or record it depends on, whichever handler performs the delete.

Rows are inserted by hand, for the same reason the node tests insert nodes by
hand: a trigger does not care who wrote the row, and a test that needed the
declare handler could not tell a trigger defect from a handler defect.
"""

import os
import tempfile

import pytest

from cpersona import admin_handlers, checks, database, session
from cpersona.database import SCHEMA_VERSION

_GRAPH_TABLES = ("entities", "entity_aliases", "entity_mentions", "relations")
_GRAPH_TRIGGERS = (
    "associations_entities_ad",
    "associations_memories_ad",
    "associations_episodes_ad",
)
_GRAPH_INDEXES = (
    "idx_entity_aliases_normalized",
    "idx_entity_mentions_ref",
    "idx_relations_subject",
    "idx_relations_object",
    "idx_relations_anchor",
)
_AT = "2026-09-18T00:00:00+00:00"


class _TempDB:
    def __init__(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "associations.db")

    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved_db = database._db
        self._saved_path = database.DB_PATH
        database._db = None
        database.DB_PATH = self.path
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db = self._saved_db
        database.DB_PATH = self._saved_path
        session.reset_pauses_for_tests()


async def _reboot():
    await database.close_db()
    database._db = None
    return await database.get_db()


async def _memory(db, content: str, agent_id: str = "agent-a") -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
        (agent_id, content, _AT),
    )
    await db.commit()
    return cur.lastrowid


async def _episode(db, summary: str, agent_id: str = "agent-a") -> int:
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords) VALUES (?, ?, ?)",
        (agent_id, summary, "kw"),
    )
    await db.commit()
    return cur.lastrowid


async def _entity(db, name: str, agent_id: str = "agent-a", aliases: tuple[str, ...] = ()) -> int:
    cur = await db.execute(
        "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (agent_id, name, name.casefold(), "agent", _AT),
    )
    entity_id = cur.lastrowid
    for alias in aliases:
        await db.execute(
            "INSERT INTO entity_aliases (entity_id, alias, normalized) VALUES (?, ?, ?)",
            (entity_id, alias, alias.casefold()),
        )
    await db.commit()
    return entity_id


async def _mention(db, entity_id: int, ref: str) -> None:
    await db.execute(
        "INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, ?, ?)",
        (entity_id, ref, "agent", _AT),
    )
    await db.commit()


async def _relation(
    db,
    subject: tuple[str, int],
    predicate: str,
    obj: tuple[str, int],
    anchor: str = "",
    agent_id: str = "agent-a",
) -> int:
    cur = await db.execute(
        "INSERT INTO relations (agent_id, subject_kind, subject_id, predicate, object_kind, object_id, "
        "anchor_ref, declared_by, declared_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent_id, subject[0], subject[1], predicate, obj[0], obj[1], anchor, "agent", _AT),
    )
    await db.commit()
    return cur.lastrowid


async def _count(db, table: str, where: str = "1=1", params: tuple = ()) -> int:
    rows = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
    return rows[0][0]


async def _relation_ids(db) -> set[int]:
    return {r[0] for r in await db.execute_fetchall("SELECT id FROM relations")}


async def _schema_version(db) -> int:
    rows = await db.execute_fetchall("SELECT MAX(version) FROM schema_version")
    return rows[0][0]


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------


def test_schema_version_is_at_least_15():
    """v15 = the declared graph (docs/ASSOCIATIVE_MEMORY_DESIGN.md §1).

    The floor, not the value: the graph arrived at v15 and later steps add
    tables beside it rather than change it. Pinning equality here made every
    later step fail a test about associations, which says nothing about
    whether the graph is intact.
    """
    assert SCHEMA_VERSION >= 15


@pytest.mark.asyncio
async def test_fresh_database_has_the_designed_tables():
    async with _TempDB():
        db = await database.get_db()

        async def columns(table):
            info = await db.execute_fetchall(f"PRAGMA table_info({table})")
            # (name, type, notnull, pk position) -- the design's §1 column for column.
            return [(c[1], c[2], c[3], c[5]) for c in info]

        assert await columns("entities") == [
            ("id", "INTEGER", 0, 1),
            ("agent_id", "TEXT", 1, 0),
            ("project_id", "TEXT", 1, 0),
            ("channel", "TEXT", 1, 0),
            ("name", "TEXT", 1, 0),
            ("normalized", "TEXT", 1, 0),
            ("declared_by", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ]
        assert await columns("entity_aliases") == [
            ("entity_id", "INTEGER", 1, 1),
            ("alias", "TEXT", 1, 0),
            ("normalized", "TEXT", 1, 2),
        ]
        assert await columns("entity_mentions") == [
            ("entity_id", "INTEGER", 1, 1),
            ("ref", "TEXT", 1, 2),
            ("declared_by", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ]
        assert await columns("relations") == [
            ("id", "INTEGER", 0, 1),
            ("agent_id", "TEXT", 1, 0),
            ("project_id", "TEXT", 1, 0),
            ("channel", "TEXT", 1, 0),
            ("subject_kind", "TEXT", 1, 0),
            ("subject_id", "INTEGER", 1, 0),
            ("predicate", "TEXT", 1, 0),
            ("object_kind", "TEXT", 1, 0),
            ("object_id", "INTEGER", 1, 0),
            ("anchor_ref", "TEXT", 1, 0),
            ("declared_by", "TEXT", 1, 0),
            ("declared_at", "TEXT", 1, 0),
        ]
        objects = {
            r[0]
            for r in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'index')")
        }
        assert set(_GRAPH_TRIGGERS) | set(_GRAPH_INDEXES) <= objects
        assert await _schema_version(db) == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_an_entity_name_is_unique_per_scope_and_a_relation_is_unique_per_declaration():
    async with _TempDB():
        db = await database.get_db()
        await _entity(db, "MizEye")
        with pytest.raises(Exception, match="UNIQUE"):
            await _entity(db, "MizEye")
        await db.rollback()
        # The same name in another agent's scope is another entity.
        await _entity(db, "MizEye", agent_id="agent-b")

        a = await _entity(db, "Kirari")
        b = await _entity(db, "mizeye-2")
        await _relation(db, ("entity", a), "maintains", ("entity", b))
        with pytest.raises(Exception, match="UNIQUE"):
            await _relation(db, ("entity", a), "maintains", ("entity", b))
        await db.rollback()
        # A different anchor is a different declaration of the same fact.
        evidence = await _memory(db, "kirari maintains it, said again")
        await _relation(db, ("entity", a), "maintains", ("entity", b), anchor=f"mem:{evidence}")


# --------------------------------------------------------------------------
# forward migration from v14
# --------------------------------------------------------------------------


async def _objects_outside_graph(db) -> dict[str, str]:
    rows = await db.execute_fetchall(
        "SELECT type || ':' || name, sql FROM sqlite_master "
        "WHERE name NOT IN ('entities', 'entity_aliases', 'entity_mentions', 'relations') "
        "AND name NOT LIKE 'associations_%' AND name NOT LIKE 'idx_entity_%' "
        "AND name NOT LIKE 'idx_relations_%' AND name NOT LIKE 'sqlite_autoindex_entit%' "
        "AND name NOT LIKE 'sqlite_autoindex_relations%'"
    )
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_v14_database_migrates_forward_without_touching_other_objects():
    async with _TempDB():
        # A v14 database: this build's schema minus everything v15 added, stamped 14.
        db = await database.get_db()
        for trigger in _GRAPH_TRIGGERS:
            await db.execute(f"DROP TRIGGER {trigger}")
        for index in _GRAPH_INDEXES:
            await db.execute(f"DROP INDEX {index}")
        for table in _GRAPH_TABLES:
            await db.execute(f"DROP TABLE {table}")
        await db.execute("DELETE FROM schema_version")
        await db.execute("INSERT INTO schema_version (version) VALUES (14)")
        await db.commit()
        mem = await _memory(db, "written before v15")
        ep = await _episode(db, "episode written before v15")
        before = await _objects_outside_graph(db)
        rows_before = (
            await db.execute_fetchall("SELECT id, content FROM memories"),
            await db.execute_fetchall("SELECT id, summary FROM episodes"),
        )

        db = await _reboot()

        assert await _schema_version(db) == SCHEMA_VERSION
        for table in _GRAPH_TABLES:
            assert await _count(db, table) == 0
        assert await _objects_outside_graph(db) == before
        assert (
            await db.execute_fetchall("SELECT id, content FROM memories"),
            await db.execute_fetchall("SELECT id, summary FROM episodes"),
        ) == rows_before
        # The migrated database's triggers work, not merely exist.
        e = await _entity(db, "Kirari")
        await _mention(db, e, f"mem:{mem}")
        await _mention(db, e, f"ep:{ep}")
        await db.execute("DELETE FROM memories WHERE id = ?", (mem,))
        await db.execute("DELETE FROM episodes WHERE id = ?", (ep,))
        await db.commit()
        assert await _count(db, "entity_mentions") == 0


@pytest.mark.asyncio
async def test_a_trigger_lost_on_a_stamped_database_comes_back_on_boot():
    """Not version-gated (bug-118): a v15 database missing a trigger regains it."""
    async with _TempDB():
        db = await database.get_db()
        await db.execute("DROP TRIGGER associations_memories_ad")
        await db.commit()

        db = await _reboot()

        mem = await _memory(db, "mentioned")
        e = await _entity(db, "Kirari")
        await _mention(db, e, f"mem:{mem}")
        await db.execute("DELETE FROM memories WHERE id = ?", (mem,))
        await db.commit()
        assert await _count(db, "entity_mentions") == 0


@pytest.mark.asyncio
async def test_declarations_survive_a_reboot():
    async with _TempDB():
        db = await database.get_db()
        a = await _entity(db, "Kirari", aliases=("kirari", "きらり"))
        b = await _entity(db, "MizEye")
        mem = await _memory(db, "kirari maintains mizeye")
        await _mention(db, a, f"mem:{mem}")
        await _relation(db, ("entity", a), "maintains", ("entity", b), anchor=f"mem:{mem}")

        db = await _reboot()

        assert await _count(db, "entities") == 2
        assert await _count(db, "entity_aliases") == 2
        assert await _count(db, "entity_mentions") == 1
        assert await _count(db, "relations") == 1


# --------------------------------------------------------------------------
# invariant 8 -- no declaration outlives its endpoints, through the real handlers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_an_entity_removes_its_aliases_mentions_and_relations_and_only_those():
    async with _TempDB():
        db = await database.get_db()
        gone = await _entity(db, "Gone", aliases=("g",))
        kept = await _entity(db, "Kept", aliases=("k",))
        third = await _entity(db, "Third")
        mem = await _memory(db, "a record")
        await _mention(db, gone, f"mem:{mem}")
        await _mention(db, kept, f"mem:{mem}")
        as_subject = await _relation(db, ("entity", gone), "maintains", ("entity", third))
        as_object = await _relation(db, ("entity", third), "depends_on", ("entity", gone))
        unrelated = await _relation(db, ("entity", kept), "maintains", ("entity", third))

        await db.execute("DELETE FROM entities WHERE id = ?", (gone,))
        await db.commit()

        assert await _count(db, "entity_aliases", "entity_id = ?", (gone,)) == 0
        assert await _count(db, "entity_aliases", "entity_id = ?", (kept,)) == 1
        assert await _count(db, "entity_mentions", "entity_id = ?", (gone,)) == 0
        assert await _count(db, "entity_mentions", "entity_id = ?", (kept,)) == 1
        assert await _relation_ids(db) == {unrelated}
        del as_subject, as_object


@pytest.mark.asyncio
async def test_delete_memory_removes_its_mentions_and_the_relations_it_anchors_or_joins():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "to delete")
        other = await _memory(db, "to keep")
        # An episode with the same numeric id as the deleted memory: the ref kind
        # is the only thing telling their mentions and relations apart.
        ep = await _episode(db, "same id, other kind")
        assert ep == mem
        e = await _entity(db, "Kirari")
        f = await _entity(db, "MizEye")
        await _mention(db, e, f"mem:{mem}")
        await _mention(db, e, f"mem:{other}")
        await _mention(db, e, f"ep:{ep}")
        anchored = await _relation(db, ("entity", e), "maintains", ("entity", f), anchor=f"mem:{mem}")
        as_subject = await _relation(db, ("mem", mem), "corrects", ("mem", other))
        as_object = await _relation(db, ("mem", other), "supersedes", ("mem", mem))
        same_id_episode = await _relation(db, ("ep", ep), "supports", ("mem", other))
        kept = await _relation(db, ("entity", e), "maintains", ("entity", f), anchor=f"mem:{other}")

        result = await admin_handlers.do_delete_memory(mem, agent_id="agent-a")

        assert result.get("ok") is True, result
        refs = {r[0] for r in await db.execute_fetchall("SELECT ref FROM entity_mentions")}
        assert refs == {f"mem:{other}", f"ep:{ep}"}
        assert await _relation_ids(db) == {same_id_episode, kept}
        del anchored, as_subject, as_object


@pytest.mark.asyncio
async def test_delete_episode_removes_its_mentions_and_relations_and_only_those():
    async with _TempDB():
        db = await database.get_db()
        ep = await _episode(db, "to delete")
        mem = await _memory(db, "same id, other kind")
        assert ep == mem
        e = await _entity(db, "Kirari")
        await _mention(db, e, f"ep:{ep}")
        await _mention(db, e, f"mem:{mem}")
        gone = await _relation(db, ("ep", ep), "supports", ("mem", mem))
        anchored = await _relation(db, ("mem", mem), "qualifies", ("mem", mem), anchor=f"ep:{ep}")
        kept = await _relation(db, ("mem", mem), "supersedes", ("mem", mem), anchor=f"mem:{mem}")

        result = await admin_handlers.do_delete_episode(ep, agent_id="agent-a")

        assert result.get("ok") is True, result
        refs = {r[0] for r in await db.execute_fetchall("SELECT ref FROM entity_mentions")}
        assert refs == {f"mem:{mem}"}
        assert await _relation_ids(db) == {kept}
        del gone, anchored


@pytest.mark.asyncio
async def test_delete_agent_data_removes_that_agents_whole_graph():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "gone", agent_id="agent-gone")
        gone = await _entity(db, "Gone", agent_id="agent-gone", aliases=("g",))
        gone_2 = await _entity(db, "Gone2", agent_id="agent-gone")
        await _mention(db, gone, f"mem:{mem}")
        # A relation with no anchor and no record endpoint: only the agent axis
        # can remove it, so the handler has to clear the graph tables itself.
        await _relation(db, ("entity", gone), "maintains", ("entity", gone_2), agent_id="agent-gone")
        kept_mem = await _memory(db, "kept", agent_id="agent-kept")
        kept = await _entity(db, "Kept", agent_id="agent-kept", aliases=("k",))
        await _mention(db, kept, f"mem:{kept_mem}")
        await _relation(db, ("entity", kept), "maintains", ("entity", kept), agent_id="agent-kept")

        result = await admin_handlers.do_delete_agent_data("agent-gone")

        assert result.get("ok") is True, result
        assert await _count(db, "entities", "agent_id = ?", ("agent-gone",)) == 0
        assert await _count(db, "entity_aliases", "entity_id IN (?, ?)", (gone, gone_2)) == 0
        assert await _count(db, "entity_mentions", "entity_id IN (?, ?)", (gone, gone_2)) == 0
        assert await _count(db, "relations", "agent_id = ?", ("agent-gone",)) == 0
        assert await _count(db, "entities", "agent_id = ?", ("agent-kept",)) == 1
        assert await _count(db, "entity_aliases", "entity_id = ?", (kept,)) == 1
        assert await _count(db, "entity_mentions", "entity_id = ?", (kept,)) == 1
        assert await _count(db, "relations", "agent_id = ?", ("agent-kept",)) == 1


@pytest.mark.asyncio
async def test_rewriting_a_memory_keeps_its_mentions():
    """A mention is a declaration about the record, not a derivation from its text (design §1)."""
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "the original wording")
        e = await _entity(db, "Kirari")
        await _mention(db, e, f"mem:{mem}")
        await _relation(db, ("entity", e), "maintains", ("entity", e), anchor=f"mem:{mem}")

        result = await admin_handlers.do_update_memory(mem, "a rewritten wording", agent_id="agent-a")

        assert result.get("ok") is True, result
        assert await _count(db, "entity_mentions") == 1
        assert await _count(db, "relations") == 1


# --------------------------------------------------------------------------
# the health check watches the triggers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_schema_objects_reports_and_repairs_a_missing_graph_trigger():
    async with _TempDB():
        db = await database.get_db()
        await db.execute("DROP TRIGGER associations_entities_ad")
        await db.commit()

        issues = await checks.check_schema_objects(db, "", fix=False)
        missing = [i for i in issues if i["object"] == "associations_entities_ad"]
        assert missing and missing[0]["state"] == "missing" and missing[0]["severity"] == "critical"

        issues = await checks.check_schema_objects(db, "", fix=True)
        repaired = [i for i in issues if i["object"] == "associations_entities_ad"]
        assert repaired and repaired[0]["fixed"] is True
        await db.commit()
        e = await _entity(db, "Kirari", aliases=("k",))
        await db.execute("DELETE FROM entities WHERE id = ?", (e,))
        await db.commit()
        assert await _count(db, "entity_aliases") == 0
