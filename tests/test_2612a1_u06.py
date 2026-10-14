"""Index builds: concurrent builders, odd stored timestamps, and repaired timestamps.

Three index files are derived from the database and published with a rename:
the block index, the coarse record index and the contiguous vector index. What
these tests pin:

- two builds of one file that run at the same time each write a temp file of
  their own, so neither truncates, interleaves or unlinks the other's, and the
  file left behind is a whole build (block, coarse and contiguous builders);
- a failed build still removes the temp files it created;
- one record whose timestamp SQLite reads as a negative year does not stop the
  coarse index from being built for every agent, and the index still answers
  exactly what the live store answers;
- a health repair that rewrites the timestamp of a record the coarse index holds
  invalidates the file, so a period search never answers from build-time stamps
  that the live store no longer has.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import os
import shutil
import tempfile

import aiosqlite
import numpy as np
import pytest
import pytest_asyncio

from cpersona import block_index, checks, coarse_index, coarse_search, config, database, vector_index
from cpersona.database import get_db

AGENT = "u06.agent"
OTHER = "u06.other"
DIM = 16


def _vector(seed: int, dim: int = DIM) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)


async def _insert(db, seed, **kw) -> int:
    row = {
        "agent_id": AGENT,
        "project_id": "",
        "channel": "",
        "content": f"row {seed}",
        "source": "{}",
        "timestamp": "2026-03-02T00:00:00+00:00",
        "created_at": "2026-03-01 00:00:%02d" % (seed % 60),
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
    coarse_index._cache.clear()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    coarse_index._cache.clear()
    for p in glob.glob(glob.escape(coarse_index.index_path("memories")) + "*"):
        os.unlink(p)


def _leftovers(out: str) -> list[str]:
    return sorted(p for p in glob.glob(glob.escape(out) + "*") if p != out)


# --------------------------------------------------------------------------------------
# Concurrent builds of one file
# --------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _block_database():
    saved = (database._db, database.DB_PATH, config.DB_PATH)
    directory = tempfile.mkdtemp()
    database._db = None
    database.DB_PATH = config.DB_PATH = os.path.join(directory, "block_index.db")
    block_index._cache.clear()
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        database._db, database.DB_PATH, config.DB_PATH = saved
        block_index._cache.clear()
        shutil.rmtree(directory, ignore_errors=True)


_BLOCK_PARENTS, _BLOCK_PER_PARENT, _BLOCK_WIDTH = 1500, 3, 16


async def _seed_blocks() -> dict:
    rng = np.random.default_rng(7)
    rows, truth = [], {}
    for pid in range(1, _BLOCK_PARENTS + 1):
        for bi in range(_BLOCK_PER_PARENT):
            # Never all zero, so a zero-filled row in the file is always a wrong row.
            bits = rng.integers(1, 256, _BLOCK_WIDTH, dtype=np.uint8).tobytes()
            rows.append(("mem", pid, bi, "agent.a", "", "", 0, 10, 0, bits, ""))
            truth[(pid, bi)] = bits
    async with database.transaction() as db:
        await db.executemany(
            "INSERT INTO record_blocks (parent_kind,parent_id,block_index,agent_id,project_id,"
            "channel,start_char,end_char,forced_boundary,embedding_bits,embedding_model)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return truth


async def _bits_temp_reaches(out: str, size: int, deadline_s: float = 30.0) -> None:
    # Globbed, not a fixed name: the builder's temp names are its own business.
    pattern = glob.escape(out) + ".*bits*"
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    while loop.time() < end:
        for name in glob.glob(pattern):
            with contextlib.suppress(FileNotFoundError):
                if os.path.getsize(name) >= size:
                    return
        await asyncio.sleep(0.0005)
    raise AssertionError("the first build never wrote half of its bits")


@pytest.mark.asyncio
async def test_two_block_index_builds_at_once_both_publish_a_whole_file(monkeypatch):
    """bug-535: the second build used to truncate the first one's temp bits under it."""
    monkeypatch.setattr(block_index, "_BUILD_PAGE", 4)  # many awaits, as a large table has
    async with _block_database():
        truth = await _seed_blocks()
        out = block_index.index_path()
        first = asyncio.ensure_future(block_index.build_block_index())
        await _bits_temp_reaches(out, len(truth) * _BLOCK_WIDTH // 2)
        second = asyncio.ensure_future(block_index.build_block_index())
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert [r if isinstance(r, BaseException) else r["built"] for r in results] == [True, True]
        block_index._cache.clear()
        index = block_index.load_block_index()
        assert index is not None and index.count == len(truth)
        wrong = sum(
            1
            for j in range(index.count)
            if index.bits[j].tobytes() != truth[(int(index.parent_id[j]), int(index.block_index[j]))]
        )
        assert wrong == 0, f"the published block index holds wrong bits for {wrong} rows"
        assert _leftovers(out) == []


@pytest.mark.asyncio
async def test_a_failed_block_index_build_removes_its_temp_files(monkeypatch):
    async with _block_database():
        await _seed_blocks()
        out = block_index.index_path()
        real_open = block_index.fileperms.open_private
        opened: list[str] = []

        def fail_on_the_header(path, mode="w", **kw):
            # The bits are written first; the header file is the second one opened.
            opened.append(path)
            if len(opened) == 2:
                raise OSError("disk full")
            return real_open(path, mode, **kw)

        monkeypatch.setattr(block_index.fileperms, "open_private", fail_on_the_header)
        with pytest.raises(OSError, match="disk full"):
            await block_index.build_block_index()
        assert _leftovers(out) == []
        assert not os.path.exists(out)


async def _race_two_builds(db, monkeypatch, build, out):
    """Two builds on two connections, interleaved at the builder's own await point.

    The first build stops after its first chunk of vectors; a record is stored; the
    second build runs its whole embedding pass and then waits until the first has
    published. Returns both outcomes.
    """
    for seed in range(1, 41):
        await _insert(db, seed)
    await db.commit()

    original = vector_index.iter_embedding_chunks
    roles: list[str] = []
    first_paused, second_streamed, first_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def interleaved(conn, table, watermark, width, *rest):
        role = "first" if not roles else "second"
        roles.append(role)
        async for chunk in original(conn, table, watermark, width, *rest):
            yield chunk
            if role == "first" and not first_paused.is_set():
                first_paused.set()
                await second_streamed.wait()
        if role == "second":
            second_streamed.set()
            await first_done.wait()

    monkeypatch.setattr(vector_index, "iter_embedding_chunks", interleaved)
    a = await aiosqlite.connect(config.DB_PATH)
    b = await aiosqlite.connect(config.DB_PATH)
    try:
        first = asyncio.ensure_future(build(a, "memories", out))
        await asyncio.wait_for(first_paused.wait(), 10)
        await _insert(db, 99)  # a record stored between the two builds' plans
        await db.commit()
        second = asyncio.ensure_future(build(b, "memories", out))
        (first_outcome,) = await asyncio.gather(first, return_exceptions=True)
        first_done.set()
        (second_outcome,) = await asyncio.gather(second, return_exceptions=True)
        return [first_outcome, second_outcome]
    finally:
        first_done.set()
        second_streamed.set()
        monkeypatch.setattr(vector_index, "iter_embedding_chunks", original)
        await a.close()
        await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [coarse_index.build_coarse_index, vector_index.build_index],
    ids=["coarse_index", "vector_index"],
)
async def test_two_record_index_builds_at_once_publish_a_whole_file(db, monkeypatch, tmp_path, build):
    """bug-558: both builders wrote one fixed `<out>.tmp`, so the second wrote into the
    inode the first had already published, and its own rename then failed."""
    out = str(tmp_path / "index")
    results = await _race_two_builds(db, monkeypatch, build, out)
    assert [r if isinstance(r, BaseException) else r["built"] for r in results] == [True, True]

    # The file left is the later build's, byte for byte what a quiet build writes now.
    reference = str(tmp_path / "reference")
    assert (await build(db, "memories", reference))["built"]
    with open(out, "rb") as fh, open(reference, "rb") as ref:
        assert fh.read() == ref.read()
    assert _leftovers(out) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,build,stream",
    [
        (coarse_index, coarse_index.build_coarse_index, "iter_embedding_chunks"),
        (vector_index, vector_index.build_index, "_stream_embeddings"),
    ],
    ids=["coarse_index", "vector_index"],
)
async def test_a_failed_record_index_build_removes_its_temp_file(db, monkeypatch, tmp_path, module, build, stream):
    for seed in range(1, 6):
        await _insert(db, seed)
    await db.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("interrupted mid-write")
        yield  # pragma: no cover - makes this an async generator for the coarse builder

    async def boom_coroutine(*args, **kwargs):
        raise RuntimeError("interrupted mid-write")

    monkeypatch.setattr(vector_index, stream, boom if stream == "iter_embedding_chunks" else boom_coroutine)
    out = str(tmp_path / "index")
    with pytest.raises(RuntimeError):
        await build(db, "memories", out)
    assert os.listdir(tmp_path) == []


# --------------------------------------------------------------------------------------
# A stored timestamp SQLite reads as a negative year
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("odd", ["2025", "0", "-0001-01-01", "0000-01-01T00:00:00+05:00"])
async def test_one_negative_year_timestamp_does_not_block_the_coarse_build(db, tmp_path, odd):
    """bug-557: another agent's record stamped '2025' (a Julian day number to SQLite)
    declined the coarse build for the whole server."""
    for seed in range(1, 6):
        await _insert(db, seed)
    odd_id = await _insert(db, 6, agent_id=OTHER, timestamp=odd)
    await db.commit()
    assert (await db.execute_fetchall("SELECT datetime(timestamp) FROM memories WHERE id = ?", (odd_id,)))[0][0].startswith("-")

    out = str(tmp_path / "coarse")
    built = await coarse_index.build_coarse_index(db, "memories", out)
    assert built["built"], built
    index = coarse_index.load_coarse_index(path=out)
    row = int(np.flatnonzero(index.ids == odd_id)[0])
    assert index.timestamp[row : row + 1].tobytes() == coarse_index.UNREADABLE_TIMESTAMP
    assert built["unreadable_timestamps"] == 1

    # The index answers what the live store answers, for an ordinary period and for
    # one whose lower bound is itself a negative year (which SQL compares as text).
    query = _vector(6)
    for period in (("2026-03-01 00:00:00", "2026-04-01 00:00:00"), ("-4706-01-01", "2026-04-01 00:00:00")):
        for agent in (AGENT, OTHER):
            kw = dict(agent_id=agent, period=period, k=10)
            from_index = await coarse_search.coarse_candidates(db, query, path=out, **kw)
            live = await coarse_search.coarse_candidates(db, query, path=out + ".absent", **kw)
            assert live.source == "live"
            assert from_index.rows() == live.rows(), (period, agent, from_index, live)


# --------------------------------------------------------------------------------------
# A health repair that rewrites the timestamp of an indexed record
# --------------------------------------------------------------------------------------

_MARCH = ("2026-03-01 00:00:00", "2026-04-01 00:00:00")


async def _built_with(db, stamp) -> int:
    for seed in range(1, 6):
        await _insert(db, seed)
    target = await _insert(db, 6, timestamp=stamp)
    await db.commit()
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    return target


async def _answers(db, target):
    query = _vector(6)  # the repaired record's own vector: distance 0
    kw = dict(agent_id=AGENT, period=_MARCH, k=10)
    live = await coarse_search.coarse_candidates(db, query, path=coarse_index.index_path() + ".absent", **kw)
    assert live.source == "live" and target in live.ids
    on = await coarse_search.coarse_candidates(db, query, **kw)
    index_only = await coarse_search.coarse_candidates(db, query, live=False, **kw)
    return live, on, index_only


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp,repair",
    [
        ("2099-01-01T00:00:00+00:00", checks.check_future_timestamp),
        ("not a time", checks.check_invalid_timestamp),
    ],
    ids=["future_timestamp", "invalid_timestamp"],
)
async def test_a_timestamp_repair_does_not_leave_the_coarse_index_answering_from_the_old_stamp(db, stamp, repair):
    """bug-547: the file kept the build-time stamp, so the repaired record was missing
    from its true period on the index path while the live store returned it."""
    target = await _built_with(db, stamp)
    found = await repair(db, AGENT, True)
    await db.commit()
    assert found and found[0]["repairable"] == 1

    live, on, index_only = await _answers(db, target)
    assert on.rows() == live.rows()
    # Index-only mode may decline (and say so); it may not answer without the record.
    assert index_only.source == "skipped" or index_only.rows() == live.rows(), index_only


@pytest.mark.asyncio
async def test_a_timestamp_repair_of_a_record_the_index_does_not_hold_keeps_the_file(db):
    """A record written after the build is read live already; repairing it costs no rebuild."""
    for seed in range(1, 6):
        await _insert(db, seed)
    await db.commit()
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    target = await _insert(db, 6, timestamp="2099-01-01T00:00:00+00:00")
    await db.commit()

    found = await checks.check_future_timestamp(db, AGENT, True)
    await db.commit()
    assert found and found[0]["repairable"] == 1
    assert os.path.exists(coarse_index.index_path())
    on = await coarse_search.coarse_candidates(db, _vector(6), agent_id=AGENT, period=_MARCH, k=10)
    assert on.source == "index" and on.ids[0] == target
