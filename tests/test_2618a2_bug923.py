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
