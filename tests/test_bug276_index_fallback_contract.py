"""bug-276: every failure of the index reaches recall as a fallback, not an exception.

The index is a derived, optional artifact whose whole design claim is that the
scan stays correct and is still the fallback. `_index_phase1` guarded
`cached_index()` and then called `select()` *outside* that guard, so an
exception raised while selecting escaped into recall — taking down the query the
index exists to accelerate.

One such exception was reachable through data rather than corruption:
`select()` filtered by source with `v.startswith(source_id)` over values taken
from `json_extract(source, '$.id')`, and SQLite hands back a JSON number as an
int. The scan does not share the defect — it filters in SQL with LIKE, which
coerces the number to text and matches it.

The claim pinned here is "recall survives a broken index", not "this particular
exception is gone", so the first test raises something the repair has no
knowledge of.
"""

from __future__ import annotations

import json
import os
import struct

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from tests.conftest import fake_embed_one

AGENT = "bug276.agent"
TOPIC = "alpha beta gamma"
_SHARED = fake_embed_one(f"{TOPIC} shared")


def _index_paths() -> tuple[str, str]:
    path = vector_index.index_path("memories")
    return path, path + ".tmp"


async def _store(db, content, *, created_at, source_json, embedding=None):
    vec = fake_embed_one(content) if embedding is None else embedding
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, ?, ?, ?, ?)",
        (
            AGENT, content, source_json, "2026-03-01T00:00:00+00:00", created_at,
            np.array(vec, dtype=np.float32).tobytes(),
        ),
    )


@pytest_asyncio.fixture
async def corpus(fake_embedding_client):
    db = await get_db()
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)
    await db.execute("DELETE FROM memories")
    for n in range(9):
        await _store(
            db, f"row {n}",
            created_at=f"2026-03-01 00:00:{n:02d}",
            source_json='{"type": "User", "id": "user-%d"}' % (n % 2),
            embedding=_SHARED if n % 3 else fake_embed_one(f"unrelated {n}"),
        )
    await db.commit()
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)


async def _search(db, **kw):
    return await vector._search_vector(db, AGENT, TOPIC, 10, min_similarity=-1.0, **kw)


async def _answer_without_the_index(db, **kw):
    keep = {}
    for path in _index_paths():
        if os.path.exists(path):
            keep[path] = open(path, "rb").read()
            os.unlink(path)
    try:
        return await _search(db, **kw)
    finally:
        for path, blob in keep.items():
            with open(path, "wb") as fh:
                fh.write(blob)


@pytest.mark.asyncio
async def test_a_raising_index_still_returns_the_scans_answer(corpus, monkeypatch):
    """Not "this exception is gone" — an exception the repair never heard of."""
    assert (await vector_index.build_index(corpus, "memories"))["built"]
    expected = await _answer_without_the_index(corpus)

    def exploding(*args, **kwargs):
        raise RuntimeError("the index is having a bad day")

    monkeypatch.setattr(vector_index, "select", exploding)

    assert await _search(corpus) == expected, (
        "recall did not survive an index that raised while selecting"
    )


@pytest.mark.asyncio
async def test_a_numeric_source_id_matches_instead_of_raising(corpus):
    """The scan's LIKE coerces the number to text; the index must match it."""
    await _store(
        corpus, "row with a numeric source id",
        created_at="2026-03-01 00:00:20",
        source_json='{"type": "User", "id": 123}',
        embedding=_SHARED,
    )
    await corpus.commit()
    assert (await vector_index.build_index(corpus, "memories"))["built"]

    with_index = await _search(corpus, source_id="12")
    without_index = await _answer_without_the_index(corpus, source_id="12")

    assert with_index == without_index, (
        "the index disagreed with the scan on a numeric source id"
    )
    assert with_index, (
        "the scan's LIKE matches a numeric id coerced to text, so this query is "
        "expected to return the row — an empty result on both sides would make "
        "the agreement above vacuous"
    )


def test_an_incomplete_header_is_reported_as_unusable(tmp_path):
    """`load_index` reads dim/count from the header; a header that parsed as JSON
    but does not spell its geometry must not escape the caller's guard as a
    KeyError. Sibling of the unguarded select() call, same hole."""
    body = json.dumps({"format": vector_index.FORMAT_VERSION, "count": 1}).encode("utf-8")
    path = tmp_path / "broken.idx"
    path.write_bytes(vector_index.MAGIC + struct.pack("<I", len(body)) + body)

    with pytest.raises(vector_index.IndexUnusable):
        vector_index.load_index("memories", str(path))


def _index_with_sources(sources: tuple) -> vector_index.VectorIndex:
    """A minimal index whose string table holds the given source values."""
    n = len(sources)
    return vector_index.VectorIndex(
        path="<synthetic>", dim=1, count=n, watermark=n,
        excluded_ids=(), unembedded_ids=(),
        embedding_model="fake", scoring_version="test",
        ids=np.arange(1, n + 1, dtype="<i8"),
        embeddings=np.zeros((n, 1), dtype="<f4"),
        agent_code=np.zeros(n, dtype="<i4"),
        project_code=np.zeros(n, dtype="<i4"),
        channel_code=np.zeros(n, dtype="<i4"),
        source_code=np.arange(n, dtype="<i4"),
        created_at=np.array([b"2026-03-01 00:00:00"] * n, dtype="S19"),
        agents=(AGENT,), projects=("",), channels=("",),
        sources=sources,
    )


def test_select_tolerates_a_numeric_value_already_in_a_built_file():
    """The builder's normalisation does not reach files written before it.

    An older build only raised when it had to sort ints against strings. A corpus
    whose source ids were *all* numeric sorted fine and produced a table of ints,
    so such a file can exist on disk right now and is still format 2 — which
    means it is still loaded. Reading it must not raise.
    """
    index = _index_with_sources((123, 456))
    positions = vector_index.select(
        index, agent_id=AGENT, project_id=None, channel="", source_id="12", limit=10
    )
    assert [int(p) for p in positions] == [0], (
        "select() must compare a numeric source id the way the scan's LIKE does, "
        "not raise on it"
    )
