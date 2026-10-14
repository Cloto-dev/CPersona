"""Block and node writes: the compare-and-swap read holds the write lock.

``write_blocks`` and ``write_nodes`` compare the record's current text with the
text their set was divided from, then replace the set. Both statements must run
in one write transaction: with the shared connection's deferred mode the read
ran in autocommit and the transaction only began at the DELETE, so another
process could delete or edit the record in between. The set was then written
for a record that no longer existed (rows no trigger would ever remove) or for
text the record no longer held, and the write still reported success.

The interleaving is forced: the read helper is wrapped so that, right after the
real read returns, a second plain sqlite3 connection on the same file (a second
server process) tries to commit at once (timeout 0). While the writer holds the
write lock that commit is refused and the write lands on the text it checked.
"""

import os
import sqlite3
import tempfile

import pytest

from cpersona import blocks, config, database, memory_handlers, nodes, session, tasks
from cpersona.database import transaction

AGENT = "u08"
TEXT = "zebra stripes run wild. zebra herds graze.\n\nzebra foals follow. zebra calls echo."
EDITED = "pasta short"


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "u08.db")
        # Never started, so nothing drains behind the test's back.
        tasks._task_queue = tasks.MemoryTaskQueue()
        await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()


def _other_process(sql, params):
    """A second connection on the same file committing at once, without waiting."""

    def run():
        c = sqlite3.connect(database.DB_PATH, timeout=0, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(sql, params)
            c.execute("COMMIT")
            return "committed"
        except sqlite3.OperationalError as e:
            return f"refused: {e}"
        finally:
            c.close()

    return run


def _race_after(monkeypatch, module, name, action, log):
    """Make ``action`` run right after module.<name> (the compare-and-swap read) returns."""
    real = getattr(module, name)

    async def wrapped(db, kind, parent_id):
        out = await real(db, kind, parent_id)
        log["outside_commit"] = action()
        return out

    monkeypatch.setattr(module, name, wrapped)


@pytest.fixture
def enabled(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


async def _one(sql, params):
    db = await database.get_db()
    return (await db.execute_fetchall(sql, params))[0][0]


async def _store():
    r = await memory_handlers.do_store(AGENT, {"content": TEXT})
    return r["id"]


def _prepared_nodes(mem_id):
    return nodes.PreparedNodes(
        "mem",
        mem_id,
        TEXT,
        [nodes.Span(0, 40, 10, 64), nodes.Span(40, len(TEXT), 10, 64)],
        [b"\x00" * 16, b"\x00" * 16],
        "fake-model",
    )


async def _drop_nodes(mem_id):
    db = await database.get_db()
    await db.execute("DELETE FROM record_nodes WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
    await db.commit()


async def _block_rows(mem_id):
    b = await _one("SELECT COUNT(*) FROM record_blocks WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
    v = await _one(
        "SELECT COUNT(*) FROM record_block_vectors WHERE parent_kind='mem' AND parent_id=?", (mem_id,)
    )
    return b, v


@pytest.mark.asyncio
async def test_write_blocks_delete_cannot_land_between_check_and_write(enabled, monkeypatch):
    async with _TempDB():
        mem_id = await _store()
        prepared = await blocks.prepare_blocks("mem", mem_id, TEXT, ())
        assert prepared is not None and len(prepared.spans) >= 2
        log = {}
        _race_after(
            monkeypatch, blocks, "_parent_row",
            _other_process("DELETE FROM memories WHERE id = ?", (mem_id,)), log,
        )
        async with transaction() as tx:
            accepted = await blocks.write_blocks(tx, prepared)
        parent = await _one("SELECT COUNT(*) FROM memories WHERE id = ?", (mem_id,))
        b, v = await _block_rows(mem_id)
        assert not (parent == 0 and (b or v)), (
            f"{b} block rows and {v} vectors written for deleted memory {mem_id} "
            f"(outside commit: {log.get('outside_commit')}, write accepted: {accepted})"
        )
        assert log["outside_commit"].startswith("refused"), log
        assert accepted is True and parent == 1 and b == len(prepared.spans) == v


@pytest.mark.asyncio
async def test_write_blocks_edit_cannot_land_between_check_and_write(enabled, monkeypatch):
    async with _TempDB():
        mem_id = await _store()
        prepared = await blocks.prepare_blocks("mem", mem_id, TEXT, ())
        log = {}
        _race_after(
            monkeypatch, blocks, "_parent_row",
            _other_process("UPDATE memories SET content = ? WHERE id = ?", (EDITED, mem_id)), log,
        )
        async with transaction() as tx:
            accepted = await blocks.write_blocks(tx, prepared)
        content = await _one("SELECT content FROM memories WHERE id = ?", (mem_id,))
        b, _ = await _block_rows(mem_id)
        assert not (content != TEXT and b), (
            f"{b} blocks of the old text attached to a record now reading {content!r} "
            f"(outside commit: {log.get('outside_commit')}, write accepted: {accepted})"
        )
        assert content == TEXT and accepted is True


@pytest.mark.asyncio
async def test_build_blocks_queue_path_refuses_the_racing_delete(enabled, monkeypatch):
    """The queue's own build, end to end, not just the helper."""
    async with _TempDB():
        mem_id = await _store()
        db = await database.get_db()
        await db.execute("DELETE FROM record_blocks WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
        await db.commit()
        log = {}
        _race_after(
            monkeypatch, blocks, "_parent_row",
            _other_process("DELETE FROM memories WHERE id = ?", (mem_id,)), log,
        )
        outcome = await blocks.build_blocks({"kind": "mem", "id": mem_id})
        parent = await _one("SELECT COUNT(*) FROM memories WHERE id = ?", (mem_id,))
        b, v = await _block_rows(mem_id)
        assert not (parent == 0 and (b or v)), (
            f"build_blocks {outcome!r} left {b} blocks / {v} vectors for a deleted memory"
        )
        assert outcome.startswith("built"), outcome


@pytest.mark.asyncio
async def test_write_nodes_delete_cannot_land_between_check_and_write(enabled, monkeypatch):
    async with _TempDB():
        mem_id = await _store()
        await _drop_nodes(mem_id)
        prepared = _prepared_nodes(mem_id)
        log = {}
        _race_after(
            monkeypatch, nodes, "_parent_text",
            _other_process("DELETE FROM memories WHERE id = ?", (mem_id,)), log,
        )
        async with transaction() as tx:
            accepted = await nodes.write_nodes(tx, prepared)
        parent = await _one("SELECT COUNT(*) FROM memories WHERE id = ?", (mem_id,))
        n = await _one("SELECT COUNT(*) FROM record_nodes WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
        assert not (parent == 0 and n), (
            f"{n} node rows written for deleted memory {mem_id} "
            f"(outside commit: {log.get('outside_commit')}, write accepted: {accepted})"
        )
        assert log["outside_commit"].startswith("refused"), log
        assert accepted is True and n == 2


@pytest.mark.asyncio
async def test_write_nodes_edit_cannot_land_between_check_and_write(enabled, monkeypatch):
    async with _TempDB():
        mem_id = await _store()
        await _drop_nodes(mem_id)
        prepared = _prepared_nodes(mem_id)
        log = {}
        _race_after(
            monkeypatch, nodes, "_parent_text",
            _other_process("UPDATE memories SET content = ? WHERE id = ?", (EDITED, mem_id)), log,
        )
        async with transaction() as tx:
            accepted = await nodes.write_nodes(tx, prepared)
        content = await _one("SELECT content FROM memories WHERE id = ?", (mem_id,))
        n = await _one("SELECT COUNT(*) FROM record_nodes WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
        assert not (content != TEXT and n), (
            f"{n} nodes of the old text attached to a record now reading {content!r} "
            f"(outside commit: {log.get('outside_commit')}, write accepted: {accepted})"
        )
        assert content == TEXT and accepted is True


@pytest.mark.asyncio
async def test_writes_join_a_transaction_that_already_holds_writes(enabled):
    """A health repair run writes nodes and then blocks in one transaction: the
    second write must join the transaction the first one opened, not begin a
    nested one (SQLite refuses that) or end it early."""
    async with _TempDB():
        mem_id = await _store()
        await _drop_nodes(mem_id)
        db = await database.get_db()
        await db.execute("DELETE FROM record_blocks WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
        await db.commit()
        prepared_blocks = await blocks.prepare_blocks("mem", mem_id, TEXT, ())
        with pytest.raises(RuntimeError, match="rolled back"):
            async with transaction() as tx:
                assert await nodes.write_nodes(tx, _prepared_nodes(mem_id)) is True
                assert tx.in_transaction
                assert await blocks.write_blocks(tx, prepared_blocks) is True
                raise RuntimeError("rolled back")
        # Both writes belonged to the one transaction the seam rolled back.
        n = await _one("SELECT COUNT(*) FROM record_nodes WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
        b, v = await _block_rows(mem_id)
        assert (n, b, v) == (0, 0, 0)
        async with transaction() as tx:
            assert await nodes.write_nodes(tx, _prepared_nodes(mem_id)) is True
            assert await blocks.write_blocks(tx, prepared_blocks) is True
        n = await _one("SELECT COUNT(*) FROM record_nodes WHERE parent_kind='mem' AND parent_id=?", (mem_id,))
        b, v = await _block_rows(mem_id)
        assert n == 2 and b == v == len(prepared_blocks.spans)
