"""Regression tests for the 2.5.2a2 audit findings C12 (update-path content cap) and
C16 (remote recall N+1 hydration).

C12 — ``do_store`` caps content with ``utils._sanitize_content`` (MAX_CONTENT_LENGTH);
``do_update_memory`` only did ``content.strip()``. An edit could therefore grow a row
past a cap the insert path enforces on every row, and the uncapped string was ALSO the
text handed to ``embed()`` and pushed verbatim to the remote ``/index`` — the two write
seams disagreed on what a stored row may contain.

C16 — the remote branch of ``_search_vector`` re-hydrated every hit with its own
awaited ``SELECT ... WHERE id = ?``. aiosqlite serialises every statement through one
background-thread executor, so a limit=100 recall paid 100 sequential round trips on
the recall hot path instead of one query per row type. The fix must keep the remote
service's score ORDER (an ``IN ()`` query returns rows in arbitrary order) and keep
skipping ids the DB no longer holds (a stale remote index entry, `sv-remote-stale-id`).
"""
import os
import tempfile

# Hermetic env, matching the sibling audit suites; the fake clients are injected by
# monkeypatch, not env. conftest pins these first, so these are no-ops in a full run.
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_252b1.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import admin_handlers as A  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona.config import MAX_CONTENT_LENGTH  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.252b1"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield db


async def _seed_memory(db, content: str, agent_id: str = AGENT) -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, created_at) "
        "VALUES (?, ?, '{}', '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')",
        (agent_id, content),
    )
    await db.commit()
    return cur.lastrowid


# ============================================================
# C12 — the edit path enforces the write path's content cap
# ============================================================


class _RecordingHttpClient:
    """Stand-in for the embedding client's httpx.AsyncClient; records POST bodies."""

    def __init__(self):
        self.post_calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None, timeout=None):
        self.post_calls.append((url, json))
        return httpx.Response(status_code=200, request=httpx.Request("POST", url))


class _RecordingEmbeddingClient:
    """Drop-in for ``vector._embedding_client`` that records the text it is asked to embed."""

    mode = "remote"
    _http_url = "http://fake-embed.local/embed"

    def __init__(self):
        self._client = _RecordingHttpClient()
        self.embedded: list[str] = []

    @property
    def post_calls(self):
        return self._client.post_calls

    async def embed(self, texts):
        self.embedded.extend(texts)
        return [[0.0] for _ in texts]

    @staticmethod
    def pack_embedding(embedding):
        import struct

        return struct.pack(f"<{len(embedding)}f", *embedding)


@pytest.mark.asyncio
async def test_c12_update_memory_enforces_the_store_content_cap(_fresh_db):
    """An oversized edit is capped at MAX_CONTENT_LENGTH, as an oversized insert is.

    Fail-first (unfixed): do_update_memory wrote the raw string, so the row held the
    full 2500 characters while do_store's row would hold exactly MAX_CONTENT_LENGTH.
    """
    db = _fresh_db
    mem_id = await _seed_memory(db, "short original body")

    res = await A.do_update_memory(mem_id, "U" * (MAX_CONTENT_LENGTH + 500), agent_id=AGENT)

    assert res.get("ok") is True, res
    rows = await db.execute_fetchall("SELECT content FROM memories WHERE id = ?", (mem_id,))
    assert len(rows[0][0]) == MAX_CONTENT_LENGTH, (
        f"update path stored {len(rows[0][0])} chars — the write path caps at "
        f"{MAX_CONTENT_LENGTH} (audit C12)"
    )
    assert res.get("truncated") is True, (
        f"truncation must be reported the way do_store reports it: {res}"
    )


@pytest.mark.asyncio
async def test_c12_update_under_the_cap_reports_no_truncation(_fresh_db):
    """The ``truncated`` marker is additive: absent unless the cap actually bit.

    Guards the fix against over-correcting into an always-present key (which would
    make the flag useless to a caller branching on it).
    """
    db = _fresh_db
    mem_id = await _seed_memory(db, "short original body")

    res = await A.do_update_memory(mem_id, "  a modest edit  ", agent_id=AGENT)

    assert res == {"ok": True, "updated_id": mem_id}, res
    rows = await db.execute_fetchall("SELECT content FROM memories WHERE id = ?", (mem_id,))
    assert rows[0][0] == "a modest edit"


@pytest.mark.asyncio
async def test_c12_capped_text_is_what_reaches_embed_and_the_remote_index(_fresh_db, monkeypatch):
    """The cap applies BEFORE the embed and the remote /index push, not only to the row.

    This is the half of C12 that a DB-length assertion alone cannot see: pre-fix, the
    uncapped string was embedded and pushed verbatim, so the remote index held text the
    database itself refuses to store.
    """
    db = _fresh_db
    mem_id = await _seed_memory(db, "short original body")

    client = _RecordingEmbeddingClient()
    monkeypatch.setattr(vector, "_embedding_client", client)
    monkeypatch.setattr(A, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(A, "STORE_BLOB", True)

    await A.do_update_memory(mem_id, "U" * (MAX_CONTENT_LENGTH + 500), agent_id=AGENT)

    assert client.embedded, "the re-embed branch was not exercised"
    assert len(client.embedded[0]) == MAX_CONTENT_LENGTH, (
        f"embed() received {len(client.embedded[0])} chars (audit C12)"
    )
    index_posts = [body for url, body in client.post_calls if url.endswith("/index")]
    assert index_posts, "the remote /index branch was not exercised"
    pushed = index_posts[0]["items"][0]["text"]
    assert len(pushed) == MAX_CONTENT_LENGTH, (
        f"remote /index received {len(pushed)} chars (audit C12)"
    )


# ============================================================
# C16 — one hydration query per row type, order and skips preserved
# ============================================================


class _SearchHttpClient:
    """Returns a fixed /search payload; records nothing else the path needs."""

    def __init__(self, payload: dict):
        self.payload = payload

    async def post(self, url, json=None, timeout=None):
        return httpx.Response(
            status_code=200, json=self.payload, request=httpx.Request("POST", url)
        )


class _SearchEmbeddingClient:
    mode = "remote"
    _http_url = "http://fake-embed.local/embed"

    def __init__(self, payload: dict):
        self._client = _SearchHttpClient(payload)

    async def embed(self, texts):
        return [[0.0] for _ in texts]


class _CountingDB:
    """Delegating proxy that counts the statements the hydration path issues.

    A counter here (rather than instrumentation in vector.py) keeps the production
    path free of test-only hooks while still measuring the exact quantity C16 is
    about: SQLite round trips per remote recall.
    """

    def __init__(self, db):
        # NOT named _db: Gate 6 in test_structural_gates.py flags any `._db` assignment
        # in tests as a connection-orphaning re-point (bug-124).
        self.conn = db
        self.fetch_sql: list[str] = []

    async def execute_fetchall(self, sql, params=()):
        self.fetch_sql.append(sql)
        return await self.conn.execute_fetchall(sql, params)

    def __getattr__(self, name):
        return getattr(self.conn, name)


async def _seed_remote_corpus(db, n_memories: int, n_episodes: int):
    mem_ids = [await _seed_memory(db, f"remote body {i}") for i in range(n_memories)]
    ep_ids = []
    for i in range(n_episodes):
        cur = await db.execute(
            "INSERT INTO episodes (agent_id, summary, start_time) VALUES (?, ?, ?)",
            (AGENT, f"remote episode {i}", f"2026-07-0{i + 1}T00:00:00+00:00"),
        )
        ep_ids.append(cur.lastrowid)
    await db.commit()
    return mem_ids, ep_ids


@pytest.mark.asyncio
async def test_c16_remote_hydration_is_one_query_per_row_type(_fresh_db, monkeypatch):
    """A 100-hit remote recall costs 2 SQLite round trips, not 100.

    Fail-first (unfixed): the per-hit ``SELECT ... WHERE id = ?`` loop issued one
    statement per hit (102 for this payload), which is the whole finding — aiosqlite
    runs them sequentially on a single background thread.
    """
    db = _fresh_db
    mem_ids, ep_ids = await _seed_remote_corpus(db, 100, 2)

    hits = [{"id": f"mem:{i}", "score": 0.9} for i in mem_ids]
    hits += [{"id": f"ep:{i}", "score": 0.5} for i in ep_ids]
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _SearchEmbeddingClient({"results": hits}))

    counting = _CountingDB(db)
    results = await vector._search_vector(counting, AGENT, "q", 100, min_similarity=0.0)

    assert len(results) == 102
    assert len(counting.fetch_sql) == 2, (
        f"{len(counting.fetch_sql)} SQLite round trips for 102 hits — the hydration must "
        f"be one batched query per row type (audit C16):\n  "
        + "\n  ".join(counting.fetch_sql)
    )


@pytest.mark.asyncio
async def test_c16_round_trips_do_not_scale_with_the_hit_count(_fresh_db, monkeypatch):
    """The round-trip count is the same for 10 hits and for 100 — the N is gone.

    An absolute bound alone could be met by accident on a small payload; this pins the
    shape of the cost curve, which is what the N+1 finding is about.
    """
    db = _fresh_db
    mem_ids, _ = await _seed_remote_corpus(db, 100, 0)
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")

    counts = []
    for n in (10, 100):
        hits = [{"id": f"mem:{i}", "score": 0.9} for i in mem_ids[:n]]
        monkeypatch.setattr(
            vector, "_embedding_client", _SearchEmbeddingClient({"results": hits})
        )
        counting = _CountingDB(db)
        res = await vector._search_vector(counting, AGENT, "q", n, min_similarity=0.0)
        assert len(res) == n
        counts.append(len(counting.fetch_sql))

    assert counts[0] == counts[1], (
        f"round trips scale with the hit count: {counts[0]} for 10 hits, {counts[1]} for "
        f"100 (audit C16)"
    )


@pytest.mark.asyncio
async def test_c16_batched_hydration_preserves_the_remote_score_order(_fresh_db, monkeypatch):
    """Results keep the service's hit order, including interleaved memories/episodes.

    The remote service ranks; the caller consumes that ranking. An ``IN ()`` query
    returns rows in whatever order SQLite picks, so the batched path must re-order —
    hence a payload deliberately ordered AGAINST the rows' id order.
    """
    db = _fresh_db
    mem_ids, ep_ids = await _seed_remote_corpus(db, 6, 2)

    hits = [
        {"id": f"mem:{mem_ids[4]}", "score": 0.99},
        {"id": f"ep:{ep_ids[1]}", "score": 0.98},
        {"id": f"mem:{mem_ids[0]}", "score": 0.97},
        {"id": f"mem:{mem_ids[5]}", "score": 0.96},
        {"id": f"ep:{ep_ids[0]}", "score": 0.95},
        {"id": f"mem:{mem_ids[2]}", "score": 0.94},
    ]
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _SearchEmbeddingClient({"results": hits}))

    results = await vector._search_vector(_CountingDB(db), AGENT, "q", 10, min_similarity=0.0)

    expected = [
        ("mem", mem_ids[4]),
        ("ep", ep_ids[1]),
        ("mem", mem_ids[0]),
        ("mem", mem_ids[5]),
        ("ep", ep_ids[0]),
        ("mem", mem_ids[2]),
    ]
    assert [r["_rid"] for r in results] == expected, (
        "batched hydration lost the remote's score order (audit C16)"
    )
    # The per-hit score must stay bound to its own row, not to a position.
    assert [r["_cosine"] for r in results] == [h["score"] for h in hits]
    assert results[0]["content"] == "remote body 4"
    assert results[1]["content"] == "[Episode] remote episode 1"


@pytest.mark.asyncio
async def test_c16_stale_remote_ids_are_still_skipped_silently(_fresh_db, monkeypatch):
    """An id the DB no longer holds is dropped, not surfaced and not an error.

    Pins the `sv-remote-stale-id` contract through the batched path: a dict lookup
    miss must behave exactly like the old ``if row:`` skip, on both row types and
    without disturbing the neighbouring hits' order.
    """
    db = _fresh_db
    mem_ids, ep_ids = await _seed_remote_corpus(db, 2, 1)

    hits = [
        {"id": "mem:99999", "score": 0.99},
        {"id": f"mem:{mem_ids[1]}", "score": 0.98},
        {"id": "ep:99999", "score": 0.97},
        {"id": f"ep:{ep_ids[0]}", "score": 0.96},
        {"id": f"mem:{mem_ids[0]}", "score": 0.95},
    ]
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _SearchEmbeddingClient({"results": hits}))

    results = await vector._search_vector(_CountingDB(db), AGENT, "q", 10, min_similarity=0.0)

    assert [r["_rid"] for r in results] == [
        ("mem", mem_ids[1]),
        ("ep", ep_ids[0]),
        ("mem", mem_ids[0]),
    ]


@pytest.mark.asyncio
async def test_c16_batched_hydration_keeps_the_agent_isolation_predicate(_fresh_db, monkeypatch):
    """A hit naming another agent's row is dropped by the fetch's agent_id predicate.

    bug-100 (fail closed): row identity is pinned by the remote id, but ownership must
    not rest on the remote index alone. The batched statement carries every predicate
    the per-hit statement carried.
    """
    db = _fresh_db
    mine = await _seed_memory(db, "mine")
    theirs = await _seed_memory(db, "theirs", agent_id="agent.other")

    hits = [{"id": f"mem:{theirs}", "score": 0.99}, {"id": f"mem:{mine}", "score": 0.98}]
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _SearchEmbeddingClient({"results": hits}))

    results = await vector._search_vector(_CountingDB(db), AGENT, "q", 10, min_similarity=0.0)

    assert [r["_rid"] for r in results] == [("mem", mine)], (
        "the batched fetch surfaced another agent's row (bug-100 class)"
    )
