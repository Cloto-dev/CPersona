"""A caller-supplied timestamp may not name a moment ahead of this clock (N-03).

``store`` takes the caller's ``timestamp`` verbatim and the confidence curve
reads ``max(0.0, now - timestamp)``. A stamp ahead of the clock is therefore
scored as the age of a row written this instant, and it never decays -- tomorrow
it is still ahead. The second half is larger: the corpus span scales the decay
RATE, so one row stamped 2099 widens three weeks into seventy years and flattens
the time axis for every other row in the scope. Both halves are recorded in the
behaviour golden (``corpus-future-*``); what is pinned here is the policy.

The shape is the one bug-290 and bug-292 established: the enforcement path is
written, mounted and tested now, and defaulted to ``warn``, so the release that
starts refusing changes a default rather than adding a code path.

Two seams write a caller's timestamp and they answer differently, on purpose:

    store             an external boundary -- a caller handing us a moment it
                      believes in, which `reject` may refuse
    import_memories   a restore -- the same corpus coming back, which reports
                      and imports, because export -> import must not be lossy

The boundary cases are exact, and they are taken on the pure function with an
explicit ``now`` rather than against the wall clock: a policy that fires at
"about" the right distance cannot be told apart from one that fires at the wrong
one, and a test that spends milliseconds between building a stamp and judging it
cannot pin the boundary it is naming.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, config, memory_handlers, session, utils
from cpersona.database import get_db
from cpersona.utils import future_timestamp_boundary, future_timestamp_issue

AGENT = "agent.future-ts"
SKEW = config.FUTURE_TIMESTAMP_SKEW_SECONDS
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(delta_seconds: float) -> str:
    return (NOW + timedelta(seconds=delta_seconds)).isoformat()


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks')"
    )
    await db.commit()
    yield db
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()


def _ahead_stamp(seconds: float) -> str:
    """A stamp `seconds` past the allowance, against the real clock."""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=SKEW + seconds)
    ).isoformat()


# ---------------------------------------------------------------------------
# The allowance itself. Exact boundaries, explicit `now`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,offset",
    [
        ("now", 0),
        ("a minute ahead", 60),
        ("exactly the allowance", SKEW),
        ("a second inside the allowance", SKEW - 1),
        ("an hour in the past", -3600),
    ],
)
def test_inside_the_allowance_reports_nothing(label, offset):
    """Ordinary clock skew is not a finding.

    `exactly the allowance` is the load-bearing one: a stamp at `now + skew` is
    the furthest a correct client can be after a network hop, so the boundary
    belongs on the accepted side. Moving it by one would turn every client that
    happens to sit on the edge into a warning nobody can act on.
    """
    assert future_timestamp_issue(_stamp(offset), now=NOW) is None, label


@pytest.mark.parametrize(
    "label,offset",
    [
        ("one second past the allowance", SKEW + 1),
        ("an hour ahead", 3600),
        ("a day ahead", 86400),
    ],
)
def test_past_the_allowance_is_reported(label, offset):
    issue = future_timestamp_issue(_stamp(offset), now=NOW)
    assert issue is not None, label
    assert issue["ahead_by_seconds"] == pytest.approx(offset), label
    assert issue["allowance_seconds"] == SKEW


def test_the_year_2099_is_reported_with_its_real_distance():
    """The directive's own example. The reported distance is the whole of it, not
    the excess past the allowance -- an operator reading '2272147200s ahead'
    knows immediately that this is a wrong century and not a wrong clock."""
    issue = future_timestamp_issue("2099-01-01T00:00:00Z", now=NOW)
    assert issue is not None
    expected = (
        datetime(2099, 1, 1, tzinfo=timezone.utc) - NOW
    ).total_seconds()
    assert issue["ahead_by_seconds"] == pytest.approx(expected)
    assert issue["timestamp"] == "2099-01-01T00:00:00Z"


def test_the_verdict_is_on_the_instant_not_the_spelling():
    """The same moment written three ways gets one answer.

    This is the property a lexicographic comparison does not have (bug-286 /
    the recall_with_context sort), and it is why the parse happens before the
    comparison rather than after it.
    """
    ahead = NOW + timedelta(seconds=SKEW + 3600)
    spellings = [
        ahead.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ahead.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        ahead.astimezone(timezone(timedelta(hours=9))).isoformat(),
        ahead.astimezone(timezone(timedelta(hours=-5))).isoformat(),
    ]
    verdicts = [future_timestamp_issue(s, now=NOW) for s in spellings]
    assert all(v is not None for v in verdicts), spellings
    assert len({round(v["ahead_by_seconds"]) for v in verdicts}) == 1, verdicts

    # And the mirror: a +09:00 stamp whose DATE PART reads later than the
    # boundary but whose instant does not is accepted. A text comparison would
    # get this one wrong in the other direction.
    inside = NOW.astimezone(timezone(timedelta(hours=9))).isoformat()
    assert future_timestamp_issue(inside, now=NOW) is None


def test_a_naive_stamp_is_read_as_utc_like_its_aware_twin():
    """bug-114's invariant, applied here: a naive value is UTC, not host-local.
    On a JST host the two would otherwise disagree by nine hours -- which is to
    say the check would fire on correct rows, or miss wrong ones, depending on
    where the server happens to run."""
    ahead = NOW + timedelta(seconds=SKEW + 7200)
    naive = ahead.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    aware = ahead.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    assert future_timestamp_issue(naive, now=NOW)["ahead_by_seconds"] == pytest.approx(
        future_timestamp_issue(aware, now=NOW)["ahead_by_seconds"]
    )


@pytest.mark.parametrize("value", ["", "not-a-date", "2026-13-45T99:99:99Z"])
def test_an_unreadable_stamp_is_not_this_findings_business(value):
    """`invalid_timestamp` owns that row and repairs it. A stamp nobody can parse
    is not a stamp anyone can call early, and two findings claiming one row would
    make their repairs race."""
    assert future_timestamp_issue(value, now=NOW) is None


def test_the_boundary_helper_and_the_verdict_agree():
    """The SQL side compares against `future_timestamp_boundary`, the Python side
    calls `future_timestamp_issue`. If they could disagree, the health check and
    the write seam would name different rows."""
    boundary = future_timestamp_boundary(now=NOW)
    assert future_timestamp_issue(boundary, now=NOW) is None
    just_past = (
        datetime.fromisoformat(boundary) + timedelta(seconds=1)
    ).isoformat()
    assert future_timestamp_issue(just_past, now=NOW) is not None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_the_shipped_defaults_are_warn_and_five_minutes():
    """Pinned because the 2.6 change is meant to be a default flip and nothing
    else. If the default moved on its own, that release would be adding a
    behaviour rather than turning one on."""
    assert config.FUTURE_TIMESTAMP_MODE == "warn"
    assert config.FUTURE_TIMESTAMP_SKEW_SECONDS == 300


def test_an_unreadable_mode_falls_back_to_warn_and_says_so(monkeypatch, caplog):
    """Never to `off`: answering a typo by removing the reporting the typo was
    reaching for is the one fallback that hides its own cause (the contract
    `_parse_choice` was written with)."""
    monkeypatch.setenv("CPERSONA_FUTURE_TIMESTAMP_MODE", "REJCT")
    with caplog.at_level(logging.WARNING):
        value = config._parse_choice(
            "CPERSONA_FUTURE_TIMESTAMP_MODE", "warn", ("warn", "reject", "off")
        )
    assert value == "warn"
    assert "CPERSONA_FUTURE_TIMESTAMP_MODE" in caplog.text


# ---------------------------------------------------------------------------
# The store seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warn_stores_the_row_and_reports_the_stamp(clean_db, caplog):
    with caplog.at_level(logging.WARNING):
        res = await memory_handlers.do_store(
            AGENT, {"content": "warned", "timestamp": _ahead_stamp(3600)}
        )
    assert res["result"] == "stored" and res["ok"] is True, res
    issue = res.get("timestamp_ahead_of_clock")
    assert issue is not None, "warn must report on the write's own answer, not only in the log"
    assert issue["allowance_seconds"] == SKEW
    assert "CPERSONA_FUTURE_TIMESTAMP_MODE=reject" in caplog.text, (
        "the log must name the setting an operator would change"
    )
    rows = await clean_db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert len(rows) == 1, "warn stores; it does not quietly drop the row"


@pytest.mark.asyncio
async def test_an_ordinary_write_carries_no_report(clean_db):
    """The absence is the property. A field that appeared on every write would be
    noise, and a caller could not use it to tell a flagged write from a clean one."""
    res = await memory_handlers.do_store(AGENT, {"content": "ordinary"})
    assert res["result"] == "stored"
    assert "timestamp_ahead_of_clock" not in res, res


@pytest.mark.asyncio
async def test_the_servers_own_clock_never_reports_itself(clean_db):
    """The verdict is taken on the value about to be written. If it were taken on
    `message["timestamp"]`, an omitted stamp would be judged as the empty string;
    if the default were computed after the check, a slow clock could flag it."""
    res = await memory_handlers.do_store(AGENT, {"content": "no stamp supplied"})
    assert "timestamp_ahead_of_clock" not in res, res


@pytest.mark.asyncio
async def test_reject_refuses_and_writes_nothing(clean_db, monkeypatch):
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    res = await memory_handlers.do_store(
        AGENT, {"content": "refused", "timestamp": "2099-01-01T00:00:00Z"}
    )
    assert res["ok"] is False and res["result"] == "rejected", res
    assert "ahead of this clock" in res["reason"], res
    rows = await clean_db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == 0, "a rejected write must leave no row"


@pytest.mark.asyncio
async def test_reject_is_decided_before_the_dedup_probes(clean_db, monkeypatch):
    """A refused write is refused whether or not an identical row exists. Deciding
    after the probes would answer `skipped` -- ok:true -- for a request the server
    means to refuse, which is the outcome the 2.5.2b1 store contract exists to
    prevent."""
    await memory_handlers.do_store(AGENT, {"content": "same body"})
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    res = await memory_handlers.do_store(
        AGENT, {"content": "same body", "timestamp": "2099-01-01T00:00:00Z"}
    )
    assert res["result"] == "rejected", res


@pytest.mark.asyncio
async def test_off_stores_silently(clean_db, monkeypatch, caplog):
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "off")
    with caplog.at_level(logging.WARNING):
        res = await memory_handlers.do_store(
            AGENT, {"content": "unwatched", "timestamp": "2099-01-01T00:00:00Z"}
        )
    assert res["result"] == "stored"
    assert "timestamp_ahead_of_clock" not in res, res
    assert "ahead of this clock" not in caplog.text
    rows = await clean_db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == "2099-01-01T00:00:00Z", "off changes the reporting, not the row"


# ---------------------------------------------------------------------------
# The import seam: reports, never refuses
# ---------------------------------------------------------------------------


def _export_line(path, timestamp: str, content: str = "restored row") -> None:
    import json

    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_type": "memory", "agent_id": AGENT, "content": content,
                            "timestamp": timestamp}) + "\n")


@pytest.mark.asyncio
async def test_a_restore_reports_a_future_stamp_and_imports_it_anyway(clean_db, tmp_path, monkeypatch):
    """Even under `reject`. The mode governs an external boundary; a restore is
    the same corpus coming back, and refusing here would make export -> import
    lossy for exactly the rows an operator most needs to see."""
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    path = tmp_path / "backup.jsonl"
    _export_line(path, "2099-01-01T00:00:00Z")

    res = await admin_handlers.do_import_memories(str(path), target_agent_id=AGENT)
    assert res["ok"] is True, res
    assert res["imported_memories"] == 1, res
    assert res["future_timestamps"] == 1, res
    rows = await clean_db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == "2099-01-01T00:00:00Z", "the restore must be faithful"


@pytest.mark.asyncio
async def test_a_clean_restore_says_nothing_about_timestamps(clean_db, tmp_path):
    path = tmp_path / "clean.jsonl"
    _export_line(path, "2026-01-01T00:00:00+00:00")
    res = await admin_handlers.do_import_memories(str(path), target_agent_id=AGENT)
    assert res["imported_memories"] == 1, res
    assert "future_timestamps" not in res, res


# ---------------------------------------------------------------------------
# The health check: the rows that are already stored
# ---------------------------------------------------------------------------


async def _mem(conn, content, timestamp, created_at="2026-06-01 00:00:00", locked=0):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, metadata, locked, created_at)"
        " VALUES (?, ?, '{}', ?, '{}', ?, ?)",
        (AGENT, content, timestamp, locked, created_at),
    )
    await conn.commit()


async def _run_check(conn, fix: bool):
    issues, _ = await checks.run_health_checks(
        conn, agent_id=AGENT, fix=fix, checks=["future_timestamp"]
    )
    return issues


@pytest.mark.asyncio
async def test_the_check_finds_only_what_is_past_the_allowance(clean_db):
    await _mem(clean_db, "honest past", "2026-01-01T00:00:00+00:00")
    await _mem(clean_db, "inside the allowance", _ahead_stamp(-SKEW + 10))
    await _mem(clean_db, "ahead of the clock", "2099-01-01T00:00:00Z")

    issues = await _run_check(clean_db, fix=False)
    assert len(issues) == 1, issues
    assert issues[0]["type"] == "future_timestamp"
    assert issues[0]["count"] == 1, issues
    assert issues[0]["severity"] == "warn"


@pytest.mark.asyncio
async def test_detection_alone_writes_nothing(clean_db):
    await _mem(clean_db, "ahead", "2099-01-01T00:00:00Z")
    await _run_check(clean_db, fix=False)
    rows = await clean_db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == "2099-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_the_fix_copies_created_at_in_the_canonical_aware_form(clean_db):
    """bug-229's lesson, inherited: `created_at` is SQLite-native naive while the
    write path stores an aware form, so copying it verbatim would mint a `naive`
    timestamp_format_drift finding the very next check refuses to repair."""
    await _mem(clean_db, "ahead", "2099-01-01T00:00:00Z", created_at="2026-06-01 09:30:00")
    issues = await _run_check(clean_db, fix=False)
    assert issues[0]["repairable"] == 1

    await _run_check(clean_db, fix=True)
    rows = await clean_db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == "2026-06-01T09:30:00+00:00", rows
    assert await _run_check(clean_db, fix=False) == [], "0 -> 0 would mean it did not fix"


@pytest.mark.asyncio
async def test_a_locked_row_is_counted_and_left_alone(clean_db):
    """bug-098: check_health(fix=true) never alters a locked memory, and
    `repairable` reports rows the fixer WILL write rather than rows it found."""
    await _mem(clean_db, "locked and ahead", "2099-01-01T00:00:00Z", locked=1)
    issues = await _run_check(clean_db, fix=False)
    assert issues[0]["count"] == 1 and issues[0]["repairable"] == 0, issues

    await _run_check(clean_db, fix=True)
    rows = await clean_db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == "2099-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_a_created_at_that_is_itself_ahead_is_not_copied(clean_db):
    """A host whose clock was wrong wrote both columns. Copying one future stamp
    over another would report a repair that repaired nothing."""
    await _mem(clean_db, "both ahead", "2099-01-01T00:00:00Z", created_at="2098-01-01 00:00:00")
    issues = await _run_check(clean_db, fix=False)
    assert issues[0]["count"] == 1 and issues[0]["repairable"] == 0, issues

    await _run_check(clean_db, fix=True)
    rows = await clean_db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == "2099-01-01T00:00:00Z", "an unrepairable row is left intact"


@pytest.mark.asyncio
async def test_an_unreadable_stamp_belongs_to_the_other_check(clean_db):
    """The two predicates are disjoint by construction. If this check claimed the
    unparseable row too, both fixers would rewrite it and the counts would
    double-report the same defect."""
    await _mem(clean_db, "unreadable", "not-a-date")
    assert await _run_check(clean_db, fix=False) == []
    issues, _ = await checks.run_health_checks(
        clean_db, agent_id=AGENT, fix=False, checks=["invalid_timestamp"]
    )
    assert issues and issues[0]["count"] == 1, issues


@pytest.mark.asyncio
async def test_the_check_reads_the_allowance_it_is_configured_with(clean_db, monkeypatch):
    """A known positive at a MOVED boundary.

    Without it the check could be comparing against a hard-coded distance and
    every test above would still pass, because they all use a stamp that is wrong
    by decades. Here the row does not move -- the allowance does -- so the only
    thing that can change the verdict is the check reading the setting.
    """
    await _mem(clean_db, "an hour ahead", _ahead_stamp(3600 - SKEW))

    monkeypatch.setattr(utils, "FUTURE_TIMESTAMP_SKEW_SECONDS", 7200)
    assert await _run_check(clean_db, fix=False) == [], (
        "an hour ahead is inside a two-hour allowance"
    )

    monkeypatch.setattr(utils, "FUTURE_TIMESTAMP_SKEW_SECONDS", 60)
    issues = await _run_check(clean_db, fix=False)
    assert issues and issues[0]["count"] == 1, (
        "the same row is past a one-minute allowance"
    )


class _FrozenDatetime(datetime):
    """A datetime whose wall-clock reads return a fixed instant.

    Subclassed rather than monkeypatched as a free function, so the arithmetic
    and parsing the same module does are inherited unchanged and only the clock
    is stopped -- the shape ``tests/behaviour_252.py`` uses for the same reason.
    """

    frozen = datetime(2020, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.frozen if tz else cls.frozen.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_the_check_reads_the_clock_it_is_given_not_sqlites(clean_db, monkeypatch):
    """The row does not move and the code does not change; only the clock does.

    SQL's ``datetime('now')`` is a second clock, and one no caller can inject.
    A check written against it would report a row as ahead in June and clean in
    August for the same corpus -- a finding about the calendar rather than about
    the data, and one that makes any recorded expectation flap on a date nobody
    chose. It is also the failure this project already paid for once, in a
    behaviour golden whose health scenarios would have gone red in January.

    The row below is stamped in 2026, which is behind the real clock. Under a
    clock frozen in 2020 it is ahead of it. If the check consulted SQL, the
    verdict here would be "nothing found".
    """
    await _mem(clean_db, "past by the wall clock", "2026-01-01T00:00:00+00:00")
    assert await _run_check(clean_db, fix=False) == [], "sanity: clean against the real clock"

    monkeypatch.setattr(utils, "datetime", _FrozenDatetime)
    issues = await _run_check(clean_db, fix=False)
    assert issues and issues[0]["count"] == 1, (
        "the check must judge against the clock its own module reads, not SQL's"
    )
