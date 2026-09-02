"""bug-277: the queue reconciles its attribution map however a pass ends.

The map that attributes a queued row to the session that enqueued it is
reconciled against the rows that still exist, so that a row deleted underneath
the queue — agent-data deletion, a move-mode merge, the stale-queue repair —
does not leave an entry behind (bug-270).

That reconcile sat on the branch a pass takes when it finds the queue empty. The
justification for not using a `finally` was that an aborted pass is re-armed and
the next one reconciles anyway — but re-arming means the *loop* survived, not
that another pass was scheduled. `_loop` catches, logs "drain aborted,
re-arming", and returns to waiting on the event; with no subsequent enqueue, no
later pass ever runs.

The claim pinned here is the one the design note makes — a drain pass ends by
reconciling the map against the rows that still exist — so the test aborts a
pass and then asserts the map is clean. It does not assert that a particular
call site was reached.
"""

from __future__ import annotations

import pytest

from cpersona import tasks


class _Boom(Exception):
    pass


@pytest.mark.asyncio
async def test_a_pass_that_raises_still_reconciles():
    queue = tasks.MemoryTaskQueue()
    queue._running = True
    # An id with no row behind it: exactly what a row deleted out of band leaves.
    queue._task_sessions[4242] = "session-a"

    async def exploding():
        raise _Boom("transient DB fault")

    queue._fetch_next = exploding

    async def no_rows():
        queue._task_sessions = {
            k: v for k, v in queue._task_sessions.items() if k in ()
        }

    queue._forget_vanished_rows = no_rows

    with pytest.raises(_Boom):
        await queue._drain(None, None)

    assert queue._task_sessions == {}, (
        "the pass aborted and left the attribution map holding an id whose row is "
        "gone — the state bug-270 was fixed to prevent"
    )


@pytest.mark.asyncio
async def test_a_failing_reconcile_does_not_abort_a_successful_drain():
    """The constraint the original placement was right about.

    A reconcile that raised inside the `finally` would convert a drain that
    succeeded into a drain that aborted, which `_loop` would then log as a
    failure and re-arm from.
    """
    queue = tasks.MemoryTaskQueue()
    queue._running = True

    async def empty_queue():
        return None

    async def exploding_reconcile():
        raise _Boom("reconcile hit the database while it was locked")

    queue._fetch_next = empty_queue
    queue._forget_vanished_rows = exploding_reconcile

    await queue._drain(None, None)  # must not raise


@pytest.mark.asyncio
async def test_the_ordinary_pass_still_reconciles():
    """Without this, a `finally` that never ran would satisfy the tests above."""
    queue = tasks.MemoryTaskQueue()
    queue._running = True
    calls = []

    async def empty_queue():
        return None

    async def counting():
        calls.append(1)

    queue._fetch_next = empty_queue
    queue._forget_vanished_rows = counting

    await queue._drain(None, None)

    assert calls == [1], "a pass that ended normally did not reconcile"
