"""What the health surface says about the coarse index, and when it says nothing.

The coarse index is read by two settings only, and only for records past the
scan window. Without a usable file both fall back to the live store, which
reads every far record's vector on every recall: the answers do not change,
the cost grows with the store, and nothing complains. The check exists so a
monitor hears about that before a user feels it.

So the quiet cases matter as much as the loud ones: a setting that is off, a
store that fits the window, and a far stratum too small to cost anything must
all say nothing, or the line becomes one nobody reads.
"""

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import acl, checks, coarse_index, config, findings, vector
from cpersona.database import get_db

AGENT = "coarsehealth.agent"
OTHER = "coarsehealth.other"
DIM = 8
WINDOW = 10


def _blob(seed: int, dim: int = DIM) -> bytes:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32).tobytes()


async def _insert(db, count, *, start=0, dim=DIM, agent=AGENT):
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, '{}', ?, ?, ?)",
        [
            (
                agent, f"row {agent} {start + n}", "2026-03-01T00:00:00+00:00",
                f"2026-03-01 {(start + n) // 3600 % 24:02d}:{(start + n) // 60 % 60:02d}"
                f":{(start + n) % 60:02d}",
                _blob(start + n, dim),
            )
            for n in range(count)
        ],
    )
    await db.commit()


def _clean_index():
    path = coarse_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest_asyncio.fixture
async def db(monkeypatch):
    # A window of ten makes "past the window" reachable with a few rows; the far
    # seats begin where the window ends unless VECTOR_REACH reaches further.
    monkeypatch.setattr(vector, "MAX_MEMORIES", WINDOW)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 5)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")
    conn = await get_db()
    _clean_index()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    _clean_index()


async def _run(db, agent=AGENT, fix=False):
    return await checks.check_coarse_index(db, agent, fix)


# ── the quiet cases ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_off_say_nothing_however_large_the_store(db, monkeypatch):
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", False)
    await _insert(db, WINDOW + 50)
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_a_store_that_fits_the_window_says_nothing(db):
    await _insert(db, WINDOW)
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_a_far_stratum_too_small_to_cost_anything_says_nothing(db):
    await _insert(db, WINDOW + checks.INDEX_MATTERS_ROWS - 1)
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_the_window_is_per_agent_not_per_store(db):
    """A recall reads one agent's records: two agents that each fit the window
    put nothing past it, however many rows the store holds in total."""
    await _insert(db, WINDOW, agent=AGENT)
    await _insert(db, WINDOW, start=1000, agent=OTHER)
    assert await _run(db, agent="") == []


@pytest.mark.asyncio
async def test_an_agent_scoped_call_counts_only_its_agent(db):
    await _insert(db, WINDOW + 20, agent=OTHER)
    await _insert(db, 3, start=1000, agent=AGENT)
    assert await _run(db, agent=AGENT) == []
    assert [i["type"] for i in await _run(db, agent=OTHER)] == ["coarse_index_absent"]


# ── where the window starts, per setting ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_far_seats_begin_past_vector_reach(db, monkeypatch):
    """With VECTOR_REACH beyond the store, the far seats read nothing past it."""
    monkeypatch.setattr(vector, "VECTOR_REACH", WINDOW + 100)
    await _insert(db, WINDOW + 20)
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_the_cue_remainder_begins_at_the_window_whatever_vector_reach_is(db, monkeypatch):
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", False)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on")
    monkeypatch.setattr(vector, "VECTOR_REACH", WINDOW + 100)
    await _insert(db, WINDOW + 20)
    issues = await _run(db)
    assert [i["type"] for i in issues] == ["coarse_index_absent"]
    assert issues[0]["rows_past_window"] == 20


# ── the loud cases ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_file_under_a_setting_that_reads_it_is_a_stamped_warning_with_its_price(db):
    await _insert(db, WINDOW + 20)
    issues = await _run(db)
    assert [i["type"] for i in issues] == ["coarse_index_absent"]
    issue = issues[0]
    assert issue["severity"] == "warn"
    assert issue["rows_past_window"] == 20
    assert issue["embedding_bytes_read_per_recall"] == 20 * DIM * 4
    assert issue["repairable"] == 1
    assert "check_health(checks=['coarse_index'], fix=true)" in issue["hint"]


@pytest.mark.asyncio
async def test_an_unreadable_file_is_unusable(db):
    await _insert(db, WINDOW + 20)
    with open(coarse_index.index_path("memories"), "wb") as fh:
        fh.write(b"not an index")
    issues = await _run(db)
    assert [(i["type"], i["severity"]) for i in issues] == [("coarse_index_unusable", "warn")]


@pytest.mark.asyncio
async def test_rows_the_index_holds_and_the_store_lost_are_reported(db):
    await _insert(db, WINDOW + 20)
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    await db.execute("DELETE FROM memories WHERE id = (SELECT MIN(id) FROM memories)")
    await db.commit()
    issues = await _run(db)
    assert [(i["type"], i["severity"]) for i in issues] == [("coarse_index_rows_missing", "warn")]
    assert issues[0]["indexed_rows"] - issues[0]["rows_still_present"] == 1


@pytest.mark.asyncio
async def test_a_grown_tail_is_an_observation_at_the_registry_default(db):
    await _insert(db, WINDOW + 20)
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    await _insert(db, 10, start=500)  # 10 > 30 * INDEX_TAIL_RATIO
    issues = await _run(db)
    assert [i["type"] for i in issues] == ["coarse_index_tail_grown"]
    assert "severity" not in issues[0], "the tail is the registry default (info), not a stamped defect"


@pytest.mark.asyncio
async def test_a_tail_inside_the_ratio_says_nothing(db):
    await _insert(db, WINDOW + 20)
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    await _insert(db, 6, start=500)  # 6 == 30 * INDEX_TAIL_RATIO: not past it
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_a_corpus_of_another_width_is_drift_and_one_of_two_widths_is_not_repairable(db):
    await _insert(db, WINDOW + 20)
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    await _insert(db, 2, start=500, dim=DIM * 2)
    issues = await _run(db)
    assert [(i["type"], i["severity"]) for i in issues] == [("coarse_index_dimension_drift", "warn")]
    assert issues[0]["repairable"] == 0, "the builder declines while two widths coexist"
    fixed = await _run(db, fix=True)
    assert "rebuilt" not in fixed[0], "no build is attempted when the builder would decline"


# ── fix ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix_builds_the_file_and_the_next_run_is_quiet(db):
    await _insert(db, WINDOW + 20)
    issues = await _run(db, fix=True)
    assert [i["type"] for i in issues] == ["coarse_index_absent"]
    assert issues[0]["rebuilt"] is True
    assert issues[0]["build"]["count"] == WINDOW + 20
    assert os.path.exists(coarse_index.index_path("memories"))
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_fix_replaces_an_unusable_file(db):
    await _insert(db, WINDOW + 20)
    with open(coarse_index.index_path("memories"), "wb") as fh:
        fh.write(b"not an index")
    issues = await _run(db, fix=True)
    assert issues[0]["type"] == "coarse_index_unusable" and issues[0]["rebuilt"] is True
    assert coarse_index.load_coarse_index("memories") is not None
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_a_report_only_run_writes_nothing(db):
    await _insert(db, WINDOW + 20)
    issues = await _run(db, fix=False)
    assert "rebuilt" not in issues[0]
    assert not os.path.exists(coarse_index.index_path("memories"))


# ── registry, guard, findings ────────────────────────────────────────────────


def test_the_repair_is_declared_cross_agent_and_the_guard_demands_every_agent():
    assert "coarse_index" in checks.CROSS_AGENT_FIX_CHECKS
    demands = acl._health_demands({"agent_id": AGENT, "fix": True, "checks": ["coarse_index"]})
    assert (acl.WILDCARD, acl.PERM_WRITE) in demands
    report_only = acl._health_demands({"agent_id": AGENT, "fix": False, "checks": ["coarse_index"]})
    assert (acl.WILDCARD, acl.PERM_WRITE) not in report_only


@pytest.mark.asyncio
async def test_stamped_states_deliver_as_degraded_and_the_tail_as_the_check(db):
    await _insert(db, WINDOW + 20)
    issues, _ = await checks.run_health_checks(db, agent_id=AGENT, fix=False, checks=["coarse_index"])
    assert [findings.finding_kind(i) for i in issues] == ["coarse_index_degraded"]
    assert findings.severity_for_kind("coarse_index_degraded") == "warn"
    assert findings.severity_for_kind("coarse_index") == "info"
