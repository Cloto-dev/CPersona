"""The coarse record index: what it holds, and what it refuses to hold.

Design: `docs/BINARY_COARSE_SEARCH_DESIGN.md` §3. The file is trusted for one
reason only: it is a pure function of the database, built from the same row plan
as the contiguous index. These tests pin the properties that trust rests on:

- the rows, their order, their named holes and their axis codes are the
  contiguous index's, row for row;
- each row's bits are the sign code block reach uses, of that row's own vector;
- each row's time is SQLite's own reading of it, so a period compared in the
  file is the period compared in SQL;
- a rebuild is byte-identical, and a file that does not hold together is
  refused rather than half-read;
- a purge that removes an agent's rows removes this file too, because it holds
  their identifiers and a one-bit form of their vectors.
"""

import json
import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import admin_handlers, blocks, coarse_index, vector_index
from cpersona.database import get_db
from cpersona.vector_index import IndexUnusable

AGENT = "coarse.agent"
OTHER_AGENT = "coarse.other"
DIM = 12  # not a multiple of eight, so the last byte of every row is padded


def _vector(seed: int, dim: int = DIM) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    v[seed % dim] = 0.0  # a component that is exactly zero, which `> 0` reads as clear
    return v


async def _insert(db, seed, **kw):
    row = {
        "agent_id": AGENT,
        "project_id": "",
        "channel": "",
        "content": f"row {seed}",
        "source": '{"type": "Agent", "id": "src.%d"}' % (seed % 3),
        "timestamp": "2026-03-01T00:00:00+00:00",
        "created_at": "2026-03-01 00:00:%02d" % (seed % 60),
        "embedding": _vector(seed).tobytes(),
    }
    row.update(kw)
    columns = ("agent_id", "project_id", "channel", "content", "source", "timestamp", "created_at", "embedding")
    await db.execute(
        f"INSERT INTO memories ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        tuple(row[c] for c in columns),
    )


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


# Spellings a stored timestamp really arrives in, and what SQLite reads each as.
_STAMPS = (
    "2026-03-01T00:00:00+00:00",
    "2026-03-01T09:00:00+09:00",  # the same instant, another offset
    "2026-03-01 12:30:00",
    "2026-03-01T12:30:00Z",
    "not a time",  # datetime() reads NULL: in no period
    "",
)


@pytest_asyncio.fixture
async def corpus(db):
    seed = 0
    for created in ("2026-03-01 00:00:00", "2026-03-01 00:00:01", "2026-03-01 00:00:02"):
        for project in ("", "proj.x"):
            for channel in ("", "chan.a"):
                for agent in (AGENT, OTHER_AGENT):
                    seed += 1
                    await _insert(
                        db, seed, created_at=created, project_id=project, channel=channel,
                        agent_id=agent, timestamp=_STAMPS[seed % len(_STAMPS)],
                    )
    await db.commit()
    return db


def test_sign_bits_are_the_bits_block_reach_stores():
    """One rule for blocks and records, or a distance between them measures nothing."""
    vectors = np.stack([_vector(seed, dim) for seed, dim in ((1, 12), (2, 12), (3, 12))])
    for row, vector in zip(coarse_index.sign_bits(vectors), vectors):
        assert row.tobytes() == blocks.pack_bits(vector.tolist())
    for dim in (8, 768, 1024):
        vector = _vector(dim, dim)
        assert coarse_index.sign_bits(vector[None, :])[0].tobytes() == blocks.pack_bits(vector.tolist())
        assert coarse_index.bits_width(dim) == len(blocks.pack_bits(vector.tolist()))


@pytest.mark.asyncio
async def test_the_rows_are_the_contiguous_index_rows(corpus, tmp_path):
    contiguous, coarse = str(tmp_path / "vec"), str(tmp_path / "bits")
    assert (await vector_index.build_index(corpus, "memories", contiguous))["built"]
    result = await coarse_index.build_coarse_index(corpus, "memories", coarse)
    assert result["built"], result

    vec = vector_index.load_index(path=contiguous)
    bits = coarse_index.load_coarse_index(path=coarse)
    assert bits.count == vec.count and bits.dim == vec.dim == DIM
    assert bits.watermark == vec.watermark
    assert bits.excluded_ids == vec.excluded_ids and bits.unembedded_ids == vec.unembedded_ids
    assert list(bits.ids) == list(vec.ids)
    for field in ("agent_code", "project_code", "channel_code", "source_code", "created_at"):
        assert list(getattr(bits, field)) == list(getattr(vec, field)), field
    for table in ("agents", "projects", "channels", "sources"):
        assert getattr(bits, table) == getattr(vec, table), table


@pytest.mark.asyncio
async def test_each_rows_bits_are_the_sign_code_of_its_own_vector(corpus, tmp_path):
    path = str(tmp_path / "bits")
    await coarse_index.build_coarse_index(corpus, "memories", path)
    index = coarse_index.load_coarse_index(path=path)

    stored = {
        r[0]: r[1]
        for r in await corpus.execute_fetchall(
            "SELECT id, embedding FROM memories WHERE agent_id IS NOT NULL AND embedding IS NOT NULL"
        )
    }
    assert index.bits.shape == (index.count, coarse_index.bits_width(DIM))
    for position, row_id in enumerate(index.ids):
        vector = np.frombuffer(stored[int(row_id)], dtype="<f4")
        assert index.bits[position].tobytes() == blocks.pack_bits(vector.tolist()), int(row_id)


@pytest.mark.asyncio
async def test_each_rows_time_is_sqlites_own_reading_of_it(corpus, tmp_path):
    path = str(tmp_path / "bits")
    result = await coarse_index.build_coarse_index(corpus, "memories", path)
    index = coarse_index.load_coarse_index(path=path)

    read = {
        r[0]: r[1]
        for r in await corpus.execute_fetchall(
            "SELECT id, datetime(timestamp) FROM memories WHERE agent_id IS NOT NULL"
        )
    }
    unreadable = 0
    for position, row_id in enumerate(index.ids):
        expected = read[int(row_id)]
        if expected is None:
            unreadable += 1
            assert index.timestamp[position] == b"", int(row_id)  # NUL-padded S19 reads back empty
        else:
            assert index.timestamp[position] == expected.encode("ascii"), int(row_id)
    assert unreadable and result["unreadable_timestamps"] == unreadable
    # The control: two spellings of one instant are one value in the file.
    utc = {bytes(index.timestamp[p]) for p, i in enumerate(index.ids)
           if read[int(i)] == "2026-03-01 00:00:00"}
    assert utc == {b"2026-03-01 00:00:00"}


@pytest.mark.asyncio
async def test_a_period_in_the_file_selects_the_rows_sql_selects(corpus, tmp_path):
    """What section 6 will rely on: comparing the bytes is comparing as SQL does."""
    path = str(tmp_path / "bits")
    await coarse_index.build_coarse_index(corpus, "memories", path)
    index = coarse_index.load_coarse_index(path=path)
    for start, end in (("2026-03-01 00:00:00", "2026-03-01 00:00:01"),
                       ("2026-03-01 12:00:00", "2026-03-02 00:00:00"),
                       ("0000-01-01 00:00:00", "9999-12-31 23:59:59")):
        in_sql = {
            r[0] for r in await corpus.execute_fetchall(
                "SELECT id FROM memories WHERE agent_id IS NOT NULL"
                " AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)",
                (start, end),
            )
        }
        lo, hi = start.encode(), end.encode()
        in_file = {int(i) for i, t in zip(index.ids, index.timestamp) if t and lo <= bytes(t) < hi}
        assert in_file == in_sql, (start, end)


@pytest.mark.asyncio
async def test_rebuild_is_byte_identical(corpus, tmp_path):
    first, second = str(tmp_path / "a"), str(tmp_path / "b")
    await coarse_index.build_coarse_index(corpus, "memories", first)
    await coarse_index.build_coarse_index(corpus, "memories", second)
    assert open(first, "rb").read() == open(second, "rb").read()


@pytest.mark.asyncio
async def test_a_mixed_width_store_is_declined_as_the_contiguous_index_declines_it(db, tmp_path):
    await _insert(db, 1)
    await _insert(db, 2, embedding=_vector(2, DIM + 4).tobytes())
    await db.commit()
    path = str(tmp_path / "bits")
    result = await coarse_index.build_coarse_index(db, "memories", path)
    assert result["built"] is False and "widths" in result["reason"]
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_episodes_get_no_coarse_index(db, tmp_path):
    result = await coarse_index.build_coarse_index(db, "episodes", str(tmp_path / "bits"))
    assert result["built"] is False


@pytest.mark.asyncio
async def test_a_file_that_does_not_hold_together_is_refused(corpus, tmp_path):
    path = str(tmp_path / "bits")
    await coarse_index.build_coarse_index(corpus, "memories", path)
    good = open(path, "rb").read()

    open(path, "wb").write(good[:-1])
    with pytest.raises(IndexUnusable, match="expected"):
        coarse_index.load_coarse_index(path=path)

    open(path, "wb").write(b"CPXIDX01" + good[8:])  # the contiguous index's magic
    with pytest.raises(IndexUnusable, match="magic"):
        coarse_index.load_coarse_index(path=path)

    header_len = int.from_bytes(good[8:12], "little")
    header = json.loads(good[12:12 + header_len])
    header["format"] = coarse_index.FORMAT_VERSION + 1
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode().ljust(header_len)
    open(path, "wb").write(good[:12] + raw + good[12 + header_len:])
    with pytest.raises(IndexUnusable, match="format"):
        coarse_index.load_coarse_index(path=path)

    assert coarse_index.load_coarse_index(path=str(tmp_path / "absent")) is None


@pytest.mark.asyncio
async def test_the_read_path_holds_the_same_arrays_as_the_mapped_one(corpus, tmp_path, monkeypatch):
    """Windows reads the arrays instead of mapping them; the answer must not depend on which."""
    path = str(tmp_path / "bits")
    await coarse_index.build_coarse_index(corpus, "memories", path)
    mapped = coarse_index.load_coarse_index(path=path)
    monkeypatch.setattr(vector_index, "_maps_files", lambda: False)
    read = coarse_index.load_coarse_index(path=path)
    assert not isinstance(read.bits, np.memmap)
    for field in ("ids", "agent_code", "project_code", "channel_code", "source_code",
                  "bits", "created_at", "timestamp"):
        assert np.array_equal(getattr(read, field), getattr(mapped, field)), field


@pytest.mark.asyncio
async def test_the_cache_picks_up_a_rebuild(corpus, tmp_path):
    path = str(tmp_path / "bits")
    await coarse_index.build_coarse_index(corpus, "memories", path)
    first = coarse_index.cached_coarse_index(path=path)
    assert coarse_index.cached_coarse_index(path=path) is first
    await _insert(corpus, 99, agent_id=AGENT)
    await corpus.commit()
    await coarse_index.build_coarse_index(corpus, "memories", path)
    assert coarse_index.cached_coarse_index(path=path).count == first.count + 1


# --- a purge removes it -------------------------------------------------------------


@pytest_asyncio.fixture
async def beside_the_database(db):
    """Both index files at their real paths, cleaned on both sides of the test."""

    def _wipe():
        for path in (vector_index.index_path("memories"), coarse_index.index_path("memories")):
            if os.path.exists(path):
                os.unlink(path)

    _wipe()
    yield db
    _wipe()


@pytest.mark.asyncio
async def test_a_purge_removes_the_coarse_index_with_the_rows_it_held(beside_the_database):
    db = beside_the_database
    await _insert(db, 1, source='{"type": "User", "id": "discord:Alice"}')
    await _insert(db, 2, agent_id=OTHER_AGENT)
    await db.commit()
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    path = coarse_index.index_path("memories")
    # The control: the file held the agent's identifiers before the purge.
    assert AGENT.encode() in open(path, "rb").read()

    result = await admin_handlers.do_delete_agent_data(agent_id=AGENT)
    assert result["ok"] and result["deleted_memories"] == 1, result

    residue = open(path, "rb").read() if os.path.exists(path) else b""
    assert AGENT.encode() not in residue and b"discord:Alice" not in residue


@pytest.mark.asyncio
async def test_a_purge_that_removed_nothing_keeps_the_coarse_index(beside_the_database):
    db = beside_the_database
    await _insert(db, 3, agent_id=OTHER_AGENT)
    await db.commit()
    await coarse_index.build_coarse_index(db, "memories")
    path = coarse_index.index_path("memories")
    before = open(path, "rb").read()

    result = await admin_handlers.do_delete_agent_data(agent_id=AGENT)
    assert result["ok"] and result["deleted_memories"] == 0, result
    assert open(path, "rb").read() == before
