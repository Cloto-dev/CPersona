"""The coarse index's reach: what recall suggests, and what the index path reads.

Design: `docs/BINARY_COARSE_SEARCH_DESIGN.md` §4 and §7.

The suggestion and the remainder hint name one call,
``check_health(checks=['coarse_index'], fix=true)``. Each must be said only when
that call would build something a time cue then reads:

- the suggestion counts the rows the check counts (embedded ones), in the scope
  the cue arm searches (its source filter included), and stays silent where a
  recall has no local vector search for the remainder to extend (no embedding
  client, or remote vector search);
- the hint is said as soon as one record of the period is past the window, so
  the fix builds the absent file from the first row past it, not only from the
  line at which the check starts to report the absence unasked.

The index path reads the rows written since the build (and the rows the change
log names) in bounded chunks, as the live supplier reads the store: a stale file
must not turn every cued recall into one read of every row since the build.
"""

import os

import aiosqlite
import numpy as np
import pytest
import pytest_asyncio

from cpersona import checks, coarse_index, coarse_search, config, vector, vector_index
from cpersona import memory_handlers as M
from cpersona.database import get_db
from cpersona.maintenance_handlers import do_check_health
from tests.conftest import FakeEmbeddingClient, fake_embed_one
from tests.test_cue_coarse import CAP, QUERY, TOTAL, OneHotClient, _seed

AGENT = "w09.agent"
WINDOW = 10
LINE = 5
TIME_CUE = {"after": "2026-03-10", "before": "2026-03-19", "confidence": "sure"}


def _clean_index():
    path = coarse_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


async def _wipe(conn):
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()


@pytest_asyncio.fixture
async def scope_db(monkeypatch):
    """A small window and line, the default cue mode, and no index file."""
    monkeypatch.setattr(vector, "MAX_MEMORIES", WINDOW)
    monkeypatch.setattr(M, "MAX_MEMORIES", WINDOW)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", LINE)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", False)
    conn = await get_db()
    _clean_index()
    await _wipe(conn)
    yield conn
    await _wipe(conn)
    _clean_index()


async def _rows(conn, n, *, embedded=True, source="w09.src.a", offset=0):
    for i in range(offset, offset + n):
        text = f"note {i} about topic{i}"
        blob = np.asarray(fake_embed_one(text), dtype=np.float32).tobytes() if embedded else None
        await conn.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at, embedding)"
            " VALUES (?, '', '', ?, ?, '2026-03-15T00:00:00+00:00', ?, ?)",
            (AGENT, text, '{"type": "User", "id": "%s"}' % source,
             "2026-03-15 00:%02d:%02d" % (i // 60 % 60, i % 60), blob),
        )
    await conn.commit()


async def _fix_run() -> list[dict]:
    """The call the suggestion and the hint name, as a client makes it."""
    result = await do_check_health(agent_id="", fix=True, checks=["coarse_index"])
    return [i for i in result["issues"] if i["check"] == "coarse_index"]


async def _suggestion(**kw):
    r = await M.do_recall(AGENT, "topic3", 5, **kw)
    return r.get("suggestion")


# ── the suggestion names a build the check makes ─────────────────────────────


@pytest.mark.asyncio
async def test_no_suggestion_without_an_embedding_client(scope_db, monkeypatch):
    """EMBEDDING_MODE=none: no vectors, no vector half, and nothing for a build to hold."""
    monkeypatch.setattr(vector, "_embedding_client", None)
    await _rows(scope_db, WINDOW + LINE + 20, embedded=False)
    assert await _suggestion(session_key="none-1") is None
    assert await _fix_run() == []  # the call would build nothing: no vectors


@pytest.mark.asyncio
async def test_no_suggestion_under_remote_vector_search(scope_db, monkeypatch):
    """The remote service ranks under its own window: the cue's remainder has no effect there."""

    class RemoteClient(FakeEmbeddingClient):
        _http_url = "http://127.0.0.1:9/embed"

    async def answered(*a, **kw):
        return []

    monkeypatch.setattr(vector, "_embedding_client", RemoteClient())
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_search_vector_remote", answered)
    await _rows(scope_db, WINDOW + LINE + 20)
    assert await _suggestion(session_key="remote-1") is None


@pytest.mark.asyncio
async def test_the_suggestion_counts_embedded_rows_only(scope_db, fake_embedding_client):
    """Rows without a vector are neither searched by the cue nor held by the index."""
    await _rows(scope_db, WINDOW + LINE - 1)  # one short of the line, embedded
    await _rows(scope_db, 40, embedded=False, offset=500)
    assert await _suggestion(session_key="emb-1") is None
    assert await checks.check_coarse_index(scope_db, "", fix=False) == []  # the check's line agrees
    # One more embedded row reaches the line: said, with the embedded count, and the
    # call it names then builds.
    await _rows(scope_db, 1, offset=900)
    said = await _suggestion(session_key="emb-2")
    assert said is not None
    assert (said["records"], said["past_window"]) == (WINDOW + LINE, LINE)
    [issue] = await _fix_run()
    assert issue["rebuilt"] is True
    assert coarse_index.cached_coarse_index("memories") is not None


@pytest.mark.asyncio
async def test_the_suggestion_counts_the_recalls_own_source(scope_db, fake_embedding_client):
    """A per-user recall searches that principal's rows; another principal's volume is not its scope."""
    await _rows(scope_db, WINDOW + LINE + 20, source="w09.src.a")
    await _rows(scope_db, 3, source="w09.src.b", offset=700)
    assert await _suggestion(session_key="src-b", source_id="w09.src.b") is None
    said = await _suggestion(session_key="src-a", source_id="w09.src.a")
    assert said is not None and said["records"] == WINDOW + LINE + 20


# ── the remainder hint names a build the check makes ─────────────────────────


@pytest_asyncio.fixture
async def cue_db(monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", OneHotClient())
    monkeypatch.setattr(vector, "MAX_MEMORIES", CAP)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(M, "MAX_MEMORIES", CAP)
    monkeypatch.setattr(M, "FTS_ENABLED", False)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", False)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")
    conn = await get_db()
    _clean_index()
    await _wipe(conn)
    yield conn
    await _wipe(conn)
    _clean_index()


@pytest.mark.asyncio
async def test_the_remainder_hint_names_a_call_that_builds(cue_db):
    """Below the line the check is silent unasked, but under fix it builds the absent file."""
    await _seed(cue_db)
    assert TOTAL - CAP < checks.INDEX_MATTERS_ROWS  # the default line: the case the hint reaches
    first = await M.do_recall("cue.coarse.agent", QUERY, 5, time_cue=TIME_CUE)
    assert first["time_cue"]["remainder"]["hint"] == M.CUE_REMAINDER_HINT
    assert await checks.check_coarse_index(cue_db, "", fix=False) == []
    [issue] = await _fix_run()
    assert issue["type"] == "coarse_index_absent" and issue["rebuilt"] is True
    assert coarse_index.cached_coarse_index("memories") is not None
    again = await M.do_recall("cue.coarse.agent", QUERY, 5, time_cue=TIME_CUE, trace=True)
    assert "remainder" not in again["time_cue"]
    assert again["trace"]["stages"][0]["remainder"] == {"mode": "auto", "supplier": "index"}


# ── the index path reads its tail in chunks ──────────────────────────────────

DIM = 16
CHUNK = 4


def _vector(seed: int, dim: int = DIM) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)


async def _insert(conn, seed, *, created_at, embedding=None):
    await conn.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at, embedding)"
        " VALUES (?, '', '', ?, '{}', '2026-03-01 06:00:00', ?, ?)",
        (AGENT, f"row {seed}", created_at, (_vector(seed) if embedding is None else embedding).tobytes()),
    )


@pytest_asyncio.fixture
async def stale(tmp_path):
    """An index over 30 rows, then 40 rows written since, interleaved on created_at."""
    conn = await get_db()
    await _wipe(conn)
    for seed in range(1, 31):
        await _insert(conn, seed, created_at="2026-03-01 00:00:%02d" % (seed % 9))
    await conn.commit()
    path = str(tmp_path / "coarse")
    await vector_index.start_logging()
    assert (await coarse_index.build_coarse_index(conn, "memories", path))["built"]
    for seed in range(31, 71):
        await _insert(conn, seed, created_at="2026-03-01 00:00:%02d" % (seed % 11))
    await conn.commit()
    yield conn, path
    await _wipe(conn)


class _BlobReads:
    """The most stored vectors any one read handed back, across every read API."""

    def __init__(self, monkeypatch, width: int):
        self.width, self.most = width, 0
        for cls, name in ((aiosqlite.Connection, "execute_fetchall"), (aiosqlite.Cursor, "fetchall"),
                          (aiosqlite.Cursor, "fetchmany")):
            monkeypatch.setattr(cls, name, self._wrap(getattr(cls, name)))

    def _wrap(self, original):
        async def spy(*a, **kw):
            rows = await original(*a, **kw)
            blobs = sum(1 for row in rows if any(isinstance(v, bytes) and len(v) == self.width for v in row))
            self.most = max(self.most, blobs)
            return rows

        return spy


@pytest.mark.asyncio
@pytest.mark.parametrize("period", [None, ("2026-02-01 00:00:00", "2026-04-01 00:00:00")])
@pytest.mark.parametrize(("start", "end"), [(0, None), (7, None), (5, 50), (45, None)])
async def test_the_index_reads_the_rows_since_its_build_in_chunks(stale, tmp_path, monkeypatch, period, start, end):
    conn, path = stale
    monkeypatch.setattr(coarse_search, "VECTOR_SCAN_CHUNK_ROWS", CHUNK)
    scope = dict(agent_id=AGENT, start=start, end=end, period=period, k=12)
    query = _vector(4242)
    live = await coarse_search.coarse_candidates(conn, query, path=str(tmp_path / "none"), **scope)
    reads = _BlobReads(monkeypatch, DIM * 4)
    got = await coarse_search.coarse_candidates(conn, query, path=path, live=False, **scope)
    assert got.source == "index"  # the index answered: a fallback proves nothing
    assert got.rows() == live.rows()
    assert any(i > 30 for i in live.ids) or start >= 45  # rows written since the build were ranked
    assert reads.most <= CHUNK, f"one read handed back {reads.most} stored vectors"


@pytest.mark.asyncio
async def test_a_tail_row_of_another_width_still_sends_the_question_to_the_live_store(stale, tmp_path, monkeypatch):
    conn, path = stale
    monkeypatch.setattr(coarse_search, "VECTOR_SCAN_CHUNK_ROWS", CHUNK)
    await _insert(conn, 999, created_at="2026-03-01 00:00:05", embedding=_vector(999, DIM * 2))
    await conn.commit()
    scope = dict(agent_id=AGENT, start=3, k=12)
    assert (await coarse_search.coarse_candidates(conn, _vector(1), path=path, live=False, **scope)).source == "skipped"
    got = await coarse_search.coarse_candidates(conn, _vector(1), path=path, **scope)
    want = await coarse_search.coarse_candidates(conn, _vector(1), path=str(tmp_path / "none"), **scope)
    assert got.source == "live" and got.rows() == want.rows()
