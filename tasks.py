"""Background task queue (Phase 5: crash-recoverable async processing).

Ported from KS2.1 (ai_karin) MemoryWorker — adapted from Rust/tokio to Python/asyncio.

Holds the module-level `_task_queue` singleton, set by `server.main()` at startup.
"""

import asyncio
import json
import logging

from _vendored_mcp_common import no_persist

from config import TASK_MAX_RETRIES, TASK_RETRY_DELAY
from database import get_db

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
        self._event.set()
        logger.info("MemoryTaskQueue: started (max_retries=%d, retry_delay=%ds)", TASK_MAX_RETRIES, TASK_RETRY_DELAY)

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
        db = await get_db()
        cursor = await db.execute(
            "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES (?, ?, ?)",
            (task_type, agent_id, json.dumps(payload)),
        )
        await db.commit()
        task_id = cursor.lastrowid
        logger.info("MemoryTaskQueue: enqueued %s for agent %s (task_id=%d)", task_type, agent_id, task_id)
        self._event.set()
        return task_id

    async def get_status(self) -> dict:
        """Get queue status for monitoring."""
        db = await get_db()
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
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
        import admin_handlers
        import memory_handlers

        while self._running:
            await self._event.wait()
            self._event.clear()

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
                        await admin_handlers.do_update_profile(agent_id, payload)
                    elif task_type == "archive_episode":
                        await memory_handlers.do_archive_episode(agent_id, payload)
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
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT id, task_type, agent_id, payload, retries FROM pending_memory_tasks ORDER BY id ASC LIMIT 1"
        )
        if not rows:
            return None
        task_id, task_type, agent_id, payload_json, retries = rows[0]
        payload = json.loads(payload_json)
        return (task_id, task_type, agent_id, payload, retries)

    async def _delete_task(self, task_id: int):
        db = await get_db()
        await db.execute("DELETE FROM pending_memory_tasks WHERE id = ?", (task_id,))
        await db.commit()

    async def _increment_retry(self, task_id: int):
        db = await get_db()
        await db.execute(
            "UPDATE pending_memory_tasks SET retries = retries + 1 WHERE id = ?",
            (task_id,),
        )
        await db.commit()


_task_queue: MemoryTaskQueue | None = None
