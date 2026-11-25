"""bug-923: a cached read connection whose worker thread has ended is replaced.

aiosqlite answers a call on the loop that made it. If that loop closed before the
answer came, the worker raised while answering and its thread ended, but the
connection still read as open, so every later read on it was queued to no one and
waited forever. The calibration tests hung that way on slower machines, after an
earlier test's loop closed with a read still in flight.
"""

import asyncio

import pytest

from cpersona import database


def _end_worker(conn) -> None:
    """End ``conn``'s worker the way a closed loop does: answering a call made on it."""
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    loop.close()
    conn._tx.put_nowait((future, lambda: 1))
    conn._thread.join(timeout=5)
    assert not conn._thread.is_alive(), "the worker did not end; the test cannot stand for the defect"


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.parametrize("getter", ["_get_read_db", "_get_background_read_db", "_get_side_read_db"])
async def test_bug_923_a_reader_whose_worker_ended_is_replaced(getter):
    get = getattr(database, getter)
    first = await get()
    if first is await database.get_db():
        pytest.skip("an in-memory database reads on the write connection")
    _end_worker(first)
    try:
        second = await asyncio.wait_for(get(), 5)

        assert second is not first, "the reader with no worker was handed out again"
        rows = await asyncio.wait_for(second.execute_fetchall("SELECT 1"), 5)
        assert [tuple(r) for r in rows] == [(1,)]
    finally:
        # Whatever the outcome, leave no reader without a worker cached: closing one
        # would wait forever at teardown.
        for name in ("_read_db", "_bg_read_db", "_side_read_db"):
            if getattr(database, name) is first:
                setattr(database, name, None)
                setattr(database, name + "_owner", None)


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
async def test_bug_923_closing_skips_a_reader_whose_worker_ended():
    """close_db() runs at every teardown; awaiting close() on such a reader never returns."""
    first = await database._get_background_read_db()
    if first is await database.get_db():
        pytest.skip("an in-memory database reads on the write connection")
    _end_worker(first)
    try:
        await asyncio.wait_for(database.close_db(), 5)
        rows = await asyncio.wait_for((await database.get_db()).execute_fetchall("SELECT 1"), 5)
        assert [tuple(r) for r in rows] == [(1,)]
    finally:
        if database._bg_read_db is first:
            database._bg_read_db, database._bg_read_db_owner = None, None


# ---- bug-924: a recall whose vector arm raises waits for the keyword arm it started

@pytest.mark.asyncio
@pytest.mark.parametrize("fusion", ["_recall_rrf", "_recall_rsf"])
async def test_bug_924_a_failed_vector_arm_leaves_no_keyword_read_running(fusion, monkeypatch):
    from cpersona import memory_handlers, vector

    db = await database.get_db()
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('a924', 'alpha beta gamma', '2026-01-01T00:00:00+00:00')"
    )
    await db.commit()
    finished = []
    real = memory_handlers._lexical_arms

    async def slow_arms(*args, **kwargs):
        await asyncio.sleep(0.2)  # still reading when the vector arm raises
        out = await real(*args, **kwargs)
        finished.append(True)
        return out

    monkeypatch.setattr(memory_handlers, "_lexical_arms", slow_arms)
    monkeypatch.setattr(vector, "_embedding_client", object())  # the vector arm raises
    before = asyncio.all_tasks()
    try:
        with pytest.raises(AttributeError):
            await getattr(memory_handlers, fusion)(db, "a924", "alpha", 10, False)
        left = [t for t in asyncio.all_tasks() - before if not t.done()]
        assert not left, f"the recall raised with its keyword arm still running: {left}"
        assert finished == [True]
    finally:
        await db.execute("DELETE FROM memories WHERE agent_id = 'a924'")
        await db.commit()
