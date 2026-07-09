"""Regression tests for bug-085: the vector scan window must not be derived
from the response limit, and its default must cover a real corpus.

The old coupling — ``min(MAX_MEMORIES, max(limit * 10, 100))`` — meant a default
``limit=10`` recall fetched and cosine-ranked only the newest 100 rows: anything
older was structurally invisible to the vector retriever. The 2.4.38 limit clamp
(bug-032) then closed the only escape hatch (passing a huge limit), which
collapsed LMEB LongMemEval from ~78 to 38.68; unclamping alone restored 76.99,
proving the window was the sole cause. The fix decouples the two concepts: the
vector scan window is ``MAX_MEMORIES`` (env ``CPERSONA_MAX_MEMORIES``, default
raised 500 -> 10000) regardless of how many rows the caller asked to receive.

The LIKE fallback is different by construction — its LIMIT applies AFTER the
content predicate, so it caps matching rows (not scanned rows) and old rows stay
reachable; a test below pins that contract so a refactor cannot silently turn it
into a recency window.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_scan_window.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from conftest import FakeEmbeddingClient, fake_embed_one  # noqa: E402
from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.scanwin"
TARGET = "zebra migration corridor sighting report"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


async def _seed(total: int, with_embeddings: bool) -> None:
    """Insert ``total`` rows where the topical TARGET is the OLDEST row and the
    ``total - 1`` newer rows are off-topic noise. created_at is explicit and
    strictly increasing so the ``ORDER BY created_at DESC LIMIT scan_limit``
    window deterministically drops the oldest rows first."""
    db = await get_db()
    pack = FakeEmbeddingClient.pack_embedding

    def blob(text):
        return pack(fake_embed_one(text)) if with_embeddings else None

    rows = [(AGENT, TARGET, "{}", "t", blob(TARGET), "2026-01-01 00:00:00")]
    for i in range(total - 1):
        content = f"unrelated grocery errand note number {i}"
        created = f"2026-02-01 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}"
        rows.append((AGENT, content, "{}", "t", blob(content), created))
    await db.executemany(
        "INSERT INTO memories (agent_id, content, source, timestamp, embedding, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


def test_max_memories_default_is_10000():
    """Pin the raised default. The conftest environment does not set
    CPERSONA_MAX_MEMORIES, so this reads the shipped fallback."""
    assert config.MAX_MEMORIES == 10000


@pytest.mark.asyncio
async def test_vector_scan_reaches_rows_beyond_old_limit_window(fake_embedding_client):
    """150 rows, target oldest, default-sized limit=10: under the old coupling
    the vector window was the newest 100 rows and the target was unreachable."""
    await _seed(150, with_embeddings=True)
    db = await get_db()
    results = await vector._search_vector(
        db, AGENT, "zebra migration corridor", limit=10, min_similarity=0.0
    )
    assert any(r["content"] == TARGET for r in results), (
        "oldest on-topic row invisible to the vector retriever — scan window is "
        "being derived from the response limit again (bug-085)"
    )


@pytest.mark.asyncio
async def test_vector_scan_reaches_rows_beyond_old_500_cap(fake_embedding_client):
    """600 rows, target oldest: under the old MAX_MEMORIES=500 default the
    target was unreachable no matter what limit the caller passed."""
    await _seed(600, with_embeddings=True)
    db = await get_db()
    results = await vector._search_vector(
        db, AGENT, "zebra migration corridor", limit=10, min_similarity=0.0
    )
    assert any(r["content"] == TARGET for r in results), (
        "oldest on-topic row invisible beyond 500 rows — MAX_MEMORIES default regressed"
    )


@pytest.mark.asyncio
async def test_keyword_like_fallback_reaches_old_rows(monkeypatch):
    """Contract pin (not a bug-085 fix): the LIKE fallback's LIMIT applies after
    the content predicate, so an old matching row is always reachable. Guards
    against a refactor turning that LIMIT into a pre-filter recency window."""
    monkeypatch.setattr(M, "FTS_ENABLED", False)
    await _seed(150, with_embeddings=False)
    db = await get_db()
    results = await M._search_memories_keyword(db, AGENT, "zebra migration", limit=10)
    assert any(r["content"] == TARGET for r in results), (
        "oldest matching row invisible to the LIKE fallback — its LIMIT has "
        "become a recency scan window"
    )
