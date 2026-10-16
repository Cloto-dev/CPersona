"""Behaviour that was pinned only by a comment, and the import preview beside concurrent reads.

- bug-064: archive_episode appends a row on every call, so it must not advertise
  idempotentHint. A host retries a tool that claims it after a lost response, and
  the episode is then stored twice.
- bug-101: the fused-gate calibration drops the pseudo-query memory from its own
  results, and only that memory. Episodes number their rows independently of
  memories, so an episode that shares the memory's integer id is a different row
  and a legitimate sample of the curve.
- bug-069: after an FTS rebuild the index is verified again, falling back to the
  structural integrity check on a SQLite build without the enhanced form. Without
  the fallback a rebuild that succeeded is reported fixed:false there.
- bug-070: an import preview runs on the read connection every read scope shares.
  Its memory INSERT is guarded, and the preview remembers what it previewed rather
  than writing it. Without the guard a concurrent read sees rows that never come
  to exist, the read scope's exit rolls back the rows the preview's dedup relies
  on, and within-file duplicates are counted as imports, so the preview stops
  matching a real run. No test that only runs the preview alone can see this.
- bug-800: the merge preview runs on the same shared read connection, and its
  memory INSERT carries the same guard. Without it, a read scope opened during
  the preview sees target rows the preview made up.

Each test fails, for the reason it names, when the guarded behaviour is removed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
import pytest_asyncio
from mcp import types

from cpersona import admin_handlers, checks, config, database, memory_handlers, server, session
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db

from conftest import fake_embed_one


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


async def _call(name: str, arguments: dict) -> types.CallToolResult:
    handler = server.registry.server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return (await handler(request)).root


async def _listed_tool(name: str) -> types.Tool:
    """The tool as a host sees it in tools/list, annotations included."""
    handler = server.registry.server.request_handlers[types.ListToolsRequest]
    listed = (await handler(types.ListToolsRequest(method="tools/list"))).root
    return next(t for t in listed.tools if t.name == name)


# ===========================================================================
# bug-064 -- archive_episode is not idempotent, and says so.
# ===========================================================================


@pytest.mark.asyncio
async def test_archive_episode_appends_on_every_call_so_it_does_not_claim_idempotence(db):
    args = {"agent_id": "agent.y12-archive", "summary": "the same session, sent twice"}
    first = await _call("archive_episode", args)
    second = await _call("archive_episode", args)
    assert not first.isError and not second.isError

    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM episodes WHERE agent_id = ?", ("agent.y12-archive",)
    )
    # The fact the annotation has to agree with: a repeated call is a second episode.
    assert rows[0][0] == 2, "two identical archive_episode calls did not store two episodes"

    tool = await _listed_tool("archive_episode")
    assert tool.annotations is not None
    assert tool.annotations.idempotentHint is False, (
        "archive_episode stores a new episode on every call but advertises idempotentHint, "
        "so a host that retries after a lost response stores the episode twice"
    )


# ===========================================================================
# bug-101 -- the calibration skips the pseudo-query memory, not an episode
# that shares its integer id.
# ===========================================================================

# Two clusters twelve hours apart: same-session neighbours are the positive
# population, the far cluster the null one. Every memory and the episode share the
# words "cluster" and "memory", so the keyword arms return the whole corpus for
# every pseudo-query, and the corpus (17 rows) stays under the 20 rows a simulated
# recall asks for, so no row's membership depends on its rank.
_CALIBRATION_ROWS = [(f"apples pears cluster one memory {i}", f"2026-05-14T00:{i:02d}:00Z") for i in range(8)]
_CALIBRATION_ROWS += [(f"engines pistons cluster two memory {i}", f"2026-05-14T12:{i:02d}:00Z") for i in range(8)]
_EPISODE_SUMMARY = "apples pears cluster one memory session summary"


async def _seed_calibration_corpus(db, agent_id: str, first_memory_id: int, episode_id: int) -> None:
    for offset, (content, ts) in enumerate(_CALIBRATION_ROWS):
        await db.execute(
            "INSERT INTO memories (id, agent_id, content, source, timestamp, embedding) "
            "VALUES (?, ?, ?, '{}', ?, ?)",
            (
                first_memory_id + offset,
                agent_id,
                content,
                ts,
                EmbeddingClient.pack_embedding(fake_embed_one(content)),
            ),
        )
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, keywords, embedding, start_time, end_time) "
        "VALUES (?, ?, ?, '', ?, ?, ?)",
        (
            episode_id,
            agent_id,
            _EPISODE_SUMMARY,
            EmbeddingClient.pack_embedding(fake_embed_one(_EPISODE_SUMMARY)),
            "2026-05-14T00:03:00Z",
            "2026-05-14T00:04:00Z",
        ),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_an_episode_sharing_the_pseudo_query_id_stays_in_the_calibration_curve(
    db, fake_embedding_client, monkeypatch
):
    # rsf with confidence off: every fused row carries the gate's own signal, so
    # every returned row is a sample and the population sizes are exact.
    monkeypatch.setattr(config, "RECALL_MODE", "rsf")
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rsf")
    monkeypatch.setattr(config, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(config, "VECTOR_MIN_SIMILARITY", 0.3)

    # The same corpus twice, under two agents. In the first the episode's id is the
    # id of a memory that serves as a pseudo-query; in the second it is the id of no
    # memory at all. Nothing else differs, so the two calibrations must collect the
    # same samples.
    colliding_memory_id = 5003
    await _seed_calibration_corpus(db, "agent.y12-collide", 5001, colliding_memory_id)
    await _seed_calibration_corpus(db, "agent.y12-apart", 6001, 9999)

    # Precondition: the pseudo-query whose id the episode shares does return it,
    # so the skip under test is actually reached.
    query_content = _CALIBRATION_ROWS[colliding_memory_id - 5001][0]
    recalled = await memory_handlers._recall_rsf(db, "agent.y12-collide", query_content, 20, False)
    assert any(
        r.get("id") == colliding_memory_id and memory_handlers._is_episode_result(r) for r in recalled
    ), "the colliding episode is not in its pseudo-query's results; this test proves nothing"

    sample_all = len(_CALIBRATION_ROWS)
    collide = await admin_handlers._calibrate_fused_gate(db, "agent.y12-collide", sample_all, 30.0, 1.0, 0.0)
    apart = await admin_handlers._calibrate_fused_gate(db, "agent.y12-apart", sample_all, 30.0, 1.0, 0.0)
    assert collide is not None and apart is not None, "the calibration collected too few samples"
    assert collide["queries_run"] == apart["queries_run"] == sample_all

    assert (collide["n_pos"], collide["n_null"]) == (apart["n_pos"], apart["n_null"]), (
        "an episode whose id equals the pseudo-query memory's id was dropped from the "
        f"calibration curve: colliding ids gave {collide['n_pos']} positive / "
        f"{collide['n_null']} null samples, distinct ids {apart['n_pos']} / {apart['n_null']}"
    )
    # And the exact populations, which also pin the other half of the condition: the
    # pseudo-query memory itself is never a sample. Every query returns the whole
    # corpus but itself. A cluster-one query sees its 7 neighbours and the episode
    # (stamped inside cluster one's window) as positive and cluster two's 8 as null;
    # a cluster-two query sees its 7 neighbours as positive and cluster one's 8 plus
    # the episode as null.
    per_cluster = len(_CALIBRATION_ROWS) // 2
    expected_pos = per_cluster * per_cluster + per_cluster * (per_cluster - 1)
    expected_null = per_cluster * per_cluster + per_cluster * (per_cluster + 1)
    assert (collide["n_pos"], collide["n_null"]) == (expected_pos, expected_null)


# ===========================================================================
# bug-069 -- the post-rebuild verification falls back like the detection does.
# ===========================================================================

_ENHANCED_CHECK = "VALUES('integrity-check', 1)"
_STRUCTURAL_CHECK = "VALUES('integrity-check')"


class _OlderFtsBuild:
    """The writer connection as a SQLite build without the enhanced FTS5 check sees it.

    The external-content form of ``integrity-check`` (``rank`` = 1) raises
    OperationalError, as it does where the build does not support it; every other
    statement runs for real. ``corrupt_after_rebuild`` makes the structural check of
    that table raise DatabaseError once it has been rebuilt, which is how real damage
    that a rebuild did not cure is reported.
    """

    def __init__(self, conn, corrupt_after_rebuild: str | None = None):
        self._conn = conn
        self._corrupt_after_rebuild = corrupt_after_rebuild
        self.rebuilt: set[str] = set()
        self.structural_checks_after_rebuild: list[str] = []

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, *args, **kwargs):
        if _ENHANCED_CHECK in sql:
            raise sqlite3.OperationalError("unsupported integrity-check form")
        table = sql.split("INSERT INTO ", 1)[1].split("(", 1)[0] if "INSERT INTO " in sql else ""
        if _STRUCTURAL_CHECK in sql and table in self.rebuilt:
            self.structural_checks_after_rebuild.append(table)
            if table == self._corrupt_after_rebuild:
                raise sqlite3.DatabaseError("database disk image is malformed")
        cursor = await self._conn.execute(sql, *args, **kwargs)
        if "VALUES('rebuild')" in sql:
            self.rebuilt.add(table)
        return cursor

    async def execute_fetchall(self, sql, *args, **kwargs):
        return await self._conn.execute_fetchall(sql, *args, **kwargs)


@pytest_asyncio.fixture
async def dropped_memories_fts(db):
    """memories_fts dropped with a row behind it; restored whatever the test does."""
    assert checks.FTS_ENABLED is True
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('agent.y12-fts', 'photosynthesis chloroplast', '{}', '2026-01-01T00:00:00+00:00')"
    )
    await db.commit()
    await db.execute("DROP TABLE memories_fts")
    await db.commit()
    yield db
    await db.rollback()
    await db.executescript(database.FTS_SQL)
    await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    await db.commit()


@pytest.mark.asyncio
async def test_a_rebuild_verified_by_the_structural_check_is_reported_fixed(dropped_memories_fts):
    conn = _OlderFtsBuild(dropped_memories_fts)

    issues = await checks.check_fts_integrity(conn, "", fix=True)
    await dropped_memories_fts.commit()

    memories = [i for i in issues if i["table"] == "memories"]
    assert [i["type"] for i in memories] == ["fts_integrity_failure"]
    assert memories[0].get("fixed") is True, (
        "the rebuild succeeded, but on a build without the enhanced integrity check the "
        f"repair was reported fixed:{memories[0].get('fixed')}"
    )
    # Not taken on trust: the rebuilt index was verified, with the form this build has.
    assert conn.structural_checks_after_rebuild == ["memories_fts"], (
        "the rebuilt index was reported fixed without the structural check running"
    )
    # The repair really happened: the restored index answers a keyword search.
    hits = await dropped_memories_fts.execute_fetchall(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'chloroplast'"
    )
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_damage_the_rebuild_did_not_cure_is_still_reported_unfixed(dropped_memories_fts):
    conn = _OlderFtsBuild(dropped_memories_fts, corrupt_after_rebuild="memories_fts")

    issues = await checks.check_fts_integrity(conn, "", fix=True)

    memories = [i for i in issues if i["table"] == "memories"]
    assert memories[0].get("fixed") is False, (
        "the index was still damaged after the rebuild, yet the repair was reported "
        f"fixed:{memories[0].get('fixed')}"
    )
    assert conn.structural_checks_after_rebuild == ["memories_fts"]


# ===========================================================================
# bug-070 -- an import preview beside concurrent reads.
# ===========================================================================

_PREVIEW_PAIRS = 20
_MSG_ID_PAIRS = 5


def _write_duplicates(path) -> str:
    """A file whose second half repeats its first on both dedup axes.

    Twenty contents written twice, and five message ids each carried by two
    different contents. A real run imports the first of each pair and skips the
    second, so 25 / 25.
    """
    records = []
    for i in range(_PREVIEW_PAIRS):
        records += [{"_type": "memory", "agent_id": "x", "content": f"repeated body {i}"}] * 2
    for i in range(_MSG_ID_PAIRS):
        records.append({"_type": "memory", "agent_id": "x", "msg_id": f"mid-{i}", "content": f"first text {i}"})
        records.append({"_type": "memory", "agent_id": "x", "msg_id": f"mid-{i}", "content": f"second text {i}"})
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    return str(path)


_EXPECTED_COUNTS = {
    "imported_memories": _PREVIEW_PAIRS + _MSG_ID_PAIRS,
    "skipped_memories": _PREVIEW_PAIRS + _MSG_ID_PAIRS,
}


def _memory_counts(result: dict) -> dict:
    return {key: result[key] for key in _EXPECTED_COUNTS}


async def _visible_rows(agent_id: str) -> int:
    async with database.connection() as rdb:
        rows = await rdb.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (agent_id,))
    return rows[0][0]


async def _real_run_counts(path: str, agent_id: str) -> dict:
    real = await admin_handlers.do_import_memories(path, target_agent_id=agent_id)
    assert real["ok"] is True
    return _memory_counts(real)


@pytest.mark.asyncio
async def test_a_preview_shows_concurrent_readers_nothing_and_counts_like_a_real_run(db, tmp_path):
    agent = "agent.y12-concurrent"
    path = _write_duplicates(tmp_path / "duplicates.jsonl")
    stop = asyncio.Event()
    seen: list[int] = []

    async def reader():
        # Another request's reads, on the read connection the preview runs on.
        while not stop.is_set():
            seen.append(await _visible_rows(agent))
            await asyncio.sleep(0)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    try:
        preview = await admin_handlers.do_import_memories(path, target_agent_id=agent, dry_run=True)
    finally:
        stop.set()
        await task

    assert preview["ok"] is True
    assert seen, "the reader never ran beside the preview; this test proves nothing"
    phantom = [count for count in seen if count]
    assert not phantom, (
        f"{len(phantom)} of {len(seen)} concurrent reads saw rows a preview should never "
        f"write (up to {max(phantom)})"
    )
    assert await _visible_rows(agent) == 0
    assert _memory_counts(preview) == _EXPECTED_COUNTS == await _real_run_counts(path, agent), (
        "the preview's counts stopped matching the real run's while another reader shared "
        "its connection"
    )


@pytest.mark.asyncio
async def test_a_read_between_preview_records_leaves_the_preview_counts_alone(db, tmp_path, monkeypatch):
    """The same race with the interleaving fixed: a read scope opens and closes after
    every record, which is the most a concurrent request can do between two records
    and the case that decides whether within-file duplicates are still caught."""
    agent = "agent.y12-interleaved"
    path = _write_duplicates(tmp_path / "duplicates.jsonl")
    seen: list[int] = []
    import_record = admin_handlers._import_memory_record

    async def import_record_then_read(conn, record, aid, tally, line_num):
        await import_record(conn, record, aid, tally, line_num)
        seen.append(await _visible_rows(agent))

    monkeypatch.setattr(admin_handlers, "_import_memory_record", import_record_then_read)
    preview = await admin_handlers.do_import_memories(path, target_agent_id=agent, dry_run=True)
    monkeypatch.setattr(admin_handlers, "_import_memory_record", import_record)

    assert preview["ok"] is True
    assert len(seen) == 2 * (_PREVIEW_PAIRS + _MSG_ID_PAIRS), "a record went past the interleaved read"
    assert _memory_counts(preview) == _EXPECTED_COUNTS, (
        "a read scope's exit rolled back what the preview had seen, so the second of a "
        f"within-file pair was counted as an import: {_memory_counts(preview)}"
    )
    assert not any(seen), "a read between two preview records saw a row the preview wrote"
    assert await _real_run_counts(path, agent) == _EXPECTED_COUNTS


# ===========================================================================
# bug-800 -- a merge preview beside concurrent reads.
# ===========================================================================

_MERGE_SOURCE_ROWS = 12


@pytest.mark.asyncio
async def test_a_read_between_merge_preview_pages_sees_no_previewed_row(db, monkeypatch):
    """The merge twin of the import test above: a read scope opens and closes after
    every page of the preview (three rows to a page here), which is when another
    request's read can interleave with it."""
    source, target = "agent.y12-merge-source", "agent.y12-merge-target"
    for i in range(_MERGE_SOURCE_ROWS):
        await memory_handlers.do_store(source, {"content": f"merge preview body {i}", "msg_id": f"m-{i}"})
    monkeypatch.setattr(admin_handlers, "_MERGE_READ_CHUNK", 3)
    seen: list[int] = []
    merge_page = admin_handlers._merge_memory_page

    async def merge_page_then_read(conn, rows, target_agent_id, tally):
        await merge_page(conn, rows, target_agent_id, tally)
        seen.append(await _visible_rows(target))

    monkeypatch.setattr(admin_handlers, "_merge_memory_page", merge_page_then_read)
    preview = await admin_handlers.do_merge_memories(source, target, dry_run=True)
    assert preview.get("ok", True) is True and "error" not in preview, preview
    assert len(seen) == _MERGE_SOURCE_ROWS // 3, "a page went past the interleaved read"
    assert not any(seen), f"a read between two preview pages saw rows the preview wrote: {seen}"
    assert await _visible_rows(target) == 0

    monkeypatch.setattr(admin_handlers, "_merge_memory_page", merge_page)
    real = await admin_handlers.do_merge_memories(source, target)
    counts = ("merged_memories", "skipped_memories")
    assert {k: preview[k] for k in counts} == {k: real[k] for k in counts} == {
        "merged_memories": _MERGE_SOURCE_ROWS,
        "skipped_memories": 0,
    }, (preview, real)
