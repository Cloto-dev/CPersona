"""The coarse candidate supplier: one answer, whichever supplier gives it.

Design: `docs/BINARY_COARSE_SEARCH_DESIGN.md` §4 and invariants 1, 3, 4 and 6 of
§8. What these tests pin:

- the live supplier returns what a plain reading of the contract returns — the
  scan the near window reads, cut to the range, ranked by Hamming distance with
  the scan position breaking ties — computed here by a separate route (the block
  quantiser and Python's own popcount), so the two suppliers cannot agree by
  sharing a mistake;
- the index supplier returns the live supplier's answer for the same store, over
  random axes, ranges, periods and depths, with rows written after the build,
  rows the build named instead of holding, and created_at ties between the two;
- the index's candidates include every row the isolation predicate admits in the
  range, and an index made stricter turns that red;
- the states the index cannot answer for (a cleared embedding, a deleted
  candidate) hand the question to the live store rather than answering wrongly;
- a row of another width keeps its scan position and is skipped.
"""

import itertools
import json
import random

import numpy as np
import pytest
import pytest_asyncio

from cpersona import blocks, coarse_index, coarse_search
from cpersona.database import get_db
from cpersona.isolation import isolation_where, source_id_where

AGENT = "coarse.search.a"
OTHER = "coarse.search.b"
DIM = 16  # few distances (0..16), so a cut-off by distance almost always falls inside a tie

_STAMPS = (
    "2026-03-01T00:00:00+00:00",  # 2026-03-01 00:00:00: the lower bound of PERIOD, exactly
    "2026-03-01T09:00:00+09:00",  # the same instant in another offset
    "2026-03-01 06:00:00",
    "2026-03-01T12:30:00Z",  # 2026-03-01 12:30:00: the upper bound of PERIOD, exactly (excluded)
    "2026-03-02 08:00:00",
    "not a time",  # datetime() reads NULL: in no period
)
PERIOD = ("2026-03-01 00:00:00", "2026-03-01 12:30:00")
WIDE = ("2026-02-01 00:00:00", "2026-04-01 00:00:00")


def _vector(seed: int, dim: int = DIM) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)


async def _insert(db, seed, **kw) -> int:
    row = {
        "agent_id": AGENT,
        "project_id": "",
        "channel": "",
        "content": f"row {seed}",
        "source": '{"type": "Agent", "id": "src.%d"}' % (seed % 3),
        "timestamp": _STAMPS[seed % len(_STAMPS)],
        "created_at": "2026-03-01 00:00:%02d" % (seed % 7),  # seven seconds: many created_at ties
        "embedding": _vector(seed).tobytes(),
    }
    row.update(kw)
    columns = tuple(row)
    cur = await db.execute(
        f"INSERT INTO memories ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        tuple(row[c] for c in columns),
    )
    return cur.lastrowid


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


async def _populate(db, seeds, rng) -> None:
    for seed in seeds:
        await _insert(
            db, seed,
            agent_id=AGENT if rng.random() < 0.8 else OTHER,
            project_id=rng.choice(["", "", "p.x", "p.y"]),
            channel=rng.choice(["", "", "c.a"]),
        )


@pytest_asyncio.fixture
async def store(db, tmp_path):
    """A store with an index, then rows the index must read live.

    Before the build: 120 ordinary rows, one whose created_at the index cannot
    spell (named as excluded) and one with no embedding (named as unembedded).
    After the build: 30 new rows on the same seven seconds, so they interleave
    with the index rows on created_at ties, and the unembedded row gains its
    embedding.
    """
    rng = random.Random(7)
    await _populate(db, range(1, 121), rng)
    await _insert(db, 900, created_at="2026-03-01T00:00:03Z")  # not canonical: the index names it
    hole = await _insert(db, 901, embedding=None)
    await db.commit()
    path = str(tmp_path / "coarse")
    built = await coarse_index.build_coarse_index(db, "memories", path)
    assert built["built"] and built["excluded"] == 1 and built["unembedded"] == 1, built
    await _populate(db, range(121, 151), rng)
    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_vector(901).tobytes(), hole))
    await db.commit()
    return db, path


def _missing(tmp_path) -> str:
    return str(tmp_path / "no-such-index")


async def _reference(db, query, *, agent_id, project_id=None, channel="", source_id="",
                     start=0, end=None, period=None, k=coarse_search.K_PROVISIONAL):
    """The contract read plainly, by a route that shares nothing with the suppliers' selection."""
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src = source_id_where(source_id)
    clause, params = "", ()
    if period is not None:
        clause, params = f" AND {coarse_index.PERIOD_PREDICATE}", tuple(period)
    rows = await db.execute_fetchall(
        f"SELECT id, embedding FROM memories WHERE {iso.clause} AND embedding IS NOT NULL"
        f"{src.and_clause}{clause} ORDER BY created_at DESC, id ASC",
        (*iso.params, *src.params, *params),
    )
    query_bits = int.from_bytes(blocks.pack_bits(np.asarray(query, dtype=np.float32).tolist()), "big")
    scored = []
    for position, (row_id, blob) in enumerate(rows):
        if position < start or (end is not None and position >= end):
            continue
        if len(blob) != len(query) * 4:
            continue
        bits = int.from_bytes(blocks.pack_bits(np.frombuffer(blob, dtype="<f4").tolist()), "big")
        scored.append((bin(bits ^ query_bits).count("1"), position, row_id))
    scored.sort()
    return [(row_id, distance, position) for distance, position, row_id in scored[:k]]


def _scopes(count: int, seed: int):
    grid = list(itertools.product(
        (AGENT, OTHER), (None, "", "p.x"), ("", "c.a"), ("", "src.1"),
        (0, 3, 9), (None, 14), (None, PERIOD, WIDE), (1, 4, 1000),
    ))
    for agent, project, channel, source, start, end, period, k in random.Random(seed).sample(grid, count):
        yield dict(agent_id=agent, project_id=project, channel=channel, source_id=source,
                   start=start, end=end, period=period, k=k)


def test_top_k_by_counting_is_the_prefix_of_the_full_sort():
    """The histogram selection keeps exactly what sorting every row would keep, in the same order."""
    rng = np.random.default_rng(3)
    for trial in range(300):
        n = int(rng.integers(0, 60))
        distances = rng.integers(0, 6, n)  # a handful of values: ties at the cut-off are the rule
        positions = rng.permutation(1000)[:n]
        ids = rng.permutation(10_000)[:n]
        k = int(rng.integers(1, 70))
        got = coarse_search._top(ids, distances, positions, k, 5)
        want = sorted(zip(distances.tolist(), positions.tolist(), ids.tolist()))[:k]
        assert list(zip(got[1].tolist(), got[2].tolist(), got[0].tolist())) == want, trial


def test_the_distance_is_the_one_blocks_compute():
    rng = np.random.default_rng(5)
    rows = coarse_index.sign_bits(rng.standard_normal((9, 24)).astype(np.float32))
    query = coarse_index.sign_bits(rng.standard_normal((1, 24)).astype(np.float32))[0]
    assert blocks.hamming_matrix(rows, query).tolist() == blocks.hamming_distances(
        [r.tobytes() for r in rows], query.tobytes()
    ).tolist()


@pytest.mark.asyncio
async def test_the_live_supplier_is_the_contract_read_plainly(store, tmp_path):
    db, _ = store
    for n, scope in enumerate(_scopes(120, seed=11)):
        query = _vector(5000 + n)
        got = await coarse_search.coarse_candidates(db, query, path=_missing(tmp_path), **scope)
        assert got.source == "live"
        assert got.rows() == await _reference(db, query, **scope), scope


@pytest.mark.asyncio
async def test_the_index_returns_what_the_live_store_returns(store, tmp_path):
    """Invariant 3: building or deleting the index changes the cost, never the answer."""
    db, path = store
    watermark = coarse_index.load_coarse_index(path=path).watermark
    answered = cut_in_a_tie = with_live_rows = 0
    for n, scope in enumerate(_scopes(200, seed=13)):
        query = _vector(7000 + n)
        from_index = await coarse_search.coarse_candidates(db, query, path=path, **scope)
        live = await coarse_search.coarse_candidates(db, query, path=_missing(tmp_path), **scope)
        assert from_index.source == "index", scope  # the index answered: a silent fallback proves nothing
        assert from_index.rows() == live.rows(), scope
        answered += bool(live.ids)
        with_live_rows += any(i > watermark for i in live.ids)
        everything = await coarse_search.coarse_candidates(db, query, path=_missing(tmp_path), **{**scope, "k": 10_000})
        cut_in_a_tie += len(everything.ids) > scope["k"] and everything.distances[scope["k"]] == live.distances[-1]
    # Not vacuous: most answers hold rows, some hold rows the index had to read live,
    # and some were cut inside a tie, where only the scan position decides.
    assert answered >= 150 and with_live_rows >= 20 and cut_in_a_tie >= 20, (answered, with_live_rows, cut_in_a_tie)


@pytest.mark.asyncio
async def test_the_index_offers_every_row_the_authority_admits(store):
    """The one-directional obligation of §4, at a depth that keeps every row of the range."""
    db, path = store
    for scope in _scopes(60, seed=17):
        scope["k"] = 10_000
        got = await coarse_search.coarse_candidates(db, _vector(1), path=path, **scope)
        assert got.source == "index"
        admitted = {row_id for row_id, _, _ in await _reference(db, _vector(1), **scope)}
        assert admitted <= set(got.ids), (scope, sorted(admitted - set(got.ids)))


@pytest.mark.asyncio
async def test_a_period_bound_the_sql_cannot_read_admits_nothing(store, tmp_path):
    db, path = store
    for p in (path, _missing(tmp_path)):
        got = await coarse_search.coarse_candidates(
            db, _vector(2), agent_id=AGENT, period=("not a time", "2026-04-01 00:00:00"), path=p,
        )
        assert got.rows() == []


@pytest.mark.asyncio
async def test_a_cleared_embedding_sends_the_question_to_the_live_store(store, tmp_path):
    """A row that left the scan moves every later row up a position the index still counts."""
    db, path = store
    # A row the file holds: a row the build named instead of holding is read live,
    # and a cleared embedding there is already the live answer.
    index = coarse_index.load_coarse_index(path=path)
    newest_indexed = int(index.ids[np.flatnonzero(index.agent_code == index.agents.index(AGENT))[0]])
    await db.execute("UPDATE memories SET embedding = NULL WHERE id = ?", (newest_indexed,))
    await db.commit()
    scope = dict(agent_id=AGENT, start=2, end=20, k=5)
    got = await coarse_search.coarse_candidates(db, _vector(3), path=path, **scope)
    assert got.source == "live"
    assert got.rows() == await _reference(db, _vector(3), **scope)


@pytest.mark.asyncio
async def test_a_deleted_candidate_sends_the_question_to_the_live_store(store, tmp_path):
    db, path = store
    query = _vector(4)
    first = await coarse_search.coarse_candidates(db, query, agent_id=AGENT, k=3, path=path)
    assert first.source == "index" and first.ids
    await db.execute("DELETE FROM memories WHERE id = ?", (first.ids[0],))
    await db.commit()
    got = await coarse_search.coarse_candidates(db, query, agent_id=AGENT, k=3, path=path)
    assert got.source == "live"
    assert first.ids[0] not in got.ids
    assert got.rows() == await _reference(db, query, agent_id=AGENT, k=3)


@pytest.mark.asyncio
async def test_a_row_of_another_width_keeps_its_scan_position_and_is_skipped(db, tmp_path):
    """Invariant 6, and the near scan's rule that its window is cut before such rows are skipped."""
    for seed in range(1, 31):
        await _insert(db, seed, created_at="2026-03-01 00:01:%02d" % seed)
    # Newer than every row above, so it takes scan position 0.
    await _insert(db, 99, created_at="2026-03-01 00:02:00", embedding=_vector(99, DIM * 2).tobytes())
    await db.commit()
    assert not (await coarse_index.build_coarse_index(db, "memories", str(tmp_path / "mixed")))["built"]
    got = await coarse_search.coarse_candidates(db, _vector(6), agent_id=AGENT, start=0, end=10, k=1000,
                                                path=_missing(tmp_path))
    assert len(got.ids) == 9  # ten positions, one of them the foreign row
    assert min(got.positions) >= 1 and max(got.positions) == 9
    assert got.rows() == await _reference(db, _vector(6), agent_id=AGENT, start=0, end=10, k=1000)


@pytest.mark.asyncio
async def test_the_lost_embedding_probe_reads_the_partial_index(db):
    plan = await db.execute_fetchall(
        "EXPLAIN QUERY PLAN SELECT id FROM memories WHERE embedding IS NULL AND id <= ? AND +agent_id = ?",
        (10, AGENT),
    )
    assert any("idx_memories_lost_embedding" in str(r[-1]) for r in plan), plan


@pytest.mark.asyncio
async def test_degenerate_requests(store, tmp_path):
    db, path = store
    assert (await coarse_search.coarse_candidates(db, _vector(1), agent_id=AGENT, k=0, path=path)).rows() == []
    assert (await coarse_search.coarse_candidates(db, _vector(1), agent_id=AGENT, start=5, end=5, path=path)).rows() == []
    with pytest.raises(ValueError):
        await coarse_search.coarse_candidates(db, _vector(1), agent_id=AGENT, start=-1, path=path)
    other_dim = await coarse_search.coarse_candidates(db, _vector(1, 8), agent_id=AGENT, path=path)
    assert other_dim.source == "live"  # an index of another dimension does not answer
    assert json.dumps(other_dim.rows())  # plain ints all the way down
