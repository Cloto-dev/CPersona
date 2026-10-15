"""Test hygiene: what one test leaves behind must not change what a later one sees.

The suite runs in one process against one database file, and get_db() hands every
test the same connection. Two helpers other modules' tests depend on are pinned
here, each by running the helper the way its module's fixture does and then
looking from where a later test stands:

1. ``test_store_flat_form``'s store fixture empties the tables after the test as
   well as before it. One test there stores under a ledger-issued ``u-`` alias;
   left in the database, the row is a reserved-prefix agent that no later test's
   ledger records, and the boot guard a later test runs refuses the database.

2. ``test_vector_index_read_path``'s statement recorders leave nothing on the
   shared connection. Undoing a patch set on the connection itself puts the old
   bound method back as an instance attribute, which shadows any spy a later test
   installs on ``aiosqlite.Connection``: that spy then sees none of what the
   connection runs, so a test counting reads fails and one asserting that a read
   did not happen passes without looking.
"""

import aiosqlite
import pytest

import test_store_flat_form as flat_form
import test_vector_index_read_path as read_path
from cpersona import acl, aliases, server
from cpersona.database import get_db


@pytest.mark.asyncio
async def test_the_flat_form_alias_test_leaves_no_reserved_agent_behind(tmp_path):
    own = tmp_path / "flat_form"
    later = tmp_path / "later"
    own.mkdir()
    later.mkdir()
    try:
        async with flat_form._clean_store() as handle:
            await flat_form.test_a_per_subject_connection_writes_to_its_own_alias(handle, own)
            stored = [row[0] for row in await handle.execute_fetchall("SELECT agent_id FROM memories")]
            # The test did write under an issued alias; without that row there is
            # nothing for the clean-up to remove and the check below proves nothing.
            assert stored and all(agent.startswith(acl.ALIAS_PREFIX) for agent in stored), stored

        # Where a later test stands: its own ledger, which records none of the
        # aliases the flat-form test was issued.
        acl.activate_ledger(aliases.AliasLedger(str(later / "alias_ledger.json")))
        await server._assert_no_reserved_agent_ids()
    finally:
        acl.activate_ledger(None)
        handle = await get_db()
        await handle.execute("DELETE FROM memories WHERE agent_id LIKE ?", (acl.ALIAS_PREFIX + "%",))
        await handle.commit()


@pytest.mark.asyncio
async def test_the_read_path_recorders_leave_the_shared_connection_as_they_found_it():
    conn = await get_db()
    try:
        with pytest.MonkeyPatch.context() as mp:
            seen = read_path._record_fetchall(mp, conn, lambda sql, parameters: (sql, tuple(parameters)))
            await conn.execute_fetchall("SELECT ?", (1,))
            async with aiosqlite.connect(":memory:") as other:
                await other.execute_fetchall("SELECT 2")
            # It records what its own connection runs, and only that.
            assert seen == [("SELECT ?", (1,))]

        assert "execute_fetchall" not in vars(conn), "the recorder left an attribute on the shared connection"

        # A spy installed on the class afterwards, as other modules' tests do,
        # sees what the shared connection runs.
        heard: list[str] = []
        real = aiosqlite.Connection.execute_fetchall

        async def spy(self, sql, parameters=None, *args, **kwargs):
            heard.append(sql)
            return await real(self, sql, parameters, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(aiosqlite.Connection, "execute_fetchall", spy)
            await conn.execute_fetchall("SELECT 3")
        assert heard == ["SELECT 3"]
    finally:
        # Whatever went wrong above, the connection goes back to reading the class.
        vars(conn).pop("execute_fetchall", None)
