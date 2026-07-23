"""Regression tests for the I3-memory fix group (252a2 audit clusters bug-146, bug-147).

bug-146 — do_store's remote-index branch discarded the POST response and set
``embedded=True`` for ANY non-raising response, including a silent HTTP 4xx/5xx
(httpx does not raise on those). A backend failure therefore reported
``embedded:true`` while the vector never landed, contradicting the store tool
contract ("embedded is true iff ... the remote index push succeeded").

bug-147 — ``_get_episode_boundary_ts`` computed MAX(created_at) over ALL of the
agent's episodes, ignoring the recall's project_id/channel scope, and runs by
default (EPISODE_PENALTY_ENABLED=true). A recall scoped to one bucket was
decayed against another bucket's most-recent episode, so in-scope
current-session memories got penalised against an unrelated project/channel.
"""
import os
import tempfile

# Keep the env hermetic; the fake clients are injected by monkeypatch, not env.
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_252a2_i3.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import memory_handlers as M  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.i3"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


# ============================================================
# bug-146 — remote /index push must gate ``embedded`` on the HTTP status
# ============================================================


class _RecordingHttpClient:
    """Minimal stand-in for the embedding client's httpx.AsyncClient.

    ``post`` returns a real ``httpx.Response`` with the configured status and
    does NOT raise on 4xx/5xx (matching httpx's own behaviour) — so only an
    explicit status check in do_store can distinguish success from failure.
    """

    def __init__(self, status: int):
        self.status = status
        self.post_calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None):
        self.post_calls.append((url, json))
        return httpx.Response(status_code=self.status, request=httpx.Request("POST", url))


class _FakeRemoteEmbeddingClient:
    """Drop-in for ``vector._embedding_client`` on the remote-index path.

    ``embed`` returns an empty vector so no LOCAL blob is written — the
    ``embedded`` flag then reflects the remote push alone, isolating bug-146.
    """

    mode = "remote"
    _http_url = "http://fake-embed.local/embed"

    def __init__(self, status: int):
        self._client = _RecordingHttpClient(status)

    @property
    def post_calls(self):
        return self._client.post_calls

    async def embed(self, texts):
        return [[] for _ in texts]


@pytest.mark.asyncio
async def test_bug146_remote_index_500_reports_embedded_false(monkeypatch):
    """A remote /index returning HTTP 500 -> store succeeds but embedded=False.

    Fail-first (unfixed): do_store discards the 500 response and sets
    remote_embedded=True unconditionally, so this asserts ``embedded is False``
    while the buggy path returns ``embedded=True``.
    """
    monkeypatch.setattr(M, "VECTOR_SEARCH_MODE", "remote")
    client = _FakeRemoteEmbeddingClient(status=500)
    monkeypatch.setattr(vector, "_embedding_client", client)

    res = await M.do_store(AGENT, {"content": "c3 five hundred", "source": {"System": "t"}})

    assert res["ok"] is True
    assert res.get("id"), res  # the SQL row still persists (no data loss)
    assert client.post_calls, "remote /index branch was not exercised"
    assert res["embedded"] is False, res


@pytest.mark.asyncio
async def test_bug146_remote_index_200_reports_embedded_true(monkeypatch):
    """Contrast: a remote /index returning HTTP 200 -> embedded=True.

    Guards against the fix over-correcting (never reporting success). Passes on
    both the buggy and fixed paths; paired with the 500 test it pins the flag to
    the actual push outcome.
    """
    monkeypatch.setattr(M, "VECTOR_SEARCH_MODE", "remote")
    client = _FakeRemoteEmbeddingClient(status=200)
    monkeypatch.setattr(vector, "_embedding_client", client)

    res = await M.do_store(AGENT, {"content": "c3 two hundred", "source": {"System": "t"}})

    assert res["ok"] is True
    assert client.post_calls, "remote /index branch was not exercised"
    assert res["embedded"] is True, res


# ============================================================
# bug-147 — the episode-boundary penalty must scope to the recall's project/channel
# ============================================================


async def _insert_episode(db, project_id: str, channel: str, created_at: str):
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (AGENT, project_id, channel, "ep", created_at),
    )
    await db.commit()


def _memory_row(rrf_score: float, timestamp: str) -> dict:
    # source is a JSON string (never a dict) so _is_episode_result() -> False.
    return {"id": 1, "timestamp": timestamp, "_rrf_score": rrf_score, "source": '{"User": "x"}'}


@pytest.mark.asyncio
async def test_bug147_boundary_scoped_to_recall_project():
    """An episode only in project A must not penalise a recall scoped to project B.

    Fail-first (unfixed): _get_episode_boundary_ts ignores project_id and returns
    A's 2026-07-23 boundary, so the B-memory (2026-07-20, before it) is decayed
    to the 0.5 floor -> _rrf_score drops 0.50 -> 0.25. Scoped correctly, project B
    has no episodes -> no boundary -> no penalty -> the score is untouched.
    """
    db = await get_db()
    await _insert_episode(db, project_id="A", channel="", created_at="2026-07-23 08:00:00")

    results = [_memory_row(0.50, "2026-07-20T09:00:00+00:00")]
    out, _range, _rc = await M._apply_recall_scoring(
        db, AGENT, results, deep=False, project_id="B", channel=""
    )

    assert out[0]["_rrf_score"] == pytest.approx(0.50), out


@pytest.mark.asyncio
async def test_bug147_boundary_scoped_to_recall_channel():
    """The same scoping must hold on the channel axis.

    Fail-first (unfixed): the channel of the episode is ignored, so a recall
    scoped to channel 'chatB' is penalised by the 'chatA' episode.
    """
    db = await get_db()
    await _insert_episode(db, project_id="", channel="chatA", created_at="2026-07-23 08:00:00")

    results = [_memory_row(0.50, "2026-07-20T09:00:00+00:00")]
    out, _range, _rc = await M._apply_recall_scoring(
        db, AGENT, results, deep=False, project_id=None, channel="chatB"
    )

    assert out[0]["_rrf_score"] == pytest.approx(0.50), out


@pytest.mark.asyncio
async def test_bug147_in_scope_episode_still_penalises():
    """Non-vacuity guard: an IN-scope episode must still penalise prior-session rows.

    Proves the bug-147 fix scopes the boundary rather than disabling the penalty
    wholesale. Passes on both the buggy and fixed paths.
    """
    db = await get_db()
    await _insert_episode(db, project_id="A", channel="", created_at="2026-07-23 08:00:00")

    results = [_memory_row(0.50, "2026-07-20T09:00:00+00:00")]
    out, _range, _rc = await M._apply_recall_scoring(
        db, AGENT, results, deep=False, project_id="A", channel=""
    )

    assert out[0]["_rrf_score"] < 0.50, out
