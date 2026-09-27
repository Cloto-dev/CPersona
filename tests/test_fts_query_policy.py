"""Retrieval-level screens for experimental punctuation policies."""
import uuid

import pytest

from cpersona import memory_handlers as M
from cpersona.database import get_db


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["memory", "episode"])
@pytest.mark.parametrize("needle,query", [
    ("salmon", "salmon?"),
    ("salmon", '"salmon"'),
    ("CVE-2024-3094", "CVE-2024-3094"),
    ("CVE-2024-3094", "CVE-2024-3094?"),
    ("bug-183", "(bug-183)"),
    ("v2.5.4", "v2.5.4."),
    ("user@example.com", "user@example.com,"),
    ("api/v1/store", "api/v1/store!"),
    ("/api/v1/store", "(/api/v1/store)"),
    ("C++", "C++?"),
    ("同じ日本語", "同じ日本語"),
    ("quoted", '"quoted"?'),
])
async def test_policy_retrieves_literal_with_sentence_delimiters(reader, needle, query):
    agent = "policy-" + uuid.uuid4().hex
    target = needle + " reference payload"
    noise = "find unrelated reference payload"
    db = await get_db()
    for content in [target, noise]:
        if reader == "memory":
            result = await M.do_store(agent, {"content": content, "source": {"type": "System", "id": "fixture"}})
            assert result["result"] == "stored", result
        else:
            await db.execute("INSERT INTO episodes (agent_id, summary) VALUES (?, ?)", (agent, content))
    await db.commit()
    search = M._search_memories_keyword if reader == "memory" else M._search_episodes_fts
    rows = await search(db, agent, "find " + query, limit=10)
    assert any(target in row["content"] for row in rows), rows
    assert any(noise in row["content"] for row in rows), "The nonempty FTS early return must be exercised"
    assert all(row["_bm25"] is not None for row in rows), "Do not pass through LIKE fallback"
