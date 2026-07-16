"""Regression tests for the 2.5.0b1 audit fixes (bug-086+).

Covers the architectural fixes landed for the pre-b1 comprehensive audit:
read/write connection separation (bug-086), init-failure memoisation (bug-087),
atomic merge mode='move' (bug-088), single-transaction queue drain (bug-089),
handler-failure routing in the drain (bug-090), and the agent-wipe queue purge
(bug-093).
"""
import asyncio
import os

import pytest
import pytest_asyncio

from cpersona import admin_handlers, database, memory_handlers, tasks
from cpersona.database import connection, get_db, transaction


@pytest_asyncio.fixture
async def clean_db():
    """A freshly-truncated DB for the b1 audit tests."""
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-086: the read seam must not observe another coroutine's uncommitted
# transaction. On the old shared connection, a connection() SELECT executed
# inside the writer's open transaction — a dedup probe could match an import
# row that was later rolled back (the acknowledged store then existed nowhere).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_seam_does_not_see_uncommitted_writes(clean_db):
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with transaction() as db:
            await db.execute(
                "INSERT INTO memories (agent_id, content, source, timestamp) "
                "VALUES ('holder', 'uncommitted sentinel', '{}', 't')"
            )
            started.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await started.wait()
    try:
        async with connection() as r:
            rows = await r.execute_fetchall(
                "SELECT COUNT(*) FROM memories WHERE agent_id = 'holder'"
            )
        assert rows[0][0] == 0, "read seam observed an uncommitted write (dirty read)"
    finally:
        release.set()
        await task

    # After the holder commits, the same read seam sees the row.
    async with connection() as r:
        rows = await r.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = 'holder'"
        )
    assert rows[0][0] == 1


# ---------------------------------------------------------------------------
# bug-086 companion: a statement that opens an implicit transaction on the read
# connection (FTS5's integrity-check is spelled as an INSERT) must not pin the
# WAL snapshot past the seam scope — connection() rolls it back on exit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_seam_unpins_snapshot_on_scope_exit(clean_db):
    async with connection() as r:
        await r.execute("BEGIN")
        assert r._conn.in_transaction
    rd = database._read_db
    if rd is not None and rd is not database._db and rd._conn is not None:
        assert not rd._conn.in_transaction, "read seam leaked an open transaction"

    # The proof that matters: a commit AFTER the (rolled-back) pin is visible.
    async with transaction() as db:
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            "VALUES ('post-pin', 'fresh row', '{}', 't')"
        )
    async with connection() as r:
        rows = await r.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = 'post-pin'"
        )
    assert rows[0][0] == 1, "read connection served a stale pinned snapshot"


# ---------------------------------------------------------------------------
# bug-086 companion: when the write connection is swapped out (close_db +
# re-init, the harnesses' boot simulations), the read seam re-keys itself
# instead of serving the old database file.
# ---------------------------------------------------------------------------


async def _reset_read_seam():
    """Close and drop the current read connection; the seam lazily reopens it.

    Restoring a stashed read-connection object is never safe — the seam may
    have closed it during a re-key — so tests always reset to None instead."""
    if database._read_db is not None and database._read_db is not database._db:
        await database._read_db.close()
    database._read_db = None
    database._read_db_owner = None


@pytest.mark.asyncio
async def test_read_seam_rekeys_after_reboot(clean_db, tmp_path, monkeypatch):
    async with connection() as r:  # materialise the read conn on the main DB
        await r.execute_fetchall("SELECT 1")

    saved_db = database._db
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "boot.db"))
    database._db = None
    try:
        async with transaction() as db:
            await db.execute(
                "INSERT INTO memories (agent_id, content, source, timestamp) "
                "VALUES ('boot-agent', 'boot row', '{}', 't')"
            )
        async with connection() as r:
            rows = await r.execute_fetchall(
                "SELECT COUNT(*) FROM memories WHERE agent_id = 'boot-agent'"
            )
        assert rows[0][0] == 1, "read seam still pointed at the pre-reboot database"
    finally:
        if database._db is not None:
            await database._db.close()
        await _reset_read_seam()
        database._db = saved_db


# ---------------------------------------------------------------------------
# bug-087: get_db() must not memoise a half-initialised connection. A failed
# boot leaves _db unset (and the connection closed) so the next call re-runs
# the idempotent ladder instead of serving missing tables from the fast path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_db_is_not_memoized_on_failed_init(tmp_path, monkeypatch):
    saved_db = database._db
    database._db = None
    await _reset_read_seam()
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "flaky-boot.db"))

    real_init = database._init_schema
    calls = {"n": 0}

    async def flaky_init(db):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected boot fault")
        return await real_init(db)

    monkeypatch.setattr(database, "_init_schema", flaky_init)
    try:
        with pytest.raises(RuntimeError, match="injected boot fault"):
            await database.get_db()
        assert database._db is None, "failed init published a half-initialised connection"

        db = await database.get_db()  # the retry re-runs the ladder and succeeds
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        )
        assert rows, "retried init did not complete the schema"
    finally:
        if database._db is not None:
            await database._db.close()
        await _reset_read_seam()
        database._db = saved_db


# ---------------------------------------------------------------------------
# bug-088: merge mode='move' runs the source wipe INSIDE the merge transaction.
# A fault anywhere in the unit (including the delete) must roll back the copy
# too — as two transactions, the committed copy survived a failed delete and
# the response contradicted the DB state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_move_is_one_atomic_unit(clean_db, monkeypatch):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('src', 'movable row', '{}', 't')"
    )
    await db.commit()

    async def failing_delete(db_, agent_id):
        raise RuntimeError("injected delete fault")

    monkeypatch.setattr(admin_handlers, "_delete_agent_rows", failing_delete)
    res = await admin_handlers.do_merge_memories("src", "dst", mode="move")
    assert res["ok"] is False and "injected delete fault" in res["error"]

    # The copy was rolled back together with the failed delete: dst empty, src intact.
    dst = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='dst'"))[0][0]
    src = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='src'"))[0][0]
    assert (dst, src) == (0, 1), "merge move committed a partial copy despite the delete fault"


@pytest.mark.asyncio
async def test_merge_move_deletes_source_in_same_call(clean_db):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('src2', 'row to move', '{}', 't')"
    )
    await db.commit()

    res = await admin_handlers.do_merge_memories("src2", "dst2", mode="move")
    assert res["ok"] is True and res["merged_memories"] == 1
    assert res["source_deleted"]["deleted_memories"] == 1

    dst = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='dst2'"))[0][0]
    src = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='src2'"))[0][0]
    assert (dst, src) == (1, 0)


# ---------------------------------------------------------------------------
# bug-093: wiping an agent also clears its crash-recovery queue rows, so the
# drain cannot resurrect data for a deleted agent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_agent_data_purges_pending_tasks(clean_db):
    db = clean_db
    await db.execute(
        "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) "
        "VALUES ('archive_episode', 'wipe-me', '[]')"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('wipe-me', 'row', '{}', 't')"
    )
    await db.commit()

    res = await admin_handlers.do_delete_agent_data("wipe-me")
    assert res["ok"] is True
    assert res["deleted_pending_tasks"] == 1
    left = (
        await db.execute_fetchall(
            "SELECT COUNT(*) FROM pending_memory_tasks WHERE agent_id='wipe-me'"
        )
    )[0][0]
    assert left == 0


# ---------------------------------------------------------------------------
# bug-090: a handler-returned failure dict is a failure — the drain must retry
# (and eventually discard), never delete-and-log-completed on the first pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_treats_failure_dict_as_failure(clean_db, monkeypatch):
    monkeypatch.setattr(tasks, "TASK_RETRY_DELAY", 0)
    calls = {"n": 0}

    async def failing_update_profile(agent_id, payload):
        calls["n"] += 1
        return {"ok": False, "error": "synthetic failure"}

    monkeypatch.setattr(admin_handlers, "do_update_profile", failing_update_profile)

    queue = tasks.MemoryTaskQueue()
    await queue.enqueue("update_profile", "agent-fd", [{"content": "x"}])
    queue._running = True
    await queue._drain(admin_handlers, memory_handlers)
    queue._running = False

    # Retried up to the cap instead of being swallowed as a success on call 1.
    assert calls["n"] >= 2, "failure dict was treated as success (no retry)"
    left = (await clean_db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks"))[0][0]
    assert left == 0  # discarded after max retries — visibly, via the error path


# ---------------------------------------------------------------------------
# bug-089: the drain's episode INSERT and its task-row delete are one
# transaction; a legacy summary-less payload fails visibly (retry → discard)
# instead of logging "completed" while writing nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_archive_is_single_transaction(clean_db, monkeypatch):
    monkeypatch.setattr(tasks, "TASK_RETRY_DELAY", 0)
    queue = tasks.MemoryTaskQueue()
    await queue.enqueue(
        "archive_episode", "agent-at", [{"content": "hi", "timestamp": "t"}]
    )

    # Fault the insert: the task row must survive the failed attempt (same
    # transaction as the insert), then be discarded after max retries.
    async def failing_insert(db, row):
        raise RuntimeError("injected insert fault")

    monkeypatch.setattr(memory_handlers, "_insert_episode_row", failing_insert)
    queue._running = True
    await queue._drain(admin_handlers, memory_handlers)
    queue._running = False

    eps = (await clean_db.execute_fetchall("SELECT COUNT(*) FROM episodes"))[0][0]
    assert eps == 0, "a failed drain attempt leaked a committed episode"


@pytest.mark.asyncio
async def test_drain_archives_and_deletes_task_atomically(clean_db, monkeypatch):
    monkeypatch.setattr(tasks, "TASK_RETRY_DELAY", 0)

    async def prepared(agent_id, history, summary="", *a, **kw):
        return (agent_id, "", "drained summary", "", None, None, None, 0, "")

    monkeypatch.setattr(memory_handlers, "_prepare_episode_row", prepared)
    queue = tasks.MemoryTaskQueue()
    await queue.enqueue("archive_episode", "agent-ok", [{"content": "hi"}])
    queue._running = True
    await queue._drain(admin_handlers, memory_handlers)
    queue._running = False

    eps = (await clean_db.execute_fetchall("SELECT COUNT(*) FROM episodes WHERE agent_id='agent-ok'"))[0][0]
    left = (await clean_db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks"))[0][0]
    assert (eps, left) == (1, 0)


# ---------------------------------------------------------------------------
# bug-091: export is atomic (temp + os.replace) — a mid-export fault must leave
# the previous backup untouched; import rejects a truncated file against its
# own header instead of silently restoring a partial corpus.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_failure_preserves_previous_backup(clean_db, tmp_path, monkeypatch):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('exp', 'row one', '{}', 't')"
    )
    await db.commit()

    out = str(tmp_path / "backup.jsonl")
    first = await admin_handlers.do_export_memories("exp", out)
    assert first["ok"] and first["memories"] == 1
    previous_bytes = open(out, "rb").read()

    import os as _os

    def failing_replace(src, dst):
        raise OSError("injected replace fault")

    monkeypatch.setattr(admin_handlers.os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected replace fault"):
        await admin_handlers.do_export_memories("exp", out)
    monkeypatch.undo()

    assert open(out, "rb").read() == previous_bytes, "failed export clobbered the previous backup"
    assert not _os.path.exists(out + ".tmp"), "failed export left a temp file behind"


@pytest.mark.asyncio
async def test_import_rejects_truncated_file(clean_db, tmp_path):
    db = clean_db
    for i in range(3):
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            f"VALUES ('trunc', 'row {i}', '{{}}', 't')"
        )
    await db.commit()

    out = str(tmp_path / "full.jsonl")
    res = await admin_handlers.do_export_memories("trunc", out)
    assert res["ok"] and res["memories"] == 3

    lines = open(out, encoding="utf-8").read().splitlines()
    truncated = str(tmp_path / "cut.jsonl")
    open(truncated, "w", encoding="utf-8").write("\n".join(lines[:-1]) + "\n")

    await db.execute("DELETE FROM memories")
    await db.commit()

    ri = await admin_handlers.do_import_memories(truncated)
    assert ri["ok"] is False and "truncated" in ri["error"]
    left = (await db.execute_fetchall("SELECT COUNT(*) FROM memories"))[0][0]
    assert left == 0, "a truncated backup was partially restored"


# ---------------------------------------------------------------------------
# bug-092: recall stats + created_at survive an export -> import round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roundtrip_preserves_recall_stats_and_created_at(clean_db, tmp_path):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, recall_count, last_recalled_at, created_at) "
        "VALUES ('rt', 'sticky row', '{}', 't', 7, '2026-01-02T00:00:00+00:00', '2020-05-05 05:05:05')"
    )
    await db.commit()

    out = str(tmp_path / "rt.jsonl")
    assert (await admin_handlers.do_export_memories("rt", out))["ok"]
    await db.execute("DELETE FROM memories")
    await db.commit()

    ri = await admin_handlers.do_import_memories(out)
    assert ri["ok"] and ri["imported_memories"] == 1
    row = (
        await db.execute_fetchall(
            "SELECT recall_count, last_recalled_at, created_at FROM memories WHERE agent_id='rt'"
        )
    )[0]
    assert row[0] == 7
    assert row[1] == "2026-01-02T00:00:00+00:00"
    assert row[2] == "2020-05-05 05:05:05"


# ---------------------------------------------------------------------------
# bug-094: a JSON-array keywords value is coerced (not an InterfaceError abort),
# and dry_run previews the same counts as the real run for such a file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_keywords_array_is_coerced(clean_db, tmp_path):
    import json as _json

    db = clean_db
    path = str(tmp_path / "hand.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_json.dumps({"_type": "header", "memory_count": 0, "episode_count": 1}) + "\n")
        f.write(
            _json.dumps(
                {"_type": "episode", "agent_id": "kw", "summary": "hand-authored", "keywords": ["a", "b"]}
            )
            + "\n"
        )

    preview = await admin_handlers.do_import_memories(path, dry_run=True)
    assert preview["ok"] and preview["imported_episodes"] == 1

    real = await admin_handlers.do_import_memories(path)
    assert real["ok"] and real["imported_episodes"] == 1, real
    kw = (await db.execute_fetchall("SELECT keywords FROM episodes WHERE agent_id='kw'"))[0][0]
    assert kw == "a b"


# ---------------------------------------------------------------------------
# bug-095/096: sidecar persistence failure is surfaced, and a RAISED
# calibration failure rolls the un-persisted beta override back.
# ---------------------------------------------------------------------------


def test_sidecar_save_failure_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(
        admin_handlers, "_calibration_sidecar_path", lambda: str(tmp_path / "no-such-dir" / "cal.json")
    )
    ok = admin_handlers._save_calibration_state(64, "m", 0.5, {})
    assert ok is False


@pytest.mark.asyncio
async def test_set_recall_precision_rolls_back_beta_on_raise(clean_db, monkeypatch):
    from cpersona import vector

    vector._agent_betas.pop("beta-agent", None)

    async def raising_calibrate(agent_id=""):
        raise RuntimeError("calibrate blew up")

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", raising_calibrate)
    res = await admin_handlers.do_set_recall_precision("beta-agent", precision="strict")
    assert res["ok"] is False and "calibrate blew up" in res["error"]
    assert "beta-agent" not in vector._agent_betas, "raised calibration leaked the beta override"


# ---------------------------------------------------------------------------
# bug-098: fixers must never mutate locked rows — lock_memory's contract is
# "this row cannot be lost or altered by maintenance".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixers_do_not_mutate_locked_rows(clean_db):
    from cpersona import maintenance_handlers

    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
        "VALUES ('lk', 'locked bad row', 'not-json{', 'not-a-timestamp', 1)"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
        "VALUES ('lk', 'unlocked bad row', 'not-json{', 'not-a-timestamp', 0)"
    )
    await db.commit()

    await maintenance_handlers.do_check_health(agent_id="lk", fix=True)

    rows = await db.execute_fetchall(
        "SELECT locked, source, timestamp FROM memories WHERE agent_id='lk' ORDER BY locked"
    )
    unlocked, locked = rows[0], rows[1]
    assert locked[1] == "not-json{", "fixer rewrote a locked row's source"
    assert locked[2] == "not-a-timestamp", "fixer rewrote a locked row's timestamp"
    assert unlocked[1] == "{}", "fixer failed to repair the unlocked row"


# ---------------------------------------------------------------------------
# bug-097: content-rewriting fixers run before duplicate_content in the
# registry, so a single fix pass converges.
# ---------------------------------------------------------------------------


def test_health_checks_order_oversized_before_duplicate():
    from cpersona.checks import HEALTH_CHECKS

    names = [c.name for c in HEALTH_CHECKS]
    assert names.index("oversized_content") < names.index("duplicate_content")


# ---------------------------------------------------------------------------
# bug-099: an acknowledgement requires the UPDATE to have matched a row — a
# row deleted between the ownership pre-check and the UPDATE returns an error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_memory_vanished_row_is_an_error(clean_db, monkeypatch):
    import contextlib as _ctx

    @_ctx.asynccontextmanager
    async def fake_connection():
        class _Stub:
            async def execute_fetchall(self, *a, **kw):
                return [("ghost-agent",)]  # pre-check "finds" the row

        yield _Stub()

    monkeypatch.setattr(admin_handlers, "connection", fake_connection)
    res = await admin_handlers.do_lock_memory(999_999_999, agent_id="ghost-agent")
    assert "error" in res, "lock acknowledged a row the UPDATE never matched"


# ---------------------------------------------------------------------------
# bug-104: no-persist skipped responses must not echo caller ids in the
# action-specific *_id keys.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_persist_skip_nulls_action_id_keys(clean_db):
    from cpersona._vendored_mcp_common import no_persist

    no_persist.pause(ttl_seconds=60)
    try:
        res = await admin_handlers.do_delete_memory(12345)
        assert res["persisted"] is False
        assert res.get("deleted_id") is None, "skipped delete echoed a truthy deleted_id"
    finally:
        no_persist.resume()


# ---------------------------------------------------------------------------
# bug-105: a report-only boot (checkup without --fix) performs no schema writes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_boot_migrations_writes_nothing(tmp_path, monkeypatch):
    import sqlite3

    saved_db = database._db
    database._db = None
    await _reset_read_seam()
    fresh = str(tmp_path / "ro-report.db")
    sqlite3.connect(fresh).close()  # an empty, schema-less database file
    monkeypatch.setattr(database, "DB_PATH", fresh)
    monkeypatch.setattr(database, "SKIP_BOOT_MIGRATIONS", True)
    try:
        db = await database.get_db()
        tables = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        assert tables == [], f"report-only boot created schema objects: {tables}"
    finally:
        if database._db is not None:
            await database._db.close()
        await _reset_read_seam()
        database._db = saved_db


# ---------------------------------------------------------------------------
# bug-106: write dedup checks the γ-visible scope, matching read semantics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_dedup_is_gamma_visible(clean_db):
    msg = {"content": "gamma dedup sentinel", "source": {}, "timestamp": "t"}

    first = await memory_handlers.do_store("gd", dict(msg))
    assert first.get("ok") and not first.get("skipped")

    # A bucket write collides with the identical global-pool row: recall('X')
    # would have surfaced both copies.
    bucket = await memory_handlers.do_store("gd", dict(msg), project_id="X")
    assert bucket.get("skipped") is True, bucket

    # Sibling buckets stay legitimately distinct (bug-044 precedent)…
    y = await memory_handlers.do_store("gd", {**msg, "content": "y-only row"}, project_id="Y")
    assert y.get("ok") and not y.get("skipped")
    x = await memory_handlers.do_store("gd", {**msg, "content": "y-only row"}, project_id="X")
    assert x.get("ok") and not x.get("skipped"), "sibling-bucket write was wrongly deduped"

    # …and a global write does not dedup against a bucket-only copy (that would
    # hide the row from every other bucket).
    g = await memory_handlers.do_store("gd", {**msg, "content": "y-only row"})
    assert g.get("ok") and not g.get("skipped"), "global write was deduped against a bucket copy"


@pytest.mark.asyncio
async def test_store_dedup_gamma_channel_axis(clean_db):
    msg = {"content": "channel dedup sentinel", "source": {}, "timestamp": "t"}
    first = await memory_handlers.do_store("gc", dict(msg))  # shared channel ''
    assert first.get("ok") and not first.get("skipped")
    scoped = await memory_handlers.do_store("gc", dict(msg), channel="discord")
    assert scoped.get("skipped") is True, "channel write missed the identical shared-channel row"


# ---------------------------------------------------------------------------
# bug-107: the confidence temporal span is computed over the recall's own
# isolation scope, not the whole agent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temporal_span_is_scoped_to_recall_axes(clean_db, monkeypatch):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, channel) "
        "VALUES ('ts', 'ancient', '{}', '2020-01-01T00:00:00+00:00', 'a')"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, channel) "
        "VALUES ('ts', 'recent one', '{}', '2026-01-01T00:00:00+00:00', 'b')"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, channel) "
        "VALUES ('ts', 'recent two', '{}', '2026-01-02T00:00:00+00:00', 'b')"
    )
    await db.commit()
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)

    row = {"id": 1, "content": "recent one", "source": "{}", "timestamp": "2026-01-01T00:00:00+00:00"}
    _, scoped_hours, _ = await memory_handlers._apply_recall_scoring(
        db, "ts", [dict(row)], False, channel="b"
    )
    _, agentwide_hours, _ = await memory_handlers._apply_recall_scoring(
        db, "ts", [dict(row)], False
    )
    assert scoped_hours < agentwide_hours, (
        f"scoped span {scoped_hours}h not tighter than agent-wide {agentwide_hours}h"
    )
    assert scoped_hours <= 25  # the 'b'∪shared window spans one day


# ---------------------------------------------------------------------------
# bug-108: the read seam must not self-deadlock when first-touch races a
# close_db (its lock is separate from get_db's init lock).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_seam_survives_closed_db_race(clean_db):
    # Simulate the race: the write connection vanishes right before the read
    # seam enters its locked section — the old shared-lock design deadlocked
    # here (asyncio.Lock is not reentrant).
    await _reset_read_seam()
    saved = database._db
    database._db = None  # as if close_db() won the race; get_db must re-init
    try:
        async with connection() as r:
            rows = await r.execute_fetchall("SELECT 1")
        assert rows == [(1,)]
    finally:
        if database._db is not None and database._db is not saved:
            await database._db.close()
        await _reset_read_seam()
        database._db = saved


# ---------------------------------------------------------------------------
# bug-109: an agent wipe during the drain's unlocked prepare window must veto
# the episode insert (the task-row delete is the claim token).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_does_not_resurrect_wiped_agent(clean_db, monkeypatch):
    monkeypatch.setattr(tasks, "TASK_RETRY_DELAY", 0)
    queue = tasks.MemoryTaskQueue()
    await queue.enqueue("archive_episode", "wiped-agent", [{"content": "hi"}])

    async def prepare_and_wipe(agent_id, history, summary="", *a, **kw):
        # The wipe lands while the drain is inside its unlocked prepare phase.
        await admin_handlers.do_delete_agent_data(agent_id)
        return (agent_id, "", "resurrected?", "", None, None, None, 0, "")

    monkeypatch.setattr(memory_handlers, "_prepare_episode_row", prepare_and_wipe)
    queue._running = True
    await queue._drain(admin_handlers, memory_handlers)
    queue._running = False

    eps = (
        await clean_db.execute_fetchall(
            "SELECT COUNT(*) FROM episodes WHERE agent_id='wiped-agent'"
        )
    )[0][0]
    assert eps == 0, "drain resurrected an episode for a wiped agent"


# ---------------------------------------------------------------------------
# bug-110: the truncation guard also covers the profile tail; abort responses
# keep per-line diagnostics; no-persist delete shape matches the real one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_rejects_file_cut_at_profile_boundary(clean_db, tmp_path):
    from cpersona.database import transaction

    db = clean_db
    async with transaction() as tdb:
        await tdb.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            "VALUES ('pb', 'a memory', '{}', 't')"
        )
        await tdb.execute(
            "INSERT INTO profiles (agent_id, user_id, content) VALUES ('pb', '', 'the profile')"
        )
    out = str(tmp_path / "with-profile.jsonl")
    res = await admin_handlers.do_export_memories("pb", out)
    assert res["ok"] and res["profiles"] == 1

    lines = open(out, encoding="utf-8").read().splitlines()
    cut = str(tmp_path / "profile-cut.jsonl")
    open(cut, "w", encoding="utf-8").write("\n".join(lines[:-1]) + "\n")  # drop the profile line

    async with transaction() as tdb:
        await tdb.execute("DELETE FROM memories WHERE agent_id='pb'")
        await tdb.execute("DELETE FROM profiles WHERE agent_id='pb'")
    ri = await admin_handlers.do_import_memories(cut)
    assert ri["ok"] is False and "truncated" in ri["error"], ri
    left = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='pb'"))[0][0]
    assert left == 0


@pytest.mark.asyncio
async def test_no_persist_delete_shape_matches_real_response(clean_db):
    from cpersona._vendored_mcp_common import no_persist

    real = await admin_handlers.do_delete_agent_data("shape-agent")
    no_persist.pause(ttl_seconds=60)
    try:
        skipped = await admin_handlers.do_delete_agent_data("shape-agent")
    finally:
        no_persist.resume()
    missing = {k for k in real if k not in skipped}
    assert not missing, f"no-persist response lost keys: {missing}"


# ---------------------------------------------------------------------------
# bug-112: a report-only boot refuses to create a missing database.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_boot_migrations_refuses_missing_db(tmp_path, monkeypatch):
    saved_db = database._db
    database._db = None
    await _reset_read_seam()
    missing = str(tmp_path / "nope" / "absent.db")
    monkeypatch.setattr(database, "DB_PATH", missing)
    monkeypatch.setattr(database, "SKIP_BOOT_MIGRATIONS", True)
    try:
        with pytest.raises(FileNotFoundError):
            await database.get_db()
        assert not os.path.exists(missing), "report-only boot created a database file"
    finally:
        if database._db is not None:
            await database._db.close()
        await _reset_read_seam()
        database._db = saved_db


# ---------------------------------------------------------------------------
# bug-113: truncating an oversized row into a dedup-index collision deletes the
# duplicate instead of crashing the check on every fix run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_truncation_collision_converges(clean_db):
    from cpersona import maintenance_handlers
    from cpersona.config import MAX_CONTENT_LENGTH
    from cpersona.database import transaction

    db = clean_db
    base = "x" * MAX_CONTENT_LENGTH
    async with transaction() as tdb:
        await tdb.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) VALUES ('ov', ?, '{}', 't')",
            (base,),
        )
        await tdb.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) VALUES ('ov', ?, '{}', 't')",
            (base + "tail beyond the cap",),
        )
    res = await maintenance_handlers.do_check_health(agent_id="ov", fix=True)
    crashed = [i for i in res["issues"] if i.get("check") == "oversized_content" and i.get("type") == "check_crashed"]
    assert not crashed, f"oversized fixer still crashes on collision: {crashed}"
    rows = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id='ov'"))[0][0]
    assert rows == 1, f"collision left {rows} rows (expected the survivor only)"
