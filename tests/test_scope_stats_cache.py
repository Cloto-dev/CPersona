"""Tests for the per-scope aggregate cache (``cpersona/scope_stats.py``).

Uncached, every recall re-derives full scans of its isolation scope before it can
answer: a ``COUNT(*)`` over ``memories`` and over ``episodes`` for the pool the quality
gate governs, plus — under ``CONFIDENCE_ENABLED`` — ``MIN/MAX(timestamp)`` over
``memories`` for the confidence span. Their answers change only when a row is written,
so a read-heavy agent pays for the same scans on every call. Each test states what the
code does WITHOUT the cache, or with the specific part of it removed, so deleting that
part turns an assertion red instead of merely making a run slower.

Three mechanisms are pinned separately, because they fail in different directions:

- the write generation, which invalidates EXACTLY on a write this process makes;
- the ``scope_stats_neutral`` exemption at the recall-count bump, without which the
  confidence-on path invalidates on every recall the entry it just filled — the
  configuration the cache exists for;
- the TTL, which is the only thing bounding staleness from a writer this process
  cannot see. Its documented cost (a stale value IS served inside the window) is
  asserted here rather than described, so nobody has to take the docstring's word.
"""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest
import pytest_asyncio

from cpersona import database, memory_handlers, scope_stats, session
from cpersona.database import connection, get_db

AGENT = "agent.scope-stats"
QUERY = "deployment rollback"
SEED_CONTENT = "session {i}: deployment rollback of the billing service"
TTL = 60.0

_SPAN = "SELECT MIN(timestamp), MAX(timestamp) FROM memories"
_COUNTS = ("SELECT COUNT(*) FROM memories", "SELECT COUNT(*) FROM episodes")
# The statements this cache exists to stop re-issuing. Matched on statement text rather
# than by spying on scope_stats itself, so a call site that stops going through the
# helper (and starts scanning again) still counts.
_AGGREGATES = (_SPAN,) + _COUNTS


@pytest.fixture(autouse=True)
def cache_on(monkeypatch):
    """Pin the cache flag and the TTL for the file.

    The claims below are about what the cache does, so they must not silently pass (or
    fail) because the ambient ``CPERSONA_SCOPE_STATS_CACHE`` /
    ``CPERSONA_SCOPE_STATS_TTL_SECONDS`` say otherwise — the suite is run with the
    cache off as part of the equivalence check. The one test that is about the flag
    being off sets it back in its own body, which lands after this.
    """
    monkeypatch.setattr(scope_stats, "SCOPE_STATS_CACHE_ENABLED", True)
    monkeypatch.setattr(scope_stats, "SCOPE_STATS_TTL_SECONDS", TTL)


@pytest.fixture
def fake_clock(monkeypatch):
    """A hand-advanced monotonic clock, so a TTL test costs no wall time.

    Starts at 0.0 deliberately: a monotonic clock reads near zero shortly after boot,
    so a cache that treated 0.0 as "never stamped" would recompute forever there. Every
    test in this file therefore also exercises that value.
    """
    state = {"t": 0.0}
    monkeypatch.setattr(scope_stats, "_clock", lambda: state["t"])
    return state


@pytest.fixture
def sql_spy(monkeypatch):
    """Record every statement executed through ``execute_fetchall``, on any connection.

    Patched on the class, not on one connection object: the recall path reads through
    the dedicated read connection while the write seam holds another, and a test that
    watched only one of them would score the other's scans as "not issued".
    """
    seen: list[str] = []
    real = aiosqlite.Connection.execute_fetchall

    async def spy(self, sql, parameters=None):
        seen.append(" ".join(str(sql).split()))
        return await real(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute_fetchall", spy)
    return seen


def _matching(seen: list[str], *prefixes: str) -> list[str]:
    return [s for s in seen if any(s.startswith(p) for p in prefixes)]


@pytest_asyncio.fixture
async def clean_db():
    """A real DB with the scanned tables emptied and no cache entries carried in."""
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    scope_stats.clear()
    return db


async def _seed(agent: str = AGENT, count: int = 60, project_id: str = "") -> None:
    """Store ``count`` rows. The content carries the bucket: a γ-visible duplicate is
    deduped away (bug-106), so identical text in a project and in the global pool would
    silently seed fewer rows than the caller asked for."""
    for i in range(count):
        content = SEED_CONTENT.format(i=i)
        if project_id:
            content = f"{content} [{project_id}]"
        res = await memory_handlers.do_store(agent, {"content": content}, project_id=project_id)
        assert res["result"] == "stored", res


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [False, True])
async def test_a_second_recall_in_one_scope_reads_no_aggregates(
    clean_db, monkeypatch, fake_clock, sql_spy, confidence
):
    """Uncached, every recall repeats the scans; cached, the second issues none.

    Both parameters are non-deep and return memory rows, so the confidence-on case is
    the production shape — and the one that also commits a recall-count bump between
    the two recalls. Without ``scope_stats_neutral`` at that bump the commit would
    invalidate the entry the first recall just filled and this would fail with all
    three statements re-issued.
    """
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", confidence)
    await _seed()
    scope_stats.clear()

    sql_spy.clear()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5)
    assert out["messages"], "the premise is a recall that returned memory rows"
    for prefix in _COUNTS:
        assert _matching(sql_spy, prefix), (
            f"the first recall never issued `{prefix}` — this test is not measuring "
            f"what it claims to; statements seen: {_matching(sql_spy, *_AGGREGATES)}"
        )
    assert bool(_matching(sql_spy, _SPAN)) is confidence, (
        "the span is read exactly when the confidence score needs it"
    )

    sql_spy.clear()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5)
    assert out["messages"], "the second recall must still answer"
    assert _matching(sql_spy, *_AGGREGATES) == [], (
        "no row was inserted or deleted between the two recalls, so the second must "
        "answer from the cache"
    )


@pytest.mark.asyncio
async def test_confidence_off_never_reads_the_span(clean_db, monkeypatch, fake_clock, sql_spy):
    """The confidence-off default must not pay for the span at all — not even once.

    The two halves are cached under one key but computed independently. Computing them
    together (the shape this replaced) makes a confidence-off install issue a MIN/MAX
    nothing reads on every cold scope.
    """
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    await _seed()
    scope_stats.clear()

    sql_spy.clear()
    for _ in range(3):
        await memory_handlers.do_recall(AGENT, QUERY, limit=5)
    assert _matching(sql_spy, _SPAN) == [], (
        f"a confidence-off recall computed the temporal span: {_matching(sql_spy, _SPAN)}"
    )


@pytest.mark.asyncio
async def test_a_store_between_two_recalls_re_issues_the_aggregates(
    clean_db, monkeypatch, fake_clock, sql_spy
):
    """A write invalidates: with a stale entry the gate would keep sizing the old pool."""
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    await _seed()
    scope_stats.clear()
    await memory_handlers.do_recall(AGENT, QUERY, limit=5)

    await memory_handlers.do_store(AGENT, {"content": "a rollback of the invoicing job"})

    sql_spy.clear()
    await memory_handlers.do_recall(AGENT, QUERY, limit=5)
    for prefix in _AGGREGATES:
        assert _matching(sql_spy, prefix), (
            f"a row was stored, so `{prefix}` must be re-read; got "
            f"{_matching(sql_spy, *_AGGREGATES)}"
        )


@pytest.mark.asyncio
async def test_a_store_re_issues_for_a_reader_on_the_writing_connection(
    clean_db, fake_clock, sql_spy
):
    """The write-generation half, isolated from the recall path.

    Reading through the connection that commits the write is the ``:memory:`` shape
    (there ``_get_read_db`` returns the write connection itself) and the one the gate
    calibration takes. Without the generation bump in ``database.transaction()`` the
    reader below keeps serving the pre-store counts until the TTL expires — a whole
    minute of gating on a pool size the database has moved past.
    """
    db = clean_db  # the write connection
    await _seed(count=3)
    scope_stats.clear()

    assert await scope_stats.get_pool_counts(db, AGENT) == (3, 0)

    await memory_handlers.do_store(AGENT, {"content": "one more rollback note"})

    sql_spy.clear()
    after = await scope_stats.get_pool_counts(db, AGENT)
    assert _matching(sql_spy, *_COUNTS), "the store must invalidate the entry"
    assert after == (4, 0), f"the stored row is missing from the pool the gate governs: {after}"


@pytest.mark.asyncio
async def test_an_outside_writer_is_seen_only_when_the_ttl_expires(
    clean_db, fake_clock, sql_spy
):
    """The TTL bound, asserted in both directions — including the stale half.

    A second process writing the same file never touches this process's write
    generation, so it is invisible until the entry ages out. The first half below
    pins that documented cost (a stale count IS served inside the window); the second
    pins the bound itself. Remove the age check from ``_fresh`` and the second half
    fails: the stale value is then served forever.
    """
    assert database.DB_PATH != ":memory:", (
        "this test needs a file database — a second connection to ':memory:' is a "
        "different, empty one"
    )
    await _seed(count=3)
    scope_stats.clear()

    async with connection() as db:
        assert await scope_stats.get_pool_counts(db, AGENT) == (3, 0)

    generation = database.write_generation()
    outside = sqlite3.connect(database.DB_PATH)
    try:
        outside.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
            (AGENT, "written by another connection", "2026-09-01T10:00:00+00:00"),
        )
        outside.commit()
    finally:
        outside.close()
    assert database.write_generation() == generation, (
        "premise: a write we did not make cannot move our own generation counter"
    )

    # Inside the TTL: the row exists in the database and the cache does not know.
    fake_clock["t"] += TTL / 2
    sql_spy.clear()
    async with connection() as db:
        stale = await scope_stats.get_pool_counts(db, AGENT)
    assert _matching(sql_spy, *_COUNTS) == [], "the entry is still young; it must be reused"
    assert stale == (3, 0), (
        "this is the documented bound, not an accident: inside the TTL an outside "
        f"write is not seen, and the cached count is served — got {stale}"
    )

    # Past it: recomputed, and the outside row is there.
    fake_clock["t"] += TTL
    sql_spy.clear()
    async with connection() as db:
        fresh = await scope_stats.get_pool_counts(db, AGENT)
    assert _matching(sql_spy, *_COUNTS), "past the TTL the counts must be re-read"
    assert fresh == (4, 0), f"the externally written row is still missing: {fresh}"


@pytest.mark.asyncio
async def test_each_isolation_triple_is_cached_separately(clean_db, fake_clock, sql_spy):
    """One entry per (agent_id, project_id, channel) — including None vs '' on project_id.

    The three axes have three different read contracts: ``project_id=None`` filters on
    nothing, ``''`` selects the global pool alone, and ``'p1'`` selects ``'p1'`` union the
    global pool. Collapsing None onto '' (the shape a key built with ``project_id or ''``
    has) would serve a project-scoped recall the global pool's numbers, which is the
    bug-107/bug-216 cross-bucket leak the scoping exists to prevent.
    """
    await _seed(count=2, project_id="")
    await _seed(count=3, project_id="p1")
    await _seed(count=4, project_id="p2")
    scope_stats.clear()

    async with connection() as db:
        counts: dict[str, int] = {}
        for label, project_id in (("none", None), ("global", ""), ("p1", "p1"), ("p2", "p2")):
            sql_spy.clear()
            memories, episodes = await scope_stats.get_pool_counts(
                db, AGENT, project_id=project_id
            )
            assert _matching(sql_spy, *_COUNTS), f"the {label} key must be computed, not borrowed"
            counts[label] = memories
            assert episodes == 0, episodes

            sql_spy.clear()
            again = await scope_stats.get_pool_counts(db, AGENT, project_id=project_id)
            assert _matching(sql_spy, *_COUNTS) == [], f"the {label} key must cache its own entry"
            assert again == (memories, episodes)

        # A shared entry would make some of these equal.
        assert counts == {"none": 9, "global": 2, "p1": 5, "p2": 6}, counts

        sql_spy.clear()
        channelled = await scope_stats.get_pool_counts(db, AGENT, channel="ops")
        assert _matching(sql_spy, *_COUNTS), "the channel axis is part of the key too"
        assert channelled == (9, 0), "channel '' rows are visible in every channel"

        sql_spy.clear()
        other_agent = await scope_stats.get_pool_counts(db, "agent.someone-else")
        assert _matching(sql_spy, *_COUNTS), "the agent axis is part of the key too"
        assert other_agent == (0, 0), other_agent


@pytest.mark.asyncio
async def test_the_two_halves_are_stamped_independently(clean_db, fake_clock, sql_spy):
    """Filling the counts must not make a later span lookup believe it has one.

    They share a key but not a stamp: a confidence-off recall computes only the counts,
    and a single per-entry stamp would let the next confidence-on caller read the
    never-computed span half as an answered ``(None, None)`` — an empty scope, i.e. no
    range scaling and no bug-207 age anchor for every row.
    """
    async with connection() as db:
        await _seed(count=2)
        scope_stats.clear()
        await scope_stats.get_pool_counts(db, AGENT)

        sql_spy.clear()
        span = await scope_stats.get_span(db, AGENT)
        assert _matching(sql_spy, _SPAN), "the span half was never computed, so it must be read"
        assert span[0] and span[1], f"the seeded rows have timestamps: {span}"


@pytest.mark.asyncio
async def test_the_disabled_cache_reads_the_aggregates_every_time(
    clean_db, monkeypatch, fake_clock, sql_spy
):
    """``CPERSONA_SCOPE_STATS_CACHE=false`` restores the pre-cache read pattern."""
    monkeypatch.setattr(scope_stats, "SCOPE_STATS_CACHE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    await _seed()
    scope_stats.clear()

    for attempt in range(3):
        sql_spy.clear()
        await memory_handlers.do_recall(AGENT, QUERY, limit=5)
        for prefix in _AGGREGATES:
            assert _matching(sql_spy, prefix), (
                f"recall {attempt} answered `{prefix}` from a cache that is switched off"
            )
