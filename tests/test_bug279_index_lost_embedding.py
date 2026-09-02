"""bug-279: a row that loses its embedding after the build must not survive in the index.

The mirror of bug-278. Two maintenance repairs blank an embedding in place — the
content sanitiser rewrites the text and nulls the vector in one statement, the
dimension repair nulls a mismatched one — both under `check_health(fix=True)`.
The scan drops such a row, because every scan phase filters
`embedding IS NOT NULL`; the index matrix still holds its bytes. Before the fix
the index path returned a row the scan excludes, ranked by a vector that no
longer exists.

The claim pinned here is the two paths' row sets agreeing across a valid->NULL
transition, measured by running the same query with the index present and with
it removed — not by asserting which branch ran.

The second test pins the repair that was deliberately NOT made: the by-id
hydrate is shared with the keyword and FTS retrievers, whose hits are not
required to carry an embedding at all, so the one-line
`AND embedding IS NOT NULL` that looks right there would silently break the
standalone keyword-only configuration.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from tests.conftest import fake_embed_one

AGENT = "bug279.agent"
TOPIC = "alpha beta gamma"
_SHARED = fake_embed_one(f"{TOPIC} shared")


def _index_paths() -> tuple[str, str]:
    path = vector_index.index_path("memories")
    return path, path + ".tmp"


async def _store(db, content, *, created_at, embedding=None, source_id="user-a"):
    vec = fake_embed_one(content) if embedding is None else embedding
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, ?, ?, ?, ?)",
        (
            AGENT, content,
            '{"type": "User", "id": "%s"}' % source_id,
            "2026-03-01T00:00:00+00:00", created_at,
            None if embedding is False else np.array(vec, dtype=np.float32).tobytes(),
        ),
    )


@pytest_asyncio.fixture
async def corpus(fake_embedding_client):
    db = await get_db()
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)
    await db.execute("DELETE FROM memories")
    # A shared embedding on most rows so scores tie: a corpus of well-separated
    # vectors ranks the same under any ordering and cannot show a wrong answer.
    for n in range(12):
        await _store(
            db, f"row {n}",
            created_at=f"2026-03-01 00:00:{n:02d}",
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
    """The same query with the index file gone — the scan's own answer."""
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


@pytest.fixture
def taken(monkeypatch):
    counter = {"index": 0, "fallback": 0}
    real = vector._index_phase1

    async def counting(*args, **kwargs):
        result = await real(*args, **kwargs)
        counter["index" if result is not None else "fallback"] += 1
        return result

    monkeypatch.setattr(vector, "_index_phase1", counting)
    return counter


@pytest.mark.asyncio
async def test_the_two_paths_agree_across_a_valid_to_null_transition(corpus, taken):
    result = await vector_index.build_index(corpus, "memories")
    assert result["built"], result

    before = await _search(corpus)
    assert taken["index"] == 1, "the index path was not taken; the comparison would be vacuous"
    assert before, "the corpus must produce hits for the transition to be observable"

    # What check_health(fix=True) does to a row it repairs: the vector goes, the
    # row stays. The index is not rebuilt — that is the state under test.
    victim = before[0]["id"]
    await corpus.execute("UPDATE memories SET embedding = NULL WHERE id = ?", (victim,))
    await corpus.commit()

    with_index = await _search(corpus)
    without_index = await _answer_without_the_index(corpus)

    assert with_index == without_index, (
        "the index path answered differently from the scan after a row lost its "
        "embedding — it is ranking a row by a vector that no longer exists"
    )
    assert victim not in [row["id"] for row in with_index], (
        "the row whose embedding was cleared is still being returned; the scan "
        "excludes it, so the index must not resurrect it"
    )


@pytest.mark.asyncio
async def test_a_keyword_only_row_with_no_embedding_is_still_returned(corpus):
    """The repair that was rejected: `AND embedding IS NOT NULL` in the hydrate.

    The by-id hydrate is shared with the retrievers that do not rank by vector at
    all. Narrowing it there would drop legitimate results from the one path that
    has to work with no embedding backend, which is a supported configuration.
    """
    await _store(corpus, "keyword only sentinel row", created_at="2026-03-01 00:01:00",
                 embedding=False)
    await corpus.commit()

    rows = await vector._fetch_rows_by_id(
        corpus,
        "SELECT id, msg_id, content, source, timestamp FROM memories WHERE id IN ({ph})",
        [row[0] for row in await corpus.execute_fetchall(
            "SELECT id FROM memories WHERE embedding IS NULL"
        )],
        (),
    )
    assert rows, (
        "the shared hydrate no longer returns rows without an embedding — that "
        "breaks keyword-only recall, which is why the one-line fix was rejected"
    )
