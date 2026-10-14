"""Regression tests for three health-repair defects in check_health / deep_check.

  bug-546  invalid_timestamp decided validity with SQLite's datetime() alone, so a
           stamp every Python reader parses ('+0900', '+09', ISO basic format) was
           counted invalid and fix=true replaced the caller's event time with the
           row's insertion time.
  bug-568  deep_check(fix=true) turned a check's exception into an error entry inside
           one transaction, so the statements that ran before the fault were
           committed: a partly applied repair under an error-only response.
  bug-571  store and import serialised NaN / Infinity into source and metadata as
           bare tokens that SQLite's json_valid rejects, and the invalid_json repair
           then blanked the whole column, every other key with it.

Each test states the correct behaviour and was seen to fail on the unfixed code for
the reason named above.
"""

import datetime
import json
import os
import sqlite3
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2612a1_u10.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402

from cpersona import config, maintenance_handlers, session  # noqa: E402
from cpersona.admin_handlers import do_import_memories  # noqa: E402
from cpersona.database import get_db  # noqa: E402
from cpersona.memory_handlers import do_store  # noqa: E402
from cpersona.utils import _parse_timestamp_utc  # noqa: E402

UTC = datetime.timezone.utc


async def _row(db, agent_id: str, content: str):
    rows = await db.execute_fetchall(
        "SELECT timestamp, created_at, source, metadata FROM memories WHERE agent_id = ? AND content = ?",
        (agent_id, content),
    )
    assert len(rows) == 1, rows
    return rows[0]


async def _insert(db, agent_id: str, content: str, *, timestamp: str, source="{}", metadata="{}", locked=0):
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, metadata, timestamp, locked) VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, content, source, metadata, timestamp, locked),
    )
    await db.commit()


def _issue(report: dict, kind: str) -> dict | None:
    for issue in report["issues"]:
        if issue.get("type") == kind:
            return issue
    return None


# ===========================================================================
# bug-546 -- invalid_timestamp must not overwrite a stamp the readers parse.
# ===========================================================================

EVENT = datetime.datetime(2025, 1, 2, 1, 0, tzinfo=UTC)  # 10:00 at +09:00

# Python's fromisoformat (the reader every scoring path uses) reads these; SQLite's
# datetime() returns NULL for all of them.
READABLE_BUT_NOT_TO_SQLITE = [
    ("2025-01-02T10:00:00+0900", "2025-01-02T01:00:00+00:00"),
    ("2025-01-02T10:00:00+09", "2025-01-02T01:00:00+00:00"),
    ("20250102T010000Z", "2025-01-02T01:00:00+00:00"),
    ("2025-01-02T10:00:00,5+0900", "2025-01-02T01:00:00.500000+00:00"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp,canonical", READABLE_BUT_NOT_TO_SQLITE)
async def test_invalid_timestamp_fix_keeps_the_instant_of_a_readable_stamp(stamp, canonical):
    session.reset_pauses_for_tests()
    agent = f"u10-ts-{stamp}"
    db = await get_db()
    # Preconditions of the defect: the package reads it, SQLite does not.
    assert _parse_timestamp_utc(stamp) is not None
    assert (await db.execute_fetchall("SELECT datetime(?)", (stamp,)))[0][0] is None

    res = await do_store(agent, {"content": f"event {stamp}", "timestamp": stamp})
    assert res["ok"], res

    report = await maintenance_handlers.do_check_health(agent_id=agent, fix=True, checks=["invalid_timestamp"])
    issue = _issue(report, "invalid_timestamp")
    assert issue is not None and issue["count"] == 1 and issue["repairable"] == 1, report

    after, _created_at, _s, _m = await _row(db, agent, f"event {stamp}")
    # The instant survives the repair, and the stored form is now one SQLite reads
    # too, so the SQL-side time filters and future_timestamp see the same instant.
    assert after == canonical, after
    assert _parse_timestamp_utc(after) == _parse_timestamp_utc(stamp)
    assert (await db.execute_fetchall("SELECT datetime(?)", (after,)))[0][0] is not None

    again = await maintenance_handlers.do_check_health(agent_id=agent, fix=False, checks=["invalid_timestamp"])
    assert _issue(again, "invalid_timestamp") is None, again


@pytest.mark.asyncio
async def test_invalid_timestamp_full_fix_run_keeps_event_time():
    """The whole registry, as an operator runs it: no later check undoes it either."""
    session.reset_pauses_for_tests()
    agent = "u10-ts-full"
    db = await get_db()
    await do_store(agent, {"content": "full run event", "timestamp": "2025-01-02T10:00:00+0900"})
    await maintenance_handlers.do_check_health(agent_id=agent, fix=True)
    after, _c, _s, _m = await _row(db, agent, "full run event")
    assert _parse_timestamp_utc(after) == EVENT, after


@pytest.mark.asyncio
async def test_invalid_timestamp_naive_readable_stamp_stays_naive():
    """A naive stamp keeps its wall-clock reading; no zone is invented for it."""
    session.reset_pauses_for_tests()
    agent = "u10-ts-naive"
    db = await get_db()
    await _insert(db, agent, "naive basic", timestamp="20250102T010000")
    await maintenance_handlers.do_check_health(agent_id=agent, fix=True, checks=["invalid_timestamp"])
    after, _c, _s, _m = await _row(db, agent, "naive basic")
    assert after == "2025-01-02T01:00:00", after


@pytest.mark.asyncio
async def test_invalid_timestamp_unreadable_stamp_still_falls_back_to_created_at():
    session.reset_pauses_for_tests()
    agent = "u10-ts-garbage"
    db = await get_db()
    await _insert(db, agent, "garbage stamp", timestamp="not a timestamp")
    await _insert(db, agent, "garbage locked", timestamp="still not one", locked=1)
    await _insert(db, agent, "readable locked", timestamp="2025-01-02T10:00:00+0900", locked=1)
    report = await maintenance_handlers.do_check_health(agent_id=agent, fix=True, checks=["invalid_timestamp"])
    issue = _issue(report, "invalid_timestamp")
    assert issue["count"] == 3 and issue["repairable"] == 1, issue
    after, created_at, _s, _m = await _row(db, agent, "garbage stamp")
    assert after == datetime.datetime.fromisoformat(created_at).replace(tzinfo=UTC).isoformat(), (after, created_at)
    assert (await _row(db, agent, "garbage locked"))[0] == "still not one"
    assert (await _row(db, agent, "readable locked"))[0] == "2025-01-02T10:00:00+0900"


# ===========================================================================
# bug-568 -- deep_check(fix=true) must roll back a repair that raised part way.
# ===========================================================================


@pytest.mark.asyncio
async def test_deep_check_rolls_back_a_repair_that_raised_part_way():
    session.reset_pauses_for_tests()
    agent = "u10-deep"
    db = await get_db()
    # One recoverable anonymous source, repaired by a check that does not fail:
    # its repair must still commit, so the rollback is per check, not per run.
    await _insert(
        db, agent, "[alice] hello there", timestamp="2026-01-01T00:00:00+00:00",
        source=json.dumps({"type": "User", "id": "", "name": ""}),
    )
    for i in range(600):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            (agent, f"x{i}"),
        )
    await db.commit()
    ids = [
        r[0]
        for r in await db.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = ? AND LENGTH(content) <= 5 ORDER BY id", (agent,)
        )
    ]
    assert len(ids) == 600
    victim = ids[550]  # lands in the second 500-row DELETE chunk
    await db.execute(
        f"CREATE TRIGGER u10_inject BEFORE DELETE ON memories WHEN old.id = {victim} "
        "BEGIN SELECT RAISE(ABORT, 'injected fault'); END"
    )
    await db.commit()
    try:
        res = await maintenance_handlers.do_deep_check(
            agent, fix=True, checks=["anonymous_source", "short_content"]
        )
    finally:
        await db.execute("DROP TRIGGER IF EXISTS u10_inject")
        await db.commit()

    assert "error" in res["results"]["short_content"], res
    # A fresh connection to the file the server is using sees only what was committed.
    db_file = (await db.execute_fetchall("PRAGMA database_list"))[0][2]
    assert db_file, "the shared connection must be file-backed for this check"
    con = sqlite3.connect(db_file)
    try:
        short_left = con.execute(
            "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND LENGTH(content) <= 5", (agent,)
        ).fetchone()[0]
        source = con.execute(
            "SELECT source FROM memories WHERE agent_id = ? AND content = '[alice] hello there'", (agent,)
        ).fetchone()[0]
    finally:
        con.close()
    assert short_left == 600, f"{600 - short_left} rows deleted under an error-only response"
    assert json.loads(source)["name"] == "alice", source
    assert res["results"]["anonymous_source"]["fixed"] == 1, res


# ===========================================================================
# bug-571 -- NaN / Infinity in source or metadata, and the invalid_json repair.
# ===========================================================================


@pytest.mark.asyncio
async def test_store_writes_valid_json_for_non_finite_floats():
    session.reset_pauses_for_tests()
    agent = "u10-nan-store"
    db = await get_db()
    a = await do_store(agent, {
        "content": "invoice 4411 quince",
        "metadata": {"invoice": "4411", "customer": "ACME", "scores": [1.5, float("nan")], "confidence": float("nan")},
    })
    b = await do_store(agent, {
        "content": "Bob said quince late",
        "source": {"type": "User", "id": "discord:bob", "name": "Bob", "trust": float("inf")},
    })
    assert a["ok"] and b["ok"], (a, b)
    valid = await db.execute_fetchall(
        "SELECT json_valid(source), json_valid(metadata) FROM memories WHERE agent_id = ?", (agent,)
    )
    assert valid == [(1, 1), (1, 1)], valid
    _t, _c, _s, meta = await _row(db, agent, "invoice 4411 quince")
    assert json.loads(meta) == {"invoice": "4411", "customer": "ACME", "scores": [1.5, None], "confidence": None}
    _t, _c, src, _m = await _row(db, agent, "Bob said quince late")
    assert json.loads(src)["id"] == "discord:bob" and json.loads(src)["trust"] is None, src

    report = await maintenance_handlers.do_check_health(agent_id=agent, fix=True, checks=["invalid_json"])
    assert _issue(report, "invalid_json") is None, report


@pytest.mark.asyncio
async def test_import_writes_valid_json_for_non_finite_floats(tmp_path, monkeypatch):
    session.reset_pauses_for_tests()
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    agent = "u10-nan-import"
    path = tmp_path / "nan.jsonl"
    path.write_text(json.dumps({
        "_type": "memory", "agent_id": agent, "content": "imported invoice 4411",
        "source": {"type": "User", "id": "discord:bob", "name": "Bob", "trust": float("inf")},
        "metadata": {"invoice": "4411", "confidence": float("nan")},
        "timestamp": "2026-10-01T00:00:00+00:00",
    }) + "\n")
    assert "NaN" in path.read_text()  # what a Python json.dumps writes by default
    imp = await do_import_memories(str(path))
    assert imp.get("ok") and imp.get("imported_memories") == 1, imp
    db = await get_db()
    _t, _c, src, meta = await _row(db, agent, "imported invoice 4411")
    assert json.loads(meta) == {"invoice": "4411", "confidence": None}, meta
    assert json.loads(src)["id"] == "discord:bob", src
    valid = await db.execute_fetchall(
        "SELECT json_valid(source), json_valid(metadata) FROM memories WHERE agent_id = ?", (agent,)
    )
    assert valid == [(1, 1)], valid


@pytest.mark.asyncio
async def test_invalid_json_repair_keeps_an_object_python_can_parse():
    """Rows already stored with bare NaN / Infinity tokens keep every other key."""
    session.reset_pauses_for_tests()
    agent = "u10-nan-repair"
    db = await get_db()
    await _insert(
        db, agent, "stored with nan", timestamp="2026-01-01T00:00:00+00:00",
        source='{"type": "User", "id": "discord:bob", "name": "Bob", "trust": Infinity}',
        metadata='{"invoice": "4411", "customer": "ACME", "confidence": NaN}',
    )
    await _insert(db, agent, "truly broken", timestamp="2026-01-01T00:00:00+00:00", metadata="{not json")
    await _insert(db, agent, "bare scalar", timestamp="2026-01-01T00:00:00+00:00", metadata="NaN")
    await _insert(
        db, agent, "locked nan", timestamp="2026-01-01T00:00:00+00:00", metadata='{"a": NaN}', locked=1
    )

    report = await maintenance_handlers.do_check_health(agent_id=agent, fix=True, checks=["invalid_json"])
    issue = _issue(report, "invalid_json")
    assert issue["bad_source"] == 1 and issue["bad_metadata"] == 4 and issue["repairable"] == 3, issue

    _t, _c, src, meta = await _row(db, agent, "stored with nan")
    assert json.loads(meta) == {"invoice": "4411", "customer": "ACME", "confidence": None}, meta
    assert json.loads(src) == {"type": "User", "id": "discord:bob", "name": "Bob", "trust": None}, src
    assert (await _row(db, agent, "truly broken"))[3] == "{}"
    assert (await _row(db, agent, "bare scalar"))[3] == "{}"
    assert (await _row(db, agent, "locked nan"))[3] == '{"a": NaN}'

    again = await maintenance_handlers.do_check_health(agent_id=agent, fix=False, checks=["invalid_json"])
    issue = _issue(again, "invalid_json")
    assert issue["bad_metadata"] == 1 and issue["repairable"] == 0, issue
