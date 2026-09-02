"""The merge pays for the interleave walk only for the shape that needs it.

With an empty tail there is nothing to interleave: the output is the selection
in the order `select()` already returns it. The walk — one tuple comparison per
output row — was 79 ms of a 212 ms arm at 100,000 rows for that shape
(benchmarks/measurements/results-contiguous-index.md), and nothing in the
answer shows whether it ran, so this pins the path rather than the timing: the
walk is a separate function, and these tests count its calls.

The exactness claims stay where they were: the empty-tail path must name the
same rows in the same order as the walk would have, for a contiguous and for a
scattered selection, and `_is_ascending_run` must still reject the shapes the
span test admits.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from tests.conftest import fake_embed_one

AGENT = "merge-paths.agent"
TOPIC = "alpha beta gamma"
_SHARED = fake_embed_one(f"{TOPIC} shared")


def _index_paths() -> tuple[str, str]:
    path = vector_index.index_path("memories")
    return path, path + ".tmp"


@pytest_asyncio.fixture
async def index(fake_embedding_client):
    db = await get_db()
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)
    await db.execute("DELETE FROM memories")
    for n in range(10):
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at, embedding) VALUES (?, '', '', ?, '{}', ?, ?, ?)",
            (
                AGENT, f"row {n}", "2026-03-01T00:00:00+00:00",
                f"2026-03-01 00:00:{n:02d}",
                np.array(_SHARED, dtype=np.float32).tobytes(),
            ),
        )
    await db.commit()
    assert (await vector_index.build_index(db, "memories"))["built"]
    built = vector_index.load_index("memories")
    assert built is not None and built.count == 10
    yield built
    await db.execute("DELETE FROM memories")
    await db.commit()
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def walk_calls(monkeypatch):
    """Count entries into the interleave walk without changing what it returns."""
    calls = []
    orig = vector._interleave_index_and_tail

    def counting(*args, **kwargs):
        calls.append(args)
        return orig(*args, **kwargs)

    monkeypatch.setattr(vector, "_interleave_index_and_tail", counting)
    return calls


def _tail_row(mem_id: int, created_at: str):
    blob = np.array(_SHARED, dtype=np.float32).tobytes()
    return (mem_id, created_at, blob, len(blob))


def _walked(index, positions, tail, scan_limit, query_dim):
    """The same merge forced through the walk: the reference, from shipped code."""
    merged_ids, from_index, index_slots, from_tail = vector._interleave_index_and_tail(
        index, positions, tail, scan_limit
    )
    return merged_ids, from_index


@pytest.mark.asyncio
async def test_an_empty_tail_does_not_enter_the_walk(index, walk_calls):
    positions = np.arange(0, index.count, dtype=np.int64)

    merged_ids, mat = vector._merge_index_and_tail(index, positions, [], 100, index.dim)

    assert walk_calls == [], "an all-index selection paid for the interleave walk"
    assert merged_ids == index.ids[positions].tolist()
    assert np.shares_memory(mat, index.embeddings), "and it must still be served as a view"


@pytest.mark.asyncio
async def test_a_tail_row_enters_the_walk(index, walk_calls):
    positions = np.arange(0, 4, dtype=np.int64)
    tail = [_tail_row(10_000, "2025-01-01 00:00:00")]

    merged_ids, _ = vector._merge_index_and_tail(index, positions, tail, 100, index.dim)

    assert len(walk_calls) == 1, "a tail row is the shape the walk exists for"
    assert merged_ids[-1] == 10_000


@pytest.mark.asyncio
async def test_the_empty_tail_path_names_the_rows_the_walk_would(index):
    """Contiguous and scattered, with and without the window cutting the selection."""
    for positions, scan_limit in (
        (np.arange(0, index.count, dtype=np.int64), 100),
        (np.array([0, 2, 3, 7], dtype=np.int64), 100),
        (np.arange(0, index.count, dtype=np.int64), 4),
        (np.array([1, 4, 5, 9], dtype=np.int64), 3),
    ):
        fast_ids, fast_mat = vector._merge_index_and_tail(index, positions, [], scan_limit, index.dim)
        walk_ids, walk_from_index = _walked(index, positions, [], scan_limit, index.dim)

        assert fast_ids == walk_ids, (positions.tolist(), scan_limit)
        assert np.array_equal(fast_mat, np.asarray(index.embeddings)[walk_from_index]), (
            positions.tolist(), scan_limit,
        )


@pytest.mark.parametrize(
    "positions, expected",
    [
        ([], False),
        ([5], True),
        ([3, 4, 5, 6], True),
        ([0, 0, 1, 3], False),   # the span test admits this one
        ([4, 3, 2], False),      # descending is not the run the slice reproduces
        ([2, 4, 6], False),
        (np.arange(10, 15), True),
        (np.array([10, 11, 13, 14]), False),
    ],
)
def test_is_ascending_run_is_exact(positions, expected):
    assert vector._is_ascending_run(positions) is expected
