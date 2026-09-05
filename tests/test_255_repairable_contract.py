"""The ``repairable`` contract and its one de-escalation rule (2.5.5).

bug-205 gave ``check_invalid_source_type`` a local rule: a finding no fix run can
act on should not pin ``status=degraded`` forever. A second instance turned up in
``check_timestamp_format_drift`` (902 aware rows normalised, 2 locked, ``degraded``
permanently), which is what promoted the rule from one check's special case to a
registry-wide contract.

The contract is only worth having if it cannot be opted out of by forgetting it.
So the load-bearing test in this file is not any single check's behaviour — it is
``test_every_fix_capable_check_has_a_seeder`` plus the parametrised declaration
test: together they mean a 26th ``fix_capable`` check cannot reach master without
either declaring ``repairable`` or being consciously written into this file.

Both directions are pinned. A suite that only proved unrepairable findings get
demoted would be satisfied by demoting everything.
"""

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from cpersona import session
from cpersona import checks, vector
from cpersona.config import MAX_CONTENT_LENGTH, MAX_PROFILE_LENGTH
from cpersona.database import get_db

AGENT = "repairable-agent"
UTC_TS = "2026-08-01T00:00:00+00:00"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _mem(conn, content, *, locked=0, source="{}", timestamp=UTC_TS, channel="", embedding=None):
    await conn.execute(
        """INSERT INTO memories (agent_id, content, source, timestamp, locked, channel, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (AGENT, content, source, timestamp, locked, channel, embedding),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# One seeder per fix_capable check: make the check fire, hand back the db the
# runner should see. Where a `locked` row is possible the seeder uses one, so the
# suite exercises the repairable == 0 branch rather than only the happy path.
# ---------------------------------------------------------------------------

SEEDERS: dict = {}


def seeder(name):
    def deco(fn):
        SEEDERS[name] = asynccontextmanager(fn)
        return fn

    return deco


@seeder("memory_annotation")
async def _s_memory_annotation(conn):
    await _mem(conn, "[Memory from bob] hello", locked=1)
    yield conn


@seeder("discord_mention")
async def _s_discord_mention(conn):
    await _mem(conn, "<@1234> hello", locked=1)
    yield conn


@seeder("oversized_content")
async def _s_oversized_content(conn):
    await _mem(conn, "x" * (MAX_CONTENT_LENGTH + 10), locked=1)
    yield conn


@seeder("duplicate_content")
async def _s_duplicate_content(conn):
    # The dedup UNIQUE index spans channel, so the same content in two channels
    # is a legal write and exactly what this check collapses. Both locked.
    await _mem(conn, "the same thing twice", locked=1, channel="")
    await _mem(conn, "the same thing twice", locked=1, channel="other")
    yield conn


@seeder("embedding_dimension")
async def _s_embedding_dimension(conn, fake_client=None):
    await _mem(conn, "wrong-length blob", embedding=b"\x00" * 8)
    yield conn


@seeder("nonfinite_embedding")
async def _s_nonfinite_embedding(conn):
    # Packed the way the store packs, not hand-written bytes: the check reads a
    # blob a real backend once produced, and a NaN that survives float32 packing
    # is the thing being detected.
    from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient

    await _mem(conn, "a vector with a NaN in it",
               embedding=EmbeddingClient.pack_embedding([0.1, float("nan"), 0.3]))
    yield conn


@seeder("null_embedding")
async def _s_null_embedding(conn):
    await _mem(conn, "no blob yet")
    yield conn


@seeder("null_episode_embedding")
async def _s_null_episode_embedding(conn):
    await conn.execute(
        "INSERT INTO episodes (agent_id, summary, start_time) VALUES (?, ?, ?)",
        (AGENT, "an episode with no blob", UTC_TS),
    )
    await conn.commit()
    yield conn


@seeder("missing_episode_start_time")
async def _s_missing_episode_start_time(conn):
    # `episodes` has no locked column, so unlike the memory seeders this one
    # cannot pin a locked-guard zero; the bound exercised instead is created_at
    # parseability — the NULL-start_time row's default created_at parses, so it
    # is repairable, and the contract value is 1.
    await conn.execute(
        "INSERT INTO episodes (agent_id, summary, start_time) VALUES (?, ?, NULL)",
        (AGENT, "an episode with no start_time"),
    )
    await conn.commit()
    yield conn


class _RaiseOnIntegrityCheck:
    """Delegates to the real connection but reports the FTS index as corrupt.

    Corrupting an FTS5 shadow table for real would leave the shared suite DB in a
    state later tests inherit if a restore ever failed to run; the runner's branch
    is reached identically this way, and nothing has to be put back.
    """

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql, *args, **kwargs):
        if "integrity-check" in sql:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return await self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._conn, item)


@seeder("fts_integrity")
async def _s_fts_integrity(conn):
    yield _RaiseOnIntegrityCheck(conn)


@asynccontextmanager
async def _index_dropped(conn, name):
    ddl = checks._EXPECTED_OBJECTS[name]["sql"]
    await conn.execute(f"DROP INDEX IF EXISTS {name}")
    await conn.commit()
    try:
        yield conn
    finally:
        # A fix=True run may have recreated it already, so drop first: the point
        # is that the next test starts from the canonical schema either way.
        await conn.execute(f"DROP INDEX IF EXISTS {name}")
        await conn.execute(ddl)
        await conn.commit()


@seeder("dedup_msg_id_index")
async def _s_dedup_msg_id_index(conn):
    async with _index_dropped(conn, "idx_memories_dedup_msg_id") as c:
        yield c


@seeder("schema_objects")
async def _s_schema_objects(conn):
    async with _index_dropped(conn, "idx_memories_dedup_content") as c:
        yield c


@seeder("invalid_json")
async def _s_invalid_json(conn):
    await _mem(conn, "bad source json", source="this is not json", locked=1)
    yield conn


@seeder("invalid_timestamp")
async def _s_invalid_timestamp(conn):
    await _mem(conn, "bad timestamp", timestamp="not-a-date", locked=1)
    yield conn


@seeder("future_timestamp")
async def _s_future_timestamp(conn):
    # Locked, so `repairable` pins the fixer's `locked = 0` guard like its
    # siblings. The stamp is readable and well-formed -- what is wrong is that it
    # names a moment ahead of the clock, which is what separates this finding
    # from `invalid_timestamp` above.
    await _mem(conn, "stamped ahead of the clock", timestamp="2099-01-01T00:00:00+00:00", locked=1)
    yield conn


@seeder("timestamp_format_drift")
async def _s_timestamp_format_drift(conn):
    await _mem(conn, "utc row", timestamp=UTC_TS)
    await _mem(conn, "aware but locked", timestamp="2026-08-01T09:00:00+09:00", locked=1)
    yield conn


@seeder("episode_timestamp_format_drift")
async def _s_episode_timestamp_format_drift(conn):
    # No locked column on episodes, so both drift classes the fixer reaches are
    # seeded: an aware offset (lossless UTC rewrite) and the pre-2.5.5 backfill
    # fingerprint (naive start_time verbatim-equal to created_at).
    await conn.execute(
        "INSERT INTO episodes (agent_id, summary, start_time) VALUES (?, ?, ?)",
        (AGENT, "recorded with an offset", "2026-08-01T09:00:00+09:00"),
    )
    await conn.execute(
        """INSERT INTO episodes (agent_id, summary, start_time, created_at)
           VALUES (?, ?, ?, ?)""",
        (AGENT, "old naive backfill", "2026-08-01 00:00:00", "2026-08-01 00:00:00"),
    )
    await conn.commit()
    yield conn


@seeder("stale_pending_tasks")
async def _s_stale_pending_tasks(conn):
    await conn.execute(
        """INSERT INTO pending_memory_tasks (task_type, agent_id, payload, created_at)
           VALUES (?, ?, ?, datetime('now', '-2 hours'))""",
        ("store", AGENT, json.dumps({"content": "queued long ago"})),
    )
    await conn.commit()
    yield conn


@seeder("empty_content")
async def _s_empty_content(conn):
    await _mem(conn, "   ", locked=1)
    yield conn


@seeder("invalid_source_type")
async def _s_invalid_source_type(conn):
    # The bare legacy string: schema-conformant, refused by normalize_source.
    await _mem(conn, "legacy bare-string source", source='"claude-code"', locked=1)
    yield conn


@seeder("oversized_profile")
async def _s_oversized_profile(conn):
    await conn.execute(
        "INSERT INTO profiles (agent_id, user_id, content) VALUES (?, ?, ?)",
        (AGENT, "", "y" * (MAX_PROFILE_LENGTH + 10)),
    )
    await conn.commit()
    yield conn


# What each seeded corpus is worth to a repair, stated up front.
#
# Presence of the key is not enough to guard the contract: a declaration that
# counts rows the fixer cannot reach is exactly the defect bug-212 was (attempts
# reported as writes), and it would satisfy an `in issue` assertion perfectly.
# Every seeder above that CAN use a locked row does, so the zeros here are the
# `locked = 0` guard of nine separate fixers, pinned.
EXPECTED_REPAIRABLE = {
    # locked rows: found, reported, and out of every fixer's reach (bug-098)
    "memory_annotation": 0,
    "discord_mention": 0,
    "oversized_content": 0,
    "duplicate_content": 0,
    "invalid_json": 0,
    "invalid_timestamp": 0,
    "future_timestamp": 0,
    "timestamp_format_drift": 0,
    "empty_content": 0,
    "invalid_source_type": 0,
    # no locked concept, or a repair that reaches every row it found
    "embedding_dimension": 1,  # NULLs a wrong-length blob, not authored content
    "nonfinite_embedding": 1,  # ditto: nulls the vector, rewrites no content
    "null_embedding": 1,
    "null_episode_embedding": 1,
    "missing_episode_start_time": 1,  # `episodes` has no locked column
    "episode_timestamp_format_drift": 2,  # ditto: one aware + one backfill copy
    "stale_pending_tasks": 1,  # queue rows, not memories
    "oversized_profile": 1,  # `profiles` has no locked column
    # object-scoped: one rebuild / one CREATE, always attempted
    "fts_integrity": 1,
    "dedup_msg_id_index": 1,
    "schema_objects": 1,
}


def _fix_capable_names() -> set:
    return {c.name for c in checks.HEALTH_CHECKS if c.fix_capable}


# ---------------------------------------------------------------------------
# The meta-test (the point of the whole contract)
# ---------------------------------------------------------------------------


def test_every_fix_capable_check_has_a_seeder():
    """A new fix_capable check cannot be added without being exercised here.

    This is the enforcement the contract rests on. Without it the declaration
    test below would only cover whatever happened to be listed, and a check that
    forgot ``repairable`` would be exempted by the very omission — one
    implementation on paper, opt-in in practice.
    """
    assert set(SEEDERS) == _fix_capable_names()
    assert set(EXPECTED_REPAIRABLE) == _fix_capable_names()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SEEDERS))
@pytest.mark.parametrize("fix", [False, True])
async def test_fix_capable_check_declares_repairable(db, fake_embedding_client, name, fix):
    """Every issue a fix_capable check emits carries ``repairable`` — under both
    values of ``fix``, because a verdict that depends on how the check was called
    is not a verdict about the data."""
    async with SEEDERS[name](db) as conn:
        issues, _ = await checks.run_health_checks(conn, agent_id=AGENT, fix=fix, checks=[name])

    assert issues, f"seeder for {name} did not make the check fire"
    for issue in issues:
        assert "repairable" in issue, f"{name} emitted {issue['type']} without repairable"
        assert issue.get("repairable") is None or isinstance(issue["repairable"], int)
        assert "repairable_undeclared" not in issue
        assert issue["repairable"] == EXPECTED_REPAIRABLE[name], (
            f"{name} declared {issue['repairable']} rows repairable on a corpus "
            f"worth {EXPECTED_REPAIRABLE[name]} — a count the fixer cannot honour "
            f"is the bug-212 defect wearing the contract's name"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SEEDERS))
async def test_repairable_does_not_depend_on_the_fix_argument(db, fake_embedding_client, name):
    """Property 1 of the contract, measured rather than asserted in prose."""
    async with SEEDERS[name](db) as conn:
        dry, _ = await checks.run_health_checks(conn, agent_id=AGENT, fix=False, checks=[name])
        dry_values = [i.get("repairable") for i in dry]

    # A fresh seed: a fix run consumes the rows, so the comparison has to be made
    # against the same starting state, not against the leftovers of the dry run.
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()

    async with SEEDERS[name](db) as conn:
        wet, _ = await checks.run_health_checks(conn, agent_id=AGENT, fix=True, checks=[name])
        wet_values = [i.get("repairable") for i in wet]

    assert dry_values == wet_values


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrepairable_warn_is_demoted_and_marked(db):
    """The bug-205 class, now generic: a warn nothing can act on stops gating."""
    await _mem(db, "legacy bare-string source", source='"claude-code"', locked=1)

    issues, summary = await checks.run_health_checks(
        db, agent_id=AGENT, fix=False, checks=["invalid_source_type"]
    )

    (issue,) = issues
    assert issue["repairable"] == 0
    assert issue["severity"] == "info"
    assert issue["needs_human_review"] is True
    assert "hint" in issue
    assert summary["warn"] == 0
    assert checks.health_status(summary) == "healthy"


@pytest.mark.asyncio
async def test_repairable_warn_still_gates(db):
    """The other direction. An unlocked row is actionable, so the gate stays."""
    await _mem(db, "legacy bare-string source", source='{"type":"assistant"}', locked=0)

    issues, summary = await checks.run_health_checks(
        db, agent_id=AGENT, fix=False, checks=["invalid_source_type"]
    )

    (issue,) = issues
    assert issue["repairable"] == 1
    assert issue["severity"] == "warn"
    assert "needs_human_review" not in issue
    assert checks.health_status(summary) == "degraded"


@pytest.mark.asyncio
async def test_critical_is_marked_but_never_de_escalated(db):
    """A broken read contract is not made healthy by being hard to repair.

    ``severity`` describes reads, ``repairable`` describes repairs. Folding one
    into the other would report a DB whose FTS index is corrupt as ``healthy``
    for the sole reason that nothing could be done about it.
    """
    issue = {"type": "fts_integrity_failure", "severity": "critical", "repairable": 0}
    checks._apply_repairable_policy(issue, checks.Check("fts_integrity", "critical", True, None))

    assert issue["severity"] == "critical"
    assert issue["needs_human_review"] is True
    assert "hint" in issue


def test_none_is_not_zero():
    """An undetermined repair set must not de-escalate (the classification cap)."""
    issue = {"type": "invalid_source_type", "severity": "warn", "repairable": None}
    checks._apply_repairable_policy(issue, checks.Check("invalid_source_type", "warn", True, None))

    assert issue["severity"] == "warn"
    assert "needs_human_review" not in issue


def test_a_missing_declaration_is_surfaced_and_never_de_escalates():
    """The fallback for a runner that forgot: no demotion, and visible where the
    operator is already looking rather than only in a log line."""
    issue = {"type": "whatever", "severity": "warn"}
    checks._apply_repairable_policy(issue, checks.Check("some_check", "warn", True, None))

    assert issue["severity"] == "warn"
    assert issue["repairable_undeclared"] is True
    assert "needs_human_review" not in issue


@pytest.mark.asyncio
async def test_report_only_checks_are_left_alone(db):
    """The policy applies to fix_capable checks only — a report-only check has no
    repair whose reach could be zero, so it must not be asked to declare one."""
    await _mem(db, "an unattributed memory", source="{}")

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=False, checks=["anonymous_source"]
    )

    (issue,) = issues
    assert "repairable" not in issue
    assert "repairable_undeclared" not in issue


# ---------------------------------------------------------------------------
# The one declaration the corpus-level tests above cannot pin
#
# Every other check's `repairable` is a row count the seeded corpus can express.
# The re-embed bound is different on two axes the corpus cannot reach: a cap in
# the thousands (a seeder would have to insert REEMBED_ROW_CAP + 1 rows to feel
# it) and a configuration in which the repair does not run at all. Both were
# measured surviving a mutation while the rest of this file stayed green, which
# is the only reason to know they needed their own test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "null_count,expected",
    [(1, 1), (checks.REEMBED_ROW_CAP - 1, checks.REEMBED_ROW_CAP - 1),
     (checks.REEMBED_ROW_CAP, checks.REEMBED_ROW_CAP),
     (checks.REEMBED_ROW_CAP * 3, checks.REEMBED_ROW_CAP)],
)
def test_reembeddable_never_claims_more_than_one_run_repairs(monkeypatch, null_count, expected):
    """A backlog is not a repair. `_reembed_null_rows` takes REEMBED_ROW_CAP rows
    per run, so a NULL corpus several times that size is one cap of available
    action and the rest of next time — declaring the backlog would describe work
    this run will not do, which is the same overstatement as counting locked
    rows. Parametrised off the constant rather than off its value: the cap is a
    corpus-scale setting now, and a test that spelled the number would pin a
    default while claiming to pin a contract."""
    monkeypatch.setattr(vector, "_embedding_client", object())

    assert checks._reembeddable(null_count) == expected


def test_reembeddable_is_zero_when_the_repair_cannot_run(monkeypatch):
    """Both configurations where a NULL count is a steady state, not a backlog.

    Without them the check reports an action that no fix run can take: under
    ``mode=none`` there is nothing to embed with, and under bug-182's remote/no-
    blob configuration a memory row is NULL *by policy* — the case that used to
    read as a dead pipeline forever.
    """
    monkeypatch.setattr(vector, "_embedding_client", None)
    assert checks._reembeddable(42) == 0

    monkeypatch.setattr(vector, "_embedding_client", object())
    assert checks._reembeddable(42, blobs_expected=False) == 0


@pytest.mark.asyncio
async def test_null_embedding_without_a_client_offers_no_action(db, monkeypatch):
    """The same property where an operator reads it."""
    monkeypatch.setattr(vector, "_embedding_client", None)
    await _mem(db, "no blob and no way to make one")

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=False, checks=["null_embedding"]
    )

    (issue,) = issues
    assert issue["repairable"] == 0
    assert issue["severity"] == "info"  # mode=none: NULL is the expected state
    assert issue["needs_human_review"] is True


# ---------------------------------------------------------------------------
# The prerequisite defect the contract was blocked on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normalized_counts_writes_not_attempts(db):
    """``normalized`` used to increment on ``canon != ts`` while the UPDATE's own
    ``AND locked = 0`` wrote nothing — a run that changed no row reported that it
    had normalised every one of them, and a severity resting on that number
    inherited the lie."""
    await _mem(db, "utc row", timestamp=UTC_TS)
    await _mem(db, "aware and locked", timestamp="2026-08-01T09:00:00+09:00", locked=1)

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=True, checks=["timestamp_format_drift"]
    )

    (issue,) = issues
    assert issue["aware"] == 1  # it was seen
    assert issue["repairable"] == 0  # and it cannot be written
    assert issue.get("normalized", 0) == 0  # so nothing was normalised
    assert issue["unfixable_locked"] == 1

    row = await db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE content = 'aware and locked'"
    )
    assert row[0][0] == "2026-08-01T09:00:00+09:00", "a locked row was rewritten"


@pytest.mark.asyncio
async def test_normalized_counts_the_unlocked_row_it_wrote(db):
    """The direction that keeps the assertion above from passing by never counting."""
    await _mem(db, "utc row", timestamp=UTC_TS)
    await _mem(db, "aware and writable", timestamp="2026-08-01T09:00:00+09:00", locked=0)

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=True, checks=["timestamp_format_drift"]
    )

    (issue,) = issues
    assert issue["repairable"] == 1
    assert issue["normalized"] == 1
    assert "unfixable_locked" not in issue

    row = await db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE content = 'aware and writable'"
    )
    assert row[0][0] == "2026-08-01T00:00:00+00:00"
