"""2.5.2b1 write-path and context-filter findings (CSC #361 items C12 / C13 / (6)).

Three defects that share one shape: a write path (or a filter) that was less
careful than its sibling, with nothing in the response admitting it.

- C12: ``content`` has been capped since 2.1; its JSON sidecars (``source`` /
  ``metadata``) and the episode's text fields never were, so one call could
  park an unbounded blob per row and ship it to the embedding backend.
- C13: every ``external_context`` entry filters recall, but only user /
  assistant entries are merged into the response — so a system entry could
  suppress a memory invisibly.
- (6): the ``/index`` push on the WRITE hot path inherited the embed client's
  30s default while every sibling remote call named a short deadline.
"""

import json

import pytest
import pytest_asyncio

from cpersona import memory_handlers, vector
from cpersona._vendored_mcp_common import no_persist
from cpersona.config import MAX_CONTENT_LENGTH, MAX_METADATA_LENGTH, REMOTE_INDEX_TIMEOUT_SECS
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks')"
    )
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# C12: the JSON sidecars are bounded, and the bound refuses rather than truncates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_metadata_is_rejected_and_writes_nothing(clean_db):
    res = await memory_handlers.do_store(
        "c12", {"content": "ordinary", "metadata": {"blob": "x" * (MAX_METADATA_LENGTH + 100)}}
    )
    assert res["result"] == "rejected" and res["ok"] is False, res
    assert "metadata too large" in res["reason"], res
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'c12'")
    assert rows[0][0] == 0, "a rejected write must leave no row"


@pytest.mark.asyncio
async def test_oversized_source_is_rejected(clean_db):
    res = await memory_handlers.do_store(
        "c12",
        {
            "content": "ordinary",
            "source": {"type": "Agent", "id": "a", "name": "n" * (MAX_METADATA_LENGTH + 100)},
        },
    )
    assert res["result"] == "rejected", res
    assert "source too large" in res["reason"], res


@pytest.mark.asyncio
async def test_metadata_at_the_cap_still_stores(clean_db):
    """The bound refuses only what exceeds it — an honest producer never meets
    the cap, and the boundary itself must not become an off-by-one refusal."""
    payload = {"blob": "x" * 100}
    res = await memory_handlers.do_store("c12", {"content": "fits", "metadata": payload})
    assert res["result"] == "stored", res
    row = await clean_db.execute_fetchall("SELECT metadata FROM memories WHERE id = ?", (res["id"],))
    assert json.loads(row[0][0]) == payload


@pytest.mark.asyncio
async def test_episode_summary_is_capped_and_reported(clean_db):
    """Episode text is prose, so it follows the memory rule: truncate to the
    content cap and say so — the same `truncated` signal do_store gives."""
    long_summary = "S" * (MAX_CONTENT_LENGTH + 500)
    res = await memory_handlers.do_archive_episode("c12", [], summary=long_summary)
    assert res["ok"] is True and res.get("truncated") is True, res
    row = await clean_db.execute_fetchall(
        "SELECT summary FROM episodes WHERE id = ?", (res["episode_id"],)
    )
    assert len(row[0][0]) == MAX_CONTENT_LENGTH


@pytest.mark.asyncio
async def test_episode_remote_index_receives_the_stored_text(clean_db, monkeypatch):
    """The vector and the row must describe the same text: indexing the
    caller's original after storing a capped copy would make recall score a
    string the database does not contain."""
    pushed: list[dict] = []

    async def fake_upsert(agent_id, items):
        pushed.extend(items)

    monkeypatch.setattr(vector, "remote_index_upsert", fake_upsert)
    res = await memory_handlers.do_archive_episode(
        "c12", [], summary="T" * (MAX_CONTENT_LENGTH + 42)
    )
    row = await clean_db.execute_fetchall(
        "SELECT summary FROM episodes WHERE id = ?", (res["episode_id"],)
    )
    assert pushed and pushed[0]["text"] == row[0][0]


# ---------------------------------------------------------------------------
# C13: a context entry that filters recall without appearing in the response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_context_entry_that_suppresses_a_memory_is_disclosed(clean_db):
    """The suppression itself is correct — the caller already holds that text.
    What was wrong is that nothing said so, and the entry is not in `messages`
    either, so the memory just vanished."""
    await memory_handlers.do_store(
        "c13", {"content": "API key rotation policy is 90 days", "source": {"type": "Agent"}}
    )
    ctx = [
        {
            "role": "system",
            "content": "API key rotation policy is 90 days",
            "timestamp": "2026-07-01T09:59:00+00:00",
        }
    ]
    res = await memory_handlers.do_recall_with_context("c13", query="", external_context=ctx)

    assert res["messages"] == [], "baseline: the entry filters the memory out"
    disclosed = res.get("context_filter_only")
    assert disclosed is not None, "the suppression must not be silent"
    assert disclosed["roles"] == ["system"]


@pytest.mark.asyncio
async def test_conversational_context_alone_adds_no_disclosure(clean_db):
    """user / assistant entries ARE merged into messages, so their filtering is
    self-evident; the disclosure would be pure payload."""
    ctx = [
        {"role": "user", "content": "hello", "timestamp": "2026-07-01T09:00:00+00:00"},
        {"role": "assistant", "content": "hi", "timestamp": "2026-07-01T09:00:01+00:00"},
    ]
    res = await memory_handlers.do_recall_with_context("c13", query="", external_context=ctx)
    assert "context_filter_only" not in res, res
    assert len(res["messages"]) == 2


@pytest.mark.asyncio
async def test_entry_without_a_role_is_disclosed_as_unset(clean_db):
    ctx = [{"content": "orphan note", "timestamp": "2026-07-01T09:00:00+00:00"}]
    res = await memory_handlers.do_recall_with_context("c13", query="", external_context=ctx)
    assert res["context_filter_only"]["roles"] == ["(unset)"], res


# ---------------------------------------------------------------------------
# (6): the write hot path states its own deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_index_push_states_its_timeout(clean_db, monkeypatch):
    """Without an explicit timeout the POST inherits the embed client's 30s
    default, so a hung endpoint blocks every store for 30s — the read path has
    been protected since bug-033 and the write path was not."""
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

    class _Client:
        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _Resp()

    class _EmbedClient:
        _http_url = "http://embed.invalid/embed"
        _client = _Client()

        async def embed(self, texts):
            return [None]

        @staticmethod
        def pack_embedding(vec):
            return b""

    monkeypatch.setattr(memory_handlers, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _EmbedClient())

    res = await memory_handlers.do_store("c6", {"content": "indexed row"})
    assert res["result"] == "stored", res
    assert captured["url"].endswith("/index")
    assert captured["kwargs"].get("timeout") == REMOTE_INDEX_TIMEOUT_SECS, captured["kwargs"]
