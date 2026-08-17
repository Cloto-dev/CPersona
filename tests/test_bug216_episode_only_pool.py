"""Regression tests for bug-216: the quality gate must count the pool it governs.

``_adaptive_min_score`` was fed a COUNT over the ``memories`` table alone, but its
threshold is applied to EVERY row the retrievers found — episodes and the profile
sentinel included. An agent with episodes and no memory rows (a session-summary-only
client, or one whose memories were removed by delete_memory / health repair while its
episodes stayed) therefore scored count 0, and count 0 returned 1.0 — at or above the
ceiling of every gate branch:

- cosine / confidence cannot reach 1.0 for a real pair,
- the rrf branch compares against ``min_score * RRF_MAX_SCALE``, which at min_score 1.0
  is the theoretical rank-1-in-all-three-retrievers maximum,
- the unscored branch needs >= 100 and the profile branch >= 50 of the same count.

So every retrieved episode was discarded and recall answered ``{"messages": []}`` while
the data was present and had been retrieved — permanently, until a memory row was
written. Two changes, pinned separately below: the count spans memories + episodes over
the recall's own isolation scope, and the empty-pool threshold is floored at the
small-pool value instead of 1.0.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_bug216.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.bug216"
EPISODE_COUNT = 60
QUERY = "deployment rollback"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


async def _seed_episodes(count: int = EPISODE_COUNT) -> None:
    """``count`` episodes, no memory rows at all — the reported corpus shape.

    start_time is left NULL (the bug-213 majority case), so each episode's recall
    timestamp is its created_at and none of them is penalised as pre-boundary — the
    gate, not the episode-boundary decay, is what this file is about.
    """
    db = await get_db()
    for i in range(count):
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, resolved) VALUES (?, ?, 0)",
            (AGENT, f"session {i}: deployment rollback of the billing service"),
        )
    await db.commit()
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (AGENT,))
    assert rows[0][0] == 0, "the premise is an EMPTY memories table"


def test_adaptive_min_score_empty_pool_is_reachable():
    """bug-216: 1.0 is above every gate branch's ceiling — an empty pool cannot mean 'reject all'."""
    assert M._adaptive_min_score(0) == 0.5
    assert M._adaptive_min_score(0) * config.RRF_MAX_SCALE < config.RRF_MAX_SCALE, (
        "the rrf threshold must stay below the rank-1-in-every-retriever maximum"
    )
    # The floor is the count -> 0 limit of the curve, so the sequence stays monotone.
    assert M._adaptive_min_score(0) >= M._adaptive_min_score(1) >= M._adaptive_min_score(500)


@pytest.mark.asyncio
async def test_recall_returns_episodes_for_an_agent_with_no_memories():
    """bug-216: episodes are part of the pool the gate governs, so they raise the count."""
    await _seed_episodes()
    out = await M.do_recall(AGENT, QUERY, limit=5)
    assert out["messages"], (
        "every retrieved episode was blocked — the gate is counting a pool it does not govern"
    )
    assert all("[Episode]" in m["content"] for m in out["messages"]), out["messages"]


@pytest.mark.asyncio
async def test_episode_only_pool_count_reaches_the_gate(monkeypatch):
    """bug-216: the count the gate keys on spans memories + episodes."""
    await _seed_episodes()
    seen: list[int] = []
    real_gate = M._apply_quality_gate

    def spy(results, min_score, memory_count, **kw):
        seen.append(memory_count)
        return real_gate(results, min_score, memory_count, **kw)

    monkeypatch.setattr(M, "_apply_quality_gate", spy)
    await M.do_recall(AGENT, QUERY, limit=5)
    assert seen == [EPISODE_COUNT], f"gate saw pool size {seen}, expected [{EPISODE_COUNT}]"
