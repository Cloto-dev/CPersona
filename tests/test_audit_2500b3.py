"""Regression tests for the 2.5.0b3 audit fixes (bug-125..129)."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from cpersona import checks, health, memory_handlers, vector
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


@pytest.fixture(autouse=True)
def reset_health_state():
    health._reset()
    yield
    health._reset()


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


async def _seed_rewrite_collision(clean_db, dirty_content):
    agent_id = "rewrite-collision-agent"
    survivor = await clean_db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) "
        "VALUES (?, 'collision body', '', X'00000000')",
        (agent_id,),
    )
    collider = await clean_db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) "
        "VALUES (?, ?, '', X'00000000')",
        (agent_id, dirty_content),
    )
    await clean_db.commit()
    return agent_id, survivor.lastrowid, collider.lastrowid


@pytest.mark.asyncio
async def test_memory_annotation_collision_deletes_rewritten_duplicate(clean_db):
    agent_id, survivor_id, collider_id = await _seed_rewrite_collision(
        clean_db, "[Memory from Discord] collision body"
    )

    await checks.check_memory_annotation(clean_db, agent_id, fix=True)

    rows = await clean_db.execute_fetchall(
        "SELECT id, content, embedding FROM memories WHERE agent_id = ? ORDER BY id",
        (agent_id,),
    )
    assert rows == [(survivor_id, "collision body", b"\x00\x00\x00\x00")]
    assert all(row[0] != collider_id for row in rows)


@pytest.mark.asyncio
async def test_discord_mention_collision_deletes_rewritten_duplicate(clean_db):
    agent_id, survivor_id, collider_id = await _seed_rewrite_collision(
        clean_db, "<@123> collision body"
    )

    await checks.check_discord_mention(clean_db, agent_id, fix=True)

    rows = await clean_db.execute_fetchall(
        "SELECT id, content, embedding FROM memories WHERE agent_id = ? ORDER BY id",
        (agent_id,),
    )
    assert rows == [(survivor_id, "collision body", b"\x00\x00\x00\x00")]
    assert all(row[0] != collider_id for row in rows)


@pytest.mark.asyncio
async def test_duplicate_content_prefers_shared_channel_survivor(clean_db):
    agent_id = "shared-survivor-agent"
    specific = await clean_db.execute(
        "INSERT INTO memories (agent_id, channel, content, timestamp) "
        "VALUES (?, 'X', 'cross-channel duplicate', '')",
        (agent_id,),
    )
    shared = await clean_db.execute(
        "INSERT INTO memories (agent_id, channel, content, timestamp) "
        "VALUES (?, '', 'cross-channel duplicate', '')",
        (agent_id,),
    )
    await clean_db.commit()

    await checks.check_duplicate_content(clean_db, agent_id, fix=True)

    rows = await clean_db.execute_fetchall(
        "SELECT id, channel FROM memories WHERE agent_id = ?", (agent_id,)
    )
    assert rows == [(shared.lastrowid, "")]
    assert rows[0][0] != specific.lastrowid


class _FailingEmbeddingClient:
    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        raise RuntimeError("backend unavailable")


class _BatchEmbeddingClient:
    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(texts)
        return [[float(len(text))] for text in texts]

    @staticmethod
    def pack_embedding(embedding):
        return bytes([int(embedding[0])])


@pytest.mark.asyncio
async def test_prefetch_null_embeddings_skips_faulted_backend(clean_db, monkeypatch):
    client = _FailingEmbeddingClient()
    monkeypatch.setattr(vector, "_embedding_client", client)
    await clean_db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('faulted', 'pending', '')"
    )
    await clean_db.commit()
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure("seed fault")
    assert health.is_faulted()

    cache = await checks.prefetch_null_embeddings(clean_db, "faulted")

    assert cache == {"memories": {}, "episodes": {}}
    assert client.calls == 0


@pytest.mark.asyncio
async def test_prefetch_null_embeddings_batches_rows(clean_db, monkeypatch):
    client = _BatchEmbeddingClient()
    monkeypatch.setattr(vector, "_embedding_client", client)
    for content in ("one", "two", "three"):
        await clean_db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES ('batched', ?, '')",
            (content,),
        )
    await clean_db.commit()

    cache = await checks.prefetch_null_embeddings(clean_db, "batched")

    assert len(client.calls) == 1
    assert set(client.calls[0]) == {"one", "two", "three"}
    assert {text for text, _ in cache["memories"].values()} == {"one", "two", "three"}
