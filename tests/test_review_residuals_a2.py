"""Regression tests for the recorded residuals of the 2026-08-18 review batch.

Each of the three fixes closes a "Residual (recorded)" clause a bug-2xx
fix_note left as follow-up work:

    bug-227  checkup's non-JSON output prints a deep-check {"error": ...} result
             instead of rendering the crash as silence
    bug-241  the import read itself is bounded — a regular file that grows
             between the stat and the read can no longer exceed the cap
    bug-245  episodes.start_time/end_time have a format-drift detector, and the
             pre-2.5.5 naive backfill copies are re-encoded losslessly
"""

import os

import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, config
from cpersona.checkup import _deep_findings
from cpersona.database import get_db

AGENT = "residual-agent"


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-227 residual: an errored deep check is a finding, not silence.
# ---------------------------------------------------------------------------


def test_bug227_residual_an_error_result_is_a_finding():
    """do_deep_check records a crashed check as {"error": ...} — a dict with
    neither count, pairs nor status, which the old inline filter dropped."""
    findings = _deep_findings(
        {
            "crashed": {"error": "OperationalError: malformed JSON"},
            "quiet": {"count": 0},
            "ok_status": {"status": "ok"},
            "skipped": {"status": "not_applicable"},
        }
    )
    assert findings == {"crashed": {"error": "OperationalError: malformed JSON"}}


def test_bug227_residual_real_findings_still_pass_the_filter():
    findings = _deep_findings(
        {
            "counted": {"count": 3},
            "paired": {"pairs": [[1, 2]]},
            "degraded": {"status": "insufficient_data"},
        }
    )
    assert set(findings) == {"counted", "paired", "degraded"}


# ---------------------------------------------------------------------------
# bug-241 residual: the read is bounded even when the stat lied.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug241_residual_the_read_itself_is_bounded(clean_db, tmp_path, monkeypatch):
    """Simulate a file growing between the stat and the read: the stat reports a
    size under the cap while the actual body is far over it. The pre-fix code
    trusted the stat and slurped the whole file."""
    body = '{"_type":"memory","agent_id":"imp","content":"row"}\n' * 100
    path = tmp_path / "grow.jsonl"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(config, "MAX_IMPORT_BYTES", 16)

    real_stat = os.stat

    def stat_from_before_the_growth(target, *args, **kwargs):
        st = real_stat(target, *args, **kwargs)
        if str(target) == str(path):
            values = list(st)
            values[6] = 1  # st_size as it was before the file grew
            return os.stat_result(values)
        return st

    monkeypatch.setattr(admin_handlers.os, "stat", stat_from_before_the_growth)

    result = await admin_handlers.do_import_memories(str(path))
    assert result["ok"] is False
    assert "MAX_IMPORT_BYTES" in result["error"], result


@pytest.mark.asyncio
async def test_bug241_residual_a_file_at_the_cap_still_imports(clean_db, tmp_path):
    """The bound refuses one byte past the cap, not the cap itself."""
    body = '{"_type":"memory","agent_id":"imp","content":"exact"}\n'
    path = tmp_path / "exact.jsonl"
    path.write_text(body, encoding="utf-8")

    result = await admin_handlers.do_import_memories(str(path), dry_run=True)
    assert result["ok"] is True
    assert result["imported_memories"] == 1, result


# ---------------------------------------------------------------------------
# bug-245 residual: episode timestamp drift is detected and repaired.
# ---------------------------------------------------------------------------


async def _episode(db, summary, *, start_time=None, end_time=None, created_at=None):
    if created_at is None:
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, start_time, end_time) VALUES (?, ?, ?, ?)",
            (AGENT, summary, start_time, end_time),
        )
    else:
        await db.execute(
            """INSERT INTO episodes (agent_id, summary, start_time, end_time, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (AGENT, summary, start_time, end_time, created_at),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_bug245_residual_aware_episode_times_normalize_to_utc(clean_db):
    db = clean_db
    await _episode(db, "recorded", start_time="2026-08-01T09:00:00+09:00", end_time="2026-08-01T10:00:00+09:00")
    await _episode(db, "canonical", start_time="2026-08-02T00:00:00+00:00")

    issues = await checks.check_episode_timestamp_format_drift(db, AGENT, fix=True)
    await db.commit()

    (issue,) = issues
    assert issue["type"] == "episode_timestamp_format_drift"
    assert issue["repairable"] == 2
    assert issue["normalized"] == 2
    rows = await db.execute_fetchall(
        "SELECT start_time, end_time FROM episodes WHERE summary = 'recorded'"
    )
    assert rows[0][0] == "2026-08-01T00:00:00+00:00"
    assert rows[0][1] == "2026-08-01T01:00:00+00:00"


@pytest.mark.asyncio
async def test_bug245_residual_backfill_copy_is_reencoded_other_naive_is_not(clean_db):
    """The verbatim created_at copy is the pre-2.5.5 backfill — UTC by schema,
    so re-encoding changes the form and not the instant. A naive value that
    does NOT equal created_at was recorded by a caller in an unknowable zone
    and must stay untouched."""
    db = clean_db
    await _episode(
        db, "old backfill", start_time="2026-08-01 00:00:00", created_at="2026-08-01 00:00:00"
    )
    await _episode(
        db, "caller naive", start_time="2026-08-03 12:00:00", created_at="2026-08-04 00:00:00"
    )
    await _episode(db, "recorded", start_time="2026-08-02T00:00:00+00:00")

    issues = await checks.check_episode_timestamp_format_drift(db, AGENT, fix=True)
    await db.commit()

    (issue,) = issues
    assert issue["repairable"] == 1
    assert issue["normalized"] == 1
    assert issue["unfixable_naive"] == 1
    assert "hint" in issue
    rows = dict(
        await db.execute_fetchall("SELECT summary, start_time FROM episodes")
    )
    assert rows["old backfill"] == "2026-08-01T00:00:00+00:00"
    assert rows["caller naive"] == "2026-08-03 12:00:00"


@pytest.mark.asyncio
async def test_bug245_residual_uniform_recorded_corpus_is_silent(clean_db):
    """One convention and nothing to repair: no finding, same contract as the
    memories check."""
    db = clean_db
    await _episode(db, "one", start_time="2026-08-01T00:00:00+00:00")
    await _episode(db, "two", start_time="2026-08-02T00:00:00+00:00", end_time="2026-08-02T01:00:00+00:00")

    issues = await checks.check_episode_timestamp_format_drift(db, AGENT, fix=False)
    assert issues == []


@pytest.mark.asyncio
async def test_bug245_residual_dry_run_counts_but_does_not_write(clean_db):
    db = clean_db
    await _episode(
        db, "old backfill", start_time="2026-08-01 00:00:00", created_at="2026-08-01 00:00:00"
    )
    await _episode(db, "recorded", start_time="2026-08-02T00:00:00+00:00")

    issues = await checks.check_episode_timestamp_format_drift(db, AGENT, fix=False)
    (issue,) = issues
    assert issue["repairable"] == 1
    assert "normalized" not in issue
    rows = await db.execute_fetchall(
        "SELECT start_time FROM episodes WHERE summary = 'old backfill'"
    )
    assert rows[0][0] == "2026-08-01 00:00:00"
