"""bug-254: the report-only whole-database scan leaves the write lock; the
fix-capable one stays, because its detection must be atomic with its repair.

check_sqlite_integrity (PRAGMA quick_check over the file, fix_capable=False)
used to run inside the ``transaction()`` a fix run holds, so every other writer
waited on a scan that can never write. Same class as bug-072/083, where the
embedding round-trips left the lock.

check_fts_integrity is O(database) too, but it is deliberately NOT moved:
deciding "needs no repair" from an unlocked pre-scan opens a window the fix run
itself falls into — the content-rewriting repairs run before fts_integrity in
registry order, and on a database with missing FTS triggers they CREATE drift
after a clean pre-scan voted no. Detection under the lock repairs whatever is
true at repair time, including damage the run's own writes just introduced.

What must stay true:

- the report-only scan runs, and never while the shared write lock is held;
- it is not repeated under the lock (the pre-fix scan and the bug-059 residual
  re-run are the only two copies, both on the read seam);
- fts corruption present AT REPAIR TIME — however recently created — is
  repaired in the same pass and reported once, with its ``fixed`` marker, and
  the verdict is consistent (no critical count without a naming issue);
- a report-only scan's findings still ride the response, in registry order;
- the repairing checks still run inside the write seam.
"""

import pytest
import pytest_asyncio

from cpersona import checks, database, maintenance_handlers
from cpersona.database import get_db

AGENT = "bug254"


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


def _spy(monkeypatch, name, fake=None):
    """Record (write-lock state, fix flag) for every call of a registry runner.

    The registry holds direct references to the runner functions, so patching
    the module-level name would not be seen by ``run_health_checks`` — the
    ``Check`` object is the seam. ``fake`` replaces the runner outright (for the
    corruption scenarios, which cannot be produced on a healthy file).
    """
    check = next(c for c in checks.HEALTH_CHECKS if c.name == name)
    original = check.runner
    calls: list[dict] = []

    async def spy(db, agent_id, fix, **kwargs):
        calls.append({"locked": database.write_lock().locked(), "fix": fix})
        if fake is not None:
            return await fake(db, agent_id, fix)
        return await original(db, agent_id, fix, **kwargs)

    monkeypatch.setattr(check, "runner", spy)
    return calls


async def _seed_one_memory(db):
    await db.execute(
        f"INSERT INTO memories (agent_id, content, timestamp) VALUES ('{AGENT}', 'bug254 row', '')"
    )
    await db.commit()


@pytest.mark.asyncio
async def test_report_only_scan_never_runs_under_the_write_lock(clean_db, monkeypatch):
    fts = _spy(monkeypatch, "fts_integrity")
    sqlite = _spy(monkeypatch, "sqlite_integrity")
    repairing = _spy(monkeypatch, "empty_content")
    await _seed_one_memory(clean_db)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    # Guard against a vacuous pass: "never under the lock" means nothing if the
    # scan stopped running at all.
    assert sqlite, "sqlite_integrity did not run"
    assert not any(call["locked"] for call in sqlite), (
        f"sqlite_integrity scanned the database while the shared write lock was held: {sqlite}"
    )
    # fts_integrity stays with the lock in a fix run — detection atomic with
    # repair — and its locked call carries fix=True.
    assert any(call["locked"] and call["fix"] for call in fts), (
        f"fts_integrity no longer runs inside the fix transaction: {fts}"
    )
    # The write seam did not disappear: a repairing check still runs inside
    # transaction(), which is where its writes must be serialised.
    assert any(call["locked"] and call["fix"] for call in repairing), (
        f"no fix-capable check ran under the write lock (bug-042/043 seam lost): {repairing}"
    )


@pytest.mark.asyncio
async def test_clean_scan_patterns_are_pinned(clean_db, monkeypatch):
    """sqlite: pre-fix scan + residual re-run, both unlocked — the locked third
    copy is gone. fts: one locked fix-run call + the unlocked residual."""
    fts = _spy(monkeypatch, "fts_integrity")
    sqlite = _spy(monkeypatch, "sqlite_integrity")
    await _seed_one_memory(clean_db)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    assert [(c["locked"], c["fix"]) for c in sqlite] == [(False, False), (False, False)], (
        f"sqlite_integrity scan pattern changed: {sqlite}"
    )
    assert [(c["locked"], c["fix"]) for c in fts] == [(True, True), (False, False)], (
        f"fts_integrity scan pattern changed: {fts}"
    )


@pytest.mark.asyncio
async def test_read_only_run_keeps_the_scans_on_the_read_seam(clean_db, monkeypatch):
    fts = _spy(monkeypatch, "fts_integrity")
    sqlite = _spy(monkeypatch, "sqlite_integrity")
    await _seed_one_memory(clean_db)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    assert [(c["locked"], c["fix"]) for c in fts] == [(False, False)]
    assert [(c["locked"], c["fix"]) for c in sqlite] == [(False, False)]


@pytest.mark.asyncio
async def test_fts_corruption_at_repair_time_is_repaired_in_the_same_pass(clean_db, monkeypatch):
    """Corruption that exists when the locked run reaches fts_integrity — however
    recently created, including by the fix run's own content rewrites — is
    repaired then and there, reported once with its ``fixed`` marker, and the
    verdict is consistent (no critical count with an empty issue list). This is
    the window an unlocked pre-scan vote would have left open."""
    state = {"corrupt": True}

    async def fake_fts(db, agent_id, fix):
        if not state["corrupt"]:
            return []
        issue = {
            "type": "fts_integrity_failure",
            "table": "memories",
            "severity": "critical",
            "repairable": 1,
        }
        if fix:
            issue["fixed"] = True
            state["corrupt"] = False
        return [issue]

    calls = _spy(monkeypatch, "fts_integrity", fake=fake_fts)
    await _seed_one_memory(clean_db)

    result = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    assert (calls[0]["locked"], calls[0]["fix"]) == (True, True), (
        f"the repair pass must find the corruption under the lock: {calls}"
    )
    reported = [i for i in result["issues"] if i["check"] == "fts_integrity"]
    assert len(reported) == 1, f"one corruption reported {len(reported)} times: {reported}"
    assert reported[0]["fixed"] is True
    # The residual re-run saw the repaired index, so the verdict is clean AND
    # consistent with the issue list — not critical-with-no-issue.
    assert result["severity_summary"]["critical"] == 0, (
        f"a repaired corruption still counts as critical: {result['severity_summary']}"
    )
    assert result["status"] != "unhealthy"


@pytest.mark.asyncio
async def test_report_only_scan_findings_survive_in_registry_order(clean_db, monkeypatch):
    """A finding produced outside the lock is merged back where a single run would put it."""

    async def fake_quick_check(db, agent_id, fix):
        return [
            {
                "type": "sqlite_integrity_failure",
                "detail": ["*** in database main ***"],
                "severity": "critical",
            }
        ]

    calls = _spy(monkeypatch, "sqlite_integrity", fake=fake_quick_check)
    # A NULL-embedding memory (null_embedding sorts BEFORE sqlite_integrity) with
    # no profile row (missing_profile sorts AFTER it), so the merged position is
    # observable rather than trivially first or last.
    await _seed_one_memory(clean_db)

    result = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    names = [i["check"] for i in result["issues"]]
    assert "sqlite_integrity" in names, f"the unlocked scan's finding was dropped: {names}"
    assert names == sorted(names, key=checks.HEALTH_CHECK_NAMES.index), (
        f"issues are no longer in registry order: {names}"
    )
    assert names.index("null_embedding") < names.index("sqlite_integrity") < names.index(
        "missing_profile"
    ), f"the merged finding landed in the wrong place: {names}"
    assert not any(call["locked"] for call in calls)
    # The residual verdict still sees it (the fake reports the same failure on the
    # post-commit re-run), so a critical finding still gates.
    assert result["status"] == "unhealthy"
    assert result["severity_summary"]["critical"] >= 1


@pytest.mark.asyncio
async def test_scan_only_selection_does_not_widen_to_the_whole_registry(clean_db, monkeypatch):
    """checks=['sqlite_integrity'] leaves the locked phase with nothing to run.

    An empty ``checks`` list means "every check" to run_health_checks, so the
    remainder must not be handed to it as one — that would run the whole
    registry, with fix=True, for a caller who asked for one report-only scan.
    """
    sqlite = _spy(monkeypatch, "sqlite_integrity")
    other = _spy(monkeypatch, "empty_content")
    await _seed_one_memory(clean_db)

    result = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["sqlite_integrity"]
    )

    assert result["checks_run"] == ["sqlite_integrity"]
    assert not other, f"the empty locked-checks list ran the whole registry: {other}"
    assert [(c["locked"], c["fix"]) for c in sqlite] == [(False, False), (False, False)]
