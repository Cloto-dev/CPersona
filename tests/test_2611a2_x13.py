"""The index files and the vector arms: five ways a recall or a build went wrong.

- One memory whose ``source`` is not JSON made both index builders raise for the
  whole server, and permanently when the row was locked (the JSON repair skips
  locked rows): the row plan read ``json_extract(source, '$.id')`` with no guard.
- A reload kept the index it replaces cached while it read the new one, so on
  Windows, where the arrays are read into memory, two copies of the corpus were
  resident at once.
- The loaders opened the file once per array, by path, so a rebuild that replaced
  the file between two arrays left one index holding arrays from two files.
- A remote ``/search`` hit whose score was not a number was returned as a cosine,
  and the recall then raised at the first comparison instead of falling back to
  the local scan, as every other malformed body does.
- The near and far vector windows are two statements over a live table, so a
  delete between them moved the far window's first row into the near window's
  last place after the near window had been read: neither list scanned it.
"""

import math
import os
import sqlite3

import numpy as np
import pytest
import pytest_asyncio

from cpersona import coarse_index, database, vector, vector_index
from cpersona import memory_handlers as M
from cpersona.database import get_db
from cpersona.isolation import isolation_where
from tests.conftest import FakeEmbeddingClient

DIM = 8
AGENT = "x13.agent"


def _vec(seed: int) -> bytes:
    return np.random.default_rng(seed).standard_normal(DIM).astype(np.float32).tobytes()


async def _insert(db, n: int, *, source='{"type": "User", "id": "x13-user"}', locked=0, agent=AGENT) -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at,"
        " embedding, locked) VALUES (?, '', '', ?, ?, '2026-03-01T00:00:00+00:00', ?, ?, ?)",
        (agent, f"row {n}", source, f"2026-03-01 00:{n // 60:02d}:{n % 60:02d}", _vec(n), locked),
    )
    return cur.lastrowid


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    vector_index._cache.clear()
    coarse_index._cache.clear()


# ── a malformed source does not stop either index from being built ─────────────


@pytest.mark.asyncio
async def test_a_locked_row_with_a_malformed_source_does_not_stop_either_build(db, tmp_path):
    """The row is indexed with no source id, which is what the scan's own source
    filter makes of it: it matches no per-user recall, and an unfiltered recall
    still ranks it. The JSON repair cannot reach a locked row, so a builder that
    raised here would raise until someone unlocked it by hand."""
    for n in range(1, 4):
        await _insert(db, n)
    bad = await _insert(db, 4, source="not json at all", locked=1)
    await db.commit()

    await vector_index.start_logging()
    try:
        built = await vector_index.build_index(db, "memories", str(tmp_path / "idx"))
        coarse = await coarse_index.build_coarse_index(db, "memories", str(tmp_path / "coarse"))
    except sqlite3.OperationalError as exc:  # the defect: "malformed JSON"
        pytest.fail(f"an index build raised on one malformed source: {exc}")
    assert built["built"] and built["count"] == 4, built
    assert coarse["built"] and coarse["count"] == 4, coarse

    index = vector_index.load_index("memories", str(tmp_path / "idx"))
    [position] = np.flatnonzero(np.asarray(index.ids) == bad)
    assert int(index.source_code[position]) == vector_index.NULL_CODE
    # A per-user selection leaves it out; an unfiltered one keeps it.
    axes = dict(agent_id=AGENT, project_id=None, channel="", limit=0)
    filtered = vector_index.select(index, source_id="x13-user", **axes)
    assert bad not in {int(index.ids[p]) for p in filtered}
    assert bad in {int(index.ids[p]) for p in vector_index.select(index, source_id="", **axes)}
    held = coarse_index.load_coarse_index("memories", str(tmp_path / "coarse"))
    [cpos] = np.flatnonzero(np.asarray(held.ids) == bad)
    assert int(held.source_code[cpos]) == vector_index.NULL_CODE


# ── a reload does not hold the replaced index while it reads the new one ───────


LOADERS = {
    "contiguous": (vector_index.build_index, vector_index.load_index, vector_index.cached_index, vector_index),
    "coarse": (coarse_index.build_coarse_index, coarse_index.load_coarse_index, coarse_index.cached_coarse_index,
               coarse_index),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("which", sorted(LOADERS))
async def test_a_reload_lets_go_of_the_replaced_index_before_reading_the_new_one(db, tmp_path, monkeypatch, which):
    """On Windows the arrays are read into memory, so an old entry still cached
    while the new file is read is a second copy of the whole corpus resident at
    the moment the server is already at its peak."""
    build, _load, cached, module = LOADERS[which]
    path = str(tmp_path / which)
    for n in range(5):
        await _insert(db, n)
    await db.commit()
    await vector_index.start_logging()
    assert (await build(db, "memories", path))["built"]
    assert cached("memories", path) is not None

    await _insert(db, 5)
    await db.commit()
    await vector_index.start_logging()
    assert (await build(db, "memories", path))["built"]

    still_cached = []

    def reading(*_a):
        still_cached.append(path in module._cache)
        return False  # the Windows read path

    monkeypatch.setattr(vector_index, "_maps_files", reading)
    fresh = cached("memories", path)
    assert fresh.count == 6
    assert still_cached == [False], "the replaced index was still cached while the new one was read"


# ── one load reads one file ────────────────────────────────────────────────────


FIELDS = {
    "contiguous": ("ids", "embeddings", "agent_code", "project_code", "channel_code", "source_code", "created_at"),
    "coarse": ("ids", "bits", "agent_code", "project_code", "channel_code", "source_code", "created_at",
               "timestamp"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mapped", [True, False], ids=["mapped", "read"])
@pytest.mark.parametrize("which", sorted(LOADERS))
async def test_a_rebuild_replacing_the_file_mid_load_does_not_mix_two_files(db, tmp_path, monkeypatch, which, mapped):
    """The replacement lands after the first array is taken and before the rest.
    A loader that reopens the file by path for each array then reads the new
    file's bytes at the old header's offsets; one that reads every array from
    the handle it validated reads the file it validated."""
    build, load, _cached, _module = LOADERS[which]
    path, newer = str(tmp_path / f"{which}.a"), str(tmp_path / f"{which}.b")
    for n in range(4):
        await _insert(db, n)
    await db.commit()
    await vector_index.start_logging()
    assert (await build(db, "memories", path))["built"]
    for n in range(4, 12):  # a larger file: nothing about its size gives it away
        await _insert(db, n)
    await db.commit()
    await vector_index.start_logging()
    assert (await build(db, "memories", newer))["built"]
    assert os.path.getsize(newer) > os.path.getsize(path)

    reference = load(path=path)
    want = {field: np.array(getattr(reference, field)) for field in FIELDS[which]}
    del reference

    monkeypatch.setattr(vector_index, "_maps_files", lambda: mapped)
    name = "memmap" if mapped else "fromfile"
    real = getattr(np, name)
    calls = []

    def replaced_after_the_first_array(*args, **kwargs):
        out = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            os.replace(newer, path)  # the rebuild's publish
        return out

    monkeypatch.setattr(np, name, replaced_after_the_first_array)
    got = load(path=path)
    monkeypatch.undo()
    assert len(calls) > 1, "the patched reader was not used, so this test proves nothing"
    assert got.count == 4
    for field, expected in want.items():
        assert np.array_equal(np.array(getattr(got, field)), expected), f"{field} came from the replacing file"


# ── a non-numeric remote score falls back to the local scan ────────────────────


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Http:
    def __init__(self, body):
        self.body = body

    async def post(self, url, json=None, timeout=None):
        return _Response(self.body)


class _RemoteClient(FakeEmbeddingClient):
    _http_url = "http://127.0.0.1:9/embed"

    def __init__(self, body):
        self._client = _Http(body)


async def _ask_remote(row_id: int, score):
    iso = isolation_where(agent_id=AGENT, project_id=None, channel="")
    vector._embedding_client = _RemoteClient({"results": [{"id": f"mem:{row_id}", "score": score}]})
    return await vector._search_vector_remote(
        await get_db(), AGENT, "query", 5, 0.0,
        iso_fetch=iso, iso_ep_fetch=iso, src_clause="", src_params=(), src_like="", channel="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "score", ["0.95", None, True, [0.9], {"value": 0.9}, math.nan, math.inf],
    ids=["string", "null", "bool", "list", "object", "nan", "inf"],
)
async def test_a_remote_hit_whose_score_is_not_a_finite_number_falls_back(db, monkeypatch, score):
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", None)
    row = await _insert(db, 1)
    await db.commit()
    assert await _ask_remote(row, score) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0.9, 1, -0.25], ids=["float", "int", "negative"])
async def test_a_remote_hit_with_a_numeric_score_is_still_the_answer(db, monkeypatch, score):
    """The control: the guard rejects what is not a cosine, not what is."""
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", None)
    row = await _insert(db, 1)
    await db.commit()
    got = await _ask_remote(row, score)
    assert [(r["id"], r["_cosine"]) for r in got] == [(row, score)]
    assert type(got[0]["_cosine"]) is float


@pytest.mark.asyncio
async def test_a_recall_survives_a_string_score_from_the_remote_service(db, monkeypatch):
    """End to end: the string reached the quality gate's comparison and the whole
    recall raised TypeError. It now answers from the local scan."""
    monkeypatch.setattr(vector, "_embedding_client", FakeEmbeddingClient())
    stored = await M.do_store(AGENT, {"content": "the lighthouse keeper logs the tide"})
    assert stored["result"] == "stored"
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(
        vector, "_embedding_client", _RemoteClient({"results": [{"id": f"mem:{stored['id']}", "score": "0.95"}]})
    )
    out = await M.do_recall(AGENT, "lighthouse keeper tide", 5)
    assert any("lighthouse" in m.get("content", "") for m in out["messages"]), out


# ── a delete between the near and far reads does not hide a row ──────────────


NEAR, REACH, ROWS = 5, 10, 10
ONE_HOT = np.zeros(DIM, dtype=np.float32)
ONE_HOT[0] = 1.0


class _OneHot(FakeEmbeddingClient):
    async def embed(self, texts):
        return [ONE_HOT.tolist() for _ in texts]


def _drop_index_files():
    for path in (vector_index.index_path("memories"), vector_index.index_path("episodes"),
                 coarse_index.index_path("memories")):
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["same connection", "other connection"])
async def test_a_delete_between_the_near_and_far_reads_does_not_hide_a_row(db, monkeypatch, writer):
    """Row positions descend with `n`; the best match sits at position NEAR, the
    far window's first row. A near-window row deleted after the near read moves
    it to position NEAR - 1, which the near list has already read, while the far
    read starts at NEAR: with the defect neither list scans it."""
    _drop_index_files()
    monkeypatch.setattr(vector, "_embedding_client", _OneHot())
    monkeypatch.setattr(vector, "MAX_MEMORIES", NEAR)
    monkeypatch.setattr(vector, "VECTOR_REACH", REACH)
    ids = []
    for n in range(ROWS):
        v = np.zeros(DIM, dtype=np.float32)
        v[0] = 0.9 if n == NEAR else 0.25
        v[1] = 1.0
        cur = await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at,"
            " embedding) VALUES (?, '', '', ?, '{}', '2026-03-01T00:00:00+00:00', ?, ?)",
            (AGENT, f"row {n}", f"2026-03-01 00:00:{ROWS - n:02d}", v.tobytes()),
        )
        ids.append(cur.lastrowid)
    await db.commit()
    target = ids[NEAR]

    real = vector._search_vector_far
    deleted = []

    async def a_delete_lands_first(conn, **kwargs):
        if not deleted:
            deleted.append(ids[0])
            if writer == "same connection":
                await conn.execute("DELETE FROM memories WHERE id = ?", (ids[0],))
            else:
                async with database.transaction() as w:
                    await w.execute("DELETE FROM memories WHERE id = ?", (ids[0],))
        return await real(conn, **kwargs)

    monkeypatch.setattr(vector, "_search_vector_far", a_delete_lands_first)

    async def search(conn):
        far: list[dict] = []
        near = await vector._search_vector(conn, AGENT, "q", 10, min_similarity=-1.0, far_out=far)
        return [r["id"] for r in near], [r["id"] for r in far]

    if writer == "same connection":
        near, far = await search(db)
    else:
        async with database.connection() as r:
            near, far = await search(r)
    assert deleted, "the delete was never injected, so this test proves nothing"
    seen = near + far
    assert target in seen, f"the row at the far window's edge was scanned by neither list: near={near} far={far}"
    assert ids[0] not in seen
    assert len(seen) == len(set(seen)) == ROWS - 1
