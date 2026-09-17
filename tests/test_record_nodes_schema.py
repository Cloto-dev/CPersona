"""SCHEMA_VERSION 14: the record_nodes table and the triggers that keep it true.

docs/OVERFLOW_TREE_DESIGN.md §1 (schema) and §4 (consistency). Nothing here
builds nodes -- that is the write path's job. These tests pin the storage layer:
the table has the designed shape, an existing database reaches it without any
other object changing, and no node survives a delete or a text change of its
parent (invariant 6), whichever handler performs it.

Nodes are inserted by hand. The triggers do not care who wrote a node, and a
test that needed the divider to exist could not tell a trigger defect from a
divider defect.

Like test_schema_v9_migration, each test owns a database file and restores the
module-level connection on exit.
"""

import os
import tempfile

import pytest

from cpersona import admin_handlers, checks, database, session
from cpersona.database import SCHEMA_VERSION

_NODE_TRIGGERS = (
    "record_nodes_memories_ad",
    "record_nodes_memories_au",
    "record_nodes_episodes_ad",
    "record_nodes_episodes_au",
)


class _TempDB:
    def __init__(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "record_nodes.db")

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


async def _memory(db, content: str, agent_id: str = "agent-n") -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
        (agent_id, content, "2026-09-17T00:00:00+00:00"),
    )
    await db.commit()
    return cur.lastrowid


async def _episode(db, summary: str, agent_id: str = "agent-n") -> int:
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords) VALUES (?, ?, ?)",
        (agent_id, summary, "kw"),
    )
    await db.commit()
    return cur.lastrowid


async def _nodes(db, kind: str, parent_id: int, count: int = 3) -> None:
    for i in range(count):
        await db.execute(
            "INSERT INTO record_nodes (parent_kind, parent_id, node_index, start_char, "
            "end_char, token_count, window, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, parent_id, i, i * 10, (i + 1) * 10, 5, 512, "test-model"),
        )
    await db.commit()


async def _node_count(db, kind: str, parent_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM record_nodes WHERE parent_kind = ? AND parent_id = ?",
        (kind, parent_id),
    )
    return rows[0][0]


async def _schema_version(db) -> int:
    rows = await db.execute_fetchall("SELECT MAX(version) FROM schema_version")
    return rows[0][0]


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------


def test_schema_version_constant_is_14():
    """v14 = record_nodes (docs/OVERFLOW_TREE_DESIGN.md)."""
    assert SCHEMA_VERSION == 14


@pytest.mark.asyncio
async def test_fresh_database_has_the_designed_table():
    async with _TempDB():
        db = await database.get_db()
        info = await db.execute_fetchall("PRAGMA table_info(record_nodes)")
        # (name, type, notnull, pk position) -- the design's §1 column for column.
        assert [(c[1], c[2], c[3], c[5]) for c in info] == [
            ("parent_kind", "TEXT", 1, 1),
            ("parent_id", "INTEGER", 1, 2),
            ("node_index", "INTEGER", 1, 3),
            ("start_char", "INTEGER", 1, 0),
            ("end_char", "INTEGER", 1, 0),
            ("token_count", "INTEGER", 1, 0),
            ("window", "INTEGER", 1, 0),
            ("embedding", "BLOB", 0, 0),
            ("embedding_model", "TEXT", 1, 0),
        ]
        triggers = {
            r[0]
            for r in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name IN ('memories', 'episodes')"
            )
        }
        assert set(_NODE_TRIGGERS) <= triggers
        assert await _schema_version(db) == 14


@pytest.mark.asyncio
async def test_a_node_key_is_unique_per_parent_and_index():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "parent")
        await _nodes(db, "mem", mem, count=1)
        with pytest.raises(Exception, match="UNIQUE"):
            await _nodes(db, "mem", mem, count=1)
        await db.rollback()


# --------------------------------------------------------------------------
# forward migration from v13
# --------------------------------------------------------------------------


async def _objects_outside_nodes(db) -> dict[str, str]:
    rows = await db.execute_fetchall(
        "SELECT type || ':' || name, sql FROM sqlite_master "
        "WHERE name != 'record_nodes' AND name NOT LIKE 'record_nodes_%' "
        "AND name NOT LIKE 'sqlite_autoindex_record_nodes%'"
    )
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_v13_database_migrates_forward_without_touching_other_objects():
    async with _TempDB():
        # A v13 database: this build's schema minus everything v14 added, stamped 13.
        db = await database.get_db()
        for trigger in _NODE_TRIGGERS:
            await db.execute(f"DROP TRIGGER {trigger}")
        await db.execute("DROP TABLE record_nodes")
        await db.execute("DELETE FROM schema_version")
        await db.execute("INSERT INTO schema_version (version) VALUES (13)")
        await db.commit()
        mem = await _memory(db, "written before v14")
        ep = await _episode(db, "episode written before v14")
        before = await _objects_outside_nodes(db)
        rows_before = (
            await db.execute_fetchall("SELECT id, content FROM memories"),
            await db.execute_fetchall("SELECT id, summary FROM episodes"),
        )

        db = await _reboot()

        assert await _schema_version(db) == 14
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_nodes") == [(0,)]
        assert await _objects_outside_nodes(db) == before
        assert (
            await db.execute_fetchall("SELECT id, content FROM memories"),
            await db.execute_fetchall("SELECT id, summary FROM episodes"),
        ) == rows_before
        # The migrated database's triggers work, not merely exist.
        await _nodes(db, "mem", mem)
        await _nodes(db, "ep", ep)
        await db.execute("DELETE FROM memories WHERE id = ?", (mem,))
        await db.execute("DELETE FROM episodes WHERE id = ?", (ep,))
        await db.commit()
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_nodes") == [(0,)]


@pytest.mark.asyncio
async def test_a_trigger_lost_on_a_stamped_database_comes_back_on_boot():
    """Not version-gated (bug-118): a v14 database missing a trigger regains it."""
    async with _TempDB():
        db = await database.get_db()
        await db.execute("DROP TRIGGER record_nodes_memories_ad")
        await db.commit()

        db = await _reboot()

        mem = await _memory(db, "parent")
        await _nodes(db, "mem", mem)
        await db.execute("DELETE FROM memories WHERE id = ?", (mem,))
        await db.commit()
        assert await _node_count(db, "mem", mem) == 0


@pytest.mark.asyncio
async def test_existing_nodes_survive_a_reboot():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "parent")
        await _nodes(db, "mem", mem)

        db = await _reboot()

        assert await _node_count(db, "mem", mem) == 3


# --------------------------------------------------------------------------
# invariant 6 -- no stale node survives, through the real handlers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_memory_removes_its_nodes_and_only_its_nodes():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "to delete")
        other = await _memory(db, "to keep")
        # An episode with the same numeric id as the deleted memory: parent_kind is
        # the only thing telling their nodes apart.
        ep = await _episode(db, "same id, other kind")
        assert ep == mem
        for kind, pid in (("mem", mem), ("mem", other), ("ep", ep)):
            await _nodes(db, kind, pid)

        result = await admin_handlers.do_delete_memory(mem, agent_id="agent-n")

        assert result.get("ok") is True, result
        assert await _node_count(db, "mem", mem) == 0
        assert await _node_count(db, "mem", other) == 3
        assert await _node_count(db, "ep", ep) == 3


@pytest.mark.asyncio
async def test_delete_episode_removes_its_nodes_and_only_its_nodes():
    async with _TempDB():
        db = await database.get_db()
        ep = await _episode(db, "to delete")
        mem = await _memory(db, "same id, other kind")
        assert ep == mem
        await _nodes(db, "ep", ep)
        await _nodes(db, "mem", mem)

        result = await admin_handlers.do_delete_episode(ep, agent_id="agent-n")

        assert result.get("ok") is True, result
        assert await _node_count(db, "ep", ep) == 0
        assert await _node_count(db, "mem", mem) == 3


@pytest.mark.asyncio
async def test_delete_agent_data_removes_every_node_of_that_agent():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "gone", agent_id="agent-gone")
        ep = await _episode(db, "gone", agent_id="agent-gone")
        kept = await _memory(db, "kept", agent_id="agent-kept")
        await _nodes(db, "mem", mem)
        await _nodes(db, "ep", ep)
        await _nodes(db, "mem", kept)

        result = await admin_handlers.do_delete_agent_data("agent-gone")

        assert result.get("ok") is True, result
        assert await _node_count(db, "mem", mem) == 0
        assert await _node_count(db, "ep", ep) == 0
        assert await _node_count(db, "mem", kept) == 3


@pytest.mark.asyncio
async def test_update_memory_with_new_text_removes_its_nodes():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "the original wording")
        other = await _memory(db, "a neighbour")
        await _nodes(db, "mem", mem)
        await _nodes(db, "mem", other)

        result = await admin_handlers.do_update_memory(mem, "a rewritten wording", agent_id="agent-n")

        assert result.get("ok") is True, result
        assert await _node_count(db, "mem", mem) == 0
        assert await _node_count(db, "mem", other) == 3


@pytest.mark.asyncio
async def test_rewriting_a_memory_with_the_same_text_keeps_its_nodes():
    """The spans still describe the text, so there is nothing to rebuild (bug-012 shape)."""
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "unchanged wording")
        await _nodes(db, "mem", mem)

        await db.execute("UPDATE memories SET content = content, locked = 1 WHERE id = ?", (mem,))
        await db.execute("UPDATE memories SET recall_count = recall_count + 1 WHERE id = ?", (mem,))
        await db.commit()

        assert await _node_count(db, "mem", mem) == 3


@pytest.mark.asyncio
async def test_changing_an_episode_summary_removes_its_nodes_and_keywords_do_not():
    async with _TempDB():
        db = await database.get_db()
        ep = await _episode(db, "the original summary")
        await _nodes(db, "ep", ep)

        # Keywords are not the text a node spans.
        await db.execute("UPDATE episodes SET keywords = 'other', summary = summary WHERE id = ?", (ep,))
        await db.commit()
        assert await _node_count(db, "ep", ep) == 3

        await db.execute("UPDATE episodes SET summary = 'a different summary' WHERE id = ?", (ep,))
        await db.commit()
        assert await _node_count(db, "ep", ep) == 0


# --------------------------------------------------------------------------
# health check watches the triggers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", _NODE_TRIGGERS)
async def test_a_missing_node_trigger_is_critical_and_repaired(trigger):
    async with _TempDB():
        db = await database.get_db()
        await db.execute(f"DROP TRIGGER {trigger}")
        await db.commit()

        issues = await checks.check_schema_objects(db, "", fix=False)
        assert [(i["object"], i["state"], i["severity"]) for i in issues] == [
            (trigger, "missing", "critical")
        ]

        fixed = await checks.check_schema_objects(db, "", fix=True)
        await db.commit()
        assert [i.get("fixed") for i in fixed] == [True]
        assert await checks.check_schema_objects(db, "", fix=False) == []
