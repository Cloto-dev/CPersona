"""Background task queue (Phase 5: crash-recoverable async processing).

Ported from KS2.1 (ai_karin) MemoryWorker — adapted from Rust/tokio to Python/asyncio.

Holds the module-level `_task_queue` singleton, set by `server.main()` at startup.
"""

import asyncio
import json
import logging

from cpersona._vendored_mcp_common import no_persist

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

    def __init__(self):
        self._event = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None

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

    async def enqueue(self, task_type: str, agent_id: str, payload: list[dict]) -> int:
        """Enqueue a task. Returns task ID."""
        # bug-042/043: transaction() serialises write+commit on the shared connection
        # so this enqueue cannot flush a concurrent import/merge's partial transaction.
        async with transaction() as db:
            cursor = await db.execute(
                "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES (?, ?, ?)",
                (task_type, agent_id, json.dumps(payload)),
            )
        task_id = cursor.lastrowid
        logger.info("MemoryTaskQueue: enqueued %s for agent %s (task_id=%d)", task_type, agent_id, task_id)
        self._event.set()
        return task_id

    async def get_status(self) -> dict:
        """Get queue status for monitoring."""
        # Queue depth is a global system resource, not agent-partitioned — the typed
        # no-filter helper call replaces the old waiver comment (Task #180).
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
        while self._running:
            task = await self._fetch_next()
            if task is None:
                break

            task_id, task_type, agent_id, payload, retries = task
            # If the session re-enters no-persist after this task was
            # enqueued, drop it instead of writing late — the user's
            # ephemeral intent overrides queued work that pre-dates it.
            if no_persist.is_paused():
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
                    result = await admin_handlers.do_update_profile(agent_id, payload)
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

                logger.info("MemoryTaskQueue: completed %s (task_id=%d)", task_type, task_id)
            except Exception as e:
                logger.error("MemoryTaskQueue: task %d (%s) failed: %s", task_id, task_type, e)
                if retries + 1 >= TASK_MAX_RETRIES:
                    logger.error("MemoryTaskQueue: task %d exceeded max retries, discarding", task_id)
                    await self._delete_task(task_id)
                else:
                    await self._increment_retry(task_id)
                    await asyncio.sleep(TASK_RETRY_DELAY)

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

    async def _increment_retry(self, task_id: int):
        # bug-042/043: transaction() serialises write+commit on the shared connection.
        async with transaction() as db:
            await db.execute(
                "UPDATE pending_memory_tasks SET retries = retries + 1 WHERE id = ?",
                (task_id,),
            )


_task_queue: MemoryTaskQueue | None = None
