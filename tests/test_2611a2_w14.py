"""Regression tests: the write seam and the SQL readers read a timestamp the same way.

  bug-587  ``future_timestamp_issue`` read a stamp with the package's parser and
           answered "nothing to report" for one it could not read, leaving the row
           to ``invalid_timestamp``. But ``invalid_timestamp``, ``future_timestamp``
           and the confidence span read the column with SQLite's date functions,
           which also accept a Julian-day number, 'now', 30 February and a lower-case
           'z'. So ``CPERSONA_FUTURE_TIMESTAMP_MODE=reject`` stored '2500000' (2132 to
           SQLite) while it refused the ISO spelling of a nearer instant, and a
           literal 'now' was flagged by no check, survived ``check_health(fix=true)``
           and became the scope's MAX(timestamp) -- a value the span's caller cannot
           parse, so the confidence span was dropped for the whole scope.

Each test states the correct behaviour and was seen to fail on the unfixed code for
the reason named above, except where a docstring says it guards the fix instead.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_w14.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import (  # noqa: E402
    admin_handlers,
    checks,
    config,
    maintenance_handlers,
    memory_handlers,
    scope_stats,
    session,
    utils,
)
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.w14"
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

# SQLite's date functions read every one of these as an instant past any sensible
# allowance; the package's parser reads none of them.
SQL_ONLY_AHEAD = [
    "2500000",  # a Julian-day number: 2132-08-31 12:00 to SQLite
    "2099-02-30T00:00:00+00:00",  # 30 February: 2099-03-02 to SQLite
    "2099-01-01T00:00:00z",  # a lower-case zone designator
]

# The same, for instants in the past (or, for 'now', the present at every read).
SQL_ONLY_PAST = [
    "now",
    "2460000.5",  # 2023-02-25
    "2026-02-30T00:00:00+00:00",  # 2026-03-02
    "2026-02-28T12:00:00z",
    "0001-01-01T00:00:00+01:00",  # year 0 to SQLite; out of the parser's range
]


def _sqlite_reads(stamp: str) -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        return conn.execute("SELECT datetime(?) IS NOT NULL", (stamp,)).fetchone()[0] == 1
    finally:
        conn.close()


def _sqlite_seconds_between(stamp: str, reference: datetime) -> float:
    conn = sqlite3.connect(":memory:")
    try:
        return conn.execute(
            "SELECT (julianday(?) - julianday(?)) * 86400.0", (stamp, reference.isoformat())
        ).fetchone()[0]
    finally:
        conn.close()


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    scope_stats.clear()
    yield conn
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    scope_stats.clear()


async def _insert(conn, timestamp, *, content=None, created_at="2026-06-01 09:30:00", locked=0, agent=AGENT):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, source, metadata, timestamp, locked, created_at)"
        " VALUES (?, ?, '{}', '{}', ?, ?, ?)",
        (agent, content or f"row {timestamp!r}", timestamp, locked, created_at),
    )
    await conn.commit()


async def _issues(conn, name, fix=False, agent=AGENT):
    issues, _ = await checks.run_health_checks(conn, agent_id=agent, fix=fix, checks=[name])
    return [i for i in issues if i.get("type") == name]


async def _stamps(conn, agent=AGENT):
    return [
        r[0]
        for r in await conn.execute_fetchall(
            "SELECT timestamp FROM memories WHERE agent_id = ? ORDER BY id", (agent,)
        )
    ]


# ===========================================================================
# The verdict: a stamp SQLite reads is judged as SQLite reads it.
# ===========================================================================


@pytest.mark.parametrize("stamp", SQL_ONLY_AHEAD)
def test_the_verdict_reads_a_stamp_only_sqlite_reads_the_way_sqlite_does(stamp):
    # Preconditions of the defect: the parser declines it, SQLite reads it.
    assert utils._parse_timestamp_utc(stamp) is None
    assert _sqlite_reads(stamp)

    issue = utils.future_timestamp_issue(stamp, now=NOW)
    assert issue is not None, (
        f"{stamp!r} is {_sqlite_seconds_between(stamp, NOW):.0f}s ahead to every SQL "
        "reader, and the write seam called it nothing to report"
    )
    assert issue["ahead_by_seconds"] == pytest.approx(_sqlite_seconds_between(stamp, NOW), abs=1.0)
    assert issue["timestamp"] == stamp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp",
    ["2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00Z", *SQL_ONLY_AHEAD, *SQL_ONLY_PAST[1:]],
)
async def test_the_seam_and_the_health_check_name_the_same_rows(db, monkeypatch, stamp):
    """One row at a time, so agreement on a count cannot hide two errors that cancel.

    'now' is left out: with the reference frozen, SQL's own clock would read it as
    ahead of the frozen instant, which says nothing about either reader."""
    monkeypatch.setattr(checks, "future_timestamp_boundary", lambda: utils.future_timestamp_boundary(now=NOW))
    await _insert(db, stamp)
    flagged = bool(await _issues(db, "future_timestamp"))
    assert flagged == (utils.future_timestamp_issue(stamp, now=NOW) is not None), (
        f"future_timestamp {'flags' if flagged else 'passes'} {stamp!r} and the write "
        "seam answers the opposite"
    )


# ===========================================================================
# The store seam under reject and warn, and the import seam's report.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", SQL_ONLY_AHEAD)
async def test_reject_refuses_a_stamp_only_sqlite_reads_as_ahead(db, monkeypatch, stamp):
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    # The control the finding compared against: a nearer instant in ISO is refused.
    control = await memory_handlers.do_store(AGENT, {"content": "control", "timestamp": "2099-03-02T00:00:00+00:00"})
    assert control["result"] == "rejected", control

    res = await memory_handlers.do_store(AGENT, {"content": f"spelled {stamp}", "timestamp": stamp})
    assert res["ok"] is False and res["result"] == "rejected", res
    assert "ahead of this clock" in res["reason"], res
    assert await _stamps(db) == [], "a refused write must leave no row"


@pytest.mark.asyncio
async def test_warn_reports_a_stamp_only_sqlite_reads_as_ahead(db):
    res = await memory_handlers.do_store(AGENT, {"content": "julian day", "timestamp": "2500000"})
    assert res["result"] == "stored", res
    assert res.get("timestamp_ahead_of_clock") is not None, (
        "warn must report this stamp on the write's own answer, as it does for its ISO twin"
    )


@pytest.mark.asyncio
async def test_reject_still_stores_a_past_stamp_only_sqlite_reads(db, monkeypatch):
    """Guards the fix rather than the defect: reject refuses what is ahead of the
    clock. A stamp no reader places ahead is invalid_timestamp's, as before."""
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    res = await memory_handlers.do_store(AGENT, {"content": "an old julian day", "timestamp": "2460000.5"})
    assert res["result"] == "stored", res


@pytest.mark.asyncio
async def test_a_restore_counts_a_stamp_only_sqlite_reads_as_ahead(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    path = tmp_path / "backup.jsonl"
    path.write_text(
        json.dumps({"_type": "memory", "agent_id": AGENT, "content": "restored", "timestamp": "2500000"}) + "\n",
        encoding="utf-8",
    )
    res = await admin_handlers.do_import_memories(str(path), target_agent_id=AGENT)
    assert res["ok"] is True and res["imported_memories"] == 1, res
    assert res.get("future_timestamps") == 1, res
    assert await _stamps(db) == ["2500000"], "the restore must stay faithful"


# ===========================================================================
# invalid_timestamp owns every other stamp the parser cannot read.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", SQL_ONLY_PAST)
async def test_invalid_timestamp_finds_and_repairs_a_stamp_only_sqlite_reads(db, stamp):
    assert utils._parse_timestamp_utc(stamp) is None and _sqlite_reads(stamp)
    await _insert(db, stamp, created_at="2026-06-01 09:30:00")

    found = await _issues(db, "invalid_timestamp")
    assert found and found[0]["count"] == 1 and found[0]["repairable"] == 1, found

    await _issues(db, "invalid_timestamp", fix=True)
    assert await _stamps(db) == ["2026-06-01T09:30:00+00:00"]
    assert await _issues(db, "invalid_timestamp") == [], "0 -> 0 would mean it did not fix"


@pytest.mark.asyncio
async def test_invalid_timestamp_leaves_a_locked_sql_only_stamp_alone(db):
    await _insert(db, "now", locked=1)
    found = await _issues(db, "invalid_timestamp", fix=True)
    assert found and found[0]["count"] == 1 and found[0]["repairable"] == 0, found
    assert await _stamps(db) == ["now"]


@pytest.mark.asyncio
@pytest.mark.parametrize("created_at", ["0000-01-01 00:00:00", "not a time"])
async def test_invalid_timestamp_does_not_count_a_repair_that_repairs_nothing(db, created_at):
    """Guards the fix: a created_at the parser cannot read either (an import can
    carry one) would replace one unreadable stamp with another, so it is not
    counted repairable and the row is left as it was."""
    await _insert(db, "now", created_at=created_at)
    found = await _issues(db, "invalid_timestamp", fix=True)
    assert found and found[0]["count"] == 1 and found[0]["repairable"] == 0, found
    assert await _stamps(db) == ["now"]


@pytest.mark.asyncio
async def test_a_sql_only_stamp_ahead_of_the_clock_is_claimed_once(db):
    """Guards the fix: the two checks stay disjoint. A stamp SQLite reads as ahead is
    future_timestamp's; invalid_timestamp claiming it too would double-report it."""
    await _insert(db, "2500000")
    assert await _issues(db, "invalid_timestamp") == []
    future = await _issues(db, "future_timestamp")
    assert future and future[0]["count"] == 1, future


@pytest.mark.asyncio
async def test_a_full_repair_leaves_no_stamp_the_parser_cannot_read(db):
    """The finding's own corpus, repaired the way an operator repairs it."""
    for stamp in ("2026-01-01T00:00:00+00:00", "2500000", "2099-02-30T00:00:00+00:00", "now"):
        await _insert(db, stamp)
    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)
    stamps = await _stamps(db)
    assert all(utils._parse_timestamp_utc(s) is not None for s in stamps), stamps
    drift = await _issues(db, "timestamp_format_drift")
    assert not any(i.get("unfixable_naive") for i in drift), drift


# ===========================================================================
# The confidence span: an end its caller cannot parse is not an end.
# ===========================================================================

GOOD_LO = "2026-03-01T00:00:00+00:00"
GOOD_HI = "2026-08-01T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp",
    [
        "now",  # sorts after every digit: the text maximum
        "2500000",  # also after '2026-...' as text
        "1721059.5",  # year 0 to SQLite, before every real stamp as text
        "0001-01-01T00:00:00+01:00",  # the text minimum, year 0 to SQLite
        # Inside the window bug-286 settles chronologically, and chronologically
        # past the real end, so the settling step would pick it: trailing spaces
        # SQLite ignores and the parser does not.
        "2026-07-31T23:00:00-05:00 ",
        "2026-03-01T01:00:00+09:00 ",
    ],
)
async def test_the_span_skips_an_end_the_caller_cannot_parse(db, stamp):
    assert utils._parse_timestamp_utc(stamp) is None and _sqlite_reads(stamp)
    for ts in (GOOD_LO, "2026-05-01T00:00:00+00:00", GOOD_HI):
        await _insert(db, ts)
    assert await scope_stats.get_span(db, AGENT) == (GOOD_LO, GOOD_HI)

    await _insert(db, stamp)
    scope_stats.clear()
    span = await scope_stats.get_span(db, AGENT)
    assert span == (GOOD_LO, GOOD_HI), (
        f"{stamp!r} became an end of the span {span!r}; the caller parses neither end "
        "it cannot read and drops the scope's span"
    )


@pytest.mark.asyncio
async def test_a_stored_now_does_not_take_the_scope_s_span(db):
    """The finding's path end to end: through store, then the span read recall uses."""
    for ts in ("2026-01-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"):
        await memory_handlers.do_store(AGENT, {"content": f"dated {ts}", "timestamp": ts})
    await memory_handlers.do_store(AGENT, {"content": "a literal now", "timestamp": "now"})
    scope_stats.clear()
    lo, hi = await scope_stats.get_span(db, AGENT, project_id="", channel="")
    assert (lo, hi) == ("2026-01-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    assert utils._parse_timestamp_utc(lo) and utils._parse_timestamp_utc(hi)


@pytest.mark.asyncio
async def test_a_scope_with_only_unreadable_stamps_has_no_span(db):
    for stamp in ("now", "2460000.5"):
        await _insert(db, stamp)
    assert await scope_stats.get_span(db, AGENT) == (None, None)
