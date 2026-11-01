"""The recall log: the trace of each real recall, kept beside the store (2.6.17).

Off by default (``CPERSONA_RECALL_LOG``). When on, every ``recall`` and
``reconstruct`` call writes one row: when it ran, which tool, the scope, the query,
the arguments, the references it returned, how long it took, and its trace — the
same trace ``trace=true`` returns, compressed with zlib and otherwise unchanged.
The trace carries references, ranks, scores and reasons, never stored text
(recall_trace.py), so the query is the only text the log adds. A session that
paused persistence writes nothing, as for every other write.

The log is a SQLite file of its own (``<db>.recall-log.db``), created owner-only,
so nothing is added to the store's schema, its backups or its exports. Several
servers may share one store (one per agent session); SQLite's locking keeps their
rows whole, and each write trims the log to the newest ``CPERSONA_RECALL_LOG_SIZE``
rows. A failed write is logged and dropped: the recall it describes has already
been answered and must not fail for it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import zlib

from cpersona import config, fileperms

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    tool TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    deep INTEGER NOT NULL DEFAULT 0,
    query TEXT NOT NULL,
    args TEXT NOT NULL,
    returned TEXT NOT NULL,
    ms REAL NOT NULL,
    trace BLOB NOT NULL
)
"""


def log_path() -> str | None:
    """Where the log lives: beside the store, or nowhere for an in-memory store."""
    db = config.DB_PATH
    if not db or db == ":memory:":
        return None
    return f"{db}.recall-log.db"


def enabled() -> bool:
    return bool(config.RECALL_LOG) and log_path() is not None


def _connect(path: str) -> sqlite3.Connection:
    fileperms.create_private(path)  # owner-only before SQLite derives -wal / -shm from it
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    return conn


def write(
    *,
    tool: str,
    agent_id: str,
    project_id: str | None,
    channel: str,
    deep: bool,
    query: str,
    args: dict,
    returned: list[str],
    ms: float,
    trace: dict,
) -> bool:
    """Append one row and trim to the configured size. Returns whether it was written."""
    path = log_path()
    if path is None:
        return False
    try:
        body = zlib.compress(json.dumps(trace, ensure_ascii=False, default=str).encode("utf-8"), 6)
        conn = _connect(path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO recall_log (at, tool, agent_id, project_id, channel, deep, query, args, returned, ms, trace)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        tool,
                        agent_id,
                        project_id or "",
                        channel or "",
                        1 if deep else 0,
                        query,
                        json.dumps(args, ensure_ascii=False, default=str),
                        json.dumps(returned, ensure_ascii=False),
                        round(ms, 2),
                        body,
                    ),
                )
                conn.execute(
                    "DELETE FROM recall_log WHERE id <= (SELECT max(id) FROM recall_log) - ?",
                    (max(1, int(config.RECALL_LOG_SIZE)),),
                )
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 — the recall was answered; the log must not fail it
        logger.warning("recall log not written: %s", type(exc).__name__)
        return False


def read_trace(blob: bytes) -> dict:
    """A stored trace, decompressed: what ``trace=true`` returned for that call."""
    return json.loads(zlib.decompress(blob).decode("utf-8"))
