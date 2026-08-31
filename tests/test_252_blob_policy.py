"""Regression tests for bug-180 and bug-182 — the maintenance layer under a
configuration that keeps no local embedding BLOBs.

``CPERSONA_STORE_BLOB=false`` with ``CPERSONA_VECTOR_SEARCH_MODE=remote`` is a
supported trade: the remote index owns the vectors, the local rows stay slim.
Two things went wrong under it.

bug-182 — the checks layer had no idea the policy existed. Every memory row is
NULL by configuration, so ``null_embedding`` reported *critical* ("the pipeline
is effectively down") on a correctly-configured deployment, and worse,
``check_health(fix=true)`` re-embedded the corpus and wrote the very BLOBs the
operator configured away — which the next store made NULL again, so every
maintenance pass repeated the whole re-embed. Episodes are deliberately NOT part
of this: ``_prepare_episode_row`` writes their BLOB regardless of the policy, so
a NULL episode embedding stays a real failure and stays repairable.

bug-180 — in that same configuration the local cosine scan, which
``_search_vector`` treats as the fallback for a remote ``/search`` outage, has no
rows to scan. Nothing said so: the degraded advisory watches the embed boundary,
not ``/search``, so the first symptom was recall quietly losing its vector arm.
The new ``vector_fallback_config`` check states the trade on the maintenance
surface, agent-scoped like every other count there (the bug-062 rule).
"""
import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_blob_policy.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import session  # noqa: E402
from cpersona import checks  # noqa: E402
from cpersona import memory_handlers  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona.config import local_blobs_stored  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.blobpolicy"
OTHER = "agent.other"


class CountingEmbedder:
    """Records every embed call so a test can assert the repair did not run."""

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    @staticmethod
    def pack_embedding(values):
        import struct

        return struct.pack(f"{len(values)}f", *values)


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield db


@pytest.fixture(autouse=True)
def _restore_client():
    before = vector._embedding_client
    yield
    vector._embedding_client = before


def _no_local_blobs(monkeypatch):
    """The configuration under test: remote search, no local BLOBs."""
    monkeypatch.setattr(checks, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(checks, "STORE_BLOB", False)


def _local_blobs(monkeypatch):
    monkeypatch.setattr(checks, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(checks, "STORE_BLOB", True)


async def _seed_memory(db, content: str, agent_id: str = AGENT) -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, created_at) "
        "VALUES (?, ?, '{}', '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')",
        (agent_id, content),
    )
    await db.commit()
    return cur.lastrowid


async def _seed_episode(db, summary: str, agent_id: str = AGENT) -> int:
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) VALUES (?, ?, '', ?)",
        (agent_id, summary, "2026-07-01T00:00:00+00:00"),
    )
    await db.commit()
    return cur.lastrowid


# ============================================================
# The shared predicate (one definition, two callers)
# ============================================================


def test_local_blobs_stored_is_the_write_gate_rule():
    # local mode always keeps a BLOB (the local scan IS the retriever there);
    # remote mode keeps one only when asked to.
    assert local_blobs_stored("local", True) is True
    assert local_blobs_stored("local", False) is True
    assert local_blobs_stored("remote", True) is True
    assert local_blobs_stored("remote", False) is False


def test_write_gate_and_checks_read_the_same_rule():
    """bug-182: the write path and the maintenance layer must not drift apart.

    Both sites call ``config.local_blobs_stored``; this pins that neither one
    re-inlines the condition (which is how the checks layer came to disagree
    with the writer in the first place).
    """
    import inspect

    from cpersona import admin_handlers, memory_handlers

    for module in (memory_handlers, admin_handlers, checks):
        src = inspect.getsource(module)
        assert 'VECTOR_SEARCH_MODE == "local" or STORE_BLOB' not in src, (
            f"{module.__name__} re-inlines the blob-storage rule instead of calling "
            "config.local_blobs_stored"
        )
        assert "local_blobs_stored(" in src


# ============================================================
# bug-191..195 audit, bug-192 — the WRITE gate itself, through the real writer
#
# Everything above reads the predicate or hand-inserts rows, so the one caller
# that actually decides whether a BLOB reaches the disk — do_store's embed
# branch — was never executed under this configuration. Replacing its argument
# with a literal (``local_blobs_stored(VECTOR_SEARCH_MODE, True)``) left the
# whole file green while the policy was silently defeated in production.
# ============================================================


def _writer_policy(monkeypatch, mode: str, store_blob: bool):
    """Set the policy the WRITER reads.

    ``memory_handlers`` binds its own module-level copies of both settings at
    import time, so patching ``checks`` (as ``_no_local_blobs`` above does) does
    not reach the write gate.
    """
    monkeypatch.setattr(memory_handlers, "VECTOR_SEARCH_MODE", mode)
    monkeypatch.setattr(memory_handlers, "STORE_BLOB", store_blob)


async def _store_and_read_blob(content: str) -> tuple[dict, bytes | None]:
    """Drive the real do_store and return its response plus the stored BLOB."""
    session.reset_pauses_for_tests()
    res = await memory_handlers.do_store(AGENT, {"content": content, "timestamp": "t"})
    assert res["result"] == "stored", res
    db = await get_db()
    row = await db.execute_fetchall("SELECT embedding FROM memories WHERE id = ?", (res["id"],))
    return res, row[0][0]


@pytest.mark.asyncio
async def test_remote_store_blob_false_leaves_no_local_blob(monkeypatch, fake_embedding_client):
    """bug-192: the configured policy must survive the actual write.

    The embedding client is working (the fake from conftest), so the embed
    branch WOULD produce a BLOB if the gate let it through — the NULL column is
    therefore evidence about the gate, not about a broken embedder. The positive
    counterpart below proves the same fixture does write a BLOB when the policy
    allows one, so this assertion cannot pass vacuously.
    """
    _writer_policy(monkeypatch, "remote", False)

    res, blob = await _store_and_read_blob("remote policy, no local blob")

    assert blob is None, "the writer stored a local BLOB the configuration disabled"
    assert res["embedded"] is False, res


@pytest.mark.asyncio
async def test_remote_store_blob_true_still_writes_the_local_blob(
    monkeypatch, fake_embedding_client
):
    """The differential for the test above: only the policy changes. Without it
    a broken embedder (or a guard written too wide) would satisfy the NULL
    assertion for entirely the wrong reason."""
    _writer_policy(monkeypatch, "remote", True)

    res, blob = await _store_and_read_blob("remote policy, blob requested")

    assert blob is not None, "the writer dropped a BLOB the configuration asked for"
    assert res["embedded"] is True, res


# ============================================================
# bug-182 — severity is policy-aware, and only for memories
# ============================================================


@pytest.mark.asyncio
async def test_null_memory_embeddings_are_info_when_the_policy_stores_none(monkeypatch):
    db = await get_db()
    for i in range(4):
        await _seed_memory(db, f"policy memory {i}")
    vector._embedding_client = CountingEmbedder()

    _no_local_blobs(monkeypatch)
    issues = await checks.check_null_embedding(db, AGENT, fix=False)
    assert len(issues) == 1
    assert issues[0]["count"] == 4
    assert issues[0]["severity"] == "info"
    assert "skipped" in issues[0]["repair"]


@pytest.mark.asyncio
async def test_the_same_corpus_is_still_critical_when_blobs_are_expected(monkeypatch):
    """The differential: only the policy changes between this test and the one
    above. Without it the finding would be indistinguishable from a fix that
    just silenced the check."""
    db = await get_db()
    for i in range(4):
        await _seed_memory(db, f"policy memory {i}")
    vector._embedding_client = CountingEmbedder()

    _local_blobs(monkeypatch)
    issues = await checks.check_null_embedding(db, AGENT, fix=False)
    assert issues[0]["severity"] == "critical"
    assert "repair" not in issues[0]


@pytest.mark.asyncio
async def test_null_episode_embeddings_stay_critical_under_the_same_policy(monkeypatch):
    """Episodes carry a BLOB in every configuration (``_prepare_episode_row`` has
    no storage gate), so the policy must NOT downgrade their verdict."""
    db = await get_db()
    for i in range(4):
        await _seed_episode(db, f"policy episode {i}")
    vector._embedding_client = CountingEmbedder()

    _no_local_blobs(monkeypatch)
    issues = await checks.check_null_episode_embedding(db, AGENT, fix=False)
    assert len(issues) == 1
    assert issues[0]["severity"] == "critical"


# ============================================================
# bug-182 — the repair obeys the policy
# ============================================================


@pytest.mark.asyncio
async def test_fix_does_not_write_blobs_the_policy_forbids(monkeypatch):
    db = await get_db()
    mem_id = await _seed_memory(db, "must stay NULL")
    embedder = CountingEmbedder()
    vector._embedding_client = embedder

    _no_local_blobs(monkeypatch)
    await checks.check_null_embedding(db, AGENT, fix=True)

    row = await db.execute_fetchall("SELECT embedding FROM memories WHERE id = ?", (mem_id,))
    assert row[0][0] is None, "the fixer wrote a BLOB the configuration disabled"
    assert embedder.calls == [], "the fixer spent embed round-trips it could never apply"


@pytest.mark.asyncio
async def test_fix_still_repairs_when_the_policy_stores_blobs(monkeypatch):
    """Same call, blobs enabled: the repair must still happen. This is the test
    that fails if the guard is written too wide."""
    db = await get_db()
    mem_id = await _seed_memory(db, "should be repaired")
    vector._embedding_client = CountingEmbedder()

    _local_blobs(monkeypatch)
    await checks.check_null_embedding(db, AGENT, fix=True)

    row = await db.execute_fetchall("SELECT embedding FROM memories WHERE id = ?", (mem_id,))
    assert row[0][0] is not None


@pytest.mark.asyncio
async def test_prefetch_skips_memories_but_keeps_episodes(monkeypatch):
    """The prefetch runs OUTSIDE the write lock and is where the HTTP burst
    lives, so the policy has to reach it too — without taking episodes with it."""
    db = await get_db()
    await _seed_memory(db, "memory with no blob")
    await _seed_episode(db, "episode with no blob")
    embedder = CountingEmbedder()
    vector._embedding_client = embedder

    _no_local_blobs(monkeypatch)
    cache = await checks.prefetch_null_embeddings(db, AGENT)

    assert cache["memories"] == {}
    assert len(cache["episodes"]) == 1
    assert embedder.calls == [["episode with no blob"]]


# ============================================================
# bug-180 — the fallback gap is disclosed
# ============================================================


@pytest.mark.asyncio
async def test_fallback_check_is_silent_when_blobs_are_stored(monkeypatch):
    db = await get_db()
    await _seed_memory(db, "any memory")
    _local_blobs(monkeypatch)
    assert await checks.check_vector_fallback_config(db, AGENT) == []


@pytest.mark.asyncio
async def test_fallback_check_reports_the_empty_safety_net(monkeypatch):
    db = await get_db()
    for i in range(3):
        await _seed_memory(db, f"unvectorised {i}")
    _no_local_blobs(monkeypatch)

    issues = await checks.check_vector_fallback_config(db, AGENT)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["type"] == "no_local_vector_fallback"
    assert issue["vector_search_mode"] == "remote"
    assert issue["store_blob"] is False
    assert issue["memories"] == 3
    assert issue["memories_with_local_embedding"] == 0
    assert "CPERSONA_STORE_BLOB=true" in issue["hint"]


@pytest.mark.asyncio
async def test_fallback_check_counts_only_the_caller_agent(monkeypatch):
    """bug-062's rule: a per-agent health call never reports another agent's
    corpus. C21 was carried over precisely because a sibling check broke this."""
    db = await get_db()
    await _seed_memory(db, "mine")
    for i in range(5):
        await _seed_memory(db, f"theirs {i}", agent_id=OTHER)
    _no_local_blobs(monkeypatch)

    issues = await checks.check_vector_fallback_config(db, AGENT)
    assert issues[0]["memories"] == 1


@pytest.mark.asyncio
async def test_fallback_check_is_registered_as_an_observation(monkeypatch):
    """It must be in the registry (or no surface runs it) and it must be info:
    the configuration is legitimate, and b1's status verdict would otherwise
    park a healthy deployment at 'degraded' forever."""
    entry = next(c for c in checks.HEALTH_CHECKS if c.name == "vector_fallback_config")
    assert entry.base_severity == "info"
    assert entry.fix_capable is False

    db = await get_db()
    await _seed_memory(db, "registry path")
    _no_local_blobs(monkeypatch)
    issues, _summary = await checks.run_health_checks(db, agent_id=AGENT, fix=False)
    found = [i for i in issues if i["type"] == "no_local_vector_fallback"]
    assert len(found) == 1
    assert found[0]["severity"] == "info"
    # An info-only summary must still read healthy — this finding on its own
    # cannot park the deployment at 'degraded'. (The seeded fixture rows raise
    # unrelated warns, so assert the mapping rather than the whole-corpus verdict.)
    assert checks.health_status({"info": 1, "warn": 0, "critical": 0}) == "healthy"
