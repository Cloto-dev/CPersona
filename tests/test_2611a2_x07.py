"""Recall paths that crashed on an input or a state they should survive, and remote routes.

1. The propagation seat read the anchor's and every candidate's stored vector with a
   single ``id IN (...)``, one bound parameter per id. The candidates come from a
   deeper ranking whose depth follows the depth floor, while every other id read on
   the recall is split at ``vector._ID_FETCH_CHUNK`` for SQLite's older 999-variable
   ceiling. Past the ceiling the read raised "too many SQL variables" and took the
   whole recall with it.

2. The rare-phrase shortcut counts the indexed rows and then, statement by statement,
   the rows holding each phrase. Rows committed in between (by any agent: both
   counts are store-wide) can make a phrase's count exceed the row count, and the idf
   ``log((N - n + 0.5) / (n + 0.5))`` then raised "math domain error". The caller
   caught only ``sqlite3.OperationalError``, so recall and reconstruct failed.

3. The episode full-text search ran its MATCH unguarded, while the memory keyword
   arm falls back when its index cannot answer. A query holding U+0000 (an
   unterminated FTS5 string) or an absent ``episodes_fts`` table failed the recall.

4. Under ``CPERSONA_VECTOR_SEARCH_MODE=remote`` the index, search, remove and purge
   URLs were cut from the embedding URL at its last ``/``. That dropped the query
   string, where an endpoint credential is carried, and turned ``.../embed/`` into
   ``.../embed/index``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
from types import SimpleNamespace

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x07.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import aiosqlite  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import admin_handlers  # noqa: E402
from cpersona import database  # noqa: E402
from cpersona import maintenance_handlers  # noqa: E402
from cpersona import memory_handlers  # noqa: E402
from cpersona import reconstruct  # noqa: E402
from cpersona import session  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402
from cpersona.database import get_db  # noqa: E402


async def _wipe(agents):
    db = await get_db()
    for agent in agents:
        for table in ("memories", "episodes", "profiles"):
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()


# --------------------------------------------------------------------------
# 1. the propagation seat's vector read is chunked
# --------------------------------------------------------------------------

SEAT_AGENT = "x07.seat"
SEAT_QUERY = "harbor lighthouse keeper logbook"
#: A chunk width far below the shipped one, so a corpus of a few dozen rows crosses it.
SEAT_CHUNK = 8
#: The variable ceiling the read connection is lowered to: above every chunked read on
#: the recall at SEAT_CHUNK (measured at 9 parameters), below one unchunked statement
#: over the seat's candidates.
SEAT_CEILING = 20


@pytest_asyncio.fixture
async def seat_corpus(fake_embedding_client):
    session.reset_pauses_for_tests()
    await _wipe([SEAT_AGENT])
    for i in range(60):
        await memory_handlers.do_store(SEAT_AGENT, {"content": f"{SEAT_QUERY} entry {i}"})
    yield
    await _wipe([SEAT_AGENT])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rrf", "rsf"])
async def test_the_propagation_seat_reads_its_vectors_under_the_variable_ceiling(seat_corpus, monkeypatch, mode):
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", mode)
    reference = await memory_handlers.do_recall(SEAT_AGENT, SEAT_QUERY, limit=3, propagation_seat=True, trace=True)
    note = reference["trace"]["propagation"]
    assert note["candidates"] > 2 * SEAT_CHUNK + 1, note
    assert len(note["seated"]) == 1, note

    monkeypatch.setattr(vector, "_ID_FETCH_CHUNK", SEAT_CHUNK)
    # A reader of its own: sqlite3 caches prepared statements per connection, and a
    # statement the reference recall prepared is reused without the ceiling's check.
    reader = await database._open_reader()
    await reader._execute(reader._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, SEAT_CEILING)

    @contextlib.asynccontextmanager
    async def limited():
        yield reader

    try:
        with monkeypatch.context() as m:
            m.setattr(memory_handlers, "connection", limited)
            out = await memory_handlers.do_recall(
                SEAT_AGENT, SEAT_QUERY, limit=3, propagation_seat=True, trace=True
            )
    finally:
        await reader.close()
    # The same seat, measured from every candidate's vector: a chunking that lost a
    # chunk would move the cosine rank or the seated row.
    assert out["trace"]["propagation"] == note


# --------------------------------------------------------------------------
# 2. a phrase count taken after rows were committed does not break the idf
# --------------------------------------------------------------------------

PHRASE_AGENT = "x07.phrase"
PHRASE_WRITER = "x07.phrase.writer"
PHRASE_QUERY = "the zebra crossing"


class _CountsDouble:
    """Answers the row count with ``n_rows`` and every phrase count with ``held``."""

    def __init__(self, n_rows: int, held: int):
        self.n_rows = n_rows
        self.held = held

    async def execute_fetchall(self, sql, params=()):
        if "memories_fts_docsize" in sql:
            return [(self.n_rows,)]
        return [(self.held,)]


@pytest.mark.asyncio
async def test_a_phrase_counted_in_more_rows_than_the_row_count_is_common():
    # 23 rows hold "the" against a row count of 20 read a moment earlier: the phrase is
    # in every row the index has, which is the idf floor, not a domain error.
    common = await memory_handlers._common_phrases(_CountsDouble(20, 23), ['"the"', '"zebra"', '"crossing"'])
    assert common == {'"the"'}


@pytest_asyncio.fixture
async def phrase_corpus(monkeypatch):
    session.reset_pauses_for_tests()
    monkeypatch.setattr(vector, "_embedding_client", None)
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    await _wipe([PHRASE_AGENT, PHRASE_WRITER])
    for i in range(20):
        await memory_handlers.do_store(PHRASE_AGENT, {"content": f"the note number {i} about the plan"})
    yield
    await _wipe([PHRASE_AGENT, PHRASE_WRITER])


def _commit_between_the_counts(monkeypatch):
    """Store rows for another agent right after the next row count is read.

    The stores commit on the writer connection, as a concurrent request's would; the
    phrase counts that follow on the read side see them, the row count did not.
    """
    original = aiosqlite.Connection.execute_fetchall
    state = {"armed": True, "fired": 0}

    async def spy(self, sql, params=None):
        out = await original(self, sql, params)
        if state["armed"] and sql.strip() == "SELECT count(*) FROM memories_fts_docsize":
            state["armed"] = False
            state["fired"] += 1
            for j in range(3):
                await memory_handlers.do_store(PHRASE_WRITER, {"content": f"the other agent wrote {j}"})
        return out

    monkeypatch.setattr(aiosqlite.Connection, "execute_fetchall", spy)
    return state


@pytest.mark.asyncio
async def test_recall_and_reconstruct_survive_rows_committed_between_the_counts(phrase_corpus, monkeypatch):
    state = _commit_between_the_counts(monkeypatch)
    out = await memory_handlers.do_recall(PHRASE_AGENT, PHRASE_QUERY, 5)
    assert state["fired"] == 1, "the interleaving did not happen, so nothing was tested"
    assert "error" not in out and out["messages"], out
    state["armed"] = True
    rebuilt = await reconstruct.do_reconstruct(PHRASE_AGENT, PHRASE_QUERY, count=3)
    assert state["fired"] == 2
    assert "error" not in rebuilt, rebuilt


@pytest.mark.asyncio
async def test_the_shortcut_failing_in_any_way_ranks_on_the_whole_expression(phrase_corpus, monkeypatch):
    db = await get_db()
    whole = await memory_handlers._search_memories_keyword(db, PHRASE_AGENT, PHRASE_QUERY, 5)

    async def broken(db, phrases):
        raise ValueError("math domain error")

    monkeypatch.setattr(memory_handlers, "_common_phrases", broken)
    ranked = await memory_handlers._search_memories_keyword(db, PHRASE_AGENT, PHRASE_QUERY, 5, rank_only=True)
    assert [r["id"] for r in ranked] == [r["id"] for r in whole]
    assert all(r["_bm25"] is not None for r in ranked), "the shortcut's failure sent the arm to LIKE"


# --------------------------------------------------------------------------
# 3. the episode full-text search degrades instead of failing the recall
# --------------------------------------------------------------------------

EP_AGENT = "x07.episodes"
NUL_QUERY = "apricot\x00jam"


@pytest_asyncio.fixture
async def episode_corpus(monkeypatch):
    session.reset_pauses_for_tests()
    monkeypatch.setattr(vector, "_embedding_client", None)
    await _wipe([EP_AGENT])
    await memory_handlers.do_store(EP_AGENT, {"content": "apricot jam recipe with lemon"})
    await memory_handlers.do_store(EP_AGENT, {"content": "photosynthesis converts light to energy"})
    await memory_handlers.do_archive_episode(
        EP_AGENT, [], summary="apricot jam and photosynthesis notes", keywords="apricot jam"
    )
    yield
    db = await get_db()
    present = await db.execute_fetchall("SELECT 1 FROM sqlite_master WHERE name = 'episodes_fts'")
    if not present:
        # The connections are shared by the whole suite: put the index back.
        await db.execute(database.FTS_TABLE_SQL["episodes_fts"])
        await db.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('rebuild')")
        await db.commit()
    await _wipe([EP_AGENT])


@pytest.mark.asyncio
async def test_a_nul_in_the_query_does_not_fail_the_episode_search(episode_corpus):
    db = await get_db()
    assert await memory_handlers._search_episodes_fts(db, EP_AGENT, NUL_QUERY, 5) == []
    # The control: without the NUL the same search finds the episode.
    assert await memory_handlers._search_episodes_fts(db, EP_AGENT, "apricot jam", 5)


@pytest.mark.asyncio
async def test_a_nul_in_the_query_does_not_fail_any_recall_tool(episode_corpus):
    out = await memory_handlers.do_recall(EP_AGENT, NUL_QUERY, 5)
    assert "error" not in out, out
    with_context = await memory_handlers.do_recall_with_context(EP_AGENT, NUL_QUERY, [], limit=5)
    assert "error" not in with_context, with_context
    rebuilt = await reconstruct.do_reconstruct(EP_AGENT, NUL_QUERY, count=3)
    assert "error" not in rebuilt, rebuilt


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rrf", "rsf", "cascade"])
async def test_recall_degrades_when_the_episode_index_is_absent(episode_corpus, monkeypatch, caplog, mode):
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", mode)
    before = await memory_handlers.do_recall(EP_AGENT, "photosynthesis", 5)
    async with database.transaction() as w:
        await w.execute("DROP TABLE episodes_fts")
    with caplog.at_level(logging.WARNING, logger="cpersona.memory_handlers"):
        out = await memory_handlers.do_recall(EP_AGENT, "photosynthesis", 5)
    assert "error" not in out, out
    # Only the episode the keyword search found is missing: the memory side answers.
    memories = [m["content"] for m in before["messages"] if not m["content"].startswith("[Episode]")]
    if mode != "cascade":
        # The fused modes find both, so the comparison has an episode to lose. The
        # cascade answers this corpus with nothing either way (measured), and is here
        # for its own call into the episode search.
        assert memories and len(memories) < len(before["messages"]), before
    assert [m["content"] for m in out["messages"]] == memories, (before, out)
    assert any("episodes_fts" in r.getMessage() for r in caplog.records), "the degradation was not said"


# --------------------------------------------------------------------------
# 4. remote routes keep the configured URL's query and handle a trailing slash
# --------------------------------------------------------------------------

REMOTE_AGENT = "x07.remote"
_REMOTE_ROUTES = {"index", "search", "remove", "purge"}


class _RemoteService:
    """A mock embedding and index service that refuses a request without the key."""

    def __init__(self, base_path: str):
        self.base_path = base_path
        self.seen: list[httpx.URL] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.url)
        if request.url.params.get("api_key") != "K":
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path.rstrip("/")
        if path == f"{self.base_path}/embed":
            n = len(json.loads(request.content)["texts"])
            return httpx.Response(200, json={"embeddings": [[0.1] * 8 for _ in range(n)]})
        if path == f"{self.base_path}/search":
            return httpx.Response(200, json={"results": []})
        if path.rsplit("/", 1)[-1] in _REMOTE_ROUTES and path.startswith(self.base_path + "/"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def routes(self) -> list[tuple[str, bool]]:
        """(path, carried the key) of every index-service request seen."""
        return [
            (u.path, u.params.get("api_key") == "K")
            for u in self.seen
            if u.path.rstrip("/").rsplit("/", 1)[-1] in _REMOTE_ROUTES
        ]


@pytest_asyncio.fixture
async def remote_service(monkeypatch):
    session.reset_pauses_for_tests()
    await _wipe([REMOTE_AGENT])
    transports: list[httpx.AsyncClient] = []

    async def install(url: str, base_path: str) -> _RemoteService:
        service = _RemoteService(base_path)
        client = EmbeddingClient(mode="http", http_url=url)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(service))
        transports.append(client._client)
        monkeypatch.setattr(vector, "_embedding_client", client)
        for module in (vector, memory_handlers, admin_handlers):
            monkeypatch.setattr(module, "VECTOR_SEARCH_MODE", "remote")
        monkeypatch.setattr(vector, "_remote_route_refusal_logged", False)
        return service

    yield install
    for transport in transports:
        await transport.aclose()
    await _wipe([REMOTE_AGENT])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    ["https://emb.example/v1/embed?api_key=K", "https://emb.example/v1/embed/?api_key=K"],
    ids=["query", "query-and-trailing-slash"],
)
async def test_store_and_search_reach_the_service_with_the_configured_query(remote_service, url):
    service = await remote_service(url, "/v1")
    stored = await memory_handlers.do_store(REMOTE_AGENT, {"content": "kubernetes ledger row"})
    assert stored.get("ok"), stored
    await memory_handlers.do_recall(REMOTE_AGENT, "kubernetes", 5)
    routes = service.routes()
    assert ("/v1/index", True) in routes and ("/v1/search", True) in routes, routes
    assert all(ok and path.startswith("/v1/") and path.count("/") == 2 for path, ok in routes), routes


@pytest.mark.asyncio
async def test_every_remote_side_effect_keeps_the_configured_query(remote_service):
    service = await remote_service("https://emb.example/v1/embed/?api_key=K", "/v1")
    first = await memory_handlers.do_store(REMOTE_AGENT, {"content": "first remote row"})
    second = await memory_handlers.do_store(REMOTE_AGENT, {"content": "second remote row"})
    await memory_handlers.do_archive_episode(REMOTE_AGENT, [], summary="a remote episode", keywords="remote")
    db = await get_db()
    ((episode_id,),) = await db.execute_fetchall("SELECT id FROM episodes WHERE agent_id = ?", (REMOTE_AGENT,))
    service.seen.clear()

    await admin_handlers.do_update_memory(first["id"], "first remote row, edited", REMOTE_AGENT)
    await admin_handlers.do_delete_memory(second["id"], REMOTE_AGENT)
    await admin_handlers.do_delete_episode(episode_id, REMOTE_AGENT)
    await vector.remote_index_upsert(REMOTE_AGENT, [{"id": "mem:1", "text": "bulk"}])
    await admin_handlers._remove_moved_source_vectors(
        REMOTE_AGENT, SimpleNamespace(copied_memory_ids=[first["id"]], copied_episode_ids=[])
    )
    await maintenance_handlers._sync_remote_index([(REMOTE_AGENT, 7, None), (REMOTE_AGENT, 8, "repaired")])
    await admin_handlers._purge_agent_remote_namespace(REMOTE_AGENT)

    routes = service.routes()
    assert sorted({path for path, _ in routes}) == ["/v1/index", "/v1/purge", "/v1/remove"], routes
    assert all(ok for _, ok in routes), routes


@pytest.mark.asyncio
async def test_a_url_that_names_no_embed_route_derives_no_remote_route(remote_service, caplog):
    service = await remote_service("https://emb.example/v1/encode?api_key=K", "/v1")
    with caplog.at_level(logging.WARNING, logger="cpersona.vector"):
        await memory_handlers.do_store(REMOTE_AGENT, {"content": "first unrouted row"})
        await memory_handlers.do_store(REMOTE_AGENT, {"content": "second unrouted row"})
        await memory_handlers.do_recall(REMOTE_AGENT, "unrouted", 5)
    assert service.routes() == [], "a route was guessed from a URL that does not name /embed"
    said = [r for r in caplog.records if "does not end in /embed" in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in caplog.records]
    assert "api_key" not in said[0].getMessage()


@pytest.mark.parametrize(
    ("url", "route", "expected"),
    [
        ("https://emb.example/v1/embed?api_key=K", "index", "https://emb.example/v1/index?api_key=K"),
        ("http://127.0.0.1:8401/embed/", "search", "http://127.0.0.1:8401/search"),
        ("http://127.0.0.1:8401/embed", "purge", "http://127.0.0.1:8401/purge"),
        ("http://127.0.0.1:8401/v1/encode", "index", None),
    ],
)
def test_remote_route_is_the_embed_urls_sibling(monkeypatch, url, route, expected):
    monkeypatch.setattr(vector, "_embedding_client", SimpleNamespace(_http_url=url))
    assert vector.remote_route(route) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "flagged"), [("http://x/v1/embed", False), ("http://x/v1/encode", True)])
async def test_a_remote_service_with_no_route_is_not_taken_to_have_ranked(monkeypatch, url, flagged):
    # The query was not embedded, so only the remote service could have ranked the
    # row by meaning; with no route to it nothing was asked, and the row's one
    # keyword vote is all it has.
    monkeypatch.setattr(vector, "_embedding_client", SimpleNamespace(_http_url=url))
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_remote_route_refusal_logged", False)
    row = {"_rrf_score": 1.0}
    await memory_handlers._mark_single_arm_rows(None, [(("mem", 1), row)], query_embedded=False)
    assert row.get("_rrf_single_arm", False) is flagged
