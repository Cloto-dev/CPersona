"""Tests for v2.4.20 source_id filter on memory search paths.

Validates that recall can scope to a single user (e.g. one Discord author) via
``json_extract(source, '$.id')`` prefix match. Addresses upstream
ClotoCore bug-344 (cross-user memory contamination in Discord multi-user
sessions).

Tests exercise ``_search_memories_keyword`` directly (the SQL layer where the
filter lives) rather than ``do_recall`` end-to-end, because the
``_apply_quality_gate`` policy in ``do_recall`` drops unscored keyword results
when the DB has fewer than 100 memories — making a small-pool E2E test
indistinguishable from a real filter bug. The keyword path is the relevant
unit of behaviour here.

End-to-end ``do_recall(... source_id=...)`` is covered by ``do_recall``'s
existing thread-through assertion (no schema change, only an optional arg) and
by the public API signature being validated through the BC test below.

The MessageSource enum on ClotoCore serialises with ``#[serde(tag = "type")]``
so memories arrive with ``source = {"type": "User", "id": "discord:X", "name": ...}``.
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_source_id.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import memory_handlers  # noqa: E402
from database import get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialise a fresh DB for each test."""
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


def _user_msg(content: str, user_id: str, name: str = "u", msg_id: str = "") -> dict:
    """Build a message with internally-tagged User source (matches ClotoCore)."""
    return {
        "id": msg_id,
        "content": content,
        "source": {"type": "User", "id": user_id, "name": name},
    }


def _agent_msg(content: str, msg_id: str = "") -> dict:
    return {
        "id": msg_id,
        "content": content,
        "source": {"type": "Agent", "id": "self"},
    }


async def _seed(*msgs: dict, agent_id: str = "agent-a") -> None:
    for msg in msgs:
        await memory_handlers.do_store(agent_id, msg)


async def _search(query: str, source_id: str = "", limit: int = 10, agent_id: str = "agent-a") -> list[str]:
    """Helper — call _search_memories_keyword directly and return contents."""
    db = await get_db()
    rows = await memory_handlers._search_memories_keyword(
        db, agent_id, query, limit, source_id=source_id
    )
    return [r["content"] for r in rows]


# ============================================================
# Backward compatibility — empty source_id = no filter
# ============================================================


@pytest.mark.asyncio
async def test_search_empty_source_id_returns_all_users():
    """source_id='' (default) keeps the existing all-users behaviour."""
    await _seed(
        _user_msg("alice talks", "discord:111", "Alice"),
        _user_msg("bob talks", "discord:222", "Bob"),
    )
    contents = sorted(await _search("talks"))
    assert contents == ["alice talks", "bob talks"]


@pytest.mark.asyncio
async def test_search_omitting_source_id_arg_is_bc():
    """Calling _search_memories_keyword without source_id matches v2.4.19 behaviour."""
    await _seed(
        _user_msg("hello A", "discord:111"),
        _user_msg("hello B", "discord:222"),
    )
    db = await get_db()
    # No source_id keyword at all
    rows = await memory_handlers._search_memories_keyword(db, "agent-a", "hello", 10)
    assert len(rows) == 2


# ============================================================
# Per-user filtering — exact prefix
# ============================================================


@pytest.mark.asyncio
async def test_search_source_id_filters_to_exact_user():
    """source_id='discord:111' returns only Alice's memories, not Bob's."""
    await _seed(
        _user_msg("alice memory", "discord:111", "Alice"),
        _user_msg("bob memory", "discord:222", "Bob"),
    )
    contents = await _search("memory", source_id="discord:111")
    assert contents == ["alice memory"]


@pytest.mark.asyncio
async def test_search_source_id_excludes_other_users():
    """Filter is symmetric — Bob's source_id returns only Bob."""
    await _seed(
        _user_msg("alice memory", "discord:111", "Alice"),
        _user_msg("bob memory", "discord:222", "Bob"),
    )
    contents = await _search("memory", source_id="discord:222")
    assert contents == ["bob memory"]


# ============================================================
# Prefix semantics — partial prefix matches multiple ids
# ============================================================


@pytest.mark.asyncio
async def test_search_source_id_prefix_matches_all_under_scheme():
    """source_id='discord:' returns all Discord-sourced memories."""
    await _seed(
        _user_msg("disc A", "discord:111"),
        _user_msg("disc B", "discord:222"),
        _user_msg("slack X", "slack:333"),
    )
    contents = sorted(await _search("", source_id="discord:"))
    assert contents == ["disc A", "disc B"]


@pytest.mark.asyncio
async def test_search_source_id_no_match_returns_empty():
    """A source_id that no memory matches returns an empty result."""
    await _seed(_user_msg("only alice", "discord:111"))
    contents = await _search("alice", source_id="discord:nonexistent")
    assert contents == []


# ============================================================
# Agent / mixed sources
# ============================================================


@pytest.mark.asyncio
async def test_search_source_id_skips_agent_messages_under_user_prefix():
    """Agent-sourced memories don't accidentally match a user prefix."""
    await _seed(
        _user_msg("from alice", "discord:111"),
        _agent_msg("from agent"),
    )
    contents = sorted(await _search("", source_id="discord:"))
    assert contents == ["from alice"]


# ============================================================
# LIKE-metachar safety
# ============================================================


@pytest.mark.asyncio
async def test_search_source_id_escapes_underscore_wildcard():
    """An underscore in source_id is treated as a literal, not a wildcard."""
    await _seed(
        _user_msg("literal underscore", "discord:1_2"),
        _user_msg("anything else", "discord:1X2"),
    )
    contents = sorted(await _search("", source_id="discord:1_"))
    # discord:1X2 must NOT match because _ is escaped
    assert contents == ["literal underscore"]


@pytest.mark.asyncio
async def test_search_source_id_escapes_percent_wildcard():
    """A percent sign in source_id is treated as a literal."""
    await _seed(
        _user_msg("with percent", "weird:%user"),
        _user_msg("without", "weird:other"),
    )
    contents = await _search("", source_id="weird:%")
    assert contents == ["with percent"]


# ============================================================
# do_recall integration — verify the optional arg threads through
# ============================================================


@pytest.mark.asyncio
async def test_do_recall_accepts_source_id_kwarg():
    """do_recall accepts source_id as an optional kwarg without raising.

    The full filter behaviour is covered above at the _search layer; here we
    only verify the public-API signature exposes source_id and forwards it
    without crashing. (The quality gate suppresses unscored small-pool
    results, so we can't assert filtered content at the do_recall layer
    without seeding 100+ memories — out of scope for a unit test.)
    """
    await _seed(_user_msg("alice", "discord:111"))
    # Both forms must work without TypeError
    result1 = await memory_handlers.do_recall("agent-a", "alice", limit=10)
    result2 = await memory_handlers.do_recall(
        "agent-a", "alice", limit=10, source_id=""
    )
    result3 = await memory_handlers.do_recall(
        "agent-a", "alice", limit=10, source_id="discord:111"
    )
    assert "messages" in result1
    assert "messages" in result2
    assert "messages" in result3


@pytest.mark.asyncio
async def test_do_recall_with_context_accepts_source_id_kwarg():
    """do_recall_with_context accepts source_id and forwards to do_recall."""
    await _seed(_user_msg("alice", "discord:111"))
    result = await memory_handlers.do_recall_with_context(
        "agent-a",
        "alice",
        external_context=[],
        limit=10,
        source_id="discord:111",
    )
    assert "messages" in result
