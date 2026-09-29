"""bug-385: a listing the row cap cut says so.

list_memories and list_episodes clamp `limit` to a row cap. A caller applying
the ordinary "count < limit means the end of the data" rule stopped at the cap
and could not tell a capped answer from a complete one: the response carried
`count` and the rows, and nothing else. The response now carries `budget_rows`
(the cap) when the caller asked for more than the cap AND rows past it exist --
the convention `budget_chars` already follows for the character budget.
"""

import pytest
import pytest_asyncio

from cpersona import admin_handlers, server
from cpersona.database import get_db

AGENT = "rowcap.agent"


def _created_at(i: int) -> str:
    return f"2026-03-01 {i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}"


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in ("memories", "episodes"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


async def _memories(db, n: int) -> None:
    await db.executemany(
        "INSERT INTO memories (agent_id, content, source, timestamp, created_at)"
        " VALUES (?, ?, '{}', 't', ?)",
        [(AGENT, f"memory {i}", _created_at(i)) for i in range(n)],
    )
    await db.commit()


async def _episodes(db, n: int) -> None:
    await db.executemany(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) VALUES (?, ?, '', ?)",
        [(AGENT, f"episode {i}", _created_at(i)) for i in range(n)],
    )
    await db.commit()


@pytest.mark.asyncio
async def test_a_listing_the_cap_cut_carries_the_marker(db):
    # The registry's reproduction: 510 memories, limit 1000.
    await _memories(db, 510)
    result = await admin_handlers.do_list_memories(AGENT, 1000)
    assert result["count"] == len(result["memories"]) == admin_handlers.LIST_MEMORIES_MAX_ROWS
    # Measured before the fix: keys were exactly count and memories.
    assert result["budget_rows"] == admin_handlers.LIST_MEMORIES_MAX_ROWS
    # The probe row is read, never returned: these are the newest 500.
    assert [m["content"] for m in result["memories"]] == [
        f"memory {i}" for i in range(509, 9, -1)
    ]


@pytest.mark.asyncio
async def test_a_listing_that_reached_the_end_at_the_cap_carries_no_marker(db):
    await _memories(db, admin_handlers.LIST_MEMORIES_MAX_ROWS)
    result = await admin_handlers.do_list_memories(AGENT, 1000)
    assert result["count"] == admin_handlers.LIST_MEMORIES_MAX_ROWS
    assert "budget_rows" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [500, 100, 0])
async def test_the_callers_own_limit_is_not_reported_as_the_cap(db, limit):
    await _memories(db, 510)
    result = await admin_handlers.do_list_memories(AGENT, limit)
    assert result["count"] == limit
    assert "budget_rows" not in result


@pytest.mark.asyncio
async def test_the_episode_listing_carries_the_marker_too(db, monkeypatch):
    monkeypatch.setattr(admin_handlers, "LIST_EPISODES_MAX_ROWS", 3)
    await _episodes(db, 5)
    capped = await admin_handlers.do_list_episodes(AGENT, 10)
    assert capped["count"] == 3
    assert capped["budget_rows"] == 3
    assert [e["summary"] for e in capped["episodes"]] == ["episode 4", "episode 3", "episode 2"]

    complete = await admin_handlers.do_list_episodes(AGENT, 3)
    assert complete["count"] == 3 and "budget_rows" not in complete


@pytest.mark.asyncio
async def test_the_marker_reaches_the_tool_boundary(db):
    await _memories(db, 510)
    result = await server.do_list_memories_boundary(AGENT, 1000)
    assert result["budget_rows"] == admin_handlers.LIST_MEMORIES_MAX_ROWS


@pytest.mark.parametrize("tool_name", ["list_memories", "list_episodes"])
def test_the_description_names_the_marker(tool_name):
    tool = next(t for t in server.registry._tools if t.name == tool_name)
    assert "budget_rows" in tool.description
    assert "carries no marker" not in tool.description
