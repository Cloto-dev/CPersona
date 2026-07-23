"""Regression tests for the 2.5.2a2 audit remediation, fix group I2 (bug-144, bug-145).

bug-144 (checks.py): ``check_invalid_source_type`` / ``check_anonymous_source`` wrapped
their whole-agent ``json_extract`` COUNT in ``except Exception: return []``. A
single malformed-JSON source row makes SQLite's ``json_extract`` raise
``OperationalError: malformed JSON``, so the SELECT aborts and the bare except
reports the entire agent clean — a silent false negative that also blocks the
mapped fixer. The fix adds a leading ``json_valid(source)`` guard (SQLite
short-circuits AND left-to-right) so malformed rows are excluded (they remain
``check_invalid_json``'s responsibility) and the check keeps working.

bug-145 (database.py + checks.py): a pre-v12 DB with two rows sharing a non-empty
``(agent_id, project_id, msg_id)`` but different content permanently blocks the
``idx_memories_dedup_msg_id`` UNIQUE index — the v12 migration swallowed the
CREATE failure and no remediation path existed. Both sides now resolve the
collision non-destructively (keep the newest row's msg_id, blank the older
unlocked colliders' msg_id; never delete a row, never touch content).
"""
import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from cpersona import checks, database
from cpersona.database import get_db


class _TempDB:
    """Point ``get_db()`` at a throwaway file, restoring global state on exit.

    Mirrors the isolation harness in ``test_schema_v9_migration.py`` so these
    tests neither observe nor pollute the shared session DB other files use.
    ``get_db()`` resolves the path via the module-level ``database.DB_PATH``
    name, so that (not ``config.DB_PATH``) is what must be swapped.
    """

    def __init__(self, name: str = "i2_test.db"):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, name)
        self._saved_db = None
        self._saved_path = None

    async def __aenter__(self):
        self._saved_db = database._db
        self._saved_path = database.DB_PATH
        database._db = None
        database.DB_PATH = self.path
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db = self._saved_db
        database.DB_PATH = self._saved_path


@pytest_asyncio.fixture
async def temp_db():
    """A brand-new current-schema DB on its own file (index present, empty tables)."""
    async with _TempDB():
        db = await get_db()
        yield db


# ---------------------------------------------------------------------------
# bug-144 — a malformed-JSON source row must not mask real source findings for the
# rest of the agent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug144_malformed_source_does_not_mask_invalid_type_and_anonymous(temp_db):
    db = temp_db
    agent = "c5"
    # (a) a malformed-JSON source row — check_invalid_json's territory, and the
    #     row that makes json_extract raise on the OLD whole-agent COUNT.
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        (agent, "bad json row", "not json at all", "2026-01-01T00:00:00Z"),
    )
    # (b) a valid-but-non-canonical source type — invalid_source_type SHOULD flag it.
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        (agent, "bot row", '{"type":"Bot","id":"b","name":"n"}', "2026-01-01T00:00:00Z"),
    )
    # (c) an anonymous User source — anonymous_source SHOULD flag it.
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        (agent, "anon row", '{"type":"User","id":"","name":""}', "2026-01-01T00:00:00Z"),
    )
    await db.commit()

    # On the unfixed code json_extract raises on the malformed row and the bare
    # `except Exception: return []` reports the whole agent clean, so both of
    # these assertions fire (the finding lists come back empty).
    it = await checks.check_invalid_source_type(db, agent, fix=False)
    assert any(i["type"] == "invalid_source_type" for i in it), (
        "malformed-JSON source row masked the Bot-type finding (check returned clean)"
    )
    inv = next(i for i in it if i["type"] == "invalid_source_type")
    # The malformed row is excluded (it is check_invalid_json's responsibility),
    # so only the Bot-type row is counted.
    assert inv["count"] == 1, f"expected exactly the Bot row, got count={inv['count']}"

    an = await checks.check_anonymous_source(db, agent, fix=False)
    assert any(i["type"] == "anonymous_source" for i in an), (
        "malformed-JSON source row masked the anonymous-source finding"
    )
    anon = next(i for i in an if i["type"] == "anonymous_source")
    assert anon["count"] == 1, f"expected exactly the anonymous row, got count={anon['count']}"


@pytest.mark.asyncio
async def test_bug144_check_invalid_json_still_owns_the_malformed_row(temp_db):
    """The guard hands malformed rows to check_invalid_json, which still sees them —
    the row is excluded from the source-type checks, not from detection entirely."""
    db = temp_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        ("c5b", "bad json row", "not json at all", "2026-01-01T00:00:00Z"),
    )
    await db.commit()
    ij = await checks.check_invalid_json(db, "c5b", fix=False)
    assert ij and ij[0]["bad_source"] == 1, (
        "the malformed source row must remain visible to check_invalid_json"
    )


# ---------------------------------------------------------------------------
# bug-145 (a) — the v12 migration resolves a pre-v12 msg_id collision so the UNIQUE
# index is created, without deleting a row or touching content.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug145_v11_migration_resolves_msg_id_collision_without_deleting_rows():
    async with _TempDB("i2_migration.db") as tmp:
        # Build a schema_version=11 DB from the real table DDL (the memories
        # columns are identical at v11) WITHOUT the v12 dedup indexes, so the
        # migration ladder runs the `current < 12` step.
        conn = await aiosqlite.connect(tmp.path)
        await conn.executescript(database.SCHEMA_SQL)
        # Two UNLOCKED rows sharing (agent_id, project_id, msg_id)=('a','','m1')
        # with DIFFERENT content — the pre-v12 concurrent-store TOCTOU race the
        # msg_id UNIQUE index was added to close.
        await conn.execute(
            "INSERT INTO memories (agent_id, project_id, msg_id, content, timestamp) "
            "VALUES ('a', '', 'm1', 'foo', '2026-01-01T00:00:00Z')"
        )
        await conn.execute(
            "INSERT INTO memories (agent_id, project_id, msg_id, content, timestamp) "
            "VALUES ('a', '', 'm1', 'bar', '2026-01-02T00:00:00Z')"
        )
        await conn.execute("INSERT INTO schema_version (version) VALUES (11)")
        await conn.commit()
        await conn.close()

        # Boot through get_db(): runs the real v12 migration (collision resolve +
        # index creation) then v13.
        db = await get_db()

        idx = await db.execute_fetchall(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_memories_dedup_msg_id'"
        )
        assert idx, (
            "idx_memories_dedup_msg_id absent after migration — the msg_id "
            "collision permanently blocked the UNIQUE index (v12 swallowed the "
            "CREATE failure with no remediation)"
        )
        # No row was deleted; content untouched.
        rows = await db.execute_fetchall(
            "SELECT content, msg_id FROM memories WHERE agent_id='a' ORDER BY id"
        )
        assert len(rows) == 2, f"a row was deleted resolving the collision: {rows}"
        assert sorted(r[0] for r in rows) == ["bar", "foo"], "content was mutated"
        # The newest row (id=2, 'bar') keeps msg_id; the older ('foo') is blanked.
        by_content = {r[0]: r[1] for r in rows}
        assert by_content["bar"] == "m1", "newest colliding row lost its msg_id"
        assert by_content["foo"] == "", "older colliding row was not blanked"

        schema_v = await db.execute_fetchall(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        )
        assert schema_v[0][0] == database.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# bug-145 (b) — the health check detects the missing index and remediates it via the
# same non-destructive collision resolution, through the public registry runner.
# ---------------------------------------------------------------------------


async def _break_msg_id_index(db):
    """Drop the msg_id index and seed a colliding pair (post-migration broken state)."""
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_msg_id")
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, msg_id, content, timestamp) "
        "VALUES ('h', '', 'm1', 'foo', '2026-01-01T00:00:00Z')"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, msg_id, content, timestamp) "
        "VALUES ('h', '', 'm1', 'bar', '2026-01-02T00:00:00Z')"
    )
    await db.commit()


@pytest.mark.asyncio
async def test_bug145_health_check_reports_missing_index(temp_db):
    db = temp_db
    await _break_msg_id_index(db)
    # Run through the public runner (fix=False, isolated to this check).
    issues, _ = await checks.run_health_checks(
        db, agent_id="", fix=False, checks=["dedup_msg_id_index"]
    )
    # Unfixed: no such check is registered, so the runner reports nothing here.
    assert any(i["type"] == "dedup_msg_id_index_missing" for i in issues), (
        "the missing msg_id dedup index is not surfaced by a remediation check"
    )


@pytest.mark.asyncio
async def test_bug145_health_check_remediates_missing_index(temp_db):
    db = temp_db
    await _break_msg_id_index(db)

    # fix=True through the public runner recreates the index by first resolving
    # the collision. On unfixed code the check is absent, the runner is a no-op,
    # and the index stays missing (this assertion fires).
    await checks.run_health_checks(db, agent_id="", fix=True, checks=["dedup_msg_id_index"])
    await db.commit()

    idx = await db.execute_fetchall(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_memories_dedup_msg_id'"
    )
    assert idx, (
        "check_health(fix=True) did not recreate idx_memories_dedup_msg_id — "
        "the collision blocks the identical CREATE and no remediation ran"
    )
    # Non-destructive: both rows survive, content untouched, newest keeps msg_id.
    rows = await db.execute_fetchall(
        "SELECT content, msg_id FROM memories WHERE agent_id='h' ORDER BY id"
    )
    assert len(rows) == 2, f"a row was deleted by the fix: {rows}"
    by_content = {r[0]: r[1] for r in rows}
    assert by_content["bar"] == "m1"
    assert by_content["foo"] == ""

    # Residual is clean, and schema_objects agrees the index is present now — the
    # registry ordering (dedup_msg_id_index before schema_objects) converges in a
    # single pass.
    residual, _ = await checks.run_health_checks(
        db, agent_id="", fix=False, checks=["dedup_msg_id_index", "schema_objects"]
    )
    assert not any(i["type"] == "dedup_msg_id_index_missing" for i in residual)
    assert not any(
        i.get("object") == "idx_memories_dedup_msg_id" for i in residual
    ), "schema_objects still flags the msg_id index after remediation"
