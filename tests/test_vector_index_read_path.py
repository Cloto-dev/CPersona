"""The contiguous index answers exactly what the live scan answers.

Design: `docs/CONTIGUOUS_INDEX_DESIGN.md`. The index is only allowed to exist
because it changes latency and nothing else, so the comparison here is not
"close enough" — it is the identical list of rows, in the identical order, with
identical scores, out of `_search_vector` itself rather than out of the scan
underneath it.

Two things this file is careful about, because without them it would pass while
proving nothing:

- **The index path must actually be taken.** Every fallback in the design is
  silent by construction (that is what makes it safe), so a test that quietly
  fell back would compare the live scan against itself and report agreement.
  `taken` counts the calls that returned index-supplied rows, and every
  equivalence test asserts it.
- **Ties have to be in the corpus.** Well-separated vectors rank the same under
  any tie-break, so a corpus without equal scores cannot tell a correct order
  from a wrong one.
"""

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from tests.conftest import fake_embed_one

AGENT = "readpath.agent"
OTHER = "readpath.other"
TOPIC = "alpha beta gamma"
_SHARED = fake_embed_one(f"{TOPIC} shared")


async def _store(db, content, *, created_at, project_id="", channel="", agent_id=AGENT,
                 source_id="user-a", embedding=None):
    vec = fake_embed_one(content) if embedding is None else embedding
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            agent_id, project_id, channel, content,
            '{"type": "User", "id": "%s"}' % source_id,
            "2026-03-01T00:00:00+00:00", created_at,
            np.array(vec, dtype=np.float32).tobytes(),
        ),
    )


@pytest_asyncio.fixture
async def corpus(fake_embedding_client):
    """A corpus with deliberate score ties inside one timestamp, axes varied.

    The same text is stored under different axes, so several rows carry the same
    embedding and therefore the same similarity — which is what makes the order
    a claim rather than an accident.
    """
    db = await get_db()
    for path in (vector_index.index_path("memories"), vector_index.index_path("memories") + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)
    await db.execute("DELETE FROM memories")

    n = 0
    for stamp in ("2026-03-01 00:00:00", "2026-03-01 00:00:01", "2026-03-01 00:00:02"):
        # Three values per axis, not two. With only '' and 'proj-x' the gamma
        # union ('X' means X *or* the global pool) admits every row, so a filter
        # on that axis excludes nothing and a test using it cannot fail. The
        # third value is what makes the axis observable.
        for project in ("", "proj-x", "proj-y"):
            for channel in ("", "c1", "c2"):
                for agent in (AGENT, OTHER):
                    n += 1
                    # Distinct content (a UNIQUE index dedups on agent/project/
                    # channel/content) but a SHARED embedding, so the scores tie
                    # while the rows stay storable. Ties are the point: a corpus
                    # of well-separated vectors ranks the same under any
                    # tie-break and cannot tell a right order from a wrong one.
                    await _store(
                        db,
                        f"row {n}",
                        created_at=stamp, project_id=project, channel=channel,
                        agent_id=agent, source_id=f"user-{n % 2}",
                        embedding=_SHARED if n % 3 else fake_embed_one(f"unrelated {n}"),
                    )
    await db.commit()
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()
    for path in (vector_index.index_path("memories"), vector_index.index_path("memories") + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def taken(monkeypatch):
    """Count the phase-1 calls the index actually answered.

    Every way this can go wrong ends in a silent fallback, so 'the answers match'
    is only worth anything alongside 'the index produced one of them'.
    """
    counter = {"index": 0, "fallback": 0}
    real = vector._index_phase1

    async def counting(*args, **kwargs):
        result = await real(*args, **kwargs)
        counter["index" if result is not None else "fallback"] += 1
        return result

    monkeypatch.setattr(vector, "_index_phase1", counting)
    return counter


async def _search(db, **kw):
    return await vector._search_vector(db, AGENT, TOPIC, 10, min_similarity=-1.0, **kw)


_SCENARIOS = {
    "no-filter": {},
    "project-gamma": {"project_id": "proj-x"},
    "project-global-only": {"project_id": ""},
    "channel": {"channel": "c1"},
    "channel-and-project": {"channel": "c1", "project_id": "proj-x"},
    "source-prefix": {"source_id": "user-1"},
    "source-and-channel": {"source_id": "user-1", "channel": "c1"},
    "unknown-source": {"source_id": "nobody-"},
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.asyncio
async def test_index_answers_exactly_what_the_scan_answers(corpus, taken, name):
    scenario = _SCENARIOS[name]
    expected = await _search(corpus, **scenario)
    assert taken["index"] == 0, "no index exists yet — the baseline must be the live scan"

    result = await vector_index.build_index(corpus, "memories")
    assert result["built"], result

    actual = await _search(corpus, **scenario)
    assert taken["index"] == 1, f"[{name}] the index path was not taken; the comparison is vacuous"
    assert actual == expected, f"[{name}] the index changed the answer"


@pytest.mark.asyncio
async def test_rows_written_after_the_build_are_still_found(corpus, taken):
    """The watermark's whole point: no window in which a new memory is invisible."""
    await vector_index.build_index(corpus, "memories")
    # Tied with the corpus and newer than all of it: scan order then says it
    # leads. Asserting the lead rather than mere membership keeps the claim
    # independent of how many rows the response limit happens to admit.
    await _store(
        corpus, f"{TOPIC} freshly written", created_at="2026-03-01 00:00:09",
        embedding=_SHARED,
    )
    await corpus.commit()

    rows = await _search(corpus)
    assert taken["index"] >= 1
    assert rows and "freshly written" in rows[0]["content"], (
        "a row written after the build must reach the answer through the exact tail, "
        "and its recency must place it where the live scan would"
    )
    assert [r["id"] for r in rows] == [r["id"] for r in await _search_without_index(corpus)]


@pytest.mark.asyncio
async def test_a_row_embedded_after_the_build_is_still_found(corpus, taken):
    """bug-278: the watermark cannot answer for a row that existed but had no vector.

    The builder collects rows that have an embedding, so a NULL row is in neither
    the matrix nor the excluded list; its id is at or below the watermark, so the
    tail did not read it either. Filling the embedding in — which is exactly what
    check_health(fix=True) does to every NULL row it finds — used to leave the row
    in no set the index path consults, and recall answered successfully with one
    memory fewer.

    Asserted as an equivalence against the scan rather than as membership: the
    claim is that the two paths return the same rows, and a membership assertion
    would still pass if the index path found the row and mis-ordered it.
    """
    # Stored with a vector and then blanked, because the helper writes a real one:
    # what matters is the state the builder sees, which is embedding IS NULL.
    await _store(corpus, f"{TOPIC} not embedded yet", created_at="2026-03-01 00:00:04",
                 embedding=_SHARED)
    await corpus.execute(
        "UPDATE memories SET embedding = NULL WHERE content = ?", (f"{TOPIC} not embedded yet",)
    )
    await corpus.commit()

    result = await vector_index.build_index(corpus, "memories")
    assert result["built"], result

    # The maintenance path fills it in. Same dimension, no re-tag, no delete.
    row = await corpus.execute_fetchall(
        "SELECT id FROM memories WHERE embedding IS NULL"
    )
    assert row, "the fixture no longer produces a NULL-embedding row"
    await corpus.execute(
        "UPDATE memories SET embedding = ? WHERE id = ?",
        (np.array(_SHARED, dtype=np.float32).tobytes(), row[0][0]),
    )
    await corpus.commit()

    rows = await _search(corpus)
    assert taken["index"] >= 1, "the index path was not taken; the comparison is vacuous"
    assert [r["id"] for r in rows] == [r["id"] for r in await _search_without_index(corpus)], (
        "a row embedded after the build must reach the answer through the exact tail"
    )
    assert any("not embedded yet" in r["content"] for r in rows), (
        "the row the scan returns is missing from the index path's answer"
    )


@pytest.mark.asyncio
async def test_a_tail_row_older_than_the_index_lands_in_the_right_place(corpus, taken):
    """An ordered merge, not a prepend.

    The import path carries a restored record's original `created_at` while ids
    are assigned fresh, so a row can be newer by id and older by timestamp. A
    concatenation would put it at the front of the window; the scan puts it where
    its timestamp says. This test is the difference between the two.
    """
    await vector_index.build_index(corpus, "memories")

    # Newest id, oldest timestamp — the shape a restore produces — and TIED with
    # the rows already in the index. The tie is what makes this test able to
    # fail: scan order is only observable through the tie-break, so a row with a
    # score of its own would rank the same however the merge ordered it.
    await _store(
        corpus, f"{TOPIC} restored", created_at="2025-01-01 00:00:00", embedding=_SHARED
    )
    await corpus.commit()

    actual = await _search(corpus)
    assert taken["index"] >= 1
    live_order = [r["id"] for r in await _search_without_index(corpus)]
    assert [r["id"] for r in actual] == live_order, "the merge put the restored row elsewhere"


async def _search_without_index(db, **kw):
    """The same query with the index removed, as the order oracle."""
    path = vector_index.index_path("memories")
    saved = open(path, "rb").read() if os.path.exists(path) else None
    if saved is not None:
        os.unlink(path)
    try:
        return await _search(db, **kw)
    finally:
        if saved is not None:
            with open(path, "wb") as fh:
                fh.write(saved)


@pytest.mark.asyncio
async def test_a_row_the_format_cannot_spell_stays_reachable(corpus, taken):
    """An excluded row is named in the header and read from the tail, not lost."""
    # Tied with the corpus, so its presence or absence is visible in the answer
    # rather than buried under the response limit. Its 'T' separator also sorts
    # after a space in SQLite's text comparison, which puts it first in scan
    # order — so if the tail forgot it, the answer loses its leading row.
    await _store(
        corpus, f"{TOPIC} odd stamp", created_at="2026-03-01T00:00:05", embedding=_SHARED
    )
    await corpus.commit()
    expected = await _search(corpus)
    assert "odd stamp" in expected[0]["content"], "the fixture must make the row observable"

    result = await vector_index.build_index(corpus, "memories")
    assert result["excluded"] == 1, "the non-canonical row should be named, not silently kept"

    actual = await _search(corpus)
    assert taken["index"] == 1
    assert actual == expected, "an excluded row must still reach the answer"


@pytest.mark.asyncio
async def test_absent_index_falls_back(corpus, taken):
    expected = await _search(corpus)
    await vector_index.build_index(corpus, "memories")
    os.unlink(vector_index.index_path("memories"))
    assert await _search(corpus) == expected
    assert taken["index"] == 0, "a deleted index must not still be answering"


@pytest.mark.asyncio
async def test_corrupt_index_falls_back(corpus, taken):
    expected = await _search(corpus)
    await vector_index.build_index(corpus, "memories")
    path = vector_index.index_path("memories")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 8)

    assert await _search(corpus) == expected
    assert taken["index"] == 0, "a corrupt index must be refused, not partially read"


@pytest.mark.asyncio
async def test_dimension_mismatch_falls_back(corpus, taken, monkeypatch):
    expected = await _search(corpus)
    await vector_index.build_index(corpus, "memories")

    index = vector_index.load_index("memories")
    monkeypatch.setattr(
        vector_index, "cached_index",
        lambda *a, **k: type(index)(**{**index.__dict__, "dim": index.dim + 1}),
    )
    assert await _search(corpus) == expected
    assert taken["index"] == 0, "a query of another dimension must not be served from this file"


@pytest.mark.asyncio
async def test_a_rebuild_is_picked_up_without_anyone_invalidating_a_cache(corpus, taken):
    """Rebuild cadence is a performance knob only if a rebuild is seen on its own."""
    await vector_index.build_index(corpus, "memories")
    await _search(corpus)  # warms the cache

    await _store(corpus, f"{TOPIC} second wave", created_at="2026-03-01 00:00:08")
    await corpus.commit()
    result = await vector_index.build_index(corpus, "memories")

    index = vector_index.cached_index("memories")
    assert index.watermark == result["watermark"], "the cache served a superseded file"
    assert taken["index"] >= 1


@pytest.mark.asyncio
async def test_a_foreign_width_row_written_after_the_build_falls_back(corpus, taken):
    """The window, not the arithmetic, is what a mixed corpus breaks.

    The live scan windows before it skips foreign-width rows, so once one exists
    the index cannot reproduce which rows fall inside the window. It declines,
    and the answer stays the scan's.
    """
    await vector_index.build_index(corpus, "memories")
    await _store(
        corpus, f"{TOPIC} wrong width", created_at="2026-03-01 00:00:07",
        embedding=np.zeros(4, dtype=np.float32),
    )
    await corpus.commit()

    expected = await _search_without_index(corpus)
    actual = await _search(corpus)
    assert actual == expected
    assert taken["index"] == 0, "a tail row of another width must send the query back to SQL"


async def _built_index(db):
    """Build the index over the fixture corpus and map it."""
    result = await vector_index.build_index(db, "memories")
    assert result["built"], result
    index = vector_index.load_index("memories")
    assert index is not None and index.count > 6, (
        "these tests slice positions out of the file and need rows to slice"
    )
    return index


def _tail_row(mem_id: int, created_at: str, embedding):
    """One row in the shape `_index_tail_rows` hands to the merge."""
    blob = np.array(embedding, dtype=np.float32).tobytes()
    return (mem_id, created_at, blob, len(blob))


def _gathered(index, positions, tail, scan_limit, query_dim, monkeypatch):
    """The same merge with the contiguous view refused: the copy path, exactly.

    Written as a monkeypatch of the predicate rather than as a re-implementation
    of the gather here, so the comparison is against the code that ships instead
    of against a second copy of it that could agree by being wrong the same way.
    """
    monkeypatch.setattr(vector, "_is_ascending_run", lambda positions: False)
    try:
        return vector._merge_index_and_tail(index, positions, tail, scan_limit, query_dim)
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_a_contiguous_all_index_selection_is_read_as_a_view(corpus):
    """No copy for the shape the whole change exists for.

    Asserted as `shares_memory` and not as latency, because latency is not
    testable here and because the failure this guards against — a return to
    gathering unconditionally — produces identical answers. Every other test in
    this file would stay green through it, so without this assertion the only
    evidence that the fast path is still taken is a benchmark nobody runs on a
    pull request.
    """
    index = await _built_index(corpus)
    positions = np.arange(1, 5, dtype=np.int64)

    merged_ids, mat = vector._merge_index_and_tail(index, positions, [], 100, index.dim)

    assert merged_ids == [int(index.ids[p]) for p in positions]
    assert np.shares_memory(mat, index.embeddings), (
        "a contiguous, all-index selection must be handed to the matmul as a slice "
        "of the mapped file, not copied out of it row by row"
    )


@pytest.mark.asyncio
async def test_a_selection_with_a_tail_row_is_copied(corpus):
    """A tail row's vector is not in the file, so no slice of the file can hold it."""
    index = await _built_index(corpus)
    positions = np.arange(0, 4, dtype=np.int64)
    # Older than every indexed row, so it lands at the end of the merge rather
    # than at the front: the interleave, not a prepend, decides where it goes.
    tail = [_tail_row(10_000, "2025-01-01 00:00:00", _SHARED)]

    merged_ids, mat = vector._merge_index_and_tail(index, positions, tail, 100, index.dim)

    assert merged_ids[-1] == 10_000
    assert not np.shares_memory(mat, index.embeddings), (
        "a matrix holding a row that is not in the file must be a copy"
    )
    assert np.array_equal(mat[-1], np.array(_SHARED, dtype=np.float32))


@pytest.mark.asyncio
async def test_a_scattered_selection_is_copied(corpus):
    """A gap in the positions is what makes the copy necessary rather than slow.

    A slice cannot spell `0, 2, 3`, and the alternative the measurement rejected
    — score every row and then take the wanted scores — changes the summation
    order for exactly this shape. So the scattered case must keep the gather.
    """
    index = await _built_index(corpus)
    positions = np.array([0, 2, 3], dtype=np.int64)

    merged_ids, mat = vector._merge_index_and_tail(index, positions, [], 100, index.dim)

    assert merged_ids == [int(index.ids[p]) for p in (0, 2, 3)]
    assert not np.shares_memory(mat, index.embeddings), (
        "a selection with a gap must not be served as a slice: the slice would "
        "carry the row between the two the caller asked for"
    )
    assert np.array_equal(mat, np.asarray(index.embeddings)[[0, 2, 3]])


@pytest.mark.asyncio
async def test_the_view_scores_bit_for_bit_like_the_copy(corpus, monkeypatch):
    """The exactness the view is only allowed to exist under.

    Mid-file rather than from position 0, because a run starting at 0 would also
    pass if the slice ignored its start; and through `_cosine_matrix`, because
    the claim is about the scores the ranking uses, not about the bytes on the
    way in. Bitwise equality, not `allclose`: the index is permitted to change
    latency and nothing else, and a float32 difference of one ulp is a different
    answer at a tie.
    """
    index = await _built_index(corpus)
    positions = np.arange(2, 6, dtype=np.int64)
    query = np.array(fake_embed_one(TOPIC), dtype=np.float32)

    view_ids, view_mat = vector._merge_index_and_tail(index, positions, [], 100, index.dim)
    copy_ids, copy_mat = _gathered(index, positions, [], 100, index.dim, monkeypatch)

    assert np.shares_memory(view_mat, index.embeddings), "the view path was not taken"
    assert not np.shares_memory(copy_mat, index.embeddings), "the copy path was not taken"
    assert view_ids == copy_ids, "the view changed which rows the merge names"

    view_scores = vector._cosine_matrix(query, view_mat)
    copy_scores = vector._cosine_matrix(query, copy_mat)
    assert view_scores.dtype == copy_scores.dtype
    assert view_scores.tobytes() == copy_scores.tobytes(), (
        "the view must score identically to the copy, bit for bit"
    )
