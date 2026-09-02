"""What the health surface says about the contiguous index, and what it stays quiet about.

Every way that index stops being used is silent by construction — a deleted
file, a file that no longer holds together, a corpus whose embedding width
moved. In all of them the answers stay correct and only the latency changes, so
nothing downstream has a reason to complain. That is exactly why it needs a line
here: an index that died a week ago otherwise reads as "somehow not faster".

The hard part is not reporting. It is the difference between the states:

- **no index** is the ordinary state before the first build and after a
  deletion. Reporting it as a defect would train a reader to ignore the check,
  which costs more than the silence it replaced.
- **an index that exists and is not being used** has to be heard.

So the tests below assert the quiet cases as carefully as the loud ones.
"""

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import checks, findings, vector_index
from cpersona.database import get_db

AGENT = "idxhealth.agent"
DIM = 8


def _blob(seed: int, dim: int = DIM) -> bytes:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32).tobytes()


async def _insert(db, count, *, start=0, dim=DIM):
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, '{}', ?, ?, ?)",
        [
            (
                AGENT, f"row {start + n}", "2026-03-01T00:00:00+00:00",
                f"2026-03-01 {(start + n) // 3600 % 24:02d}:{(start + n) // 60 % 60:02d}"
                f":{(start + n) % 60:02d}",
                _blob(start + n, dim),
            )
            for n in range(count)
        ],
    )
    await db.commit()


def _clean_index():
    path = vector_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    _clean_index()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    _clean_index()


async def _run(db):
    return await checks.check_vector_index(db, AGENT, False)


@pytest.mark.asyncio
async def test_no_index_on_a_small_corpus_says_nothing(db):
    """The ordinary state. A check that fires here is a check nobody reads."""
    await _insert(db, 20)
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_no_index_on_a_corpus_large_enough_to_matter_is_an_observation(db):
    await _insert(db, checks.INDEX_MATTERS_ROWS)
    issues = await _run(db)
    assert [i["type"] for i in issues] == ["vector_index_absent"]
    assert "severity" not in issues[0], "absence is the registry default (info), not a stamped defect"
    assert issues[0]["memories_with_local_embedding"] == checks.INDEX_MATTERS_ROWS


@pytest.mark.asyncio
async def test_a_healthy_index_says_nothing(db):
    await _insert(db, 50)
    assert (await vector_index.build_index(db, "memories"))["built"]
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_an_index_that_does_not_hold_together_is_a_warning(db):
    await _insert(db, 50)
    await vector_index.build_index(db, "memories")
    path = vector_index.index_path("memories")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 8)

    issues = await _run(db)
    assert [i["type"] for i in issues] == ["vector_index_unusable"]
    assert issues[0]["severity"] == "warn"
    assert "rebuilt, never repaired" in issues[0]["hint"]


@pytest.mark.asyncio
async def test_a_corpus_whose_width_moved_is_a_warning(db):
    """What a model swap looks like from here: the file is intact and unusable."""
    await _insert(db, 50)
    await vector_index.build_index(db, "memories")
    await _insert(db, 1, start=900, dim=DIM * 2)

    issues = await _run(db)
    assert [i["type"] for i in issues] == ["vector_index_dimension_drift"]
    assert issues[0]["severity"] == "warn"
    assert issues[0]["index_dim"] == DIM
    assert issues[0]["corpus_widths_bytes"] == [DIM * 4, DIM * 8]


@pytest.mark.asyncio
async def test_a_tail_past_the_rebuild_threshold_is_an_observation(db):
    """The one staleness this design admits: never a wrong answer, only a longer
    exact read on every query."""
    await _insert(db, 50)
    await vector_index.build_index(db, "memories")
    await _insert(db, 20, start=500)  # 40% of the indexed rows

    issues = await _run(db)
    assert [i["type"] for i in issues] == ["vector_index_tail_grown"]
    assert "severity" not in issues[0], "a rebuild being due is not a defect"
    assert issues[0]["indexed_rows"] == 50 and issues[0]["rows_past_watermark"] == 20


@pytest.mark.asyncio
async def test_a_short_tail_stays_quiet(db):
    await _insert(db, 50)
    await vector_index.build_index(db, "memories")
    await _insert(db, 5, start=500)  # 10%, under the threshold
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_the_findings_surface_separates_the_two_severities(db):
    """The kinds have to differ, or a reader routing on severity sees `info` for
    an index that stopped working."""
    await _insert(db, 50)
    await vector_index.build_index(db, "memories")
    path = vector_index.index_path("memories")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 8)
    degraded = (await _run(db))[0] | {"check": "vector_index"}

    await _insert(db, 20, start=500)
    _clean_index()
    quiet = None
    for issue in await _run(db):
        quiet = issue | {"check": "vector_index"}

    assert findings.finding_kind(degraded) == "vector_index_degraded"
    assert findings.severity_for_kind("vector_index_degraded") == "warn"
    if quiet is not None:
        assert findings.finding_kind(quiet) == "vector_index"
        assert findings.severity_for_kind("vector_index") == "info"


@pytest.mark.asyncio
async def test_the_check_is_registered_and_report_only(db):
    check = next(c for c in checks.HEALTH_CHECKS if c.name == "vector_index")
    assert check.base_severity == "info"
    assert check.fix_capable is False, (
        "an index is a derived artifact: the repair is to delete it, which is not this "
        "check's business and would need the repairable contract if it were"
    )


# --------------------------------------------------------------------------------------
# The episode table has its own index, and its own line in the report.
# --------------------------------------------------------------------------------------


async def _insert_episodes(db, count, *, dim=DIM):
    await db.executemany(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, embedding,"
        " start_time, resolved, created_at) VALUES (?, '', '', ?, '', ?, NULL, 0, ?)",
        [
            (
                AGENT, f"episode {n}", _blob(10_000 + n, dim),
                f"2026-03-02 {n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}",
            )
            for n in range(count)
        ],
    )
    await db.commit()


def _clean_episode_index():
    path = vector_index.index_path("episodes")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest_asyncio.fixture
async def db_with_episodes(db):
    _clean_episode_index()
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield db
    await db.execute("DELETE FROM episodes")
    await db.commit()
    _clean_episode_index()


@pytest.mark.asyncio
async def test_each_table_reports_its_own_absence(db_with_episodes):
    """A memory index that exists says nothing about the episode one."""
    db = db_with_episodes
    await _insert(db, 50)
    await _insert_episodes(db, checks.INDEX_MATTERS_ROWS)
    assert (await vector_index.build_index(db, "memories"))["built"]
    issues = await _run(db)
    assert [(i["type"], i["table"]) for i in issues] == [("vector_index_absent", "episodes")]
    assert issues[0]["episodes_with_local_embedding"] == checks.INDEX_MATTERS_ROWS
    assert "--table episodes" in issues[0]["hint"]


@pytest.mark.asyncio
async def test_both_indexes_healthy_say_nothing(db_with_episodes):
    db = db_with_episodes
    await _insert(db, 50)
    await _insert_episodes(db, 50)
    assert (await vector_index.build_index(db, "memories"))["built"]
    assert (await vector_index.build_index(db, "episodes"))["built"]
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_an_episode_index_that_does_not_hold_together_names_its_table(db_with_episodes):
    db = db_with_episodes
    await _insert_episodes(db, 50)
    assert (await vector_index.build_index(db, "episodes"))["built"]
    path = vector_index.index_path("episodes")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 8)
    issues = await _run(db)
    assert [(i["type"], i["table"], i["severity"]) for i in issues] == [
        ("vector_index_unusable", "episodes", "warn")
    ]
