"""The heuristic rrf gate and rows only one retriever can vote for.

Under ``CPERSONA_RECALL_MODE=rrf`` with no calibrated gate, ``_apply_quality_gate``
compares a row's reciprocal-rank score with ``min_score * RRF_MAX_SCALE``, where
``RRF_MAX_SCALE = 3 / (RRF_K + 1)`` is the score of a row ranked first by every
retriever. One vote is worth at most ``1 / (RRF_K + 1)``, and ``_adaptive_min_score``
is above 1/3 for every pool of 30 rows or fewer, so a row that only one retriever
can rank never passed there -- not even the exact text of a stored memory at rank 1.
That is every recall of a small agent under ``CPERSONA_EMBEDDING_MODE=none``, every
recall while the query cannot be embedded, and every row stored without an embedding.

The fix caps the heuristic threshold at the one-vote maximum for exactly those rows,
so a single retriever's first-ranked row is admitted and its second is not, and a row
the vector retriever could have ranked keeps the three-vote threshold.

Each scenario inserts its own rows under its own agent id, with controlled embedding
blobs, and reads the gate decisions from the recall trace.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a1_u04.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import config  # noqa: E402
from cpersona import health  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona import scope_stats  # noqa: E402
from cpersona import vector as V  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient, EmbedOutcome  # noqa: E402
from cpersona.database import get_db  # noqa: E402

from tests.conftest import FakeEmbeddingClient, fake_embed_one  # noqa: E402

TARGET = "the deploy pipeline uses kubernetes on the staging cluster named orchid"
ONE_VOTE = 1.0 / (config.RRF_K + 1)


class _OutageClient(FakeEmbeddingClient):
    """A configured client whose backend is down: every embed produces nothing."""

    async def embed(self, texts):
        return None

    async def embed_with_outcome(self, texts):
        return None, EmbedOutcome(attempted=True, ok=False, error="backend down")


@pytest.fixture(autouse=True)
def _default_rrf_config(monkeypatch):
    """The code defaults the defect lives in, pinned so the suite's env cannot move them:
    rrf fusion, confidence off, no calibrated gate, block reach off (its seats are not
    gated, so they would hide what the gate decided)."""
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    monkeypatch.setattr(config, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", False)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", False)
    monkeypatch.setattr(V, "_fused_gate_signal", None)
    monkeypatch.setattr(V, "_get_fused_gate", lambda agent_id: None)
    monkeypatch.setattr(V, "_embedding_client", None)
    health._reset()
    yield
    health._reset()


@pytest_asyncio.fixture(autouse=True)
async def _empty_tables():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.execute("DELETE FROM profiles")
    await db.commit()
    scope_stats.clear()
    yield


async def _insert(agent: str, content: str, blob: bytes | None) -> int:
    db = await get_db()
    ts = "2026-01-01T00:00:00Z"
    cur = await db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, embedding, created_at) "
        "VALUES (?, '', ?, '{}', ?, ?, ?)",
        (agent, content, ts, blob, ts),
    )
    await db.commit()
    return cur.lastrowid


def _blob(text: str) -> bytes:
    return EmbeddingClient.pack_embedding(fake_embed_one(text))


async def _seed(agent: str, n: int, *, target_blob: bytes | None, filler_blobs: bool) -> int:
    """The target plus n-1 rows that share no word with it; returns the target's id."""
    target_id = await _insert(agent, TARGET, target_blob)
    for i in range(n - 1):
        text = f"filler note {i} about lunch menu item {i * 7}"
        await _insert(agent, text, _blob(text) if filler_blobs else None)
    scope_stats.clear()
    return target_id


def _decision(out: dict, ref: str) -> dict:
    for d in out["trace"]["gate"]["decisions"]:
        if d["ref"] == ref:
            return d
    raise AssertionError(f"no gate decision for {ref}: {out['trace']['gate']['decisions']!r}")


def _contents(out: dict) -> list[str]:
    return [m["content"] for m in out.get("messages", [])]


def _heuristic(pool: int) -> float:
    return M._adaptive_min_score(pool) * config.RRF_MAX_SCALE


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [1, 10, 30])
async def test_mode_none_returns_the_exact_match_on_a_small_pool(n):
    """No embedding client: the keyword retriever is the only one that can vote. The
    exact text of a stored memory, ranked first by that retriever, must be returned."""
    agent = f"u04.none.{n}"
    tid = await _seed(agent, n, target_blob=None, filler_blobs=False)
    out = await M.do_recall(agent, TARGET, 5, trace=True)
    d = _decision(out, f"mem:{tid}")
    assert TARGET in _contents(out), (
        f"pool {n}: exact-text recall returned {_contents(out)!r}; gate {d!r}"
    )
    assert d["passed"] is True and d["score"] == pytest.approx(ONE_VOTE)
    assert d["threshold"] == pytest.approx(min(_heuristic(n), ONE_VOTE))


@pytest.mark.asyncio
async def test_query_embed_failure_returns_the_exact_match(monkeypatch):
    """The backend is configured but down, so the query is never embedded and the
    vector retriever votes for nothing -- although every row has a stored vector."""
    monkeypatch.setattr(V, "_embedding_client", _OutageClient())
    agent = "u04.outage"
    tid = await _seed(agent, 10, target_blob=_blob(TARGET), filler_blobs=True)
    out = await M.do_recall(agent, TARGET, 5, trace=True)
    d = _decision(out, f"mem:{tid}")
    assert TARGET in _contents(out), f"outage recall returned {_contents(out)!r}; gate {d!r}"
    assert d["threshold"] == pytest.approx(ONE_VOTE)


@pytest.mark.asyncio
async def test_row_without_embedding_is_returned_while_the_vector_arm_runs(monkeypatch):
    """The backend is healthy and the vector retriever ranks the embedded rows, but the
    target was stored without an embedding (stored during an outage, not re-embedded
    yet), so only the keyword retriever can vote for it."""
    monkeypatch.setattr(V, "_embedding_client", FakeEmbeddingClient())
    agent = "u04.nullrow"
    tid = await _seed(agent, 10, target_blob=None, filler_blobs=True)
    out = await M.do_recall(agent, TARGET, 5, trace=True)
    d = _decision(out, f"mem:{tid}")
    near = [a["ref"] for a in out["trace"]["arms"]["vector_near"]]
    assert near and f"mem:{tid}" not in near, near  # the vector arm ran and voted, not for it
    assert TARGET in _contents(out), f"null-embedding row not returned: {_contents(out)!r}; gate {d!r}"
    assert d["threshold"] == pytest.approx(ONE_VOTE)


@pytest.mark.asyncio
async def test_a_row_the_vector_arm_could_rank_keeps_the_three_vote_threshold(monkeypatch):
    """Control for the scope of the fix. The target HAS a stored vector, packed from a
    text disjoint from its content, so the healthy vector retriever could have voted for
    it and did not. Its keyword-only score is held to the unchanged three-vote threshold
    and, on a pool of 10, still blocked: the cap applies to rows one retriever is the
    only one able to rank, not to every row one retriever happened to rank."""
    monkeypatch.setattr(V, "_embedding_client", FakeEmbeddingClient())
    agent = "u04.twoarm"
    tid = await _seed(agent, 10, target_blob=_blob("zebra quartz violin meadow"), filler_blobs=True)
    out = await M.do_recall(agent, TARGET, 5, trace=True)
    d = _decision(out, f"mem:{tid}")
    near = [a["ref"] for a in out["trace"]["arms"]["vector_near"]]
    assert near and f"mem:{tid}" not in near, near  # the vector arm ran and voted, not for it
    assert d["score"] == pytest.approx(ONE_VOTE), d
    assert d["threshold"] == pytest.approx(_heuristic(10)), d
    assert d["passed"] is False and TARGET not in _contents(out)


@pytest.mark.asyncio
async def test_single_arm_cap_admits_the_first_rank_only():
    """The cap is the one-vote maximum, not a rescale of the heuristic to one vote: on
    a small pool the second-ranked keyword hit is still below the threshold."""
    agent = "u04.ranks"
    ids = [
        await _insert(agent, "kubernetes kubernetes kubernetes rollout notes", None),
        await _insert(agent, "a note that mentions kubernetes once among many other words here", None),
    ]
    for i in range(8):
        await _insert(agent, f"filler note {i} about lunch menu item {i * 7}", None)
    scope_stats.clear()
    out = await M.do_recall(agent, "kubernetes", 5, trace=True)
    first, second = _decision(out, f"mem:{ids[0]}"), _decision(out, f"mem:{ids[1]}")
    assert first["passed"] is True and first["score"] == pytest.approx(ONE_VOTE), first
    assert second["score"] == pytest.approx(1.0 / (config.RRF_K + 2)), second
    assert second["passed"] is False, second
    assert _contents(out) == ["kubernetes kubernetes kubernetes rollout notes"]


@pytest.mark.asyncio
async def test_large_pool_threshold_is_unchanged_for_single_arm_rows():
    """Past a pool of about 31 the heuristic is already below one vote; the cap must
    leave it there rather than raise it to the one-vote maximum."""
    agent = "u04.large"
    tid = await _seed(agent, 60, target_blob=None, filler_blobs=False)
    out = await M.do_recall(agent, TARGET, 5, trace=True)
    d = _decision(out, f"mem:{tid}")
    assert _heuristic(60) < ONE_VOTE
    assert d["threshold"] == pytest.approx(_heuristic(60)), d
    assert TARGET in _contents(out)


@pytest.mark.asyncio
async def test_remote_answer_leaves_the_three_vote_threshold(monkeypatch):
    """Remote vector search answered (here: with no match). The query is not embedded
    locally on that path, but the remote service ranked from its own index and could
    have voted for the target, so the target keeps the unchanged threshold."""

    class _RemoteClient(FakeEmbeddingClient):
        _http_url = "http://127.0.0.1:9/embed"

    async def _answered_empty(*args, **kwargs):
        return []

    monkeypatch.setattr(V, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(V, "_embedding_client", _RemoteClient())
    monkeypatch.setattr(V, "_search_vector_remote", _answered_empty)
    agent = "u04.remote"
    tid = await _seed(agent, 10, target_blob=None, filler_blobs=True)
    out = await M.do_recall(agent, TARGET, 5, trace=True)
    d = _decision(out, f"mem:{tid}")
    assert d["threshold"] == pytest.approx(_heuristic(10)), d
    assert d["passed"] is False
