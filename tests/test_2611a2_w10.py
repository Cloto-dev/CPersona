"""Regression tests for the store field bounds and the empty-query recency order.

  bug-570  store capped content and the source / metadata JSON, but stored the
           caller's id, channel, project_id and timestamp at any length (a 4 MB id
           grew the database by about six times that, the id being a key of the
           dedup indexes too). archive_episode took channel and project_id the
           same way.
  bug-575  the empty-query "recent memories" listing and the LIKE fallback ordered
           by created_at alone. created_at has one-second resolution, so a batch
           stored within one second tied and came back oldest-stored first: the
           limit kept the earliest rows of the newest second and cut the newest.

Each test states the correct behaviour and was seen to fail on the unfixed code for
the reason named above.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_w10.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
from mcp import types  # noqa: E402

from cpersona import memory_handlers, server, session  # noqa: E402
from cpersona.database import get_db  # noqa: E402

# The published bounds. Written out rather than imported so a missing bound reads
# as a failed assertion, not as an import error.
KEY_CAP = 256
TIMESTAMP_CAP = 64


async def _call(name: str, arguments: dict) -> types.CallToolResult:
    handler = server.registry.server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return (await handler(request)).root


def _schema(tool_name: str) -> dict:
    for tool in server.registry._tools:
        if tool.name == tool_name:
            return tool.inputSchema
    raise AssertionError(f"no tool named {tool_name}")


async def _count(agent: str, table: str = "memories") -> int:
    db = await get_db()
    return (await db.execute_fetchall(f"SELECT COUNT(*) FROM {table} WHERE agent_id = ?", (agent,)))[0][0]


# ===========================================================================
# bug-570 -- the store's identifier fields and timestamp are bounded.
# ===========================================================================


def test_store_schema_declares_the_field_bounds():
    props = _schema("store")["properties"]
    assert props["id"].get("maxLength") == KEY_CAP, props["id"]
    assert props["channel"].get("maxLength") == KEY_CAP, props["channel"]
    assert props["project_id"].get("maxLength") == KEY_CAP, props["project_id"]
    assert props["timestamp"].get("maxLength") == TIMESTAMP_CAP, props["timestamp"]
    legacy = props["message"]["properties"]
    assert legacy["id"].get("maxLength") == KEY_CAP, legacy["id"]
    assert legacy["timestamp"].get("maxLength") == TIMESTAMP_CAP, legacy["timestamp"]


def test_archive_episode_schema_declares_the_field_bounds():
    props = _schema("archive_episode")["properties"]
    assert props["channel"].get("maxLength") == KEY_CAP, props["channel"]
    assert props["project_id"].get("maxLength") == KEY_CAP, props["project_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "i" * (KEY_CAP + 1)),
        ("channel", "c" * (KEY_CAP + 1)),
        ("project_id", "p" * (KEY_CAP + 1)),
        ("timestamp", "2026-10-12T00:00:00+00:00" + " " * TIMESTAMP_CAP),
    ],
    ids=["id", "channel", "project_id", "timestamp"],
)
async def test_an_oversized_field_is_refused_at_the_tool_boundary(field, value):
    session.reset_pauses_for_tests()
    agent = f"w10-mcp-{field}"
    result = await _call("store", {"agent_id": agent, "content": f"deploy notes {field}", field: value})
    # Before the fix the call stored the row and answered result 'stored'.
    assert result.isError is True, result.content[0].text[:200]
    assert result.content[0].text.startswith("Input validation error"), result.content[0].text[:200]
    assert await _count(agent) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["id", "channel", "project_id"])
async def test_do_store_refuses_a_key_field_past_the_bound(field):
    """The handler enforces the bound too: not every caller reaches it through the schema."""
    session.reset_pauses_for_tests()
    agent = f"w10-handler-{field}"
    message = {"content": f"over the bound {field}"}
    kwargs = {}
    long_value = "x" * (KEY_CAP + 1)
    if field == "id":
        message["id"] = long_value
    else:
        kwargs[field] = long_value
    res = await memory_handlers.do_store(agent, message, **kwargs)
    assert res["ok"] is False and res["result"] == "rejected", res
    assert field in res["reason"] and str(KEY_CAP) in res["reason"], res
    assert await _count(agent) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["id", "channel", "project_id"])
async def test_do_store_accepts_a_key_field_at_the_bound(field):
    session.reset_pauses_for_tests()
    agent = f"w10-handler-at-{field}"
    message = {"content": f"at the bound {field}"}
    kwargs = {}
    value = "x" * KEY_CAP
    if field == "id":
        message["id"] = value
    else:
        kwargs[field] = value
    res = await memory_handlers.do_store(agent, message, **kwargs)
    assert res["result"] == "stored", res
    db = await get_db()
    column = "msg_id" if field == "id" else field
    stored = await db.execute_fetchall(f"SELECT {column} FROM memories WHERE id = ?", (res["id"],))
    assert stored[0][0] == value


@pytest.mark.asyncio
async def test_do_store_refuses_a_timestamp_past_the_bound():
    session.reset_pauses_for_tests()
    agent = "w10-ts-long"
    res = await memory_handlers.do_store(agent, {"content": "long stamp", "timestamp": "t" * (TIMESTAMP_CAP + 1)})
    assert res["ok"] is False and res["result"] == "rejected", res
    assert "timestamp" in res["reason"] and str(TIMESTAMP_CAP) in res["reason"], res
    assert await _count(agent) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp",
    [
        "2026-10-12T10:00:00+09:00",
        "2026-10-12T01:00:00Z",
        "2026-10-12T01:00:00",
        "20261012T010000Z",
        "",  # an undated row, as an import leaves it; recall treats it as unknown age
        # Unparseable but short: the bound is on length only. Such a row is the
        # invalid_timestamp check's to report and repair, not the store's to refuse.
        "not a timestamp",
        "9999-12-31T23:59:59-05:00",
        "t" * TIMESTAMP_CAP,  # at the bound
    ],
    ids=["offset", "zulu", "naive", "basic", "empty", "unparseable", "out-of-range", "at-bound"],
)
async def test_do_store_keeps_accepting_timestamps_within_the_bound(stamp):
    session.reset_pauses_for_tests()
    agent = f"w10-ts-ok-{stamp or 'empty'}"
    res = await memory_handlers.do_store(agent, {"content": f"readable stamp {stamp!r}", "timestamp": stamp})
    assert res["result"] == "stored", res
    db = await get_db()
    stored = await db.execute_fetchall("SELECT timestamp FROM memories WHERE id = ?", (res["id"],))
    assert stored[0][0] == stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["channel", "project_id"])
async def test_archive_episode_refuses_a_key_field_past_the_bound(field):
    session.reset_pauses_for_tests()
    agent = f"w10-ep-{field}"
    res = await memory_handlers.do_archive_episode(
        agent, [], summary=f"an episode with a long {field}", **{field: "e" * (KEY_CAP + 1)}
    )
    assert res["ok"] is False and res["episode_id"] is None, res
    assert field in res["error"] and str(KEY_CAP) in res["error"], res
    assert await _count(agent, "episodes") == 0

    at_bound = await memory_handlers.do_archive_episode(
        agent, [], summary=f"an episode with a {field} at the bound", **{field: "e" * KEY_CAP}
    )
    assert at_bound["ok"] is True and at_bound["episode_id"], at_bound


# ===========================================================================
# bug-575 -- the empty-query listing breaks created_at ties newest-stored first.
# ===========================================================================


async def _store_a_batch_in_one_second(agent: str, n: int = 12) -> list[int]:
    ids = []
    for i in range(n):
        res = await memory_handlers.do_store(
            agent,
            {"content": f"note {i} written at hour {i}", "timestamp": f"2026-10-12T{i:02d}:00:00+00:00"},
        )
        assert res["result"] == "stored", res
        ids.append(res["id"])
    # The tie is forced rather than left to the clock, so the test does not depend
    # on how fast twelve stores run.
    db = await get_db()
    await db.execute("UPDATE memories SET created_at = '2026-10-12 12:00:00' WHERE agent_id = ?", (agent,))
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_empty_query_listing_returns_the_newest_stored_rows_of_a_tied_second():
    session.reset_pauses_for_tests()
    agent = "w10-recent"
    ids = await _store_a_batch_in_one_second(agent)
    db = await get_db()
    rows = await memory_handlers._search_memories_keyword(db, agent, "", 3)
    # Before the fix: the three OLDEST rows of the second, oldest first.
    assert [r["id"] for r in rows] == sorted(ids, reverse=True)[:3], [r["content"] for r in rows]


@pytest.mark.asyncio
async def test_like_fallback_orders_a_tied_second_newest_stored_first(monkeypatch):
    session.reset_pauses_for_tests()
    agent = "w10-like"
    ids = await _store_a_batch_in_one_second(agent)
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)
    db = await get_db()
    rows = await memory_handlers._search_memories_keyword(db, agent, "written at hour", 3)
    assert [r["id"] for r in rows] == sorted(ids, reverse=True)[:3], [r["content"] for r in rows]


@pytest.mark.asyncio
async def test_empty_query_recall_returns_the_newest_stored_rows_of_a_tied_second():
    session.reset_pauses_for_tests()
    agent = "w10-recall"
    ids = await _store_a_batch_in_one_second(agent)
    out = await memory_handlers.do_recall(agent, "", limit=3)
    got = {m["ref"] for m in out["messages"]}
    assert got == {f"mem:{i}" for i in sorted(ids, reverse=True)[:3]}, [m["content"] for m in out["messages"]]
