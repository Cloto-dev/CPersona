"""Import and migration paths: behaviour that was correct but that no test held.

    - A profile restored over an existing row keeps the file's updated_at on the
      ON CONFLICT arm too, not only on a fresh INSERT. updated_at is the sole input to
      the stale-profile check, so a restamp to the import time disarms it.
    - An export cut inside the episode section is refused and rolled back even when
      its memory and profile counts still match the header.
    - The import reads at most one byte past the size cap, however large the file is
      by the time it is opened. The size check runs on a stat taken earlier, and a
      file that grows in between must not be read into memory whole.
    - migrate_channel_axis, forced into a dry run by the caller's no-persist pause,
      says so with repairs_skipped, and says nothing of the kind to a caller that is
      not paused or that asked for the dry run itself.
"""

import json
from pathlib import Path

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config, maintenance_handlers, server, session
from cpersona.database import get_db

TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks")
A = "sess-y06-A"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in TABLES:
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    session.reset_pauses_for_tests()


def _write_jsonl(path: Path, records) -> str:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# A profile restored over an existing row keeps the file's updated_at.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_restored_over_an_existing_row_keeps_the_files_updated_at(db, tmp_path):
    # The stored row is older than the file's, so the restore applies it and the
    # write goes through the ON CONFLICT arm rather than the INSERT arm.
    await db.execute(
        "INSERT INTO profiles (agent_id, user_id, content, updated_at) "
        "VALUES ('y06', '', 'stored profile', '2019-01-01 00:00:00')"
    )
    await db.commit()
    path = _write_jsonl(
        tmp_path / "profile.jsonl",
        [
            {
                "_type": "profile",
                "agent_id": "y06",
                "user_id": "",
                "content": "restored profile",
                "updated_at": "2019-05-05 05:05:05",
            }
        ],
    )

    result = await admin_handlers.do_import_memories(path)
    assert result["ok"] is True and result["profile_updated"] is True, result

    rows = await db.execute_fetchall(
        "SELECT content, updated_at FROM profiles WHERE agent_id = 'y06' AND user_id = ''"
    )
    assert len(rows) == 1, rows
    assert rows[0][0] == "restored profile", "the conflict arm did not apply the file's profile"
    assert rows[0][1] == "2019-05-05 05:05:05", (
        "a profile restored over an existing row was re-stamped instead of keeping the file's updated_at"
    )


# ---------------------------------------------------------------------------
# An export cut inside the episode section is refused and rolled back.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_cut_inside_the_episode_section_is_refused_and_rolled_back(db, tmp_path):
    # An agent with no profile: profile_count is 0, so a cut after the memories
    # leaves both the memory and the profile counts matching the header.
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES ('y06ep', 'a memory', '{}', 't')"
    )
    for i in range(2):
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, keywords, created_at) VALUES (?, ?, '', ?)",
            ("y06ep", f"episode {i}", f"2020-01-0{i + 1} 00:00:00"),
        )
    await db.commit()

    full = tmp_path / "full.jsonl"
    exported = await admin_handlers.do_export_memories("y06ep", str(full))
    assert exported["ok"], exported

    lines = full.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    header = records[0]
    assert header["_type"] == "header"
    assert (header["memory_count"], header["episode_count"], header["profile_count"]) == (1, 2, 0), header
    kinds = [r["_type"] for r in records[1:]]
    assert kinds == ["memory", "episode", "episode"], kinds

    # Drop the last episode line: the file now ends inside the episode section.
    cut = tmp_path / "cut.jsonl"
    cut.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    for table in ("memories", "episodes"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()

    result = await admin_handlers.do_import_memories(str(cut))
    assert result["ok"] is False, f"a file cut inside the episode section was accepted: {result}"
    assert "truncated" in result["error"] and "2 episodes" in result["error"], result
    assert result["imported_memories"] == 0 and result["imported_episodes"] == 0, result

    left_mem = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'y06ep'"))[0][0]
    left_ep = (await db.execute_fetchall("SELECT COUNT(*) FROM episodes WHERE agent_id = 'y06ep'"))[0][0]
    assert (left_mem, left_ep) == (0, 0), "a backup cut inside the episode section was partially restored"


# ---------------------------------------------------------------------------
# The import read is bounded by the cap, not by the file.
# ---------------------------------------------------------------------------


class _CountingReader:
    """A binary file whose reads are counted, so the test sees what was pulled into memory."""

    def __init__(self, fh, counter: list[int]):
        self._fh = fh
        self._counter = counter

    def read(self, *args):
        data = self._fh.read(*args)
        self._counter[0] += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, name):
        return getattr(self._fh, name)


def _grow_on_open(monkeypatch, target: Path, extra: int) -> list[int]:
    """Make ``target`` grow by ``extra`` bytes between the size check and the open.

    The handler's stat has already passed when it opens the file, so appending at
    open time is exactly the race the bounded read exists for. Shadows ``open`` in
    the handler module only; every other module keeps the builtin.
    """
    import builtins

    real_open = builtins.open
    counter = [0]

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == str(target) and mode == "rb":
            with real_open(target, "ab") as grow:
                grow.write(b"x" * extra)
            return _CountingReader(real_open(path, mode, *args, **kwargs), counter)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(admin_handlers, "open", fake_open, raising=False)
    return counter


@pytest.mark.asyncio
async def test_import_reads_no_more_than_one_byte_past_the_cap_of_a_file_that_grew(db, tmp_path, monkeypatch):
    body = '{"_type":"memory","agent_id":"y06","content":"row"}\n'
    path = tmp_path / "grows.jsonl"
    path.write_text(body, encoding="utf-8")
    cap = len(body) + 8
    monkeypatch.setattr(config, "MAX_IMPORT_BYTES", cap)
    counter = _grow_on_open(monkeypatch, path, extra=64 * 1024)

    result = await admin_handlers.do_import_memories(str(path))

    assert result["ok"] is False and "MAX_IMPORT_BYTES" in result["error"], result
    assert counter[0] > 0, "the handler did not read through the module's open"
    assert counter[0] <= cap + 1, (
        f"the import read {counter[0]} bytes of a file that outgrew the {cap}-byte cap; "
        f"one byte past the cap ({cap + 1}) is all it needs to refuse it"
    )
    rows = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'y06'"))[0][0]
    assert rows == 0


def test_import_agent_scan_reads_no_more_than_one_byte_past_the_cap(tmp_path, monkeypatch):
    # The ACL guard's scan of the same file reads it the way the import does, and is
    # bounded the same way.
    body = '{"_type":"memory","agent_id":"y06","content":"row"}\n'
    path = tmp_path / "grows.jsonl"
    path.write_text(body, encoding="utf-8")
    cap = len(body) + 8
    monkeypatch.setattr(config, "MAX_IMPORT_BYTES", cap)
    counter = _grow_on_open(monkeypatch, path, extra=64 * 1024)

    assert admin_handlers.import_file_agents(str(path)) == set()
    assert counter[0] > 0, "the scan did not read through the module's open"
    assert counter[0] <= cap + 1, (
        f"the agent scan read {counter[0]} bytes of a file that outgrew the {cap}-byte cap"
    )


# ---------------------------------------------------------------------------
# migrate_channel_axis discloses the no-persist downgrade.
# ---------------------------------------------------------------------------


async def _seed_recoverable(conn):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, metadata, channel) "
        "VALUES ('y06mig', 'bridge row', '{}', 't', ?, 'discord')",
        (json.dumps({"session_id": "111222333444555666:shared"}),),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_migrate_forced_to_dry_run_by_no_persist_reports_repairs_skipped(db):
    await _seed_recoverable(db)
    await server.do_pause_persistence(ttl_seconds=120, session_key=A)

    out = await maintenance_handlers.do_migrate_channel_axis(agent_id="y06mig", dry_run=False, session_key=A)

    assert out["dry_run"] is True, out
    assert out["recoverable_total"] == 1 and out["migrated"] == 0, out
    assert out.get("repairs_skipped") is True, (
        "a migration the pause forced into a dry run reads as one the caller asked for"
    )
    assert "no-persist" in out.get("repairs_skip_reason", ""), out
    channel = (await db.execute_fetchall("SELECT channel FROM memories WHERE agent_id = 'y06mig'"))[0][0]
    assert channel == "discord", "the paused migration moved a row"


@pytest.mark.asyncio
async def test_migrate_carries_no_repairs_skipped_when_nothing_was_downgraded(db):
    await _seed_recoverable(db)
    await server.do_pause_persistence(ttl_seconds=120, session_key=A)

    # Paused, but the caller asked for the dry run itself: nothing was skipped.
    asked = await maintenance_handlers.do_migrate_channel_axis(agent_id="y06mig", dry_run=True, session_key=A)
    assert asked["dry_run"] is True
    assert "repairs_skipped" not in asked and "repairs_skip_reason" not in asked, asked

    # Not paused: the real run happens and nothing is reported as skipped.
    session.reset_pauses_for_tests()
    real = await maintenance_handlers.do_migrate_channel_axis(agent_id="y06mig", dry_run=False, session_key=A)
    assert real["dry_run"] is False and real["migrated"] == 1, real
    assert "repairs_skipped" not in real and "repairs_skip_reason" not in real, real
