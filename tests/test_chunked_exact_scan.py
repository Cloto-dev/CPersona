"""The fallback vector scan reads its window in chunks, and what that costs.

The scan that answers when the contiguous index cannot used to materialise its
whole window twice over -- a list holding one blob per row, then `b"".join` of
all of them. At 1,000,000 rows x 768 dimensions that is about 6.1 GB, so the
fallback did not answer slowly on an 8 GB machine, it was killed. Reading the
cursor `VECTOR_SCAN_CHUNK_ROWS` rows at a time makes the peak O(chunk).

The change is only worth making if the answer does not move, so this file is
mostly about equivalence, and it measures the arithmetic rather than trusting
it. The claim the design started from -- "a row's dot product does not depend on
the other rows in the matrix, so chunking cannot change a score" -- is true of
the mathematics and FALSE of the implementation: `mat @ query_vec` is dispatched
to the platform BLAS, which picks its kernel from the row count, so the same row
scored in a matrix of 4 rows and in a matrix of 2,400 can differ in the last
place. Measured here with Accelerate at 64 dimensions: every matrix of 64 rows
or more agrees bit for bit with the full window, every smaller one disagrees by
about one ULP on a quarter (chunks of 1) to a half (chunks of 7) of its rows (at
768 dimensions the switch sits at 16 rows). So the exactness tests below do not
assume a platform: they MEASURE the
row count at which this machine's BLAS changes its answer and then demand
bit-identity exactly where it is owed, rather than being green on one machine
and red on another for a reason nothing in the file mentions.

Two consequences worth stating plainly, because they are properties of the
change rather than of the tests:

- No matrix that is scored may be small, so the scan never multiplies a
  window's ragged remainder on its own -- a short tail is merged into the chunk
  before it, which is what `test_the_lookahead_merges_a_short_tail_into_the_
  chunk_before_it` pins. Before that merge, a window of 2,050 rows in chunks of
  512 moved one of its last two scores and a window of 2,100 moved 27 of its
  last 52; those exact sizes are parametrised below and are now bit-identical.
- Below the switch point the shift changes the ANSWER, not just the scores. On
  the tie-dense corpus this file builds -- 2,400 rows of which 218 share one
  vector exactly and 343 more sit a single ULP away, queried with that vector --
  chunks of 7 with a limit of 25 returned 24 different ids out of 25, because
  the cut falls inside the tie group where a last-place move decides everything.
  Chunk sizes that small are a test instrument rather than a configuration, and
  the shipped default is eight times the switch point, but that is the shape of
  the risk and it is why the cases below fix the row list before comparing
  scores.

The cases that are not about arithmetic (boundaries, skipped rows, the
threshold, ties, isolation, the cursor's lifetime) are built on one-hot vectors,
whose dot product is a single product plus zeros and is therefore exact in any
summation order on any machine. They test the loop, and the loop alone.
"""

import asyncio
import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from cpersona.isolation import isolation_where
from tests.conftest import fake_embed_one

AGENT = "chunkscan.agent"
OTHER = "chunkscan.other"
DIM = 64
SCAN_LIMIT = 10000

MEM_SQL = """SELECT id, embedding
               FROM memories
               WHERE agent_id = ? AND embedding IS NOT NULL
               ORDER BY created_at DESC, id ASC
               LIMIT ?"""

# A query that reads one component. `_vec(s) @ ONE_HOT` is `s * 1.0` plus 63
# additions of zero, which is exactly `s` however the BLAS orders the sum -- so
# every score below is a number the test chose, on every platform and at every
# chunk size, and a tie is a tie rather than a coincidence of rounding.
ONE_HOT = np.zeros(DIM, dtype=np.float32)
ONE_HOT[0] = 1.0


def _vec(score: float) -> bytes:
    """A row whose similarity against ONE_HOT is exactly `score`."""
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = np.float32(score)
    return v.tobytes()


@pytest_asyncio.fixture(autouse=True)
async def db():
    """A connection with no rows and no index file.

    The index has to be absent, not merely unused: `_scan_memories_local` asks
    it first, and a run that answered from the index would exercise none of this
    and still pass.
    """
    conn = await get_db()
    _drop_indexes()
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    _drop_indexes()


def _drop_indexes():
    for table in ("memories", "episodes"):
        for path in (vector_index.index_path(table), vector_index.index_path(table) + ".tmp"):
            if os.path.exists(path):
                os.unlink(path)


async def _seed(db, blobs, *, table="memories", agent_id=AGENT, project_id="",
                channel="", source_id="user-a", tag=""):
    """Insert rows in scan order: the first blob is the newest row.

    `created_at` descends, so the ORDER BY hands them back in the order given
    and an ordinal is a position in this list.
    """
    ids = []
    for n, blob in enumerate(blobs):
        stamp = f"2026-03-01 00:{59 - n // 60:02d}:{59 - n % 60:02d}"
        if table == "memories":
            cur = await db.execute(
                "INSERT INTO memories (agent_id, project_id, channel, content, source,"
                " timestamp, created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, project_id, channel,
                 f"row {agent_id} {project_id} {channel} {source_id} {tag} {n}",
                 '{"type": "User", "id": "%s"}' % source_id,
                 "2026-03-01T00:00:00+00:00", stamp, blob),
            )
        else:
            cur = await db.execute(
                "INSERT INTO episodes (agent_id, project_id, channel, summary, created_at,"
                " embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, project_id, channel, f"episode {n}", stamp, blob),
            )
        ids.append(cur.lastrowid)
    await db.commit()
    return ids


async def _scan(db, *, limit, min_sim=-1.0, agent_id=AGENT, sql=MEM_SQL, params=None,
                query_vec=ONE_HOT):
    return await vector._chunked_cosine_scan(
        db, sql, params if params is not None else (agent_id, SCAN_LIMIT),
        query_vec, DIM, min_sim, limit,
    )


# --------------------------------------------------------------------------
# (a) chunk boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rows", [1, 3, 4, 5, 8, 11])
@pytest.mark.asyncio
async def test_every_row_is_scored_at_and_around_a_chunk_boundary(db, monkeypatch, rows):
    """chunk-1, chunk, chunk+1, two whole chunks and a ragged tail.

    The ragged case is the one this parametrisation exists for: a loop that
    stops when a batch is short instead of when it is empty loses the last
    chunk, and it loses it silently -- the answer is a well-formed list of
    slightly fewer rows, which is what recall loss looks like.
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    scores = [0.9 - 0.01 * n for n in range(rows)]
    ids = await _seed(db, [_vec(s) for s in scores])

    got = await _scan(db, limit=None)

    assert got == [(n, ids[n], pytest.approx(scores[n])) for n in range(rows)], (
        f"{rows} rows in chunks of 4 did not come back whole and in scan order"
    )


# --------------------------------------------------------------------------
# (b) rows the scan skips
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_and_foreign_width_rows_are_skipped_without_taking_an_ordinal(db, monkeypatch):
    """A skipped row must not advance the tie-break position.

    The ordinal is the position among the rows that PASSED the width filter --
    what the old scan indexed into. If a skipped row advanced it, two corpora
    differing only in a foreign-width row (what a mid-flight model swap leaves
    behind) would break ties between the same rows differently.
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    wrong_width = np.zeros(DIM // 2, dtype=np.float32).tobytes()
    blobs = [
        _vec(0.99),     # kept, ordinal 0
        None,           # NULL: not selected by the statement at all
        _vec(0.98),     # kept, ordinal 1
        wrong_width,    # skipped by the width filter
        b"",            # empty blob: falsy, skipped
        _vec(0.97),     # kept, ordinal 2
        wrong_width,
        _vec(0.96),     # kept, ordinal 3
        _vec(0.95),     # kept, ordinal 4
        None,
        wrong_width,
        _vec(0.94),     # kept, ordinal 5
        _vec(0.93),     # kept, ordinal 6
        wrong_width,
        _vec(0.92),     # kept, ordinal 7
        _vec(0.91),     # kept, ordinal 8
    ]
    ids = await _seed(db, blobs)
    seen = []
    real = vector._cosine_batch

    def recording(query_vec, query_dim, blob_list):
        seen.append(len(blob_list))
        return real(query_vec, query_dim, blob_list)

    monkeypatch.setattr(vector, "_cosine_batch", recording)

    got = await _scan(db, limit=None)

    # The skipped rows have to straddle more than one scored matrix. An error
    # that advances the ordinal once per batch is invisible in a single batch
    # (every row shifts by the same amount and the ordinals stay in order), so a
    # fixture small enough to be merged into one would assert nothing here --
    # measured: it let exactly that mutation live.
    assert len(seen) == 3, (
        f"the fixture was scored as {seen}; it must span several matrices for a "
        "per-batch ordinal error to be observable"
    )
    assert got == [
        (0, ids[0], pytest.approx(0.99)),
        (1, ids[2], pytest.approx(0.98)),
        (2, ids[5], pytest.approx(0.97)),
        (3, ids[7], pytest.approx(0.96)),
        (4, ids[8], pytest.approx(0.95)),
        (5, ids[11], pytest.approx(0.94)),
        (6, ids[12], pytest.approx(0.93)),
        (7, ids[14], pytest.approx(0.92)),
        (8, ids[15], pytest.approx(0.91)),
    ], "a row the scan cannot use was scored, or consumed a tie-break position"


# --------------------------------------------------------------------------
# (c) the threshold boundary
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_scoring_exactly_the_threshold_survives(db, monkeypatch):
    """`>=`, not `>`. The boundary is reachable: the fusion callers pass a
    calibrated threshold, and a stored vector can land on it exactly."""
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    threshold = float(np.float32(0.5))
    below = float(np.nextafter(np.float32(0.5), np.float32(-1.0)))
    ids = await _seed(db, [_vec(0.9), _vec(threshold), _vec(below), _vec(0.1)])

    got = await _scan(db, limit=None, min_sim=threshold)

    assert [row_id for _, row_id, _ in got] == [ids[0], ids[1]], (
        "the row sitting exactly on the threshold was dropped (`>` instead of `>=`)"
    )


# --------------------------------------------------------------------------
# (d) ties and the top-`limit` cut
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tie_larger_than_the_limit_keeps_the_earliest_rows_in_scan_order(db, monkeypatch):
    """Scan order is the tie-break, and it decides membership rather than
    presentation: when the cut falls inside a group of equally-scored rows, the
    EARLIER rows (created_at DESC, then id ASC) are the ones that survive, and
    what comes back is in scan order, not in score order."""
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    # Ten identical scores spanning three chunks, so the cut is decided across
    # chunk boundaries rather than inside one batch.
    ids = await _seed(db, [_vec(0.5)] * 10)

    got = await _scan(db, limit=3)

    assert [(o, row_id) for o, row_id, _ in got] == [(0, ids[0]), (1, ids[1]), (2, ids[2])], (
        "the tie was broken towards the later rows, or the survivors came back "
        "in score order instead of scan order"
    )


@pytest.mark.asyncio
async def test_the_cut_keeps_the_highest_scores_and_returns_them_in_scan_order(db, monkeypatch):
    """The cut is `nlargest(limit, key=(score, -ordinal))` re-sorted by ordinal:
    a bounded heap has to agree with it, not merely keep something."""
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    scores = [0.1, 0.9, 0.3, 0.7, 0.5, 0.95, 0.2, 0.6, 0.4, 0.8]
    ids = await _seed(db, [_vec(s) for s in scores])

    got = await _scan(db, limit=4)

    # 0.95, 0.9, 0.8, 0.7 -- at ordinals 5, 1, 9, 3, handed back by ordinal.
    assert [(o, row_id) for o, row_id, _ in got] == [
        (1, ids[1]), (3, ids[3]), (5, ids[5]), (9, ids[9])
    ]


@pytest.mark.asyncio
async def test_a_limit_larger_than_the_survivors_cuts_nothing(db, monkeypatch):
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    ids = await _seed(db, [_vec(0.9), _vec(0.8), _vec(0.7)])

    got = await _scan(db, limit=50)

    assert [row_id for _, row_id, _ in got] == ids


# --------------------------------------------------------------------------
# (e) the isolation axes still exclude what they excluded
# --------------------------------------------------------------------------


@pytest.mark.parametrize("axis", ["agent", "project", "channel", "source"])
@pytest.mark.asyncio
async def test_each_isolation_axis_still_excludes_what_it_excludes(db, monkeypatch, axis):
    """A regression pin. The statement is unchanged, so this is here to say so:
    the chunked read passes the same SQL with the same parameters, and a scan
    that lost an axis on the way would return another agent's rows.

    Three values per axis, not two: with only '' and 'x' the project union
    ('x' means x OR the global pool) admits every row, so a filter on that axis
    would exclude nothing and the test could not fail.
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    mine = await _seed(db, [_vec(0.9), _vec(0.8)])
    if axis == "agent":
        await _seed(db, [_vec(0.7)], agent_id=OTHER)
        kwargs = dict(agent_id=AGENT, project_id=None, channel="", source_id="")
        expected = set(mine)
    elif axis == "project":
        await _seed(db, [_vec(0.7)], project_id="proj-x")
        await _seed(db, [_vec(0.6)], project_id="proj-y")
        kwargs = dict(agent_id=AGENT, project_id="", channel="", source_id="")
        expected = set(mine)
    elif axis == "channel":
        # A channel-scoped read admits the channel AND the channel-less rows,
        # and excludes the other channel -- `''` is "no filter" on this axis by
        # contract, so a read that passed it would exclude nothing and the case
        # would be unable to fail.
        scoped = await _seed(db, [_vec(0.7)], channel="c1")
        await _seed(db, [_vec(0.6)], channel="c2")
        kwargs = dict(agent_id=AGENT, project_id=None, channel="c1", source_id="")
        expected = set(mine) | set(scoped)
    else:
        await _seed(db, [_vec(0.7)], source_id="user-b")
        await _seed(db, [_vec(0.6)], source_id="user-c")
        kwargs = dict(agent_id=AGENT, project_id=None, channel="", source_id="user-a")
        expected = set(mine)

    got = await _scan_memories(db, **kwargs)

    assert {item["id"] for _, item in got} == expected, (
        f"the {axis} axis stopped excluding rows on the chunked read"
    )


async def _scan_memories(db, *, agent_id, project_id, channel, source_id, limit=100,
                         min_sim=-1.0):
    """`_scan_memories_local` wired the way `_search_vector` wires it."""
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src_like = vector._escape_like_prefix(source_id)
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params = (src_like,) if src_like else ()
    return await vector._scan_memories_local(
        db, iso, src_clause, src_params, SCAN_LIMIT, limit, ONE_HOT, DIM, min_sim,
        agent_id=agent_id, project_id=project_id, channel=channel, source_id=source_id,
    )


@pytest.mark.asyncio
async def test_the_memory_scan_still_takes_the_fallback_and_returns_hydrated_rows(db, monkeypatch):
    """The wiring, end to end: the chunked read feeds the same hydrate.

    Also pins that these tests are not quietly answered by the index -- if they
    were, the chunk size would be irrelevant and every case above would be
    vacuous.
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    seen = []
    real = vector._index_phase1

    async def counting(*args, **kwargs):
        result = await real(*args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(vector, "_index_phase1", counting)
    ids = await _seed(db, [_vec(0.9), _vec(0.8), _vec(0.7), _vec(0.6), _vec(0.5)])

    got = await _scan_memories(db, agent_id=AGENT, project_id=None, channel="", source_id="")

    assert seen and all(r is None for r in seen), "the index answered; the fallback was not tested"
    assert [item["id"] for _, item in got] == ids
    assert all(item["content"] for _, item in got), "the hydrate stopped returning payloads"


# --------------------------------------------------------------------------
# (f) the episode scan, whose limit may be None
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_episode_scan_with_no_limit_keeps_every_survivor(db, monkeypatch):
    """`limit=None` is the pre-split behaviour and the episode caller can pass
    it: nothing is cut, however many chunks the window took."""
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    scores = [0.9 - 0.05 * n for n in range(11)]
    ids = await _seed(db, [_vec(s) for s in scores], table="episodes")

    iso = isolation_where(agent_id=AGENT, project_id=None, channel="")
    got = await vector._scan_episodes_local(
        db, iso, SCAN_LIMIT, ONE_HOT, DIM, -1.0, "", "",
        limit=None, agent_id=AGENT, project_id=None,
    )

    assert [item["id"] for _, item in got] == ids, (
        "the unlimited episode scan lost survivors -- the cut ran on a None limit"
    )


@pytest.mark.asyncio
async def test_the_episode_scan_still_honours_a_limit(db, monkeypatch):
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    ids = await _seed(db, [_vec(0.1), _vec(0.9), _vec(0.4), _vec(0.8), _vec(0.2)],
                      table="episodes")

    iso = isolation_where(agent_id=AGENT, project_id=None, channel="")
    got = await vector._scan_episodes_local(
        db, iso, SCAN_LIMIT, ONE_HOT, DIM, -1.0, "", "",
        limit=2, agent_id=AGENT, project_id=None,
    )

    assert [item["id"] for _, item in got] == [ids[1], ids[3]]


# --------------------------------------------------------------------------
# (g) equivalence with the implementation this replaced
# --------------------------------------------------------------------------


def _old_scan_rank(rows, query_vec, query_dim, effective_min_sim, limit):
    """The pre-change ranking, verbatim: filter, join, one matmul, then the cut.

    Reimplemented here rather than imported from history on purpose. Reading it
    out of `git show master:cpersona/vector.py` works exactly once -- the moment
    this change lands on master, the "old" implementation such a helper extracts
    is the new one, and the comparison silently becomes a run of the same code
    against itself.
    """
    import heapq

    valid_ids = []
    blobs = []
    for row in rows:
        blob = row[1]
        if blob and len(blob) == query_dim * 4:
            valid_ids.append(row[0])
            blobs.append(blob)
    if not valid_ids:
        return []
    sims = vector._cosine_batch(query_vec, query_dim, blobs)
    survivors = [
        (valid_ids[i], float(sim_val))
        for i, sim_val in enumerate(sims)
        if sim_val >= effective_min_sim
    ]
    if limit is not None and limit < len(survivors):
        keep = heapq.nlargest(limit, range(len(survivors)), key=lambda i: (survivors[i][1], -i))
        survivors = [survivors[i] for i in sorted(keep)]
    return survivors


@pytest_asyncio.fixture
async def near_tie_corpus(db):
    """2,400 rows of real embeddings, seeded with rows one ULP apart.

    Near-ties rather than exact ties, because an exact tie is decided by the
    ordinal and cannot show a score moving; two rows a single ULP apart are what
    a one-ULP shift can reorder. Real (dense, normalised) vectors, because the
    one-hot rows the rest of the file uses are exact by construction and would
    make this test unable to observe the thing it exists for.
    """
    base = np.array(fake_embed_one("alpha beta gamma"), dtype=np.float32)
    blobs = []
    for n in range(2400):
        if n % 7 == 0:
            vec = base.copy()
            lane = n % DIM
            vec[lane] = np.nextafter(vec[lane], np.float32(1e9))
        elif n % 11 == 0:
            vec = base.copy()
        else:
            vec = np.array(fake_embed_one(f"alpha beta row {n}"), dtype=np.float32)
        blobs.append(vec.tobytes())
    await _seed(db, blobs)
    return base


async def _window(db, limit=SCAN_LIMIT):
    """The window as the old implementation read it: one statement, every row."""
    return await db.execute_fetchall(MEM_SQL, (AGENT, limit))


def _scored_batch_sizes(rows: int, chunk: int) -> list[int]:
    """The row counts the scan hands to `_cosine_batch` for a window of `rows`.

    Stated here independently of the loop rather than read off it, so that the
    two have to agree: `test_the_lookahead_merges_a_short_tail_into_the_chunk_
    before_it` asserts the implementation's actual calls against this, and the
    exactness probe below uses it to ask the platform about the same shapes the
    run will really use. A short tail is merged into the chunk before it, so the
    only small matrix possible is a window smaller than one chunk.
    """
    if rows <= chunk:
        return [rows] if rows else []
    whole, tail = divmod(rows, chunk)
    sizes = [chunk] * whole
    if tail:
        sizes[-1] += tail
    return sizes


def _arithmetic_survives_chunking(rows, query_vec, chunk: int) -> bool:
    """Does this platform's BLAS give the same scores when the same bytes are
    multiplied in the batches the scan will use?

    Asked of the arithmetic alone -- `_cosine_batch` over the same blob list,
    with no cursor, no loop and no heap in the way -- so it is a fact about the
    machine rather than about the code under test. Nothing here can hide a
    defect in the scan: the row list and the order are asserted unconditionally,
    and a wrong query vector or a re-implemented dot product moves scores far
    beyond the bound the other branch allows.

    It has to try the WHOLE window rather than a prefix of `chunk` rows: at
    chunk 1 the first row's score happens to agree with the full matmul while a
    third of the others do not, so a prefix probe reports stability that the run
    does not have.
    """
    blobs = [row[1] for row in rows]
    full = vector._cosine_batch(query_vec, DIM, blobs)
    taken = 0
    parts = []
    for size in _scored_batch_sizes(len(blobs), chunk):
        parts.append(vector._cosine_batch(query_vec, DIM, blobs[taken : taken + size]))
        taken += size
    part = np.concatenate(parts)
    return np.array_equal(full.view(np.uint32), part.view(np.uint32))


@pytest.mark.parametrize("chunk", [1, 7, 512, 100000])
@pytest.mark.asyncio
async def test_the_chunked_scan_answers_what_the_old_scan_answered(
    db, monkeypatch, near_tie_corpus, chunk
):
    """Same rows, same order, and the same float32 scores where the arithmetic
    is owed to be identical.

    `limit=None` and a threshold below every score, so the row list is the whole
    window and cannot be reordered by a score moving -- that isolates the
    scores, which is what this test is about. What the cut does when a score
    moves is a different question, and the module docstring records the measured
    answer (it can change the returned id).
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", chunk)
    rows = await _window(db)
    expected = _old_scan_rank(rows, near_tie_corpus, DIM, -1.0, None)

    got = [
        (row_id, score)
        for _, row_id, score in await _scan(db, limit=None, query_vec=near_tie_corpus)
    ]

    assert [i for i, _ in got] == [i for i, _ in expected], (
        f"chunk={chunk} returned a different set of rows, or a different order"
    )

    # Exactness is demanded exactly where this machine can owe it.
    if _arithmetic_survives_chunking(rows, near_tie_corpus, chunk):
        assert got == expected, (
            f"chunk={chunk} moved a score although every chunk it scored reaches "
            "the row count at which this platform's BLAS is stable -- the "
            "difference is this code, not the arithmetic"
        )
    else:
        # Below the switch point the scores may move, but only by what
        # re-associating the sum can do: for a DIM-term float32 dot product of
        # unit vectors that is DIM * eps (the terms sum to at most 1 by
        # Cauchy-Schwarz). Measured here, the largest move is about 3 ULP. A
        # real defect -- the wrong row, the wrong query vector, a hand-written
        # dot product -- is orders of magnitude outside this, so the bound still
        # has teeth while refusing to encode one machine's rounding.
        bound = DIM * float(np.finfo(np.float32).eps)
        moved = [(a, b) for (_, a), (_, b) in zip(got, expected) if abs(a - b) > bound]
        assert not moved, (
            f"chunk={chunk} moved {len(moved)} scores by more than {bound:.2e}, which is "
            f"more than re-associating the sum can account for: {moved[:5]}"
        )


@pytest.mark.asyncio
async def test_a_window_that_fits_in_one_chunk_is_bit_identical(db, monkeypatch, near_tie_corpus):
    """The claim that holds on every platform, because it is the same call.

    One chunk means one matmul over exactly the rows the old scan multiplied, in
    the same order -- so this is the one place `==` on float32 is owed
    unconditionally, and it is the configuration that ships (a window smaller
    than 512 rows).
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 100000)
    rows = await _window(db)
    expected = _old_scan_rank(rows, near_tie_corpus, DIM, 0.2, 25)

    got = [
        (row_id, score)
        for _, row_id, score in await _scan(
            db, limit=25, min_sim=0.2, query_vec=near_tie_corpus
        )
    ]

    assert got == expected
    assert expected, "the fixture must produce survivors, or this proves nothing"


# The window sizes that drifted before the short tail was merged: 2,050 ends in
# 2 rows, 2,060 in 12 and 2,100 in 52, all below the 64-row switch point
# measured here, and 27 of those 52 scores moved. 2,048 and 2,400 end in 512 and
# 352 and never drifted -- they are here so the test cannot pass by only
# covering the easy shapes.
@pytest.mark.parametrize("window", [2048, 2050, 2060, 2100, 2400])
@pytest.mark.asyncio
async def test_the_shipped_chunk_size_is_exact_on_windows_that_used_to_drift(
    db, monkeypatch, near_tie_corpus, window
):
    """The default configuration, on windows that do not divide evenly.

    The window is set by the statement's LIMIT rather than by seeding a
    different corpus, so every case here is the same rows and the same query --
    only the ragged remainder changes, which is the variable under test.

    Every row of the window is compared, not the top of the answer: the rows
    that drifted are the ones in the ragged remainder, which is the OLDEST end
    of the window and almost never reaches a limited response. A version of this
    test that compared a top-25 passed with the drift still present and with the
    tail dropped entirely -- measured, not supposed.
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 512)
    rows = await _window(db, limit=window)
    assert len(rows) == window, "the corpus is too small to cut this window"
    if not _arithmetic_survives_chunking(rows, near_tie_corpus, 512):
        pytest.skip(
            "this platform's BLAS does not give the same scores for the batch sizes "
            f"{_scored_batch_sizes(window, 512)}; bit identity cannot be owed here"
        )
    expected = _old_scan_rank(rows, near_tie_corpus, DIM, -1.0, None)

    got = [
        (row_id, score)
        for _, row_id, score in await _scan(
            db, limit=None, min_sim=-1.0, query_vec=near_tie_corpus,
            params=(AGENT, window),
        )
    ]

    assert len(expected) == window, "the oracle must rank the whole window"
    assert got == expected


@pytest.mark.parametrize("rows,sizes", [(9, [4, 5]), (8, [4, 4]), (3, [3]), (4, [4])])
@pytest.mark.asyncio
async def test_the_lookahead_merges_a_short_tail_into_the_chunk_before_it(
    db, monkeypatch, rows, sizes
):
    """The shape of the read, not its answer.

    Nine rows in chunks of four must be scored as [4, 5] and never [4, 4, 1]:
    the answer is the same either way on a corpus this small and this exact, so
    the batch sizes are the only place the merge is visible. They are also the
    whole reason the merge exists -- a matrix of one row is the case the BLAS
    answers differently.

    Every seeded row passes the width filter, so what `_cosine_batch` receives
    is the batch the cursor produced.
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    await _seed(db, [_vec(0.9 - 0.01 * n) for n in range(rows)])
    seen = []
    real = vector._cosine_batch

    def recording(query_vec, query_dim, blobs):
        seen.append(len(blobs))
        return real(query_vec, query_dim, blobs)

    monkeypatch.setattr(vector, "_cosine_batch", recording)

    await _scan(db, limit=None)

    assert seen == sizes, (
        f"{rows} rows in chunks of 4 were scored as {seen} rather than {sizes}; a "
        "matrix smaller than the chunk was multiplied on its own"
    )
    assert seen == _scored_batch_sizes(rows, 4), "the oracle and the case disagree"


# --------------------------------------------------------------------------
# (h) the cursor's lifetime
# --------------------------------------------------------------------------


class _WatchedStatement:
    """`execute`'s return value, with the cursor's own `close` observed.

    The close being watched is the real one aiosqlite performs on the way out of
    the `async with`, not a reimplementation of it: the wrapper replaces the
    method on the cursor instance and calls through.
    """

    def __init__(self, opened, watch):
        self._opened = opened
        self._watch = watch

    async def __aenter__(self):
        cursor = await self._opened.__aenter__()
        original = cursor.close

        async def watched_close():
            self._watch["closed"] += 1
            await original()

        cursor.close = watched_close
        return cursor

    async def __aexit__(self, *exc_info):
        return await self._opened.__aexit__(*exc_info)


class _WatchedDb:
    def __init__(self, conn, watch):
        self._conn = conn
        self._watch = watch

    def execute(self, sql, params=()):
        self._watch["opened"] += 1
        return _WatchedStatement(self._conn.execute(sql, params), self._watch)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _raise_on_second_chunk(monkeypatch, exc):
    """Let the first chunk through and fail inside the loop on the second."""
    calls = {"n": 0}
    real = vector._cosine_batch

    def failing(query_vec, query_dim, blobs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise exc
        return real(query_vec, query_dim, blobs)

    monkeypatch.setattr(vector, "_cosine_batch", failing)
    return calls


@pytest.mark.asyncio
async def test_the_cursor_is_closed_on_the_ordinary_path(db, monkeypatch):
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    await _seed(db, [_vec(0.9)] * 10)
    watch = {"opened": 0, "closed": 0}

    got = await _scan(_WatchedDb(db, watch), limit=None)

    assert len(got) == 10
    assert watch == {"opened": 1, "closed": 1}


@pytest.mark.asyncio
async def test_an_exception_inside_the_loop_propagates_and_closes_the_cursor(db, monkeypatch):
    """The scan does not catch. A window it could not read must not come back
    as a short answer that is indistinguishable from a small corpus."""
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    await _seed(db, [_vec(0.9)] * 10)
    watch = {"opened": 0, "closed": 0}
    boom = RuntimeError("the matmul failed")
    _raise_on_second_chunk(monkeypatch, boom)

    with pytest.raises(RuntimeError) as caught:
        await _scan(_WatchedDb(db, watch), limit=None)

    assert caught.value is boom
    assert watch == {"opened": 1, "closed": 1}


@pytest.mark.asyncio
async def test_cancellation_inside_the_loop_propagates_and_closes_the_cursor(db, monkeypatch):
    """Cancellation is the case a bare `except Exception` would swallow and a
    missing `async with` would leak: the statement outlives the task."""
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 4)
    await _seed(db, [_vec(0.9)] * 10)
    watch = {"opened": 0, "closed": 0}
    _raise_on_second_chunk(monkeypatch, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _scan(_WatchedDb(db, watch), limit=None)

    assert watch == {"opened": 1, "closed": 1}
