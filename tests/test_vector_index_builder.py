"""The contiguous index builder: what it writes, and what it refuses to write.

Design: `docs/CONTIGUOUS_INDEX_DESIGN.md`. The builder's whole job is to be a
pure function of the database content — the query path is allowed to trust the
file only because the file cannot disagree with the rows it was built from.
These tests pin the three properties that trust rests on:

- the row set and its order are the ones the live scan produces
  (`created_at` DESC, then `id` ASC — the tie-break the answer depends on);
- a rebuild on unchanged content is byte-identical, which is what makes
  "throw it away and rebuild" a safe repair story;
- a file that does not hold together is rejected rather than half-read.
"""

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector_index
from cpersona.database import get_db

AGENT = "vecidx.agent"
OTHER_AGENT = "vecidx.other"
DIM = 8


def _vec(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(DIM).astype(np.float32).tobytes()


async def _insert(db, **kw):
    row = {
        "agent_id": AGENT,
        "project_id": "",
        "channel": "",
        "content": f"row {kw.get('created_at', '')} {kw.get('seed', 0)}",
        "source": '{"type": "Agent", "id": "src.a"}',
        "timestamp": "2026-03-01T00:00:00+00:00",
        "created_at": "2026-03-01 00:00:00",
        "embedding": _vec(kw.get("seed", 0)),
    }
    row.update({k: v for k, v in kw.items() if k != "seed"})
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(row[k] for k in
              ("agent_id", "project_id", "channel", "content", "source", "timestamp",
               "created_at", "embedding")),
    )


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


@pytest_asyncio.fixture
async def corpus(db):
    """Three timestamps, several rows each, axes varied inside every tie group."""
    seed = 0
    for stamp in ("2026-03-01 00:00:00", "2026-03-01 00:00:01", "2026-03-01 00:00:02"):
        for project in ("", "proj.x"):
            for channel in ("", "chan.a"):
                for agent in (AGENT, OTHER_AGENT):
                    seed += 1
                    await _insert(
                        db, seed=seed, created_at=stamp, project_id=project,
                        channel=channel, agent_id=agent,
                        source='{"type": "Agent", "id": "src.%d"}' % (seed % 3),
                    )
    await db.commit()
    return db


async def _canonical_rows(db):
    """What the live phase-1 scan sees, in the order it sees it."""
    return await db.execute_fetchall(
        "SELECT id, created_at FROM memories WHERE agent_id IS NOT NULL"
        " AND embedding IS NOT NULL ORDER BY created_at DESC, id ASC"
    )


@pytest.mark.asyncio
async def test_index_holds_the_scan_order(corpus, tmp_path):
    path = str(tmp_path / "idx")
    result = await vector_index.build_index(corpus, "memories", path)
    assert result["built"], result

    idx = vector_index.load_index(path=path)
    expected = [r[0] for r in await _canonical_rows(corpus)]
    assert list(idx.ids) == expected, "index rows must be the scan's rows, in the scan's order"
    assert idx.count == len(expected)
    assert idx.dim == DIM


@pytest.mark.asyncio
async def test_embeddings_round_trip_bit_for_bit(corpus, tmp_path):
    """The value of this index is exactness; a transform anywhere in the write
    path would spend it silently, so compare the bytes rather than the norms."""
    path = str(tmp_path / "idx")
    await vector_index.build_index(corpus, "memories", path)
    idx = vector_index.load_index(path=path)

    stored = await corpus.execute_fetchall(
        "SELECT id, embedding FROM memories WHERE agent_id IS NOT NULL AND embedding IS NOT NULL"
    )
    by_id = {r[0]: r[1] for r in stored}
    for position, row_id in enumerate(idx.ids):
        assert idx.embeddings[position].tobytes() == by_id[int(row_id)]


@pytest.mark.asyncio
async def test_rebuild_is_byte_identical(corpus, tmp_path):
    first, second = str(tmp_path / "a"), str(tmp_path / "b")
    await vector_index.build_index(corpus, "memories", first)
    await vector_index.build_index(corpus, "memories", second)
    assert open(first, "rb").read() == open(second, "rb").read()


@pytest.mark.asyncio
async def test_axis_codes_decode_to_the_row_values(corpus, tmp_path):
    path = str(tmp_path / "idx")
    await vector_index.build_index(corpus, "memories", path)
    idx = vector_index.load_index(path=path)

    stored = await corpus.execute_fetchall(
        "SELECT id, agent_id, project_id, channel, json_extract(source, '$.id')"
        " FROM memories WHERE agent_id IS NOT NULL"
    )
    by_id = {r[0]: r[1:] for r in stored}
    for position, row_id in enumerate(idx.ids):
        agent, project, channel, source = by_id[int(row_id)]
        assert idx.agents[idx.agent_code[position]] == agent
        assert idx.projects[idx.project_code[position]] == project
        assert idx.channels[idx.channel_code[position]] == channel
        assert idx.sources[idx.source_code[position]] == source


@pytest.mark.asyncio
async def test_watermark_bounds_the_index_and_new_rows_stay_outside(corpus, tmp_path):
    path = str(tmp_path / "idx")
    result = await vector_index.build_index(corpus, "memories", path)
    idx = vector_index.load_index(path=path)
    assert idx.watermark == max(idx.ids)

    await _insert(corpus, seed=999, created_at="2026-03-01 00:00:09")
    await corpus.commit()
    reloaded = vector_index.load_index(path=path)
    assert list(reloaded.ids) == list(idx.ids), "an index must not grow without a rebuild"
    assert reloaded.watermark == result["watermark"]


@pytest.mark.asyncio
async def test_mixed_widths_decline_the_build(corpus, tmp_path):
    """A single foreign-width row disqualifies the whole file, on purpose.

    The live scan applies its window BEFORE skipping foreign-width rows, so it
    ranks whatever survives inside the newest MAX_MEMORIES. An index holding one
    width would rank the newest MAX_MEMORIES *of that width* — more rows, and
    more rows is a different answer even when each is scored identically. The
    state is what a model swap looks like from here; declining keeps the scan
    correct (and merely slower) until the swap finishes.
    """
    await corpus.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', 'narrow', '{}', ?, ?, ?)",
        (AGENT, "2026-03-01T00:00:00+00:00", "2026-03-01 00:00:03",
         np.zeros(3, dtype=np.float32).tobytes()),
    )
    await corpus.commit()

    path = str(tmp_path / "idx")
    result = await vector_index.build_index(corpus, "memories", path)
    assert result["built"] is False
    assert "widths" in result["reason"]
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_non_canonical_created_at_is_named_not_dropped(corpus, tmp_path):
    """The import path carries a restored record's own created_at through, so a
    value this fixed-width format cannot spell is a real state. It must leave a
    trace the query path can act on — silence here is a row that stops being
    retrievable with nothing to show for it."""
    await _insert(corpus, seed=77, created_at="2026-03-01T00:00:04")  # 'T', not a space
    await corpus.commit()
    odd = (await corpus.execute_fetchall("SELECT MAX(id) FROM memories"))[0][0]

    path = str(tmp_path / "idx")
    await vector_index.build_index(corpus, "memories", path)
    idx = vector_index.load_index(path=path)
    assert odd not in set(int(i) for i in idx.ids)
    assert odd in idx.excluded_ids


@pytest.mark.asyncio
async def test_too_many_exclusions_declines_the_build(corpus, tmp_path, monkeypatch):
    monkeypatch.setattr(vector_index, "MAX_EXCLUDED_IDS", 1)
    for n in range(2):
        await _insert(corpus, seed=100 + n, created_at=f"2026-03-01T00:00:0{n}")
    await corpus.commit()

    path = str(tmp_path / "idx")
    result = await vector_index.build_index(corpus, "memories", path)
    assert result["built"] is False
    assert "non-canonical" in result["reason"]
    assert not os.path.exists(path), "a declined build leaves no file to be picked up"


@pytest.mark.asyncio
async def test_empty_corpus_declines_rather_than_writing_an_empty_index(db, tmp_path):
    path = str(tmp_path / "idx")
    result = await vector_index.build_index(db, "memories", path)
    assert result["built"] is False
    assert not os.path.exists(path)


def test_missing_file_is_not_an_error(tmp_path):
    assert vector_index.load_index(path=str(tmp_path / "absent")) is None


@pytest.mark.asyncio
async def test_truncated_file_is_rejected(corpus, tmp_path):
    path = str(tmp_path / "idx")
    await vector_index.build_index(corpus, "memories", path)
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 4)
    with pytest.raises(vector_index.IndexUnusable):
        vector_index.load_index(path=path)


@pytest.mark.asyncio
async def test_body_longer_than_the_header_claims_is_rejected(corpus, tmp_path):
    """Truncation is only half of it: a file that grew — a partial write followed
    by a second one, a concatenation — disagrees with its header in the other
    direction and must fail the same way."""
    path = str(tmp_path / "idx")
    await vector_index.build_index(corpus, "memories", path)
    with open(path, "ab") as fh:
        fh.write(b"\x00" * 32)
    with pytest.raises(vector_index.IndexUnusable):
        vector_index.load_index(path=path)


@pytest.mark.asyncio
async def test_foreign_magic_is_rejected(corpus, tmp_path):
    path = str(tmp_path / "idx")
    await vector_index.build_index(corpus, "memories", path)
    with open(path, "r+b") as fh:
        fh.write(b"NOTANIDX")
    with pytest.raises(vector_index.IndexUnusable):
        vector_index.load_index(path=path)


@pytest.mark.asyncio
async def test_temp_file_does_not_survive_a_failed_build(corpus, tmp_path, monkeypatch):
    """A build that dies partway must not leave a `.tmp` that a later reader or a
    resumed build could mistake for content."""
    async def boom(*args, **kwargs):
        raise RuntimeError("interrupted mid-write")

    monkeypatch.setattr(vector_index, "_stream_embeddings", boom)
    path = str(tmp_path / "idx")
    with pytest.raises(RuntimeError):
        await vector_index.build_index(corpus, "memories", path)
    assert not os.path.exists(f"{path}.tmp")
    assert not os.path.exists(path)
