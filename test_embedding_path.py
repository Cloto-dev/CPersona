"""End-to-end guard for the embedding / vector-recall hot path.

This is the test the "embedding-path CI blindspot" was missing. Embedding-wiring
regressions repeatedly reached production because the suite never exercised the real
``store -> embed -> vector-search -> fusion`` path:

- v2.4.23 (bug-001): the catalog-install path read only ``CPERSONA_``-prefixed env, so
  embeddings were silently OFF (FTS-only degradation) on the install nobody tested.
- v2.4.27: a ``do_recall`` refactor broke the production (``CONFIDENCE_ENABLED``) path;
  the integration recall test was skipped because it hangs without a resident embedding
  server.

The older hot-path test (``test_do_recall_response``) mocks ``_recall_rsf`` and therefore
skips the embedding layer entirely -- it catches scoring bugs but not a silently-off
embedding. These tests run the genuine vector path against a real temp SQLite DB using
the deterministic, offline ``fake_embedding_client`` (see ``conftest.py``).

The non-vacuous guarantee is in ``test_store_embedding_is_observable_on_off``: with
embeddings enabled a stored row carries a real embedding blob; disabled, it is NULL.
A bug-001-class regression flips the enabled case to NULL and fails this file, instead
of passing silently.
"""
import os
import tempfile

# The fake client is injected by fixture, not built from env; keep the env hermetic.
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_embedding_path.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import memory_handlers as M  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.embpath"
# Two topically disjoint memories. Queries share tokens with exactly one of them, so the
# bag-of-words fake embedding ranks the right one first (and FTS/keyword agrees, which is
# fine -- the isolation of the *vector* layer is asserted via the embedding blob, below).
MEM_PI = "raspberry pi gpio sensor wiring tutorial"
MEM_BREAD = "sourdough bread bakery proofing recipe"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


async def _store(content):
    return await M.do_store(AGENT, {"content": content, "source": {"System": "t"}})


async def _embedding_of(content):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = ? AND content = ? LIMIT 1",
        (AGENT, content),
    )
    assert rows, f"row not stored: {content!r}"
    return rows[0][0]


@pytest.mark.asyncio
async def test_store_embedding_is_observable_on_off(fake_embedding_client):
    """The bug-001 guard: an enabled embedding writes a real blob; disabled writes NULL.

    The contrast is what makes the enabled assertion meaningful -- a silently-off
    embedding (bug-001) flips the enabled row to NULL and fails this test.
    """
    res = await _store(MEM_PI)
    assert res.get("ok") and not res.get("skipped"), res
    blob = await _embedding_of(MEM_PI)
    assert blob is not None, "embeddings enabled but stored row has no embedding"
    assert len(blob) == 64 * 4, "embedding blob dimension/packing changed unexpectedly"


@pytest.mark.asyncio
async def test_store_writes_null_embedding_when_disabled():
    """Negative control (no fake client -> _embedding_client is None = embeddings off)."""
    assert vector._embedding_client is None
    res = await _store(MEM_PI)
    assert res.get("ok") and not res.get("skipped"), res
    assert await _embedding_of(MEM_PI) is None


@pytest.mark.asyncio
async def test_vector_search_ranks_topical_match_first(fake_embedding_client):
    """Real store -> embed -> vector-search: the on-topic row outranks the off-topic one."""
    await _store(MEM_PI)
    await _store(MEM_BREAD)
    db = await get_db()
    results = await vector._search_vector(db, AGENT, "raspberry pi gpio wiring", limit=5, min_similarity=0.0)
    assert results, "vector search returned nothing for an on-topic query"
    assert results[0]["content"] == MEM_PI
    cos = {r["content"]: r["_cosine"] for r in results}
    if MEM_BREAD in cos:
        assert cos[MEM_PI] > cos[MEM_BREAD]


@pytest.mark.parametrize("recall_mode", ["cascade", "rrf", "rsf"])
@pytest.mark.parametrize("confidence", [True, False])
@pytest.mark.asyncio
async def test_do_recall_surfaces_vector_hit(monkeypatch, fake_embedding_client, recall_mode, confidence):
    """Full production hot path across the env matrix that historically regressed.

    ``RECALL_MODE`` and ``CONFIDENCE_ENABLED`` are read once at import, so do_recall binds
    them by value; pin both here so every combination is exercised regardless of the
    ambient default (v2.4.27 broke only under CONFIDENCE_ENABLED=true).
    """
    monkeypatch.setattr(M, "RECALL_MODE", recall_mode)
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", confidence)
    await _store(MEM_PI)
    await _store(MEM_BREAD)
    # Query identical to the stored text -> cosine ~1.0, so the quality gate keeps it
    # even in a small pool.
    out = await M.do_recall(AGENT, MEM_PI, limit=5)
    assert "messages" in out
    contents = [m["content"] for m in out["messages"]]
    assert MEM_PI in contents, f"vector hit missing (mode={recall_mode}, confidence={confidence}): {contents}"
