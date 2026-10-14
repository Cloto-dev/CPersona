"""Shared, process-wide files change only under a grant that covers every agent.

Three files and one mode belong to the whole deployment rather than to any one
agent: the database (and its sidecars), whose mode the operator chooses, and the
three derived index files, each holding every agent's rows. These tests pin who
may change them and in which order:

* the file-permission repair narrows the shared files, so under an ACL it needs
  read-write on '*', like the other repairs whose effect is not one agent's;
* a purge under a grant scoped to one agent removes that agent's rows but leaves
  the shared index files to the operator (readers hydrate from the database, so
  the purged rows drop out of every answer), while a purge under a grant that
  covers every agent still removes them with the rows they held (bug-337);
* an index build whose rows were read before such a purge does not publish them
  after it (bug-515): the purge would otherwise be undone on disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

import numpy as np
import pytest

from cpersona import (
    acl,
    admin_handlers,
    block_index,
    coarse_index,
    config,
    database,
    server,
    vector_index,
)
from cpersona.database import read_snapshot

VICTIM = "u17-purged-agent"
KEEPER = "u17-kept-agent"
BLOCK_INSERT = (
    "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id,"
    " channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model)"
    " VALUES ('mem', ?, 0, ?, '', '', 0, 1, 0, ?, 'm')"
)


@contextlib.asynccontextmanager
async def _database():
    """A fresh database in a directory of its own, so the index files beside it are ours."""
    saved = (database._db, database.DB_PATH, config.DB_PATH)
    directory = tempfile.mkdtemp()
    database._db = None
    database.DB_PATH = config.DB_PATH = os.path.join(directory, "u17.db")
    block_index._cache.clear()
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        database._db, database.DB_PATH, config.DB_PATH = saved
        block_index._cache.clear()
        shutil.rmtree(directory, ignore_errors=True)


@contextlib.contextmanager
def _acting_as(grants: dict):
    """Activate an ACL holding exactly ``grants`` for one client, and act as that client."""
    acl.activate(acl.AclConfig(grants_by_client={"u17": grants}, token_entries=(("token-u17", "u17"),)))
    token = acl.set_principal(acl.Principal("u17"))
    try:
        yield
    finally:
        acl.reset_principal(token)
        acl.activate(None)


@pytest.fixture
def blocks_on(monkeypatch):
    monkeypatch.setattr(config, "BLOCK_INDEX_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)


@pytest.fixture
def ledger_beside_the_database(monkeypatch):
    # The repair reaches every path fileperms owns; keep all of them in the test's directory.
    monkeypatch.setattr(config, "ALIAS_LEDGER_FILE", "")


def _vec(seed: int, dim: int = 8) -> bytes:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32).tobytes()


async def _seed(db, agent: str, seed: int) -> int:
    """One embedded memory with one block row, enough for all three index files."""
    cursor = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, created_at, embedding)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            agent,
            f"held content {seed}",
            json.dumps({"type": "User", "id": f"discord:{agent}"}),
            "2026-03-01T00:00:00+00:00",
            "2026-03-01 00:00:0%d" % seed,
            _vec(seed),
        ),
    )
    await db.execute(BLOCK_INSERT, (cursor.lastrowid, agent, b"\x00" * 4))
    await db.commit()
    return cursor.lastrowid


def _index_files() -> list[str]:
    return [
        vector_index.index_path("memories"),
        coarse_index.index_path("memories"),
        block_index.index_path(),
    ]


async def _build_all(db) -> dict[str, bytes]:
    assert (await vector_index.build_index(db, "memories"))["built"]
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    assert (await block_index.build_block_index())["built"]
    files = {path: Path(path).read_bytes() for path in _index_files()}
    # The control: every file held the agent about to be purged.
    for path, content in files.items():
        assert VICTIM.encode() in content, f"{path} never held {VICTIM}"
    return files


def _residue(path: str) -> bytes:
    return Path(path).read_bytes() if os.path.exists(path) else b""


# --------------------------------------------------------------------------------------
# the file-permission repair needs every agent
# --------------------------------------------------------------------------------------


def test_the_file_permission_repair_demands_every_agent():
    demands = acl._health_demands({"agent_id": "A", "fix": True, "checks": ["file_permissions"]})
    assert (acl.WILDCARD, acl.PERM_WRITE) in demands, demands


@pytest.mark.asyncio
async def test_a_single_agent_grant_does_not_narrow_the_shared_database(ledger_beside_the_database):
    async with _database() as db:
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, 'x', '2026-01-01T00:00:00+00:00')",
            (VICTIM,),
        )
        await db.commit()
        # The operator widened the database on purpose, for a backup reader in its group.
        os.chmod(config.DB_PATH, 0o640)
        guarded = server.registry._handlers["check_health"]
        with _acting_as({VICTIM: acl.PERM_WRITE}):
            result = await guarded({"agent_id": VICTIM, "fix": True, "checks": ["file_permissions"]})
        mode = stat.S_IMODE(os.stat(config.DB_PATH).st_mode)
    assert mode == 0o640, f"a grant on one agent narrowed the shared database to {mode:04o}: {result}"
    assert result.get("error") == "permission_denied", result


@pytest.mark.asyncio
async def test_a_grant_on_every_agent_still_runs_the_file_permission_repair(ledger_beside_the_database):
    """The demand is sized to the repair, not a refusal: '*' read-write still narrows."""
    async with _database() as db:
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, 'x', '2026-01-01T00:00:00+00:00')",
            (VICTIM,),
        )
        await db.commit()
        os.chmod(config.DB_PATH, 0o640)
        guarded = server.registry._handlers["check_health"]
        with _acting_as({"*": acl.PERM_WRITE}):
            result = await guarded({"agent_id": VICTIM, "fix": True, "checks": ["file_permissions"]})
        mode = stat.S_IMODE(os.stat(config.DB_PATH).st_mode)
    assert result.get("error") != "permission_denied", result
    assert mode == 0o600, result


# --------------------------------------------------------------------------------------
# a purge under a grant scoped to one agent leaves the shared index files alone
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_single_agent_purge_keeps_the_shared_index_files(blocks_on):
    async with _database() as db:
        await _seed(db, VICTIM, 1)
        await _seed(db, KEEPER, 2)
        before = await _build_all(db)
        guarded = server.registry._handlers["delete_agent_data"]
        with _acting_as({VICTIM: acl.PERM_WRITE}):
            result = await guarded({"agent_id": VICTIM})
        assert result.get("ok") and result["deleted_memories"] == 1, result
        gone = [os.path.basename(p) for p in before if _residue(p) != before[p]]
        assert not gone, f"a purge authorised on one agent removed index files every agent shares: {gone}"
        # What makes keeping them safe: the kept file still answers, and the
        # purged agent's rows are not among its answers.
        axes = {"project_id": "", "channel": "", "width": 4}
        kept = await block_index.examined(db, ("m", "m"), agent_id=KEEPER, **axes)
        purged = await block_index.examined(db, ("m", "m"), agent_id=VICTIM, **axes)
    assert kept is not None and kept.keys(), "the kept file stopped answering for the other agent"
    assert purged is not None and not purged.keys(), purged.keys()


@pytest.mark.asyncio
async def test_a_purge_under_a_grant_on_every_agent_still_removes_the_index_files(blocks_on):
    """bug-337 is kept where the grant covers every agent the files hold."""
    async with _database() as db:
        await _seed(db, VICTIM, 1)
        await _seed(db, KEEPER, 2)
        before = await _build_all(db)
        guarded = server.registry._handlers["delete_agent_data"]
        with _acting_as({"*": acl.PERM_WRITE}):
            result = await guarded({"agent_id": VICTIM})
        assert result.get("ok") and result["deleted_memories"] == 1, result
        leaked = [os.path.basename(p) for p in before if VICTIM.encode() in _residue(p)]
    assert not leaked, f"the purged agent is still on disk in {leaked}"


# --------------------------------------------------------------------------------------
# a build that read its rows before a purge does not publish them after it
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_block_index_build_does_not_republish_a_purged_agent(blocks_on, monkeypatch):
    async with _database() as db:
        await _seed(db, VICTIM, 1)
        await _seed(db, KEEPER, 2)
        purged = {}
        original = block_index._write_rows

        async def rows_then_purge(snap, tmp, bits_tmp, width):
            # The build has read every row on its snapshot; the purge lands now,
            # before the build publishes, as the queue and a client interleave.
            header = await original(snap, tmp, bits_tmp, width)
            purged.update(await admin_handlers.do_delete_agent_data(VICTIM))
            return header

        monkeypatch.setattr(block_index, "_write_rows", rows_then_purge)
        built = await block_index.build_block_index()
        assert purged.get("ok") and purged["deleted_memories"] == 1, purged
        residue = _residue(block_index.index_path())
    assert VICTIM.encode() not in residue, f"the build put the purged agent back on disk: {built}"


@pytest.mark.parametrize("which", ["contiguous", "coarse"])
@pytest.mark.asyncio
async def test_a_vector_index_build_does_not_republish_a_purged_agent(which, monkeypatch):
    async with _database() as db:
        await _seed(db, VICTIM, 1)
        await _seed(db, KEEPER, 2)
        purged = {}
        original = vector_index.iter_embedding_chunks

        async def chunks_then_purge(conn, table, watermark, width):
            async for blobs in original(conn, table, watermark, width):
                yield blobs
            # Every embedding is read; the purge lands before the file is published.
            purged.update(await admin_handlers.do_delete_agent_data(VICTIM))

        monkeypatch.setattr(vector_index, "iter_embedding_chunks", chunks_then_purge)
        async with read_snapshot() as snap:
            if which == "contiguous":
                built = await vector_index.build_index(snap, "memories")
                path = vector_index.index_path("memories")
            else:
                built = await coarse_index.build_coarse_index(snap, "memories")
                path = coarse_index.index_path("memories")
        assert purged.get("ok") and purged["deleted_memories"] == 1, purged
        residue = _residue(path)
    assert VICTIM.encode() not in residue, f"the build put the purged agent back on disk: {built}"


@pytest.mark.asyncio
async def test_a_build_a_purge_overtook_does_not_hold_off_the_next(blocks_on, monkeypatch):
    """Discarding is not declining: the queue's next build may run at once."""

    async def superseded(*args, **kwargs):
        return {"built": False, "reason": vector_index.SUPERSEDED_REASON}

    monkeypatch.setattr(block_index, "build_block_index", superseded)
    monkeypatch.setattr(block_index, "_declined_until", 0.0)
    await block_index.build_task({})
    assert block_index._declined_until == 0.0
