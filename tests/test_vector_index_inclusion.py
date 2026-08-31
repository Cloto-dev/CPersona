"""The index may not hide a row the isolation authority admits.

Design: `docs/CONTIGUOUS_INDEX_DESIGN.md` §3. `isolation_where()` stays the one
authority on who owns a row, and the hydrate re-applies it. The index filters
only because the top-k cut happens before that hydrate — an index that ignored
the axes would fill the cut with rows the hydrate then drops and return a short
answer. So it filters, but it is never asked to be *right*:

| If the index filter is | What happens | Severity |
| --- | --- | --- |
| Too loose | The hydrate drops the extras | Correct. Merely wasteful |
| Too strict | Rows vanish silently | Undetectable recall loss |

One side, and therefore checkable:

    set(index candidates) ⊇ set(rows the SQL predicate admits)

The asymmetry is the whole point of asserting only this direction. A too-loose
index costs work; a too-strict one costs answers, and costs them in the one way
nothing downstream can see — the caller receives a shorter list and has no way to
know a row was ever a candidate.

The axis values below always include a third member per axis. With only `''` and
one project, a filter on `'X'` admits `X ∪ ''` — every row — so the axis excludes
nothing and a containment that ignored it would still hold.
"""

import os
import random

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from cpersona.isolation import isolation_where

DIM = 6
AGENTS = ("agent.a", "agent.b", "")
PROJECTS = ("", "proj.x", "proj.y")
CHANNELS = ("", "chan.1", "chan.2")
SOURCES = (None, "user-1", "user-2", "user-10")


@pytest_asyncio.fixture
async def indexed():
    """A randomised corpus and the index built over it."""
    db = await get_db()
    path = vector_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)
    await db.execute("DELETE FROM memories")

    rng = random.Random(20260901)
    for n in range(400):
        source = rng.choice(SOURCES)
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rng.choice(AGENTS), rng.choice(PROJECTS), rng.choice(CHANNELS),
                f"row {n}",
                "{}" if source is None else '{"type": "User", "id": "%s"}' % source,
                "2026-03-01T00:00:00+00:00",
                f"2026-03-01 00:{n // 60:02d}:{n % 60:02d}",
                np.random.default_rng(n).standard_normal(DIM).astype(np.float32).tobytes(),
            ),
        )
    await db.commit()

    result = await vector_index.build_index(db, "memories", path)
    assert result["built"], result
    index = vector_index.load_index("memories", path)
    yield db, index

    await db.execute("DELETE FROM memories")
    await db.commit()
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


async def _authority_admits(db, agent_id, project_id, channel, source_id):
    """Exactly the rows the SQL predicate lets through — the authority's answer."""
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src_like = vector._escape_like_prefix(source_id) if source_id else ""
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    rows = await db.execute_fetchall(
        f"SELECT id FROM memories WHERE {iso.clause} AND embedding IS NOT NULL{src_clause}",
        (*iso.params, *((src_like,) if src_like else ())),
    )
    return {r[0] for r in rows}


def _index_offers(index, agent_id, project_id, channel, source_id):
    positions = vector_index.select(
        index, agent_id=agent_id, project_id=project_id, channel=channel,
        source_id=source_id, limit=0,
    )
    return {int(index.ids[p]) for p in positions}


_AXIS_COMBINATIONS = [
    (agent, project, channel, source)
    for agent in AGENTS
    for project in (None, *PROJECTS)
    for channel in CHANNELS
    for source in ("", "user-1", "user-")
]


@pytest.mark.asyncio
async def test_index_never_hides_a_row_the_authority_admits(indexed):
    db, index = indexed
    misses = []
    covered = 0
    for agent, project, channel, source in _AXIS_COMBINATIONS:
        admitted = await _authority_admits(db, agent, project, channel, source)
        offered = _index_offers(index, agent, project, channel, source)
        covered += len(admitted)
        if not admitted <= offered:
            misses.append(
                f"agent={agent!r} project={project!r} channel={channel!r} source={source!r}: "
                f"{len(admitted - offered)} row(s) hidden, e.g. {sorted(admitted - offered)[:5]}"
            )
    assert not misses, "the index hid rows the authority admits:\n  " + "\n  ".join(misses)
    # A containment that never had anything to contain would pass by vacuity.
    assert covered > 1000, f"the axis matrix admitted only {covered} rows in total"


@pytest.mark.asyncio
async def test_the_matrix_contains_combinations_that_actually_exclude(indexed):
    """Guards the guard: if every filter admitted everything, the property above
    would hold no matter how the index filtered."""
    db, index = indexed
    everything = await _authority_admits(db, "agent.a", None, "", "")
    narrowed = [
        (p, c, s)
        for p in PROJECTS
        for c in CHANNELS
        for s in ("", "user-1")
        if await _authority_admits(db, "agent.a", p, c, s) < everything
    ]
    assert narrowed, "no axis value in the matrix excludes anything — the fixture is too uniform"
    assert index.count > 0
