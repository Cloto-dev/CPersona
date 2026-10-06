"""A recall's arms run beside each other where none needs another's result.

aiosqlite runs a connection's statements one at a time on one worker thread, so
arms that share the request connection take the sum of their times. On 100,000
memories of real length the keyword arm alone was three quarters of a recall
(504 of 677 ms, measured on an Apple laptop), and it reads only the query text;
the block arm reads only the query vector. The keyword arms now run on a side
connection while the query is embedded and scanned, and the block arm starts as
soon as the vector exists.

The instrument for "beside" is a rendezvous: each of two arms, once started,
waits for the other to have started. Arms that overlap both proceed. Arms that
run in line, in either order, leave the first one waiting for an arm that has
not begun, and the recall fails on the timeout instead of returning.
"""

import asyncio
import os
import tempfile

import pytest

from cpersona import admin_handlers, blocks, config, database, memory_handlers, nodes, session, tasks, vector

AGENT = "agent.beside"

#: Long enough for arms that overlap to meet on a slow runner, short enough that
#: arms in line fail the test quickly.
RENDEZVOUS_S = 3.0

RECORDS = [
    "the pilot ran in march. the budget was approved in april.",
    "the warehouse rota changed after the pilot. staffing was reviewed.",
    "procurement asked about the pilot budget twice.",
]


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "concurrent_arms.db")
        self.queue = tasks.MemoryTaskQueue()
        self.queue._running = True
        tasks._task_queue = self.queue
        await database.get_db()
        return self

    async def __aexit__(self, *exc):
        side = database._side_read_db
        await database.close_db()
        if side is not None and side._connection is not None:
            # close_db missed it (test_close_db_closes_the_side_connection says so);
            # close it here, so the run fails that test instead of hanging at exit.
            await side.close()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()

    async def store(self, texts):
        for text in texts:
            await memory_handlers.do_store(AGENT, {"content": text})
        await self.queue._drain(admin_handlers, memory_handlers, nodes)


@pytest.fixture
def blocks_on(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    return fake_embedding_client


def _rendezvous(monkeypatch, first, second):
    """Make two arms wait for each other: (module, name) pairs, wrapped in place."""
    started = {id(first): asyncio.Event(), id(second): asyncio.Event()}

    def wrap(arm, other):
        module, name = arm
        real = getattr(module, name)

        async def met(*args, **kwargs):
            started[id(arm)].set()
            await asyncio.wait_for(started[id(other)].wait(), RENDEZVOUS_S)
            return await real(*args, **kwargs)

        monkeypatch.setattr(module, name, met)

    wrap(first, second)
    wrap(second, first)


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_side_seam_is_its_own_connection():
    async with _TempDB():
        async with database.side_connection() as side, database.connection() as rq:
            assert side is not rq
            assert side is not database._db
        async with database.background_connection() as bg, database.side_connection() as side:
            assert side is not bg


@pytest.mark.asyncio
async def test_close_db_closes_the_side_connection():
    # Every aiosqlite connection owns a non-daemon thread; one close_db leaves
    # open keeps the interpreter from exiting (bug-124).
    async with _TempDB():
        async with database.side_connection() as side:
            pass
        await database.close_db()
        leaked = side._connection is not None
        if leaked:
            # Close it here, so a regression fails this test instead of hanging the
            # run at interpreter exit on the connection's worker thread.
            await side.close()
        assert database._side_read_db is None
        assert not leaked, "close_db left the side connection open"
        await database.get_db()


@pytest.mark.asyncio
async def test_beside_the_request_connection_is_the_side_connection():
    async with _TempDB():
        async with database.connection() as rq, database.beside(rq) as arm_db:
            assert arm_db is not rq
            assert arm_db is database._side_read_db


@pytest.mark.asyncio
async def test_beside_any_other_connection_is_that_connection():
    """A calibration's simulate queries take seconds of keyword search each, on the
    background seam. Beside it they stay on it: moved to the side connection, they
    would queue every request's keyword arm behind them."""
    async with _TempDB():
        async with database.background_connection() as bg, database.beside(bg) as arm_db:
            assert arm_db is bg
        writer = await database.get_db()
        async with database.beside(writer) as arm_db:
            assert arm_db is writer


@pytest.mark.asyncio
async def test_calibration_never_touches_the_side_connection(monkeypatch, fake_embedding_client):
    async def refuse():
        raise AssertionError("calibration read on the side connection")

    async with _TempDB() as tmp:
        await tmp.store([f"memory {i} about the pilot and the budget" for i in range(12)])
        monkeypatch.setattr(database, "_get_side_read_db", refuse)
        result = await admin_handlers.do_calibrate_threshold(agent_id=AGENT)
        assert result.get("ok") is True, result
        assert result.get("new_threshold") is not None, result


# --------------------------------------------------------------------------
# the arms overlap
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fusion", ["rrf", "rsf"])
async def test_the_keyword_arm_runs_while_the_vector_arm_does(monkeypatch, fake_embedding_client, fusion):
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", fusion)
    async with _TempDB() as tmp:
        await tmp.store(RECORDS)
        _rendezvous(
            monkeypatch,
            (memory_handlers, "_search_vector"),
            (memory_handlers, "_search_memories_keyword"),
        )
        result = await memory_handlers.do_recall(agent_id=AGENT, query="pilot budget", limit=5)
        assert result["messages"], result


@pytest.mark.asyncio
async def test_the_block_arm_runs_while_the_keyword_arm_does(monkeypatch, blocks_on):
    async with _TempDB() as tmp:
        await tmp.store(RECORDS)
        _rendezvous(
            monkeypatch,
            (blocks, "search"),
            (memory_handlers, "_search_memories_keyword"),
        )
        result = await memory_handlers.do_recall(agent_id=AGENT, query="pilot budget", limit=5)
        assert result["messages"], result


# --------------------------------------------------------------------------
# what running beside must not change
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_keyword_arm_that_raises_still_fails_the_recall(monkeypatch, fake_embedding_client):
    async def broken(*args, **kwargs):
        raise RuntimeError("keyword arm failed")

    async with _TempDB() as tmp:
        await tmp.store(RECORDS)
        monkeypatch.setattr(memory_handlers, "_search_memories_keyword", broken)
        with pytest.raises(RuntimeError, match="keyword arm failed"):
            await memory_handlers.do_recall(agent_id=AGENT, query="pilot budget", limit=5)


@pytest.mark.asyncio
async def test_a_block_arm_that_raises_still_fails_the_recall(monkeypatch, blocks_on):
    async def broken(*args, **kwargs):
        raise RuntimeError("block arm failed")

    async with _TempDB() as tmp:
        await tmp.store(RECORDS)
        monkeypatch.setattr(blocks, "search", broken)
        with pytest.raises(RuntimeError, match="block arm failed"):
            await memory_handlers.do_recall(agent_id=AGENT, query="pilot budget", limit=5)


@pytest.mark.asyncio
async def test_no_vector_leaves_no_block_arm_waiting(monkeypatch, blocks_on):
    """With no query vector the block arm never starts, and nothing is left behind
    waiting for one."""
    async with _TempDB() as tmp:
        await tmp.store(RECORDS)
        monkeypatch.setattr(vector, "_embedding_client", None)
        before = {t for t in asyncio.all_tasks() if not t.done()}
        await memory_handlers.do_recall(agent_id=AGENT, query="pilot budget", limit=5)
        await asyncio.sleep(0)  # a cancelled task finishes on the next turn of the loop
        left = [t for t in asyncio.all_tasks() if not t.done() and t not in before]
        assert left == [], left


@pytest.mark.asyncio
async def test_the_block_fetch_is_counted_once(monkeypatch, blocks_on):
    async with _TempDB() as tmp:
        await tmp.store(RECORDS)
        result = await memory_handlers.do_recall(agent_id=AGENT, query="pilot budget", limit=5, trace=True)
        assert result["trace"]["budget"]["used"]["block_fetch"] == 1, result["trace"]["budget"]
