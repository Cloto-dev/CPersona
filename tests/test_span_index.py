"""The confidence span is answered by a seek, and the bug-237 predicate stays.

`scope_stats.get_span` reads MIN/MAX(timestamp) over the isolation scope on every
recall that returns a row. Two things make that a seek rather than a walk of the
scope, and neither works without the other:

- ``idx_memories_span`` carries the whole isolation predicate plus the aggregated
  column, so the statement never has to read a row body;
- the pair is asked as TWO statements. SQLite's MIN/MAX optimisation applies to a
  single aggregate, so ``MIN(x), MAX(x)`` in one SELECT is a walk over any index.

Measured at 100,000 rows (benchmarks/measurements/results-recall-path-profile.md):
10.6 ms as it shipped, 5.3 ms with the index alone, 15.4 ms with the split alone,
0.007 ms with both.

What this file is careful about: the ``datetime(timestamp) IS NOT NULL`` test was
believed to be what made the statement unindexable, and removing it was going to
be the price of the index. It is not — the seek stops at the first row satisfying
the whole WHERE, so the function runs on the rows visited rather than on the
scope. The predicate is therefore kept, and the tests below are what say so: they
put unparseable rows exactly where a seek would meet them first.
"""

import numpy as np
import pytest
import pytest_asyncio

from cpersona import scope_stats, session
from cpersona.checks import _EXPECTED_OBJECTS
from cpersona.database import get_db
from cpersona.isolation import isolation_where

AGENT = "agent.span-index"
OTHER = "agent.span-other"
GOOD_LO = "2026-03-01T00:00:00+00:00"
GOOD_HI = "2026-08-01T00:00:00+00:00"
# Sorts before every well-formed stamp ('0' < '2') and after every one ('9' > '2'),
# so a seek from either end meets one of these before it meets a real row.
BAD_LO = "0000-not-a-date"
BAD_HI = "9999-not-a-date"


async def _store(db, timestamp, *, agent=AGENT, project_id="", channel=""):
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, ?, ?, ?, '{}', ?, '2026-03-01 00:00:00', ?)",
        (agent, project_id, channel, f"row {timestamp} {agent} {project_id} {channel}",
         timestamp, np.zeros(4, dtype=np.float32).tobytes()),
    )


@pytest_asyncio.fixture
async def corpus():
    """One scope with a known span, plus a neighbour on the agent axis."""
    session.reset_pauses_for_tests()
    db = await get_db()
    await db.execute("DELETE FROM memories")
    for ts in (GOOD_LO, "2026-05-01T00:00:00+00:00", GOOD_HI):
        await _store(db, ts)
    await _store(db, "2020-01-01T00:00:00+00:00", agent=OTHER)
    await _store(db, "2030-01-01T00:00:00+00:00", agent=OTHER)
    await db.commit()
    scope_stats.clear()
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()
    scope_stats.clear()


def _span_sql(agg):
    iso = isolation_where(agent_id=AGENT, project_id=None, channel="")
    return (
        f"SELECT {agg}(timestamp) FROM memories "
        f"WHERE timestamp != '' AND datetime(timestamp) IS NOT NULL{iso.and_clause}",
        iso.params,
    )


@pytest.mark.asyncio
async def test_boot_creates_the_span_index_and_health_knows_it(corpus):
    """Present after boot, and registered — the second is what lets health restore it."""
    names = [
        r[0] for r in await corpus.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_memories_span'"
        )
    ]
    assert names == ["idx_memories_span"], "boot did not create the span index"
    assert "idx_memories_span" in _EXPECTED_OBJECTS, (
        "an unregistered index is one check_health(fix=True) cannot put back"
    )


@pytest.mark.asyncio
async def test_each_half_is_answered_from_the_span_index(corpus):
    """Both statements, because only one of them being served would still be a walk.

    Asserted on the plan because the answer is identical either way — the index
    changes nothing but which pages are read to produce it.
    """
    for agg in ("MIN", "MAX"):
        sql, params = _span_sql(agg)
        plan = " ".join(
            r[-1] for r in await corpus.execute_fetchall("EXPLAIN QUERY PLAN " + sql, params)
        )
        assert "idx_memories_span" in plan, (agg, plan)
        assert "idx_memories_isolation" not in plan, (agg, plan)
        assert "idx_memories_agent" not in plan, (agg, plan)


@pytest.mark.asyncio
async def test_the_index_is_seeked_not_walked(corpus):
    """The assertion the plan cannot make, and the one this file was missing.

    A plan says which index answered the statement, not how much of it was read —
    and an index whose ordering column sits behind an unconstrained axis is named
    in exactly the same words while being walked end to end. That mistake was made
    here: the first order shipped in this change was
    (agent_id, project_id, channel, timestamp), which seeks only when every axis
    is narrowed, and the reference machine measured 26 ms a statement rather than
    the microseconds the plan implied.

    So the claim is differential and self-calibrating: the same corpus, the same
    statement, the isolation-ordered index against this one, counted in virtual
    machine steps. No absolute constant to go stale, and the wrong order is what
    calibrates "walked".
    """
    # Distinct stamps, so the content (which carries the stamp) stays unique under
    # the composite UNIQUE index and every row is really stored.
    for i in range(2000):
        await _store(corpus, "2026-04-%02dT%02d:%02d:00+00:00"
                     % (1 + i // 1440, (i % 1440) // 60, i % 60))
        if i % 500 == 0:
            await corpus.commit()
    await corpus.commit()

    async def steps():
        counter = {"n": 0}
        scope_stats.clear()
        # Set from inside the connection's own thread: aiosqlite owns it, and a
        # handler installed from the test's thread raises rather than counting.
        def _bump():
            counter["n"] += 1

        await corpus._execute(corpus._conn.set_progress_handler, _bump, 1)
        try:
            await scope_stats.get_span(corpus, AGENT)
        finally:
            await corpus._execute(corpus._conn.set_progress_handler, None, 0)
        return counter["n"]

    seeked = await steps()

    await corpus.execute("DROP INDEX idx_memories_span")
    await corpus.execute(
        "CREATE INDEX idx_memories_span ON memories(agent_id, project_id, channel, timestamp)"
    )
    await corpus.commit()
    walked = await steps()

    await corpus.execute("DROP INDEX idx_memories_span")
    await corpus.execute(
        "CREATE INDEX idx_memories_span ON memories(agent_id, timestamp, project_id, channel)"
    )
    await corpus.commit()

    assert walked > seeked * 5, (
        "the span is not being seeked: the isolation-ordered index cost "
        f"{walked} virtual machine steps against {seeked} for this one, and a seek "
        "should be cheaper than a walk by far more than that"
    )


@pytest.mark.asyncio
async def test_unparseable_rows_at_both_ends_do_not_reach_the_span(corpus):
    """The predicate the index was going to cost us, doing its job under a seek.

    Both rows are inside the scope and sort outside every real stamp, so a seek
    from either end meets one first. bug-237's failure was exactly this shape one
    value further in — an '' that collapsed MIN and took the whole confidence
    block down with it — so the claim is that the span is unmoved, not merely
    that nothing raised.
    """
    before = await scope_stats.get_span(corpus, AGENT)
    assert before == (GOOD_LO, GOOD_HI), before

    for ts in (BAD_LO, BAD_HI, ""):
        await _store(corpus, ts)
    await corpus.commit()
    scope_stats.clear()

    assert await scope_stats.get_span(corpus, AGENT) == (GOOD_LO, GOOD_HI), (
        "an unparseable or empty stamp reached the span"
    )


@pytest.mark.asyncio
async def test_the_span_is_unchanged_when_the_index_is_dropped(corpus):
    """Losing it costs pages read, never an answer — the reason it is registered `warn`.

    A database that has not been reopened since the index was added, or one an
    operator dropped, must produce what an indexed one produces.
    """
    for ts in (BAD_LO, BAD_HI, ""):
        await _store(corpus, ts)
    await corpus.commit()
    scope_stats.clear()
    indexed = await scope_stats.get_span(corpus, AGENT)

    await corpus.execute("DROP INDEX idx_memories_span")
    await corpus.commit()
    scope_stats.clear()
    try:
        assert await scope_stats.get_span(corpus, AGENT) == indexed
        assert indexed == (GOOD_LO, GOOD_HI), indexed
    finally:
        await corpus.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_span"
            " ON memories(agent_id, timestamp, project_id, channel)"
        )
        await corpus.commit()


@pytest.mark.asyncio
async def test_the_span_is_read_over_the_recall_s_own_scope(corpus):
    """bug-107, re-pinned here because the index carries the axes that enforce it.

    A missing column would still return the right answer — SQLite would read the
    row — so this cannot catch a wrong index on its own. It is here so that a
    change to the axes cannot pass on the plan test alone.

    Three values per axis, not two: with only '' and one bucket the gamma union
    ('X' means X *or* the global pool) admits every row, and an implementation
    that ignored the axis would pass.
    """
    await _store(corpus, "2026-01-01T00:00:00+00:00", project_id="proj-x")
    await _store(corpus, "2026-12-01T00:00:00+00:00", channel="c1")
    await corpus.commit()

    # project '' = the global pool only: the proj-x row is out. The c1 row is in,
    # because an unscoped channel read is not a filter (knob2 v2).
    scope_stats.clear()
    assert await scope_stats.get_span(corpus, AGENT, project_id="") == (
        GOOD_LO, "2026-12-01T00:00:00+00:00"
    ), "the global-pool read admitted a project-scoped row"

    # proj-y = proj-y union the global pool, which still excludes proj-x.
    scope_stats.clear()
    assert await scope_stats.get_span(corpus, AGENT, project_id="proj-y") == (
        GOOD_LO, "2026-12-01T00:00:00+00:00"
    ), "a project read admitted another project's row"

    # proj-x = proj-x union the global pool, which is everything here.
    scope_stats.clear()
    assert await scope_stats.get_span(corpus, AGENT, project_id="proj-x") == (
        "2026-01-01T00:00:00+00:00", "2026-12-01T00:00:00+00:00"
    ), "the gamma union dropped either the bucket or the global pool"

    # channel c2 = c2 or the channel-global rows, which excludes the c1 row. The
    # project axis is unset here, so it filters nothing and the proj-x row is in:
    # the two axes are independent, and this is where that shows.
    scope_stats.clear()
    assert await scope_stats.get_span(corpus, AGENT, channel="c2") == (
        "2026-01-01T00:00:00+00:00", GOOD_HI
    ), "a channel read admitted another channel's row, or narrowed the project axis"

    # The neighbouring agent is never in any of them.
    scope_stats.clear()
    assert await scope_stats.get_span(corpus, OTHER) == (
        "2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00"
    ), "the agent axis is not a filter here"
