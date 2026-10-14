"""merge_memories (and its import twin): the episode dedup probe, the preview on a
database without the dedup indexes, the derived builds for written records, and the
copied profile's timestamp.

    - The episode dedup gate has no index on summary. Probing it once per incoming
      episode read every episode already in the bucket, so merging or restoring E
      episodes cost O(E^2) row reads while the write lock was held. The probe must
      not issue a bucket-wide summary comparison per row; deduplication itself must
      answer as before (same bucket skipped, other bucket kept, in-run repeats
      skipped, preview equal to the real run).
    - On a database whose dedup UNIQUE indexes could not be created, the source can
      hold two identical memories. The real merge sees its own uncommitted copy and
      skips the second; the preview must count the same.
    - Records a merge writes get their block and node builds queued, as store and
      update_memory queue them. A moved record otherwise loses its blocks (the delete
      trigger drops them) and its copy never gets any until a restart.
    - A copied profile keeps the source's updated_at instead of the merge time.
"""

import json
import os
import re
import tempfile

import pytest

from cpersona import admin_handlers, config, database, session, tasks

SRC = "x04.src"
DST = "x04.dst"


class _TempDB:
    """A database of its own, and a task queue that is never started, so what a call
    queued is exactly what the table holds afterwards."""

    def __init__(self, queue: bool = False):
        self._with_queue = queue

    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "x04.db")
        if self._with_queue:
            queue = tasks.MemoryTaskQueue()
            queue._running = True
            tasks._task_queue = queue
        else:
            tasks._task_queue = None
        self.db = await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()


async def _insert_episode(db, agent, summary, project="", channel=""):
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary) VALUES (?, ?, ?, ?)",
        (agent, project, channel, summary),
    )
    return cur.lastrowid


async def _insert_memory(db, agent, content, msg_id="", locked=0):
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, msg_id, source, timestamp, locked)"
        " VALUES (?, ?, ?, '{}', '2026-01-01T00:00:00Z', ?)",
        (agent, content, msg_id, locked),
    )
    return cur.lastrowid


# A summary comparison that is not keyed by a row id: the per-row bucket scan.
_BUCKET_PROBE = re.compile(r"FROM episodes WHERE (?!id = )[^;]*\bsummary = ", re.IGNORECASE)


async def _trace(statements: list[str]):
    """Record every statement on both connections the merge and import use."""
    writer = await database.get_db()
    reader = await database._get_read_db()
    for conn in {id(writer): writer, id(reader): reader}.values():
        await conn.set_trace_callback(statements.append)
    return (writer, reader)


async def _untrace(conns):
    for conn in conns:
        await conn.set_trace_callback(None)


async def _seed_episode_fixture(db):
    # Target: 'alpha' and 'beta' in the default bucket, 'gamma' only in project p.
    for summary in ("alpha", "beta"):
        await _insert_episode(db, DST, summary)
    await _insert_episode(db, DST, "gamma", project="p")
    # Source: 'alpha' (same bucket: skip), 'gamma' (default bucket: the target only has
    # it under p, so it is copied), 'delta' twice (the second is an in-run repeat: skip),
    # 'alpha' under project q (another bucket: copied).
    for summary, project in (("alpha", ""), ("gamma", ""), ("delta", ""), ("delta", ""), ("alpha", "q")):
        await _insert_episode(db, SRC, summary, project=project)
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_merge_episode_dedup_does_not_scan_the_bucket_per_row(dry_run):
    async with _TempDB() as tmp:
        await _seed_episode_fixture(tmp.db)
        statements: list[str] = []
        conns = await _trace(statements)
        try:
            res = await admin_handlers.do_merge_memories(SRC, DST, dry_run=dry_run)
        finally:
            await _untrace(conns)
        assert res["ok"] is True, res
        assert (res["merged_episodes"], res["skipped_episodes"]) == (3, 2), res
        scans = [s for s in statements if _BUCKET_PROBE.search(s)]
        assert scans == [], f"{len(scans)} per-row bucket scans, first: {scans[:1]}"
        if not dry_run:
            rows = await tmp.db.execute_fetchall(
                "SELECT project_id, summary FROM episodes WHERE agent_id = ? ORDER BY id", (DST,)
            )
            assert rows == [("", "alpha"), ("", "beta"), ("p", "gamma"), ("", "gamma"), ("", "delta"), ("q", "alpha")]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_import_episode_dedup_does_not_scan_the_bucket_per_row(dry_run, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORT_DIR", str(tmp_path))
    async with _TempDB() as tmp:
        await _seed_episode_fixture(tmp.db)
        path = tmp_path / "eps.jsonl"
        records = [
            {"_type": "episode", "agent_id": SRC, "summary": s, "project_id": p, "channel": ""}
            for s, p in (("alpha", ""), ("gamma", ""), ("delta", ""), ("delta", ""), ("alpha", "q"))
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        statements: list[str] = []
        conns = await _trace(statements)
        try:
            res = await admin_handlers.do_import_memories(str(path), target_agent_id=DST, dry_run=dry_run)
        finally:
            await _untrace(conns)
        assert res["ok"] is True, res
        assert (res["imported_episodes"], res["skipped_episodes"]) == (3, 2), res
        scans = [s for s in statements if _BUCKET_PROBE.search(s)]
        assert scans == [], f"{len(scans)} per-row bucket scans, first: {scans[:1]}"


@pytest.mark.asyncio
async def test_a_digest_collision_does_not_skip_a_distinct_episode(monkeypatch):
    """Every summary digests to the same key: a hit must be confirmed against the row."""
    monkeypatch.setattr(
        admin_handlers._EpisodeSummaryIndex,
        "_key",
        staticmethod(lambda project_id, channel, summary: b"one key for everything"),
    )
    async with _TempDB() as tmp:
        await _seed_episode_fixture(tmp.db)
        res = await admin_handlers.do_merge_memories(SRC, DST, dry_run=False)
        assert (res["merged_episodes"], res["skipped_episodes"]) == (3, 2), res


@pytest.mark.asyncio
async def test_merge_episode_dedup_is_linear_in_the_rows_read():
    """The cost behind the probe: SQLite's own count of rows a full-scan step visited.

    With a per-row bucket probe, merging N source episodes into a bucket of N reads
    about N * N rows; without it, a walk of the table per agent. Measured with the
    progress handler (VM instructions), the ratio between N and 2N tells the two
    apart: about 4 for the quadratic probe, about 2 for a linear pass.
    """

    async def cost(n: int) -> int:
        async with _TempDB() as tmp:
            for i in range(n):
                await _insert_episode(tmp.db, DST, f"target episode {i} " + "x" * 200)
                await _insert_episode(tmp.db, SRC, f"source episode {i} " + "y" * 200)
            await tmp.db.commit()
            ticks = [0]

            def tick():
                ticks[0] += 1
                return 0

            await tmp.db.set_progress_handler(tick, 100)
            try:
                res = await admin_handlers.do_merge_memories(SRC, DST, dry_run=False)
            finally:
                await tmp.db.set_progress_handler(None, 100)
            assert res["merged_episodes"] == n, res
            return ticks[0]

    small, large = await cost(150), await cost(300)
    assert large / small < 3.0, (small, large)


async def _drop_dedup_indexes(db):
    """The legacy state: the dedup UNIQUE indexes could not be created."""
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_content")
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_msg_id")
    await db.commit()


@pytest.mark.asyncio
async def test_merge_preview_matches_the_real_run_on_duplicate_source_content():
    async with _TempDB() as tmp:
        await _drop_dedup_indexes(tmp.db)
        await _insert_memory(tmp.db, SRC, "legacy", locked=1)
        await _insert_memory(tmp.db, SRC, "legacy", locked=1)
        await tmp.db.commit()
        preview = await admin_handlers.do_merge_memories(SRC, DST, dry_run=True)
        real = await admin_handlers.do_merge_memories(SRC, DST, dry_run=False)
        assert (real["merged_memories"], real["skipped_memories"]) == (1, 1), real
        assert (preview["merged_memories"], preview["skipped_memories"]) == (1, 1), preview


@pytest.mark.asyncio
async def test_merge_preview_matches_the_real_run_on_duplicate_source_msg_id():
    async with _TempDB() as tmp:
        await _drop_dedup_indexes(tmp.db)
        await _insert_memory(tmp.db, SRC, "first text", msg_id="m-1")
        await _insert_memory(tmp.db, SRC, "second text", msg_id="m-1")
        await tmp.db.commit()
        preview = await admin_handlers.do_merge_memories(SRC, DST, dry_run=True)
        real = await admin_handlers.do_merge_memories(SRC, DST, dry_run=False)
        assert (real["merged_memories"], real["skipped_memories"]) == (1, 1), real
        assert (preview["merged_memories"], preview["skipped_memories"]) == (1, 1), preview


LONG = " ".join(f"word{i}" for i in range(40))


@pytest.fixture
def builds_on(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    fake_embedding_client.token_window = 16
    return fake_embedding_client


async def _queued(db) -> dict[str, set[tuple[str, str, int]]]:
    rows = await db.execute_fetchall("SELECT task_type, agent_id, payload FROM pending_memory_tasks")
    out: dict[str, set] = {}
    for task_type, agent, payload in rows:
        body = json.loads(payload)
        out.setdefault(task_type, set()).add((agent, body["kind"], body["id"]))
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["copy", "move"])
async def test_merge_queues_block_and_node_builds_for_written_records(builds_on, mode):
    async with _TempDB(queue=True) as tmp:
        await _insert_memory(tmp.db, SRC, "short note")
        await _insert_memory(tmp.db, SRC, LONG)
        await _insert_episode(tmp.db, SRC, "an episode")
        await _insert_memory(tmp.db, DST, "short note")  # skipped: the target has it
        await tmp.db.commit()

        res = await admin_handlers.do_merge_memories(SRC, DST, mode=mode)
        assert res["ok"] is True and res["merged_memories"] == 1 and res["merged_episodes"] == 1, res

        mems = dict(
            (content, mid)
            for mid, content in await tmp.db.execute_fetchall(
                "SELECT id, content FROM memories WHERE agent_id = ?", (DST,)
            )
        )
        (ep_id,) = [r[0] for r in await tmp.db.execute_fetchall("SELECT id FROM episodes WHERE agent_id = ?", (DST,))]
        queued = await _queued(tmp.db)
        written = {(DST, "mem", mems[LONG]), (DST, "ep", ep_id)}
        # The skipped row is the target's own and already had its chance at store time.
        assert queued.get("build_blocks") == written, queued
        # Only the record past the token window gets nodes.
        assert queued.get("build_nodes") == {(DST, "mem", mems[LONG])}, queued


@pytest.mark.asyncio
async def test_merge_preview_queues_no_builds(builds_on):
    async with _TempDB(queue=True) as tmp:
        await _insert_memory(tmp.db, SRC, LONG)
        await _insert_episode(tmp.db, SRC, "an episode")
        await tmp.db.commit()
        res = await admin_handlers.do_merge_memories(SRC, DST, dry_run=True)
        assert res["merged_memories"] == 1 and res["merged_episodes"] == 1, res
        assert await _queued(tmp.db) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["copy", "move"])
async def test_merge_keeps_the_copied_profile_updated_at(mode):
    async with _TempDB() as tmp:
        await tmp.db.execute(
            "INSERT INTO profiles (agent_id, user_id, content, updated_at)"
            " VALUES (?, 'u1', 'likes tea', '2025-01-01 00:00:00')",
            (SRC,),
        )
        await tmp.db.commit()
        res = await admin_handlers.do_merge_memories(SRC, DST, mode=mode)
        assert res["profile_copied"] is True, res
        rows = await tmp.db.execute_fetchall("SELECT updated_at FROM profiles WHERE agent_id = ?", (DST,))
        assert rows == [("2025-01-01 00:00:00",)]


def test_regenerating_the_golden_keeps_the_merge_profile_timestamp_it_recorded():
    """The merge golden recorded the merge-time stamp; the new value is excused in
    the replay, and a regeneration must not write it over the recorded one."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "capture-behaviour.py"
    spec = importlib.util.spec_from_file_location("capture_behaviour_x04", path)
    capture = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = capture
    spec.loader.exec_module(capture)

    for sid in ("merge-copy-skip", "merge-move"):
        recorded = {sid: {"db": {"profiles": [
            {"content": "source profile", "updated_at": "<generated>"},
            {"content": "kept", "updated_at": "2026-01-01T00:00:07Z"},
        ]}}}
        observed = {sid: {"db": {"profiles": [
            {"content": "source profile", "updated_at": "2026-01-01T00:00:07Z"},
            {"content": "changed", "updated_at": "2026-01-01T00:00:07Z"},
        ]}}}
        written = capture._reconciled(observed, recorded)
        rows = written[sid]["db"]["profiles"]
        assert rows[0]["updated_at"] == "<generated>", "the recorded value was overwritten"
        assert rows[1]["content"] == "changed", "an unlisted row was excused"
