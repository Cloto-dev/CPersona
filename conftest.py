"""Shared pytest configuration and fixtures for the CPersona test suite.

Two structural guarantees live here, both motivated by the recurring "embedding-path
CI blindspot": an embedding-wiring regression reaching production because the test that
would have caught it was never run (the suite was historically run by hand with ad-hoc
flags, so it rarely ran at all).

1. Hermetic-by-default environment. Pinned BEFORE any ``cpersona`` import so a test that
   forgets to set ``CPERSONA_EMBEDDING_MODE`` can never fall through to a real embedding
   endpoint -- which is what makes a bare ``pytest`` block forever. Combined with the
   per-test ``timeout`` in pyproject, every run is bounded and order-independent, so the
   suite is safe to run in CI on every change.

2. A deterministic in-process embedding client (the ``fake_embedding_client`` fixture)
   that lets a test exercise the REAL ``store -> embed -> vector-search -> fusion`` hot
   path offline. Mocking ``_recall_rsf`` (as the older tests do) skips the embedding
   layer entirely, so it cannot catch a silently-embeddings-off regression; this fixture
   closes that gap. See ``test_embedding_path.py``.
"""
import hashlib
import os
import tempfile

# Pin a hermetic environment before cpersona is imported anywhere in the suite. conftest
# is imported by pytest ahead of every test module, so these defaults win the import-order
# race that individual test files would otherwise each have to handle by hand.
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "conftest.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

_FAKE_DIM = 64


def fake_embed_one(text: str) -> list[float]:
    """Deterministic, L2-normalised, bag-of-words embedding.

    Each whitespace token contributes a fixed pseudo-random unit vector seeded by a hash
    of the token, so texts that share tokens land close in cosine space and unrelated
    texts land far apart -- enough for the vector retriever to rank a topically-matching
    row above an unrelated one, deterministically and without any network call.
    """
    vec = np.zeros(_FAKE_DIM, dtype=np.float64)
    tokens = text.lower().split() or [text.lower()]
    for tok in tokens:
        seed = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
        vec += np.random.default_rng(seed).standard_normal(_FAKE_DIM)
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        vec[0], norm = 1.0, 1.0
    return (vec / norm).astype(np.float32).tolist()


class FakeEmbeddingClient:
    """Drop-in for ``vector._embedding_client`` with no network. Mirrors only the surface
    the store and local vector-search paths touch: ``embed()``, ``mode``, ``_http_url``,
    ``_client``."""

    mode = "fake"
    _http_url = None
    _client = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [fake_embed_one(t) for t in texts]

    @staticmethod
    def pack_embedding(embedding: list[float]) -> bytes:
        """Little-endian float32 packing, byte-identical to the real client, so the
        null-embedding repair / calibration paths (which call
        ``_embedding_client.pack_embedding``) can be exercised offline."""
        import struct

        return struct.pack(f"<{len(embedding)}f", *embedding)


@pytest.fixture
def fake_embedding_client(monkeypatch):
    """Install the deterministic client as the module-level singleton for one test, so
    the store and recall paths take the real (local) vector branch offline."""
    from cpersona import vector

    client = FakeEmbeddingClient()
    monkeypatch.setattr(vector, "_embedding_client", client)
    return client


@pytest.fixture(scope="session", autouse=True)
def _close_singleton_db():
    """Close the cached aiosqlite connection at session end.

    ``database.get_db`` caches one connection for the whole process, and aiosqlite runs
    each connection on a NON-daemon worker thread. Left open, that thread blocks the
    interpreter's shutdown join forever -- so ``pytest`` prints its summary and then hangs
    instead of exiting, which would wedge CI past its job timeout. Closing the singleton
    stops the worker thread so the process exits cleanly.
    """
    import asyncio

    yield
    from cpersona import database

    rdb = database._read_db
    if rdb is not None and rdb is not database._db:
        try:
            asyncio.run(rdb.close())
        except Exception:
            pass
    database._read_db = None

    db = database._db
    if db is not None:
        try:
            asyncio.run(db.close())
        except Exception:
            pass
        database._db = None
