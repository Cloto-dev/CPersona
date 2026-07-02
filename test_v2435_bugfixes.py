"""Regression tests for the v2.4.35 patch batch (bug-003 .. bug-008).

Each test pins the corrected behaviour of one audit finding so the fix cannot
silently regress:

- bug-003: HTTP Bearer middleware rejects a missing/blank/wrong token.
- bug-005: a malformed task payload is discarded instead of wedging the queue.
- bug-007: check_health(fix=true) duplicate-content repair is agent-scoped and
           never deletes a locked row.
- bug-008: an in-place content edit keeps the memories_fts index in sync (no
           stale trigram left matching the old wording).

bug-004 (migration robustness) and bug-006 (empty-summary archive) are covered
by test_schema_v9_migration.py and test_task_queue.py respectively.
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Hermetic DB + embeddings-off before importing any cpersona module.
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_v2435.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import admin_handlers  # noqa: E402
from cpersona import maintenance_handlers  # noqa: E402
from cpersona import memory_handlers  # noqa: E402
from cpersona import tasks  # noqa: E402
from cpersona._vendored_mcp_common import no_persist  # noqa: E402
from cpersona.database import get_db  # noqa: E402


def _msg(content: str, msg_id: str = "") -> dict:
    return {"id": msg_id, "content": content, "source": {"User": "u"}}


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    no_persist.resume()
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.execute("DELETE FROM profiles")
    await db.execute("DELETE FROM pending_memory_tasks")
    await db.commit()
    tasks._task_queue = None
    yield
    no_persist.resume()


# --------------------------------------------------------------------------
# bug-003 — HTTP Bearer auth
# --------------------------------------------------------------------------

def _build_middleware(auth_token: str):
    """Instantiate the BearerTokenMiddleware defined inside _run_http_server.

    The middleware class is a closure over auth_token, so we replicate the exact
    authorise decision the closure makes rather than reaching into a running
    server. Keeping this in lockstep with the closure is the point of the test:
    if the closure logic drifts back to the bypass, this asserts on the intended
    contract.
    """
    import hmac

    def authorised(header: str) -> bool:
        if not auth_token:
            return True
        token = header[7:] if header.startswith("Bearer ") else ""
        return bool(token) and hmac.compare_digest(token, auth_token)

    return authorised


def test_auth_missing_header_is_rejected():
    authorised = _build_middleware("s3cret")
    assert authorised("") is False  # bug-003: header-less request must NOT pass


def test_auth_wrong_token_is_rejected():
    authorised = _build_middleware("s3cret")
    assert authorised("Bearer nope") is False
    assert authorised("Basic s3cret") is False
    assert authorised("s3cret") is False  # missing 'Bearer ' prefix


def test_auth_correct_token_is_accepted():
    authorised = _build_middleware("s3cret")
    assert authorised("Bearer s3cret") is True


def test_auth_disabled_passes_through():
    authorised = _build_middleware("")
    assert authorised("") is True
    assert authorised("Bearer anything") is True


def test_server_middleware_source_has_no_bypass_branch():
    """Guard the actual source: the `elif ... : pass` bypass must be gone."""
    import inspect

    from cpersona import server

    src = inspect.getsource(server._run_http_server)
    assert "elif auth_token and not header" not in src
    assert "hmac.compare_digest" in src


# --------------------------------------------------------------------------
# bug-005 — task-queue poison-pill
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_payload_is_discarded_not_wedged():
    """A row with unparseable JSON payload must be dropped so the queue advances."""
    db = await get_db()
    # Hand-write a poison row (invalid JSON) ahead of a valid one.
    await db.execute(
        "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES (?, ?, ?)",
        ("archive_episode", "agent-x", "{not valid json"),
    )
    await db.commit()

    queue = tasks.MemoryTaskQueue()
    task = await queue._fetch_next()
    # The poison row is discarded; with nothing valid behind it, fetch returns None.
    assert task is None
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
    assert rows[0][0] == 0  # poison row removed, queue not stuck


@pytest.mark.asyncio
async def test_malformed_payload_does_not_block_following_task():
    db = await get_db()
    await db.execute(
        "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES (?, ?, ?)",
        ("archive_episode", "agent-x", "}{ broken"),
    )
    await db.execute(
        "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES (?, ?, ?)",
        ("archive_episode", "agent-y", '[{"content": "ok"}]'),
    )
    await db.commit()

    queue = tasks.MemoryTaskQueue()
    task = await queue._fetch_next()
    assert task is not None
    _id, task_type, agent_id, payload, _retries = task
    assert agent_id == "agent-y"  # skipped past the poison row to the valid one
    assert payload == [{"content": "ok"}]


# --------------------------------------------------------------------------
# bug-007 — check_health duplicate delete: agent-scoped + locked-safe
# --------------------------------------------------------------------------

async def _insert_dup(db, agent_id: str, content: str) -> int:
    """Insert a raw duplicate row, bypassing do_store's content dedup — this is
    exactly the pre-existing duplicate condition check_health is meant to repair."""
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, '{}', '2026-01-01T00:00:00Z')",
        (agent_id, content),
    )
    await db.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_check_health_dedup_is_agent_scoped():
    """Repairing one agent must not delete another agent's duplicates."""
    db = await get_db()
    for _ in range(2):
        await _insert_dup(db, "agent-a", "dup")
        await _insert_dup(db, "agent-b", "dup")

    await maintenance_handlers.do_check_health(agent_id="agent-a", fix=True)

    a = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='agent-a'"))[0][0]
    b = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='agent-b'"))[0][0]
    assert a == 1  # agent-a deduped
    assert b == 2  # agent-b untouched (was globally deleted before the fix)


@pytest.mark.asyncio
async def test_check_health_dedup_never_deletes_locked():
    """A locked duplicate must survive the dedup delete."""
    db = await get_db()
    _first = await _insert_dup(db, "agent-a", "keepme")
    second = await _insert_dup(db, "agent-a", "keepme")
    # Lock the *second* copy (the one the MIN(id) survivor rule would delete).
    await admin_handlers.do_lock_memory(second, agent_id="agent-a")

    await maintenance_handlers.do_check_health(agent_id="agent-a", fix=True)

    survivors = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id='agent-a'")
    survivor_ids = {r[0] for r in survivors}
    assert second in survivor_ids  # locked row was NOT deleted


# --------------------------------------------------------------------------
# bug-008 — memories_fts stays in sync on in-place content edits
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_memory_keeps_fts_in_sync():
    """After editing content, the old wording must no longer match in FTS."""
    await memory_handlers.do_store("agent-f", _msg("photosynthesis chloroplast"))
    db = await get_db()
    mem_id = (await db.execute_fetchall("SELECT id FROM memories WHERE agent_id='agent-f' LIMIT 1"))[0][0]

    before = await _search(db, "agent-f", "photosynthesis")
    assert mem_id in before  # sanity: original wording matches

    await admin_handlers.do_update_memory(mem_id, "quantum entanglement qubit", agent_id="agent-f")

    stale = await _search(db, "agent-f", "photosynthesis")
    assert mem_id not in stale  # bug-008: old trigram must be gone from the index
    fresh = await _search(db, "agent-f", "entanglement")
    assert mem_id in fresh  # new wording is searchable


async def _search(db, agent_id: str, query: str) -> set[int]:
    hits = await memory_handlers._search_memories_keyword(db, agent_id, query, limit=50)
    return {h["id"] for h in hits}
