"""Regression tests for the 2.5.0b3 recall audit fixes (bug-125..126)."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from cpersona import memory_handlers, vector
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


def _assert_default_recall_config():
    assert memory_handlers.RECALL_MODE == "rrf"
    assert memory_handlers.CONFIDENCE_ENABLED is False
    assert memory_handlers.EPISODE_PENALTY_ENABLED is True


@pytest.mark.asyncio
async def test_empty_query_recall_bypasses_unscored_volume_gate(clean_db, fake_embedding_client):
    _assert_default_recall_config()
    agent_id = "empty-query-agent"
    contents = [
        "alpha launch checklist",
        "bravo database migration",
        "charlie customer notes",
        "delta release summary",
    ]
    for content in contents:
        stored = await memory_handlers.do_store(
            agent_id,
            {"content": content, "source": {"System": "test"}},
        )
        assert stored["ok"] and not stored.get("skipped")

    recent = await memory_handlers.do_recall(agent_id, query="", limit=10)
    assert {message["content"] for message in recent["messages"]} == set(contents), (
        "empty-query recall must return the small corpus's pure-recency rows"
    )

    unrelated_query = "ocean forest canyon"
    candidates = await vector._search_vector(
        clean_db,
        agent_id,
        unrelated_query,
        limit=10,
        min_similarity=(
            vector._get_vector_threshold(agent_id) * memory_handlers.RRF_THRESHOLD_FACTOR
        ),
    )
    assert candidates, "the unrelated query must produce a real pre-gate vector candidate"

    unrelated = await memory_handlers.do_recall(agent_id, query=unrelated_query, limit=10)
    assert unrelated["messages"] == [], (
        "a meaningful single-channel match in a small corpus must still be quality-gated"
    )


@pytest.mark.asyncio
async def test_episode_penalty_resorts_with_profile_row(clean_db):
    _assert_default_recall_config()
    agent_id = "profile-penalty-agent"
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) "
        "VALUES (?, 'boundary', 'boundary', datetime('now'))",
        (agent_id,),
    )
    await clean_db.commit()

    rows = [
        {
            "id": 1,
            "content": "old cross-session hit",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "_rrf_score": 0.05,
        },
        {
            "id": 2,
            "content": "current-session hit",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "_rrf_score": 0.045,
        },
        {
            "id": -1,
            "content": "[Profile] persistent user context",
            "source": {"System": "profile"},
            "timestamp": "",
        },
    ]

    results, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, agent_id, rows, deep=False
    )
    assert [row["id"] for row in results] == [2, 1, -1], (
        "the profile sentinel must not prevent the episode penalty from reordering scored rows"
    )
