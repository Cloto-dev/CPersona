"""A rebuild succeeds while the index it replaces is still loaded.

A running server keeps the index it loaded in ``vector_index._cache`` for as long as the process lives, and a
rebuild — from the CLI, from another process, or from this one — writes a temporary file and moves it over the
old one. A production user on Windows reported that the rebuild sometimes failed with the file replace refused,
and that it had once succeeded three times in a row with the same setup: consistent with the failure depending
on whether the server had searched (and so loaded the index) since it started.

POSIX lets a file that is still mapped be replaced, so on Linux and macOS this test passes either way. The
Windows job in CI is where it tells a held mapping from a released one.
"""

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector_index
from cpersona.database import get_db

AGENT = "rebuild.agent"
DIM = 8


async def _insert(db, n: int) -> None:
    rng = np.random.default_rng(n)
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at, embedding)"
        " VALUES (?, '', '', ?, '{}', '2026-03-01T00:00:00+00:00', ?, ?)",
        (AGENT, f"row {n}", f"2026-03-01 00:00:{n:02d}", rng.standard_normal(DIM).astype(np.float32).tobytes()),
    )


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    for n in range(5):
        await _insert(conn, n)
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


@pytest.mark.asyncio
async def test_a_rebuild_replaces_an_index_the_process_still_holds(db, tmp_path):
    path = str(tmp_path / "idx")
    try:
        await vector_index.build_index(db, "memories", path)
        held = vector_index.cached_index("memories", path)  # what a server that has searched keeps
        assert held is not None and held.count == 5
        before = np.array(held.embeddings[0])

        await _insert(db, 5)
        await db.commit()
        result = await vector_index.build_index(db, "memories", path)  # moves a new file over the held one
        assert result["built"] and result["count"] == 6

        fresh = vector_index.cached_index("memories", path)
        assert fresh.count == 6, "the cache kept serving the index the rebuild replaced"
        # The held copy still reads what it read before: replacing the file did not
        # change data a search already had.
        assert np.array_equal(np.array(held.embeddings[0]), before)
    finally:
        vector_index._cache.pop(path, None)


FIELDS = ("ids", "embeddings", "agent_code", "project_code", "channel_code", "source_code", "created_at")


@pytest.mark.asyncio
async def test_read_mode_loads_the_same_arrays_and_maps_nothing(db, tmp_path, monkeypatch):
    """The Windows path (_maps_files() False) on every platform: the arrays equal the mapped ones, field by field,
    and none of them is a mapping — a mapping is what kept the file from being replaced."""
    path = str(tmp_path / "idx")
    await vector_index.build_index(db, "memories", path)
    reference = vector_index.load_index("memories", path)

    monkeypatch.setattr(vector_index, "_maps_files", lambda: False)
    read = vector_index.load_index("memories", path)
    for field in FIELDS:
        got, want = getattr(read, field), getattr(reference, field)
        assert not isinstance(got, np.memmap), field
        assert got.dtype == want.dtype and got.shape == want.shape, field
        assert np.array_equal(got, want), field

