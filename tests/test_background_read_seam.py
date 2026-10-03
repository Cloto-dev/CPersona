"""Background reads run beside the requests, not ahead of them.

aiosqlite runs each connection's statements one at a time on one worker thread.
With a single read connection, a search issued while a calibration's simulate
query or a page of the block sweep was running waited for it: measured on a
23,867-record store, 24 s during a startup calibration and 19 s during the boot
sweep, against 0.6 s with neither.

The instrument here is a statement that takes a known time: a SQL function that
sleeps, registered on whatever connection the background work was handed. A read
on the request seam issued while it runs must come back before it ends. On a
shared connection it cannot, because it is queued behind the sleep.
"""

import asyncio
import contextlib
import os
import tempfile
import time

import pytest

from cpersona import admin_handlers, blocks, config, database, session, tasks

PAUSE_S = 1.0
#: A read that does not queue behind the pause returns in milliseconds; one that
#: does returns after the rest of the pause. Half the pause separates the two with
#: room on either side for a slow runner.
NOT_QUEUED_S = PAUSE_S / 2


def _pause(seconds):
    time.sleep(seconds)
    return 1


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "background_seam.db")
        tasks._task_queue = None
        await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()


async def _request_read_seconds() -> float:
    t0 = time.perf_counter()
    async with database.connection() as db:
        await db.execute_fetchall("SELECT 1")
    return time.perf_counter() - t0


async def _hold(db, started: asyncio.Event) -> None:
    """Run a statement of PAUSE_S on *db*, signalling once it has been submitted."""
    await db.create_function("pause", 1, _pause)
    pending = asyncio.ensure_future(db.execute_fetchall("SELECT pause(?)", (PAUSE_S,)))
    # Let the statement reach the worker thread before the request read is issued.
    await asyncio.sleep(0.05)
    started.set()
    await pending


async def _measure_while(work, started: asyncio.Event) -> float:
    task = asyncio.ensure_future(work)
    await started.wait()
    waited = await _request_read_seconds()
    await task
    return waited


@pytest.mark.asyncio
async def test_background_seam_is_its_own_connection():
    async with _TempDB():
        async with database.background_connection() as bg, database.connection() as rq:
            assert bg is not rq
            assert bg is not database._db


@pytest.mark.asyncio
async def test_close_db_closes_the_background_connection():
    # Every aiosqlite connection owns a non-daemon thread; one close_db leaves
    # open keeps the interpreter from exiting (bug-124).
    async with _TempDB():
        async with database.background_connection() as bg:
            pass
        await database.close_db()
        assert database._bg_read_db is None
        assert bg._connection is None, "close_db left the background connection open"
        await database.get_db()


@pytest.mark.asyncio
async def test_a_request_read_does_not_wait_for_a_background_statement():
    async with _TempDB():
        started = asyncio.Event()

        async def work():
            async with database.background_connection() as db:
                await _hold(db, started)

        waited = await _measure_while(work(), started)
        assert waited < NOT_QUEUED_S, f"request read waited {waited:.2f}s behind background work"


@pytest.mark.asyncio
async def test_the_instrument_sees_a_shared_connection():
    # Teeth for the two tests that rely on it: the same pause on the request
    # connection itself does hold a request read up.
    async with _TempDB():
        started = asyncio.Event()

        async def work():
            async with database.connection() as db:
                await _hold(db, started)

        waited = await _measure_while(work(), started)
        assert waited >= NOT_QUEUED_S, f"a queued read came back in {waited:.2f}s"


@pytest.mark.asyncio
async def test_calibration_reads_beside_the_requests(monkeypatch):
    async with _TempDB():
        started = asyncio.Event()

        async def slow_sample(db, agent_id, sample_n):
            await _hold(db, started)
            return None, {"ok": False, "error": "stub sample"}

        monkeypatch.setattr(admin_handlers, "_sample_embeddings", slow_sample)
        waited = await _measure_while(admin_handlers.do_calibrate_threshold(agent_id="agent.cal"), started)
        assert waited < NOT_QUEUED_S, f"a request waited {waited:.2f}s behind a calibration"


@pytest.mark.asyncio
async def test_block_sweep_pages_are_read_beside_the_requests(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    async with _TempDB():
        started = asyncio.Event()

        async def slow_page(db, kind, after_id, limit):
            await _hold(db, started)
            return []

        monkeypatch.setattr(blocks, "_page", slow_page)
        waited = await _measure_while(blocks.backfill({}), started)
        assert waited < NOT_QUEUED_S, f"a request waited {waited:.2f}s behind a page of the sweep"


@pytest.mark.asyncio
async def test_startup_calibration_lists_agents_on_the_background_seam(monkeypatch):
    # The agent listing scans every stored row; on a large store that is a long
    # statement of its own.
    entered = []
    real = admin_handlers.background_connection

    @contextlib.asynccontextmanager
    async def counting():
        entered.append(1)
        async with real() as db:
            yield db

    async def calibrated(agent_id="", **_):
        return {"ok": True}

    async with _TempDB():
        monkeypatch.setattr(admin_handlers, "background_connection", counting)
        monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", calibrated)
        monkeypatch.setattr(admin_handlers, "_read_calibration_sidecar", lambda: (None, False))
        status = await admin_handlers.ensure_calibrated_on_startup(True, True)
        assert status["global_ok"] is True
        assert entered == [1], "the agent listing did not go through the background seam"
