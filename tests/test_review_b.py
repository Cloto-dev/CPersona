"""Regression tests for the import/merge integrity review batch (bug-218..bug-246).

One test per fixed defect, each written to fail against the pre-fix code:

    bug-218  a move no longer wipes a skipped source profile
    bug-219  a merged episode keeps its created_at
    bug-220  re-importing the same file adds no episodes
    bug-221  imported content goes through the write path's sanitiser
    bug-222  a move no longer deletes a skipped LOCKED source memory
    bug-223  an imported profile keeps its updated_at
    bug-231  calibrate_threshold refuses an unknown method
    bug-235  read_snapshot() works under DB_PATH=':memory:'
    bug-241  the import size cap requires a regular file
    bug-242  a non-UTF-8 import file answers with the error_response shape
    bug-246  calibration sidecar backups do not accumulate per boot
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config, database
from cpersona.database import get_db, read_snapshot


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


def _write_jsonl(path, records) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# bug-218 / bug-222: mode='move' deletes what it copied, and only that.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug218_move_leaves_the_skipped_source_profile_where_it_is(clean_db):
    """A profile the target already holds is SKIPPED, and a skip on a key with no
    content-equivalence means the source document exists nowhere else."""
    db = clean_db
    await db.execute("INSERT INTO profiles (agent_id, content) VALUES ('src', 'profile of A')")
    await db.execute("INSERT INTO profiles (agent_id, content) VALUES ('dst', 'profile of B')")
    await db.commit()

    result = await admin_handlers.do_merge_memories("src", "dst", mode="move")
    assert result["ok"] and result["skipped_profile"] is True
    assert result["profile_copied"] is False
    assert result["source_deleted"]["deleted_profiles"] == 0
    assert result["left_at_source"]["profiles"] == 1

    rows = await db.execute_fetchall("SELECT agent_id, content FROM profiles ORDER BY agent_id")
    assert rows == [("dst", "profile of B"), ("src", "profile of A")], (
        "the move deleted a profile it never copied"
    )


@pytest.mark.asyncio
async def test_bug222_move_keeps_the_skipped_locked_source_memory(clean_db):
    """A locked memory whose content collides with an (unlocked) target row is skipped
    by the copy, so the move must not delete it — that is the lock being dropped."""
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked)"
        " VALUES ('src', 'shared content', '{}', 't', 1)"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked)"
        " VALUES ('dst', 'shared content', '{}', 't', 0)"
    )
    await db.commit()

    result = await admin_handlers.do_merge_memories("src", "dst", mode="move")
    assert result["ok"] and result["merged_memories"] == 0 and result["skipped_memories"] == 1
    assert result["source_deleted"]["deleted_memories"] == 0
    assert result["left_at_source"]["memories"] == 1

    rows = await db.execute_fetchall(
        "SELECT agent_id, locked FROM memories WHERE content = 'shared content' ORDER BY agent_id"
    )
    assert rows == [("dst", 0), ("src", 1)], "the locked source memory was deleted uncopied"


@pytest.mark.asyncio
async def test_bug218_a_clean_move_still_empties_the_source(clean_db):
    """Nothing skipped: the move is the wipe it always was, queue rows included."""
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp)"
        " VALUES ('src', 'only in source', '{}', 't')"
    )
    await db.execute("INSERT INTO episodes (agent_id, summary) VALUES ('src', 'source episode')")
    await db.execute("INSERT INTO profiles (agent_id, content) VALUES ('src', 'source profile')")
    await db.execute(
        "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES ('store', 'src', '{}')"
    )
    await db.commit()

    result = await admin_handlers.do_merge_memories("src", "dst", mode="move")
    assert result["ok"]
    assert result["source_deleted"] == {
        "ok": True,
        "agent_id": "src",
        "deleted_memories": 1,
        "deleted_episodes": 1,
        "deleted_profiles": 1,
        "deleted_pending_tasks": 1,
    }
    assert result["left_at_source"] == {"memories": 0, "episodes": 0, "profiles": 0}

    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM {table} WHERE agent_id = 'src'"  # noqa: S608 - literal table names
        )
        assert rows[0][0] == 0, f"{table} still holds source rows after a clean move"


# ---------------------------------------------------------------------------
# bug-219: a merged episode keeps its created_at.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug219_merge_preserves_episode_created_at(clean_db):
    db = clean_db
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, created_at)"
        " VALUES ('src', 'old session summary', '2020-01-01 00:30:00')"
    )
    await db.commit()

    result = await admin_handlers.do_merge_memories("src", "dst")
    assert result["ok"] and result["merged_episodes"] == 1

    rows = await db.execute_fetchall(
        "SELECT created_at FROM episodes WHERE agent_id = 'dst' AND summary = 'old session summary'"
    )
    assert rows[0][0] == "2020-01-01 00:30:00", (
        "the merged episode was re-stamped with merge time, moving the episode boundary"
    )


# ---------------------------------------------------------------------------
# bug-220 / bug-221 / bug-223: the import path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug220_reimporting_the_same_file_adds_no_episodes(clean_db, tmp_path):
    """idempotentHint=True is a promise a host acts on by retrying."""
    db = clean_db
    path = _write_jsonl(
        tmp_path / "backup.jsonl",
        [
            {"_type": "memory", "agent_id": "imp", "content": "a memory", "msg_id": "m1"},
            {"_type": "episode", "agent_id": "imp", "summary": "a session"},
        ],
    )

    first = await admin_handlers.do_import_memories(path)
    assert first["ok"] and first["imported_episodes"] == 1 and first["skipped_episodes"] == 0

    second = await admin_handlers.do_import_memories(path)
    assert second["ok"] and second["imported_episodes"] == 0
    assert second["skipped_episodes"] == 1, "the re-imported episode was not reported as skipped"

    rows = await db.execute_fetchall("SELECT COUNT(*) FROM episodes WHERE agent_id = 'imp'")
    assert rows[0][0] == 1, "the second import duplicated the episode"


@pytest.mark.asyncio
async def test_bug220_a_preview_dedups_episodes_within_the_file(clean_db, tmp_path):
    """The dry_run arm has no INSERT of its own to collide against (bug-070/071 class),
    so its counts must come from the within-file set."""
    path = _write_jsonl(
        tmp_path / "dupes.jsonl",
        [
            {"_type": "episode", "agent_id": "imp", "summary": "repeated session"},
            {"_type": "episode", "agent_id": "imp", "summary": "repeated session"},
        ],
    )
    preview = await admin_handlers.do_import_memories(path, dry_run=True)
    assert preview["ok"] and preview["imported_episodes"] == 1
    assert preview["skipped_episodes"] == 1

    real = await admin_handlers.do_import_memories(path)
    assert (real["imported_episodes"], real["skipped_episodes"]) == (1, 1), (
        "the preview and the real run disagree on the same file"
    )


@pytest.mark.asyncio
async def test_bug221_import_applies_the_write_paths_content_policy(clean_db, tmp_path):
    """The cap, the empty-after-sanitisation refusal and the annotation stripper are
    the same three the store path applies — an import file is not more trusted."""
    db = clean_db
    oversized = "X" * (config.MAX_CONTENT_LENGTH + 5000)
    path = _write_jsonl(
        tmp_path / "raw.jsonl",
        [
            {"_type": "memory", "agent_id": "imp", "content": oversized, "msg_id": "big"},
            {"_type": "memory", "agent_id": "imp", "content": "   ", "msg_id": "ws"},
            {"_type": "memory", "agent_id": "imp", "content": "[Memory from foo] real body"},
            {"_type": "episode", "agent_id": "imp", "summary": "  "},
        ],
    )

    result = await admin_handlers.do_import_memories(path)
    assert result["ok"]
    assert result["imported_memories"] == 2 and result["skipped_memories"] == 1
    assert result["imported_episodes"] == 0 and result["skipped_episodes"] == 1
    assert any("truncated" in e for e in result["errors"]), result
    assert any("empty after sanitisation" in e for e in result["errors"]), result

    rows = await db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = 'imp' ORDER BY LENGTH(content)"
    )
    assert [row[0] for row in rows] == ["real body", "X" * config.MAX_CONTENT_LENGTH], (
        "imported content bypassed the cap and/or the annotation stripper"
    )
    ws_rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = 'imp' AND TRIM(content) = ''"
    )
    assert ws_rows[0][0] == 0, "a whitespace-only record was stored as a live memory"


@pytest.mark.asyncio
async def test_bug223_import_preserves_the_profile_updated_at(clean_db, tmp_path):
    """profiles.updated_at is the sole input to deep_stale_profile: a restore that
    re-stamps it disarms the staleness detector it is meant to survive."""
    db = clean_db
    path = _write_jsonl(
        tmp_path / "profile.jsonl",
        [
            {
                "_type": "profile",
                "agent_id": "imp",
                "user_id": "",
                "content": "my profile",
                "updated_at": "2019-05-05 05:05:05",
            }
        ],
    )
    assert (await admin_handlers.do_import_memories(path))["ok"]

    rows = await db.execute_fetchall("SELECT updated_at FROM profiles WHERE agent_id = 'imp'")
    assert rows[0][0] == "2019-05-05 05:05:05", "the restored profile was re-stamped"


@pytest.mark.asyncio
async def test_bug223_a_profile_record_without_updated_at_still_gets_one(clean_db, tmp_path):
    """COALESCE, not a bare bind: an older export carries no updated_at at all."""
    db = clean_db
    path = _write_jsonl(
        tmp_path / "legacy.jsonl",
        [{"_type": "profile", "agent_id": "imp", "user_id": "", "content": "legacy profile"}],
    )
    assert (await admin_handlers.do_import_memories(path))["ok"]

    rows = await db.execute_fetchall("SELECT updated_at FROM profiles WHERE agent_id = 'imp'")
    assert rows[0][0], "the profile landed with a NULL updated_at"


# ---------------------------------------------------------------------------
# bug-241 / bug-242: what the import path accepts to read.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug241_import_refuses_a_non_regular_file(clean_db, tmp_path):
    """os.path.getsize reports 0 for a FIFO or a character device, so the cap passed
    exactly the inputs whose read is unbounded."""
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)

    result = await admin_handlers.do_import_memories(str(fifo))
    assert result["ok"] is False
    assert "not a regular file" in result["error"], result


@pytest.mark.asyncio
async def test_bug241_the_cap_reads_the_stat_it_took(clean_db, tmp_path, monkeypatch):
    body = '{"_type":"memory","agent_id":"imp","content":"row"}\n'
    path = tmp_path / "big.jsonl"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(config, "MAX_IMPORT_BYTES", len(body) - 1)

    result = await admin_handlers.do_import_memories(str(path))
    assert result["ok"] is False and "MAX_IMPORT_BYTES" in result["error"]


@pytest.mark.asyncio
async def test_bug242_a_non_utf8_file_answers_the_documented_failure_shape(clean_db, tmp_path):
    """UnicodeDecodeError derives from ValueError, so it escaped the OSError handler and
    the registry wrapper answered a bare {"error": ...} with no `ok`."""
    path = tmp_path / "latin1.jsonl"
    path.write_bytes(b'{"_type":"memory","agent_id":"imp","content":"caf\x93"}\n')

    result = await admin_handlers.do_import_memories(str(path))
    assert result["ok"] is False
    assert "could not read" in result["error"], result


# ---------------------------------------------------------------------------
# bug-231: calibrate_threshold refuses an unknown method.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug231_an_unknown_calibration_method_is_refused(clean_db):
    """The percentile fall-through returned a threshold computed by a method the
    caller did not ask for, echoed their spelling back, and persisted it."""
    result = await admin_handlers.do_calibrate_threshold("cal", method="z-score", z_factor=2.0)
    assert result["ok"] is False
    assert "z-score" in result["error"]
    for name in ("separation", "percentile", "zscore"):
        assert name in result["error"], result


def test_bug231_the_schema_advertises_the_same_enum_the_handler_validates():
    from cpersona import server

    tool = next(t for t in server.registry._tools if t.name == "calibrate_threshold")
    assert tool.inputSchema["properties"]["method"]["enum"] == list(config.CALIBRATE_METHODS)


@pytest.mark.asyncio
async def test_bug231_every_advertised_method_is_accepted(clean_db, monkeypatch):
    """The refusal must not shut out a valid spelling: each advertised name reaches the
    sampler (which then declines for want of embeddings, not for want of a method)."""
    for name in config.CALIBRATE_METHODS:
        result = await admin_handlers.do_calibrate_threshold("cal", method=name)
        assert "Invalid method" not in (result.get("error") or ""), (name, result)


# ---------------------------------------------------------------------------
# bug-235: read_snapshot() under an in-memory database.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug235_read_snapshot_shares_the_connection_for_an_in_memory_db(
    clean_db, monkeypatch
):
    """A second aiosqlite.connect(':memory:') opens a DIFFERENT, empty database, so the
    streaming export read a schema-less file (_get_read_db special-cases this already)."""
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp)"
        " VALUES ('snap', 'in-memory row', '{}', 't')"
    )
    await db.commit()

    monkeypatch.setattr(database, "DB_PATH", ":memory:")
    async with read_snapshot() as snap:
        assert snap is db
        rows = await snap.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = 'snap'"
        )
    assert rows[0][0] == 1

    # The shared connection survives the scope: closing it would take the process's
    # only database with it.
    assert (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'snap'"))[0][
        0
    ] == 1


# ---------------------------------------------------------------------------
# bug-246: calibration sidecar backups are bounded.
# ---------------------------------------------------------------------------


class _TickingClock:
    """Stands in for ``admin_handlers.datetime``: every read is one boot later."""

    def __init__(self):
        self.reads = 0

    def now(self, tz=None):
        self.reads += 1
        return datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc) + timedelta(
            hours=self.reads
        )


@pytest.mark.asyncio
async def test_bug246_a_second_boot_on_the_same_version_writes_no_second_backup(
    tmp_path, monkeypatch
):
    """The staleness flag stays set when calibration cannot succeed, so this path runs
    on EVERY boot; without the guard each one left another byte-identical copy."""
    sidecar = tmp_path / "cpersona-calibration.json"
    sidecar.write_text(json.dumps({"scoring_version": "254a3"}), encoding="utf-8")
    monkeypatch.setattr(admin_handlers, "_calibration_sidecar_path", lambda: str(sidecar))
    # Successive boots, not successive seconds: the backup name carries a
    # one-second-resolution stamp, so two calls inside the same second would
    # overwrite one file and the count would look bounded whatever the code did.
    monkeypatch.setattr(admin_handlers, "datetime", _TickingClock())

    first = admin_handlers._backup_calibration_sidecar("254a3")
    assert first and os.path.exists(first)
    second = admin_handlers._backup_calibration_sidecar("254a3")
    assert second == first, "a second boot on the same scoring version wrote another copy"

    backups = [p for p in os.listdir(tmp_path) if ".before-" in p]
    assert len(backups) == 1, backups

    # A version that really did move still earns its own evidence file.
    other = admin_handlers._backup_calibration_sidecar("255a1")
    assert other and other != first
    assert len([p for p in os.listdir(tmp_path) if ".before-" in p]) == 2


def test_bug246_backups_are_pruned_to_the_five_most_recent(tmp_path, monkeypatch):
    sidecar = tmp_path / "cpersona-calibration.json"
    sidecar.write_text(json.dumps({"scoring_version": "v9"}), encoding="utf-8")
    monkeypatch.setattr(admin_handlers, "_calibration_sidecar_path", lambda: str(sidecar))

    now = time.time()
    for i in range(8):
        stale = tmp_path / f"cpersona-calibration.json.before-v{i}-2026010{i}-000000"
        stale.write_text("{}", encoding="utf-8")
        os.utime(stale, (now - (100 - i), now - (100 - i)))

    kept_new = admin_handlers._backup_calibration_sidecar("v9")
    backups = sorted(p for p in os.listdir(tmp_path) if ".before-" in p)
    assert len(backups) == admin_handlers._CALIBRATION_BACKUP_KEEP, backups
    assert os.path.basename(kept_new) in backups, "the sweep deleted the copy it just wrote"
    # The five newest by mtime are v4..v7 plus the one just written; v0..v3 are gone.
    assert not any(f"before-v{i}-" in p for i in range(4) for p in backups), backups
