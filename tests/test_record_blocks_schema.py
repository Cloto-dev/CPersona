"""SCHEMA_VERSION 16: the record_blocks table and the triggers that keep it true.

docs/BLOCK_REACH_DESIGN.md §6 (schema and lifecycle) and §8 (invariants).
Nothing here divides a record -- that is the segmenter's job, and a test that
needed it could not tell a trigger defect from a segmenter defect. Blocks are
inserted by hand; the triggers do not care who wrote them.

Two behaviours are specific to this table and have no counterpart in the node
tests. Blocks carry the isolation axes, because a coarse pass that filtered
after its top-k cut would spend that cut on rows the authority then drops. So
a retag has to reach them -- and must NOT destroy them, since the text is
unchanged and the vectors stay valid. The axis triggers update; the content
triggers delete.

Like test_record_nodes_schema, each test owns a database file and restores the
module-level connection on exit.
"""

import os
import tempfile

import pytest

from cpersona import admin_handlers, checks, database, session
from cpersona.database import SCHEMA_VERSION

_BLOCK_TRIGGERS = (
    "record_blocks_memories_ad",
    "record_blocks_memories_au",
    "record_blocks_memories_ax",
    "record_blocks_episodes_ad",
    "record_blocks_episodes_au",
    "record_blocks_episodes_ax",
)


class _TempDB:
    def __init__(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "record_blocks.db")

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


async def _memory(
    db, content: str, agent_id: str = "agent-b", project_id: str = "", channel: str = ""
) -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (agent_id, project_id, channel, content, "2026-09-21T00:00:00+00:00"),
    )
    await db.commit()
    return cur.lastrowid


async def _episode(
    db, summary: str, agent_id: str = "agent-b", project_id: str = "", channel: str = ""
) -> int:
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords) "
        "VALUES (?, ?, ?, ?, ?)",
        (agent_id, project_id, channel, summary, "kw"),
    )
    await db.commit()
    return cur.lastrowid


async def _blocks(
    db,
    kind: str,
    parent_id: int,
    count: int = 3,
    agent_id: str = "agent-b",
    project_id: str = "",
    channel: str = "",
) -> None:
    for i in range(count):
        await db.execute(
            "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, "
            "project_id, channel, start_char, end_char, forced_boundary, "
            "embedding_bits, embedding_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                parent_id,
                i,
                agent_id,
                project_id,
                channel,
                i * 10,
                (i + 1) * 10,
                0,
                b"\x00" * 128,
                "test-model",
            ),
        )
    await db.commit()


async def _block_count(db, kind: str, parent_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM record_blocks WHERE parent_kind = ? AND parent_id = ?",
        (kind, parent_id),
    )
    return rows[0][0]


async def _axes(db, kind: str, parent_id: int) -> set[tuple[str, str, str]]:
    rows = await db.execute_fetchall(
        "SELECT agent_id, project_id, channel FROM record_blocks "
        "WHERE parent_kind = ? AND parent_id = ?",
        (kind, parent_id),
    )
    return {tuple(r) for r in rows}


async def _schema_version(db) -> int:
    rows = await db.execute_fetchall("SELECT MAX(version) FROM schema_version")
    return rows[0][0]


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------


def test_schema_version_is_at_least_16():
    assert SCHEMA_VERSION >= 16


@pytest.mark.asyncio
async def test_fresh_database_has_the_designed_table():
    async with _TempDB():
        db = await database.get_db()
        info = await db.execute_fetchall("PRAGMA table_info(record_blocks)")
        # (name, type, notnull, pk position) -- §6 column for column. The axes
        # sit next to the identity because they are read with it, and the
        # vector is a bit string rather than a float32 blob (§3).
        assert [(c[1], c[2], c[3], c[5]) for c in info] == [
            ("parent_kind", "TEXT", 1, 1),
            ("parent_id", "INTEGER", 1, 2),
            ("block_index", "INTEGER", 1, 3),
            ("agent_id", "TEXT", 1, 0),
            ("project_id", "TEXT", 1, 0),
            ("channel", "TEXT", 1, 0),
            ("start_char", "INTEGER", 1, 0),
            ("end_char", "INTEGER", 1, 0),
            ("forced_boundary", "INTEGER", 1, 0),
            ("embedding_bits", "BLOB", 0, 0),
            ("embedding_model", "TEXT", 1, 0),
        ]
        triggers = {
            r[0]
            for r in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('memories', 'episodes')"
            )
        }
        assert set(_BLOCK_TRIGGERS) <= triggers
        assert await _schema_version(db) == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_the_axis_index_exists():
    """Without it the coarse pass filters by scanning, which is the thing it
    exists not to do."""
    async with _TempDB():
        db = await database.get_db()
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'record_blocks'"
        )
        assert "idx_record_blocks_axes" in {r[0] for r in rows}


@pytest.mark.asyncio
async def test_a_block_key_is_unique_per_parent_and_index():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "parent")
        await _blocks(db, "mem", mem, count=1)
        with pytest.raises(Exception, match="UNIQUE"):
            await _blocks(db, "mem", mem, count=1)
        await db.rollback()


# --------------------------------------------------------------------------
# forward migration from v15
# --------------------------------------------------------------------------


async def _objects_outside_blocks(db) -> dict[str, str]:
    rows = await db.execute_fetchall(
        "SELECT type || ':' || name, sql FROM sqlite_master "
        "WHERE name != 'record_blocks' AND name NOT LIKE 'record_blocks_%' "
        "AND name NOT LIKE 'idx_record_blocks%' "
        "AND name NOT LIKE 'sqlite_autoindex_record_blocks%'"
    )
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_v15_database_migrates_forward_without_touching_other_objects():
    async with _TempDB():
        # A v15 database: this build's schema minus everything v16 added, stamped 15.
        db = await database.get_db()
        for trigger in _BLOCK_TRIGGERS:
            await db.execute(f"DROP TRIGGER {trigger}")
        await db.execute("DROP INDEX idx_record_blocks_axes")
        await db.execute("DROP TABLE record_blocks")
        await db.execute("DELETE FROM schema_version")
        await db.execute("INSERT INTO schema_version (version) VALUES (15)")
        await db.commit()
        mem = await _memory(db, "written before v16")
        ep = await _episode(db, "episode written before v16")
        before = await _objects_outside_blocks(db)
        rows_before = (
            await db.execute_fetchall("SELECT id, content FROM memories"),
            await db.execute_fetchall("SELECT id, summary FROM episodes"),
        )

        db = await _reboot()

        assert await _schema_version(db) == SCHEMA_VERSION
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_blocks") == [(0,)]
        assert await _objects_outside_blocks(db) == before
        assert (
            await db.execute_fetchall("SELECT id, content FROM memories"),
            await db.execute_fetchall("SELECT id, summary FROM episodes"),
        ) == rows_before
        # The migrated database's triggers work, not merely exist.
        await _blocks(db, "mem", mem)
        await _blocks(db, "ep", ep)
        await db.execute("DELETE FROM memories WHERE id = ?", (mem,))
        await db.execute("DELETE FROM episodes WHERE id = ?", (ep,))
        await db.commit()
        assert await db.execute_fetchall("SELECT COUNT(*) FROM record_blocks") == [(0,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", _BLOCK_TRIGGERS)
async def test_a_trigger_lost_on_a_stamped_database_comes_back_on_boot(trigger):
    """Not version-gated (bug-118): a v16 database missing one regains it."""
    async with _TempDB():
        db = await database.get_db()
        await db.execute(f"DROP TRIGGER {trigger}")
        await db.commit()

        db = await _reboot()

        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?", (trigger,)
        )
        assert rows, f"{trigger} did not come back"


@pytest.mark.asyncio
async def test_existing_blocks_survive_a_reboot():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "parent")
        await _blocks(db, "mem", mem)

        db = await _reboot()

        assert await _block_count(db, "mem", mem) == 3


# --------------------------------------------------------------------------
# no stale block survives its parent's text
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_memory_removes_its_blocks_and_only_its_blocks():
    async with _TempDB():
        db = await database.get_db()
        doomed = await _memory(db, "doomed")
        spared = await _memory(db, "spared")
        await _blocks(db, "mem", doomed)
        await _blocks(db, "mem", spared)

        await db.execute("DELETE FROM memories WHERE id = ?", (doomed,))
        await db.commit()

        assert await _block_count(db, "mem", doomed) == 0
        assert await _block_count(db, "mem", spared) == 3


@pytest.mark.asyncio
async def test_delete_episode_removes_its_blocks_and_only_its_blocks():
    async with _TempDB():
        db = await database.get_db()
        doomed = await _episode(db, "doomed")
        spared = await _episode(db, "spared")
        await _blocks(db, "ep", doomed)
        await _blocks(db, "ep", spared)

        await db.execute("DELETE FROM episodes WHERE id = ?", (doomed,))
        await db.commit()

        assert await _block_count(db, "ep", doomed) == 0
        assert await _block_count(db, "ep", spared) == 3


@pytest.mark.asyncio
async def test_delete_agent_data_removes_every_block_of_that_agent():
    """Through the real handler: blocks are reached by the triggers, not by a
    call-site the handler has to remember."""
    async with _TempDB():
        db = await database.get_db()
        mine = await _memory(db, "mine", agent_id="agent-b")
        theirs = await _memory(db, "theirs", agent_id="agent-other")
        ep = await _episode(db, "mine", agent_id="agent-b")
        await _blocks(db, "mem", mine)
        await _blocks(db, "mem", theirs, agent_id="agent-other")
        await _blocks(db, "ep", ep)

        result = await admin_handlers.do_delete_agent_data("agent-b")
        assert result["ok"] is True

        db = await database.get_db()
        assert await _block_count(db, "mem", mine) == 0
        assert await _block_count(db, "ep", ep) == 0
        assert await _block_count(db, "mem", theirs) == 3


@pytest.mark.asyncio
async def test_update_memory_with_new_text_removes_its_blocks():
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "before")
        await _blocks(db, "mem", mem)

        await db.execute("UPDATE memories SET content = ? WHERE id = ?", ("after", mem))
        await db.commit()

        assert await _block_count(db, "mem", mem) == 0


@pytest.mark.asyncio
async def test_rewriting_a_memory_with_the_same_text_keeps_its_blocks():
    """The WHEN guard: an update that writes the text with itself leaves every
    span still true, so rebuilding them would be pure cost."""
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "unchanged")
        await _blocks(db, "mem", mem)

        await db.execute("UPDATE memories SET content = ? WHERE id = ?", ("unchanged", mem))
        await db.commit()

        assert await _block_count(db, "mem", mem) == 3


@pytest.mark.asyncio
async def test_changing_an_episode_summary_removes_its_blocks_and_keywords_do_not():
    async with _TempDB():
        db = await database.get_db()
        ep = await _episode(db, "before")
        await _blocks(db, "ep", ep)

        await db.execute("UPDATE episodes SET keywords = ? WHERE id = ?", ("other", ep))
        await db.commit()
        assert await _block_count(db, "ep", ep) == 3

        await db.execute("UPDATE episodes SET summary = ? WHERE id = ?", ("after", ep))
        await db.commit()
        assert await _block_count(db, "ep", ep) == 0


# --------------------------------------------------------------------------
# a retag moves the axes and keeps the vectors (§6)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,value",
    [("project_id", "moved"), ("channel", "elsewhere"), ("agent_id", "agent-new")],
)
async def test_retagging_a_memory_updates_the_axes_and_keeps_the_blocks(column, value):
    """The text did not change, so the vectors are still valid -- rebuilding
    them would spend embedding calls to arrive at identical bits."""
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "text", project_id="before", channel="here")
        await _blocks(db, "mem", mem, project_id="before", channel="here")

        await db.execute(f"UPDATE memories SET {column} = ? WHERE id = ?", (value, mem))
        await db.commit()

        assert await _block_count(db, "mem", mem) == 3
        axes = await _axes(db, "mem", mem)
        assert len(axes) == 1
        row = await db.execute_fetchall(
            "SELECT agent_id, project_id, channel FROM memories WHERE id = ?", (mem,)
        )
        assert axes == {tuple(row[0])}


@pytest.mark.asyncio
async def test_retagging_an_episode_updates_the_axes_and_keeps_the_blocks():
    async with _TempDB():
        db = await database.get_db()
        ep = await _episode(db, "text", project_id="before")
        await _blocks(db, "ep", ep, project_id="before")

        await db.execute("UPDATE episodes SET project_id = ? WHERE id = ?", ("moved", ep))
        await db.commit()

        assert await _block_count(db, "ep", ep) == 3
        assert await _axes(db, "ep", ep) == {("agent-b", "moved", "")}


@pytest.mark.asyncio
async def test_writing_the_same_axes_back_does_not_touch_the_blocks():
    """The WHEN guard on the axis triggers.

    Asserting the axes afterwards cannot see this: an unguarded trigger would
    write the same values back and the assertion would still pass. The count of
    changed rows can — a no-op retag must change the record row and nothing
    else, so dropping the guard makes this red.
    """
    async with _TempDB():
        db = await database.get_db()
        mem = await _memory(db, "text", project_id="same", channel="same")
        await _blocks(db, "mem", mem, project_id="same", channel="same")

        before = db.total_changes
        await db.execute("UPDATE memories SET project_id = ? WHERE id = ?", ("same", mem))
        await db.commit()
        changed = db.total_changes - before

        assert changed == 1, f"a no-op retag changed {changed} rows, not just the record"
        assert await _axes(db, "mem", mem) == {("agent-b", "same", "same")}
        assert await _block_count(db, "mem", mem) == 3


@pytest.mark.asyncio
async def test_a_retag_touches_only_that_record_s_blocks():
    async with _TempDB():
        db = await database.get_db()
        moved = await _memory(db, "moved", project_id="before")
        stayed = await _memory(db, "stayed", project_id="before")
        await _blocks(db, "mem", moved, project_id="before")
        await _blocks(db, "mem", stayed, project_id="before")

        await db.execute("UPDATE memories SET project_id = ? WHERE id = ?", ("after", moved))
        await db.commit()

        assert await _axes(db, "mem", moved) == {("agent-b", "after", "")}
        assert await _axes(db, "mem", stayed) == {("agent-b", "before", "")}


# --------------------------------------------------------------------------
# the health check sees a missing object
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", _BLOCK_TRIGGERS)
async def test_a_missing_block_trigger_is_critical_and_repaired(trigger):
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


@pytest.mark.asyncio
async def test_a_missing_axis_index_is_reported_and_repaired():
    async with _TempDB():
        db = await database.get_db()
        await db.execute("DROP INDEX idx_record_blocks_axes")
        await db.commit()

        issues = await checks.check_schema_objects(db, "", fix=False)
        assert [(i["object"], i["state"]) for i in issues] == [
            ("idx_record_blocks_axes", "missing")
        ]

        fixed = await checks.check_schema_objects(db, "", fix=True)
        await db.commit()
        assert [i.get("fixed") for i in fixed] == [True]
        assert await checks.check_schema_objects(db, "", fix=False) == []
