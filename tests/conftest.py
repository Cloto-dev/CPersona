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
import logging
import os
import tempfile

# Pin a hermetic environment before cpersona is imported anywhere in the suite. conftest
# is imported by pytest ahead of every test module, so these defaults win the import-order
# race that individual test files would otherwise each have to handle by hand.
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "conftest.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")
# Same hermeticity rule for the 2.5.1 operating-context sidecar: a developer's real
# ~/.cpersona/operating-context.toml must not leak warnings/instructions into the
# suite. Tests that exercise the feature opt back in per-test via monkeypatch.
os.environ.setdefault("CPERSONA_OPERATING_CONTEXT", "off")

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
    the store and local vector-search paths touch: ``embed()``, ``embed_with_outcome()``,
    ``mode``, ``_http_url``, ``_client``."""

    mode = "fake"
    _http_url = None
    _client = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [fake_embed_one(t) for t in texts]

    async def embed_with_outcome(self, texts: list[str]):
        """The detailed entry point the recall path reads, derived from ``embed()``.

        A double that only offers ``embed()`` sends the recall path into an
        ``AttributeError`` rather than the branch under test, so the surface has to grow
        with the client's. Derived rather than hand-written so the two cannot disagree.
        """
        from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome

        result = await self.embed(texts)
        return result, EmbedOutcome(
            attempted=True,
            ok=bool(result),
            error=None if result else "test double produced no embeddings",
        )

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


@pytest.fixture(autouse=True)
def _clear_scope_stats_cache():
    """Empty the per-scope aggregate cache around every test.

    ``scope_stats`` invalidates on ``database.write_generation()``, which the write seam
    bumps after each commit — so production, where the seam is the only committer
    (structural Gate 1), invalidates exactly. The suite is the exception: most fixtures
    write with ``get_db()`` + ``db.commit()`` directly, and that commit is invisible to
    the counter. Left alone, one test's cached scope sizes another's quality gate: a
    file whose earlier test recalled 2 rows under an agent id made a later test's 51
    freshly inserted rows score as a corpus of 2, and its profile row was gate-blocked
    for a reason nothing in that test mentions. Order-dependent, so it appears and
    disappears with unrelated edits.

    Clearing between tests removes the cross-test half. A test that writes AROUND the
    seam and then recalls twice in one body can still read a stale count — write through
    ``do_store`` (or call ``scope_stats.clear()``) when that is the shape you need.
    """
    from cpersona import scope_stats

    scope_stats.clear()
    yield
    scope_stats.clear()


@pytest.fixture(autouse=True)
def _no_leaked_mgp_log_filter():
    """Fail the test that leaves an MGP validation filter on a live log handler.

    ``install_mgp_validation_filter`` attaches to the root logger AND to each of
    its handlers. A teardown that only clears the logger side leaves the filter
    on pytest's capture handlers for the rest of the session, where it silently
    drops every later "Failed to validate request:" record — a test disabling
    other tests' assertions, in exactly the failure class the filtered code is
    about. Measured once: after the 2.5.3 follow-up test ran, the filter was
    live on _LiveLoggingNullHandler, _FileHandler and LogCaptureHandler, and a
    probe from an unrelated logger came back invisible.

    Checked here rather than as a standalone test because a standalone one can
    only fail if it happens to run after the leak, and it names the victim
    instead of the culprit. This fixture runs after every test and reports the
    one that leaked.

    It also detaches what it found, so exactly one test fails. Left in place,
    the dirty state makes every subsequent test fail too, and a wall of errors
    is read as "the suite is broken" rather than "this test leaked" — the first
    name in the list is the only one that means anything, and it is the easiest
    to miss.
    """
    yield
    leaked = [
        (handler, f)
        for handler in logging.getLogger().handlers
        for f in handler.filters
        if type(f).__name__ == "_MgpValidationFilter"
    ]
    for handler, log_filter in leaked:
        handler.removeFilter(log_filter)
    leaked = [(type(h).__name__, type(f).__name__) for h, f in leaked]
    assert not leaked, (
        f"this test left an MGP validation filter on a live handler ({leaked}); it will "
        "swallow 'Failed to validate request:' records for every test after it. Remove "
        "the filter from root.handlers as well as from the root logger in teardown."
    )


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
