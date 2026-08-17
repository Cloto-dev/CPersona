"""bug-208 (an earlier decision): episodes with no ``start_time``, visible and repairable.

Production measured 333 of 500 episodes (66.6%) with NULL/'' ``start_time``
while every one of them carried a NOT NULL ``created_at``. bug-213 already
taught the READ paths to score such rows by ``created_at``
(``utils.episode_timestamp``); this check is the write-side complement —
``check_health`` surfaces the gap, and ``fix=true`` materialises the same
fallback into the rows for consumers that read ``start_time`` directly.

The tests pin the four properties the task names:

- detection is the default (``fix=false`` writes nothing),
- the fix copies ``created_at`` and drives the count to zero — the task's
  acceptance is a measured before/after delta, "0 -> 0 means it did not fix",
- a row whose ``created_at`` does not parse is counted but excluded from
  ``repairable`` and left alone (copying garbage would trade "no timestamp"
  for "wrong timestamp"),
- the hint states that ``created_at`` is when the episode was RECORDED, not
  the time of what it describes (the approximation caveat is a MUST from the
  task, because after the fix the row is indistinguishable from a genuine one).

The ``repairable`` contract (an earlier decision) is exercised for this check in
``test_255_repairable_contract.py``; these tests cover the check's own
behaviour.
"""

import pytest
import pytest_asyncio

from cpersona import checks
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

AGENT = "agent.start-time"
GENUINE_TS = "2026-08-01T09:00:00+00:00"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


async def _episode(conn, summary: str, start_time, created_at: str = "2026-08-10 12:00:00"):
    await conn.execute(
        "INSERT INTO episodes (agent_id, summary, start_time, created_at) VALUES (?, ?, ?, ?)",
        (AGENT, summary, start_time, created_at),
    )
    await conn.commit()


async def _run(conn, fix: bool):
    issues, _ = await checks.run_health_checks(
        conn, agent_id=AGENT, fix=fix, checks=["missing_episode_start_time"]
    )
    return issues


@pytest.mark.asyncio
async def test_detection_counts_null_and_empty_but_not_genuine(db):
    await _episode(db, "null start", None)
    await _episode(db, "empty start", "")
    await _episode(db, "genuine start", GENUINE_TS)

    issues = await _run(db, fix=False)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["type"] == "missing_episode_start_time"
    assert issue["count"] == 2
    assert issue["repairable"] == 2
    assert issue["severity"] == "info"
    assert "recorded" in issue["hint"], (
        "the hint must say created_at is the recording time, not the event time — "
        "after a fix the row cannot be told apart from a genuine one"
    )

    # Detection is the default: nothing was written.
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM episodes WHERE start_time IS NULL OR start_time = ''"
    )
    assert rows[0][0] == 2


@pytest.mark.asyncio
async def test_fix_copies_created_at_and_drives_the_count_to_zero(db):
    await _episode(db, "null start", None, created_at="2026-08-10 12:00:00")
    await _episode(db, "empty start", "", created_at="2026-08-11 06:30:00")
    await _episode(db, "genuine start", GENUINE_TS)

    issues = await _run(db, fix=True)
    assert issues[0]["count"] == 2

    # The repaired rows carry their created_at, byte for byte.
    rows = await db.execute_fetchall(
        "SELECT summary, start_time, created_at FROM episodes ORDER BY id"
    )
    by_summary = {r[0]: (r[1], r[2]) for r in rows}
    assert by_summary["null start"][0] == by_summary["null start"][1] == "2026-08-10 12:00:00"
    assert by_summary["empty start"][0] == by_summary["empty start"][1] == "2026-08-11 06:30:00"
    # The genuine row was not touched.
    assert by_summary["genuine start"][0] == GENUINE_TS

    # The acceptance the task states: a measured before/after delta. 2 -> 0.
    assert await _run(db, fix=False) == [], "the count did not drop to zero after the fix"


@pytest.mark.asyncio
async def test_unparseable_created_at_is_counted_but_not_repaired(db):
    await _episode(db, "healable", None, created_at="2026-08-10 12:00:00")
    await _episode(db, "unhealable", None, created_at="not-a-timestamp")

    issues = await _run(db, fix=True)
    assert issues[0]["count"] == 2
    assert issues[0]["repairable"] == 1, (
        "a row whose created_at does not parse must not be claimed repairable"
    )

    rows = await db.execute_fetchall("SELECT summary, start_time FROM episodes ORDER BY id")
    by_summary = dict(rows)
    assert by_summary["healable"] == "2026-08-10 12:00:00"
    assert by_summary["unhealable"] is None, (
        "copying an unparseable created_at trades 'no timestamp' for 'garbage timestamp'"
    )

    # The unhealable row keeps the finding alive — visible, not silently absorbed.
    residual = await _run(db, fix=False)
    assert residual[0]["count"] == 1
    assert residual[0]["repairable"] == 0
