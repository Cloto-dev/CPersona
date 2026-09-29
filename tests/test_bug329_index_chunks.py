"""bug-329: the index path builds a copied window a chunk at a time.

When the window cannot be a view of the mapped file -- a row written since the
build, a second agent's rows interleaved, any gap in the selection -- phase 1
used to build the whole window as one matrix, so its memory grew with the scan
window and the reach while the SQL scan it stands in for held one chunk. It now
scores a copied window in the ranges the SQL scan scores, which bounds the peak
by the chunk and makes the two suppliers hand `_cosine_matrix` the same rows in
the same shapes.
"""

import os
import tracemalloc

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from tests.conftest import fake_embed_one

AGENT = "chunks.agent"
TOPIC = "alpha beta gamma"


def _clean_index():
    for path in (vector_index.index_path("memories"), vector_index.index_path("memories") + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)


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


async def _store(db, n, *, start=0, vectors=None):
    await db.executemany(
        "INSERT INTO memories (agent_id, content, source, timestamp, created_at, embedding)"
        " VALUES (?, ?, '{}', 't', ?, ?)",
        [
            (
                AGENT, f"row {i}", f"2026-03-01 {i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}",
                (
                    np.array(fake_embed_one(f"{TOPIC} {i % 5}"), dtype=np.float32)
                    if vectors is None else vectors[i - start]
                ).tobytes(),
            )
            for i in range(start, start + n)
        ],
    )
    await db.commit()


@pytest.fixture(autouse=True)
def seven_row_chunks(monkeypatch):
    # Small enough that a 61-row window spans several chunks and a short tail.
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 7)


@pytest.fixture
def scored_rows(monkeypatch):
    """The row count of every matrix `_cosine_matrix` scores, in call order."""
    seen: list[int] = []
    real = vector._cosine_matrix

    def recording(query_vec, mat):
        seen.append(int(mat.shape[0]))
        return real(query_vec, mat)

    monkeypatch.setattr(vector, "_cosine_matrix", recording)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [1, 6, 7, 8, 13, 14, 15, 21, 22, 50])
async def test_the_bounds_are_the_ranges_the_scan_scores(db, scored_rows, rows):
    await _store(db, rows)
    query = np.array(fake_embed_one(TOPIC), dtype=np.float32)
    await vector._chunked_cosine_scan(
        db,
        "SELECT id, embedding FROM memories WHERE agent_id = ? ORDER BY id LIMIT ?",
        (AGENT, rows),
        query,
        query.shape[0],
        -1.0,
        None,
    )
    assert scored_rows == [hi - lo for lo, hi in vector._scan_chunk_bounds(rows, 7)]


@pytest.mark.asyncio
async def test_a_copied_window_is_scored_in_the_scans_chunks(
    db, scored_rows, monkeypatch, fake_embedding_client
):
    await _store(db, 60)
    assert (await vector_index.build_index(db, "memories"))["built"]
    await _store(db, 1, start=60)  # written since the build: the window is copied
    taken = []
    real_phase1 = vector._index_phase1

    async def counting(*args, **kwargs):
        result = await real_phase1(*args, **kwargs)
        if kwargs.get("table", "memories") == "memories":
            taken.append(result is not None)
        return result

    monkeypatch.setattr(vector, "_index_phase1", counting)

    async def search():
        return await vector._search_vector(db, AGENT, TOPIC, 10, min_similarity=-1.0)

    from_index = await search()
    index_shapes = list(scored_rows)
    scored_rows.clear()
    monkeypatch.setattr(vector_index, "cached_index", lambda table="memories", path=None: None)
    from_scan = await search()
    scan_shapes = list(scored_rows)

    assert taken == [True, False], "one run from the index, the other from the scan"
    # 61 rows in chunks of 7, the short remainder carried into the last chunk.
    assert index_shapes == [7] * 7 + [12]
    assert index_shapes == scan_shapes
    assert from_index and from_index == from_scan


@pytest.mark.asyncio
async def test_the_peak_does_not_grow_with_the_window(db, monkeypatch):
    """Measured with the shipped chunk size: before the fix the traced peak was
    at least the whole window matrix; now it is a fraction of it."""
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 64)
    dim, rows = 256, 8000
    rng = np.random.default_rng(329)
    vectors = rng.standard_normal((rows, dim)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    await _store(db, rows, vectors=list(vectors))
    assert (await vector_index.build_index(db, "memories"))["built"]
    index = vector_index.load_index("memories")
    positions = np.arange(0, rows, 2, dtype=np.int64)  # every other row: a gap each time
    window = len(positions)
    query = vectors[0]

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        ids, sims = vector._score_merged_window(index, positions, [], window, dim, query)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    window_bytes = window * dim * 4
    assert len(ids) == window and sims.shape == (window,)
    # Before the fix: a (window x dim) matrix plus its gather blocks, >= 1.0x.
    assert peak < 0.5 * window_bytes, (peak, window_bytes)

    # And the scores are the whole-window reference, scored in the same ranges.
    _, whole = vector._merge_index_and_tail(index, positions, [], window, dim)
    reference = np.concatenate([
        vector._cosine_matrix(query, np.array(whole[lo:hi]))
        for lo, hi in vector._scan_chunk_bounds(window, 64)
    ])
    assert sims.tobytes() == reference.tobytes()
