"""Background task queue (Phase 5: crash-recoverable async processing).

Ported from KS2.1 (ai_karin) MemoryWorker — adapted from Rust/tokio to Python/asyncio.

Holds the module-level `_task_queue` singleton, set by `server.main()` at startup.
"""

import asyncio
import json
import logging

from cpersona import session
from cpersona.config import TASK_MAX_RETRIES, TASK_RETRY_DELAY
from cpersona.database import connection, transaction
from cpersona.isolation import isolation_where

logger = logging.getLogger(__name__)


class MemoryTaskQueue:
    """DB-persisted background task queue with crash recovery.

    Tasks (update_profile, archive_episode) are serialized to SQLite on enqueue,
    processed asynchronously in FIFO order, and deleted on success.
    On startup, any pending tasks from a previous crash are automatically recovered.
    """

    #: task_id -> the session_key that enqueued it. In-process only: the queue row
    #: carries no session column (adding one is a schema change and a retention
    #: question, deliberately out of scope), so attribution lives beside the queue
    #: object that owns the row. Bounded because the key space is client-supplied
    #: and the map must not outlive the rows it describes — every delete/complete
    #: path drops its entry, and the cap is the backstop for a path that does not.
    #:
    #: A row that survives a process restart loses its attribution and falls back
    #: to TRANSPORT_KEY. That is correct rather than lossy: a restart ends every
    #: session, so no key from the previous process still names a live caller, and
    #: the shared bucket is the honest place for work nobody is left to own.
    _MAX_ATTRIBUTED_TASKS = 4096

    def __init__(self):
        self._event = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None
        self._task_sessions: dict[int, str] = {}

    async def start(self):
        """Start the background processing loop."""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        # Without a done-callback an unhandled exception in _loop dies silently
        # (the exception is only surfaced when the Task is GC'd) — the queue
        # would appear alive while no longer draining (bug-005).
        self._task.add_done_callback(self._on_loop_done)
        self._event.set()
        logger.info("MemoryTaskQueue: started (max_retries=%d, retry_delay=%ds)", TASK_MAX_RETRIES, TASK_RETRY_DELAY)

    @staticmethod
    def _on_loop_done(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("MemoryTaskQueue: processing loop exited abnormally: %s", exc, exc_info=exc)

    async def stop(self):
        """Stop the background loop gracefully."""
        self._running = False
        self._event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                logger.warning("MemoryTaskQueue: forced shutdown after timeout")

    async def enqueue(
        self,
        task_type: str,
        agent_id: str,
        payload: list[dict],
        session_key: str = session.TRANSPORT_KEY,
    ) -> int:
        """Enqueue a task. Returns task ID.

        ``session_key`` is the ALREADY-RESOLVED key of the caller that enqueued the
        work, recorded so the drain can consult the right pause (see ``_drain``).
        It defaults to the shared bucket, which is what an unattributed enqueue
        means and what every caller got before the parameter existed.
        """
        # bug-042/043: transaction() serialises write+commit on the shared connection
        # so this enqueue cannot flush a concurrent import/merge's partial transaction.
        async with transaction() as db:
            cursor = await db.execute(
                "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES (?, ?, ?)",
                (task_type, agent_id, json.dumps(payload)),
            )
        task_id = cursor.lastrowid
        self._remember_session(task_id, session_key)
        logger.info("MemoryTaskQueue: enqueued %s for agent %s (task_id=%d)", task_type, agent_id, task_id)
        self._event.set()
        return task_id

    def _remember_session(self, task_id: int, session_key: str) -> None:
        """Attribute a queued row to the session that enqueued it (bounded)."""
        if session_key == session.TRANSPORT_KEY:
            # The fallback is what an absent entry already means, so storing it
            # would spend the cap to record nothing.
            self._forget_session(task_id)
            return
        if task_id not in self._task_sessions and len(self._task_sessions) >= self._MAX_ATTRIBUTED_TASKS:
            # Oldest task id first: the queue is FIFO, so the lowest id is the row
            # closest to being drained (and to dropping its entry anyway).
            del self._task_sessions[min(self._task_sessions)]
        self._task_sessions[task_id] = session_key

    def _forget_session(self, task_id: int) -> None:
        """Drop a row's attribution, on the paths this queue owns.

        Those are not the only ways a row goes away, which is what
        :meth:`_forget_vanished_rows` is for.
        """
        self._task_sessions.pop(task_id, None)

    async def _forget_vanished_rows(self) -> None:
        """Drop attributions whose queue rows are gone (bug-270).

        Rows disappear underneath this queue as well as through it: agent-data
        deletion, a move-mode merge and the stale-queue repair each delete from
        ``pending_memory_tasks`` without going through the queue at all, and the
        drain already knows it (the archive_episode block reads its own DELETE's
        rowcount for exactly that reason). Calling _forget_session from those three
        sites would be correct only until the fourth one is written, and would make
        a health probe and two admin handlers reach into in-process queue state to
        do it. Reconciling against the rows that exist covers any path, including
        ones not yet written.

        The query is bounded by the attribution cap, not by the queue: it asks only
        about ids this map holds. Ids attributed after the snapshot are not in it,
        so a row enqueued while this runs cannot be forgotten by it.
        """
        held = list(self._task_sessions)
        if not held:
            return
        placeholders = ",".join("?" * len(held))
        async with connection() as db:
            rows = await db.execute_fetchall(
                f"SELECT id FROM pending_memory_tasks WHERE id IN ({placeholders})", held
            )
        live = {row[0] for row in rows}
        for task_id in held:
            if task_id not in live:
                self._forget_session(task_id)

    def _session_for(self, task_id: int) -> str:
        """The key whose pause governs this row; the shared bucket if unattributed."""
        return self._task_sessions.get(task_id, session.TRANSPORT_KEY)

    async def get_status(self) -> dict:
        """Get queue status for monitoring."""
        # Queue depth is a global system resource, not agent-partitioned — the typed
        # no-filter helper call replaces the old waiver comment.
        iso = isolation_where(agent_id=None)
        async with connection() as db:
            rows = await db.execute_fetchall(f"SELECT COUNT(*) FROM pending_memory_tasks{iso.where}")
        pending = rows[0][0] if rows else 0
        return {
            "enabled": True,
            "pending": pending,
            "max_retries": TASK_MAX_RETRIES,
            "retry_delay": TASK_RETRY_DELAY,
        }

    async def _loop(self):
        """Main processing loop — waits for signal, drains all pending tasks."""
        # Lazy module-import to break circular dependency: handler modules import
        # `tasks` (this module) for _task_queue access, while _loop dispatches
        # back into the handlers. Attribute access via module ensures runtime
        # patching of admin_handlers.do_update_profile / memory_handlers.do_archive_episode
        # propagates (preserves v2.4.10 monolith-era test patchability).
        from cpersona import admin_handlers
        from cpersona import memory_handlers

        while self._running:
            await self._event.wait()
            self._event.clear()

            try:
                await self._drain(admin_handlers, memory_handlers)
            except Exception as e:
                # Never let an unexpected error (e.g. a transient DB fault in
                # _fetch_next) terminate the loop — that would silently stop all
                # future processing (bug-005). Log and wait for the next signal.
                logger.error("MemoryTaskQueue: drain aborted, re-arming: %s", e, exc_info=e)

    async def _drain(self, admin_handlers, memory_handlers):
        """Drain all currently-pending tasks in FIFO order."""
        try:
            while self._running:
                task = await self._fetch_next()
                if task is None:
                    break

                task_id, task_type, agent_id, payload, retries = task
                # If the session re-enters no-persist after this task was
                # enqueued, drop it instead of writing late — the user's
                # ephemeral intent overrides queued work that pre-dates it.
                # WHOSE pause: the session that enqueued the row, not whichever one
                # happens to be paused while the worker runs. A parallel session's
                # pause must not discard work it never asked for, which is exactly
                # what a single global flag did.
                task_session_key = self._session_for(task_id)
                if session.is_paused_for(task_session_key):
                    logger.info(
                        "MemoryTaskQueue: skipping task %d (%s) under no-persist mode",
                        task_id,
                        task_type,
                    )
                    await self._delete_task(task_id)
                    continue
                logger.info(
                    "MemoryTaskQueue: processing %s (task_id=%d, agent=%s, retry=%d/%d)",
                    task_type,
                    task_id,
                    agent_id,
                    retries,
                    TASK_MAX_RETRIES,
                )
                try:
                    if task_type == "update_profile":
                        # bug-090: a handler-returned failure dict is a FAILURE, not a
                        # success to delete-and-log-completed — route it into the retry
                        # path like a raise. (The upsert itself is idempotent, so the
                        # separate delete transaction below is redo-safe.)
                        # Same key the gate above consulted: the handler re-checks its
                        # own pause, and defaulting there would make it ask about the
                        # shared bucket instead of this row's session.
                        result = await admin_handlers.do_update_profile(
                            agent_id, payload, session_key=task_session_key
                        )
                        if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
                            raise RuntimeError(f"handler returned failure: {result.get('error') or result}")
                        await self._delete_task(task_id)
                    elif task_type == "archive_episode":
                        # bug-089: prepare (embedding HTTP) outside the lock, then run
                        # the episode INSERT and the task-row delete in ONE transaction.
                        # As two commits, a crash between them replayed the bare INSERT
                        # on the next boot and duplicated the episode; a failure of the
                        # delete alone had the same effect (at-least-once redo against a
                        # non-idempotent insert). A legacy history-only payload (no
                        # summary) raises here and lands in the retry/discard path below
                        # — visible, instead of a silent bogus "completed".
                        row = await memory_handlers._prepare_episode_row(agent_id, payload, summary="")
                        async with transaction() as db:
                            # bug-109: the task-row DELETE doubles as the claim token.
                            # rowcount 0 means the row vanished during the unlocked
                            # prepare window — a delete_agent_data / merge move wiped
                            # the agent (bug-093 purge) — so inserting now would
                            # resurrect data for a deleted agent. Delete-first makes
                            # the whole unit self-cancelling in that case.
                            cur = await db.execute(
                                "DELETE FROM pending_memory_tasks WHERE id = ?", (task_id,)
                            )
                            # The attribution is NOT dropped here. This block is a
                            # transaction: an insert that raises rolls the DELETE back and
                            # the task row survives to be retried — but a dict mutation
                            # does not roll back, so forgetting here would strand the
                            # retry in the shared keyless bucket. A session that armed a
                            # no-persist pause in the meantime would then have its episode
                            # written anyway. Dropping it belongs on the paths that end the
                            # task: the completion below, and _delete_task.
                            if cur.rowcount:
                                await memory_handlers._insert_episode_row(db, row)
                            else:
                                logger.info(
                                    "MemoryTaskQueue: task %d vanished during prepare "
                                    "(agent wiped) — skipping insert",
                                    task_id,
                                )
                    else:
                        logger.error("MemoryTaskQueue: unknown task type %s, discarding", task_type)
                        await self._delete_task(task_id)

                    self._forget_session(task_id)
                    logger.info("MemoryTaskQueue: completed %s (task_id=%d)", task_type, task_id)
                except Exception as e:
                    logger.error("MemoryTaskQueue: task %d (%s) failed: %s", task_id, task_type, e)
                    if retries + 1 >= TASK_MAX_RETRIES:
                        logger.error("MemoryTaskQueue: task %d exceeded max retries, discarding", task_id)
                        await self._delete_task(task_id)
                    else:
                        await self._increment_retry(task_id)
                        await asyncio.sleep(TASK_RETRY_DELAY)
        finally:
            # bug-277: on every way a pass can end, not only the one that ends by
            # finding the queue empty. The earlier placement rested on an aborted
            # pass being re-armed, but re-arming means the loop survived — it goes
            # back to waiting on the event, and with no later enqueue no further
            # pass ever runs. The map then keeps entries whose rows are gone, which
            # is the state bug-270 was fixed to prevent.
            #
            # Swallowed and logged rather than allowed to propagate, which is the
            # constraint the original placement was right about: a reconciliation
            # that raised in here would turn a drain that succeeded into a drain
            # that aborted.
            try:
                await self._forget_vanished_rows()
            except Exception as e:
                logger.warning(
                    "MemoryTaskQueue: attribution reconcile failed, entries may be stale: %s",
                    e,
                )

    async def _fetch_next(self) -> tuple | None:
        # The queue is a global FIFO by design — typed no-filter helper (the
        # structural gate's sanctioned spelling for a deliberate global scan).
        iso_all = isolation_where(agent_id=None)
        while True:
            async with connection() as db:
                rows = await db.execute_fetchall(
                    f"SELECT id, task_type, agent_id, payload, retries FROM pending_memory_tasks{iso_all.where}"
                    " ORDER BY id ASC LIMIT 1",
                    iso_all.params,
                )
            if not rows:
                return None
            task_id, task_type, agent_id, payload_json, retries = rows[0]
            try:
                payload = json.loads(payload_json)
            except (ValueError, TypeError) as e:
                # A single malformed payload row must not wedge the queue: the
                # head row would re-raise on every drain (including after
                # restart) and stall all following tasks forever (bug-005).
                # Discard the poison row and advance to the next one instead.
                logger.error(
                    "MemoryTaskQueue: task %d (%s) has an unparseable payload, discarding: %s",
                    task_id,
                    task_type,
                    e,
                )
                await self._delete_task(task_id)
                continue
            return (task_id, task_type, agent_id, payload, retries)

    async def _delete_task(self, task_id: int):
        # bug-042/043: transaction() serialises write+commit on the shared connection.
        async with transaction() as db:
            await db.execute("DELETE FROM pending_memory_tasks WHERE id = ?", (task_id,))
        # The row is gone, so its attribution has nothing left to describe.
        self._forget_session(task_id)

    async def _increment_retry(self, task_id: int):
        # bug-042/043: transaction() serialises write+commit on the shared connection.
        async with transaction() as db:
            await db.execute(
                "UPDATE pending_memory_tasks SET retries = retries + 1 WHERE id = ?",
                (task_id,),
            )


_task_queue: MemoryTaskQueue | None = None
