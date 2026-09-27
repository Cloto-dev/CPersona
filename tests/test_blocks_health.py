"""check_missing_blocks: seeing and closing the gap the queue has not built yet.

docs/BLOCK_REACH_DESIGN.md §7 — the write path queues construction and a bounded
sweep builds what was there before, both on the queue. This check reports how
many records should hold blocks and do not, and under fix builds a bounded number
of them in-line: where the queue is off it is the only builder, and after an
upgrade that left existing sets without their re-rank vectors it is the way an
operator moves the rebuild along without waiting for the sweep.
"""

import os
import tempfile

import pytest

from cpersona import (
    blocks,
    checks,
    config,
    database,
    maintenance_handlers,
    session,
    tasks,
)

AGENT = "agent.blocks-health"

#: Divides into several blocks.
MANY = "一文目です。二文目です。\n\n三文目です。四文目です。"
#: One sentence: divides into one block, which is the record itself.
ONE = "ひとつの文だけ"


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "blocks_health.db")
        tasks._task_queue = None  # the write path queues nothing: this check is the only builder
        self.db = await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()

    async def memory(self, content, *, agent=AGENT, locked=0):
        cur = await self.db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, locked) VALUES (?, ?, ?, ?)",
            (agent, content, "2026-09-23T00:00:00+00:00", locked),
        )
        await self.db.commit()
        return cur.lastrowid

    async def episode(self, summary, *, agent=AGENT):
        cur = await self.db.execute(
            "INSERT INTO episodes (agent_id, summary) VALUES (?, ?)", (agent, summary)
        )
        await self.db.commit()
        return cur.lastrowid

    async def run(self, fix, agent=AGENT):
        issues, _ = await checks.run_health_checks(
            self.db, agent_id=agent, fix=fix, checks=["missing_blocks"]
        )
        await self.db.commit()
        return issues

    async def counts(self, kind, parent_id):
        blocks_n = await self.db.execute_fetchall(
            "SELECT COUNT(*) FROM record_blocks WHERE parent_kind = ? AND parent_id = ?",
            (kind, parent_id),
        )
        vectors_n = await self.db.execute_fetchall(
            "SELECT COUNT(*) FROM record_block_vectors WHERE parent_kind = ? AND parent_id = ?",
            (kind, parent_id),
        )
        return blocks_n[0][0], vectors_n[0][0]


@pytest.fixture
def opted_in(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


# --------------------------------------------------------------------------
# opt-in: a deployment that did not ask hears nothing and pays nothing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_reported_or_built_when_blocks_are_off(fake_embedding_client, monkeypatch):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", False)
    async with _TempDB() as tmp:
        mem = await tmp.memory(MANY)
        assert await tmp.run(fix=True) == []
        assert await tmp.counts("mem", mem) == (0, 0)
        assert await checks.prefetch_missing_blocks(AGENT) is None


@pytest.mark.asyncio
async def test_nothing_is_reported_without_an_embedding_client(monkeypatch):
    from cpersona import vector

    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(vector, "_embedding_client", None)
    async with _TempDB() as tmp:
        await tmp.memory(MANY)
        assert await tmp.run(fix=False) == []


# --------------------------------------------------------------------------
# what is counted
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_record_without_blocks_is_found(opted_in):
    async with _TempDB() as tmp:
        await tmp.memory(MANY)
        await tmp.episode(MANY)
        (issue,) = await tmp.run(fix=False)
        assert issue["type"] == "missing_blocks"
        assert (issue["count"], issue["memories"], issue["episodes"]) == (2, 1, 1)
        assert issue["repairable"] == 2
        assert issue["severity"] == "info"
        assert "built" not in issue


@pytest.mark.asyncio
async def test_a_record_that_divides_into_one_block_is_not_a_gap(opted_in):
    """It has no blocks by design; counting it would report a gap no build closes."""
    async with _TempDB() as tmp:
        await tmp.memory(ONE)
        assert await tmp.run(fix=False) == []


@pytest.mark.asyncio
async def test_a_record_with_a_current_set_is_not_found(opted_in):
    async with _TempDB() as tmp:
        await tmp.memory(MANY)
        await tmp.run(fix=True)
        assert await tmp.run(fix=False) == []


@pytest.mark.asyncio
async def test_a_set_without_its_vectors_is_found(opted_in):
    """The state a 2.6.0a5 deployment is in after upgrading."""
    async with _TempDB() as tmp:
        mem = await tmp.memory(MANY)
        await tmp.run(fix=True)
        await tmp.db.execute("DELETE FROM record_block_vectors WHERE parent_id = ?", (mem,))
        await tmp.db.commit()
        (issue,) = await tmp.run(fix=False)
        assert issue["count"] == 1


@pytest.mark.asyncio
async def test_another_agents_record_is_not_counted(opted_in):
    async with _TempDB() as tmp:
        await tmp.memory(MANY, agent="agent.other")
        assert await tmp.run(fix=False) == []


# --------------------------------------------------------------------------
# the repair
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_builds_blocks_and_their_vectors_and_touches_no_record(opted_in):
    async with _TempDB() as tmp:
        mem = await tmp.memory(MANY, locked=1)
        before = await tmp.db.execute_fetchall("SELECT * FROM memories WHERE id = ?", (mem,))

        (issue,) = await tmp.run(fix=True)

        assert issue["built"] == 1 and "build_failed" not in issue
        n_blocks, n_vectors = await tmp.counts("mem", mem)
        assert n_blocks > 1 and n_vectors == n_blocks
        assert await tmp.db.execute_fetchall("SELECT * FROM memories WHERE id = ?", (mem,)) == before
        assert await tmp.run(fix=False) == []


@pytest.mark.asyncio
async def test_a_run_builds_at_most_the_cap(opted_in, monkeypatch):
    monkeypatch.setattr(checks, "BLOCK_REPAIR_RECORD_CAP", 1)
    async with _TempDB() as tmp:
        first = await tmp.memory(MANY)
        second = await tmp.memory(MANY + "五文目です。")

        (issue,) = await tmp.run(fix=True)

        assert (issue["count"], issue["repairable"], issue["built"]) == (2, 1, 1)
        assert (await tmp.counts("mem", first))[0] > 0
        assert await tmp.counts("mem", second) == (0, 0)
        (issue,) = await tmp.run(fix=False)
        assert issue["count"] == 1


@pytest.mark.asyncio
async def test_one_failed_build_costs_only_that_record(opted_in, monkeypatch):
    real_prepare = blocks.prepare_blocks

    async def flaky(kind, parent_id, text, node_bounds):
        if text.startswith("壊れる"):
            raise RuntimeError("embedding request failed")
        return await real_prepare(kind, parent_id, text, node_bounds)

    monkeypatch.setattr(blocks, "prepare_blocks", flaky)
    async with _TempDB() as tmp:
        broken = await tmp.memory("壊れる記録です。" + MANY)
        fine = await tmp.memory(MANY)

        (issue,) = await tmp.run(fix=True)

        assert (issue["built"], issue["build_failed"]) == (1, 1)
        assert await tmp.counts("mem", broken) == (0, 0)
        assert (await tmp.counts("mem", fine))[0] > 0


@pytest.mark.asyncio
async def test_check_health_builds_outside_the_write_lock(opted_in, monkeypatch):
    """bug-072's rule: the embedding requests run before the lock is taken."""
    held = []
    real_prepare = blocks.prepare_blocks

    async def spy(*args):
        held.append(database.write_lock().locked())
        return await real_prepare(*args)

    monkeypatch.setattr(blocks, "prepare_blocks", spy)
    async with _TempDB() as tmp:
        mem = await tmp.memory(MANY)
        result = await maintenance_handlers.do_check_health(
            agent_id=AGENT, fix=True, checks=["missing_blocks"]
        )

        assert held == [False]
        (issue,) = [i for i in result["issues"] if i["check"] == "missing_blocks"]
        assert issue["built"] == 1
        n_blocks, n_vectors = await tmp.counts("mem", mem)
        assert n_blocks > 1 and n_vectors == n_blocks


@pytest.mark.asyncio
async def test_a_record_changed_after_the_prefetch_is_not_given_stale_blocks(opted_in):
    async with _TempDB() as tmp:
        mem = await tmp.memory(MANY)
        cache = {"blocks": await checks.prefetch_missing_blocks(AGENT)}
        assert len(cache["blocks"]["prepared"]) == 1
        await tmp.db.execute(
            "UPDATE memories SET content = ? WHERE id = ?", ("別の本文です。もう一文。", mem)
        )
        await tmp.db.commit()

        issues, _ = await checks.run_health_checks(
            tmp.db, agent_id=AGENT, fix=True, checks=["missing_blocks"], embedding_cache=cache
        )
        await tmp.db.commit()

        assert issues[0]["built"] == 0
        assert await tmp.counts("mem", mem) == (0, 0)


@pytest.mark.asyncio
async def test_the_finder_agrees_with_the_builder(opted_in):
    """Three readers of "are this record's blocks current" — the builder, the
    sweep and this check — must give one answer. After the check builds a
    record, the builder must find nothing left to do."""
    async with _TempDB() as tmp:
        mem = await tmp.memory(MANY)
        await tmp.run(fix=True)
        assert await blocks.build_blocks({"kind": "mem", "id": mem}) == "blocks already current"
