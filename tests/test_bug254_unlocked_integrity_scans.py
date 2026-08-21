"""bug-254: check_health(fix=True) must not scan the whole database under the write lock.

The two whole-database READ scans — check_fts_integrity (FTS5 'integrity-check'
over both indexes) and check_sqlite_integrity (PRAGMA quick_check over the file)
— used to run inside the ``transaction()`` that a fix run holds, so every other
writer waited on work that writes nothing. Same class as bug-072/083, where the
embedding round-trips left the lock; these tests are the same shape as the
behavioural Gate 3 in test_structural_gates.py, because a static gate cannot see
work reached through the check registry either.

What must stay true after the move:

- the scans run, and never while the shared write lock is held;
- a clean scan is not repeated under the lock (the pre-fix scan and the bug-059
  residual re-run are the only two, both on the read seam);
- a corrupt fix-capable scan is still REPAIRED under the lock, and is reported
  once, with its ``fixed`` marker;
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
async def test_integrity_scans_never_run_under_the_write_lock(clean_db, monkeypatch):
    fts = _spy(monkeypatch, "fts_integrity")
    sqlite = _spy(monkeypatch, "sqlite_integrity")
    repairing = _spy(monkeypatch, "empty_content")
    await _seed_one_memory(clean_db)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    # Guard against a vacuous pass: "never under the lock" means nothing if the
    # scans stopped running at all.
    assert fts, "fts_integrity did not run"
    assert sqlite, "sqlite_integrity did not run"
    assert not any(call["locked"] for call in fts), (
        f"fts_integrity scanned the database while the shared write lock was held: {fts}"
    )
    assert not any(call["locked"] for call in sqlite), (
        f"sqlite_integrity scanned the database while the shared write lock was held: {sqlite}"
    )
    # The write seam did not disappear with them: a repairing check still runs
    # inside transaction(), which is where its writes must be serialised.
    assert any(call["locked"] and call["fix"] for call in repairing), (
        f"no fix-capable check ran under the write lock (bug-042/043 seam lost): {repairing}"
    )


@pytest.mark.asyncio
async def test_clean_scans_run_exactly_twice_and_both_on_the_read_seam(clean_db, monkeypatch):
    """Pre-fix scan + bug-059 residual re-run. The locked third copy is gone."""
    fts = _spy(monkeypatch, "fts_integrity")
    sqlite = _spy(monkeypatch, "sqlite_integrity")
    await _seed_one_memory(clean_db)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    for name, calls in (("fts_integrity", fts), ("sqlite_integrity", sqlite)):
        assert [(c["locked"], c["fix"]) for c in calls] == [(False, False), (False, False)], (
            f"{name} scan pattern changed: {calls}"
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
async def test_corrupt_fix_capable_scan_is_still_repaired_under_the_lock(clean_db, monkeypatch):
    """A corrupt FTS index: detected unlocked, rebuilt under the lock, reported once."""

    async def fake_fts(db, agent_id, fix):
        issue = {
            "type": "fts_integrity_failure",
            "table": "memories",
            "severity": "critical",
            "repairable": 1,
        }
        if fix:
            issue["fixed"] = True
        return [issue]

    calls = _spy(monkeypatch, "fts_integrity", fake=fake_fts)
    await _seed_one_memory(clean_db)

    result = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    assert (calls[0]["locked"], calls[0]["fix"]) == (False, False), (
        f"detection must precede the lock: {calls}"
    )
    assert (calls[1]["locked"], calls[1]["fix"]) == (True, True), (
        f"the rebuild is a write and must be serialised by transaction(): {calls}"
    )
    reported = [i for i in result["issues"] if i["check"] == "fts_integrity"]
    assert len(reported) == 1, f"one corruption reported {len(reported)} times: {reported}"
    assert reported[0]["fixed"] is True, (
        "the response kept the detection copy instead of the repaired one (the fix-only "
        f"fields would be lost): {reported[0]}"
    )


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
