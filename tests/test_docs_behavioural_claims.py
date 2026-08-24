"""Locks for behavioural claims the published documentation makes in prose.

Reading the pages against the implementation turned up six defects at once,
four of them already published for several releases, every one of them green
under all three guards this repo runs over its documentation. Those guards read
numbers (check-docs-facts), link and fragment resolution (check-doc-anchors),
and translation freshness (check-i18n-drift). None of them can read a sentence
about behaviour, which is where all six lived.

The answer is not a fourth guard that tries to read prose. It is to route each
claim to a mechanism that can already settle it: a behavioural test when the
claim describes observable behaviour, a structural gate when it asserts an
absence over every path (tests/test_structural_gates.py, Gate 14), and
check-docs-facts when it is a constant or a schema-derived count.

This file is the first of those lanes. Each test names the page and quotes the
claim, so a failure says which sentence to go and read — the sentence is the
contract, and the test is downstream of it. When a claim is deliberately
changed, the page and the test move together.
"""

import pytest
import pytest_asyncio

from cpersona import admin_handlers, memory_handlers
from cpersona.database import get_db

AGENT_A = "docs-claim-a"
AGENT_B = "docs-claim-b"


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _seed(db, agent_id, contents):
    for index, content in enumerate(contents):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, created_at) "
            "VALUES (?, ?, '', ?)",
            (agent_id, content, f"2026-08-{20 - index:02d} 00:00:00"),
        )
    await db.commit()


# --------------------------------------------------------------------------------------
# docs/architecture.md, "Isolation axes":
#
#   "Omitting the axis is the opposite case, and it is deliberate: a cross-agent
#    scan. The listing tools take it that way — a `list_memories` call with no
#    `agent_id` returns rows belonging to every agent in the database."
#
# The page said the opposite until Phase 6: it described omission as failing
# closed. It does not — do_list_memories maps a falsy agent_id to `or None`,
# which drops the predicate. The asymmetry is intentional and is what the
# maintenance and dashboard paths rely on, so it needs a lock in BOTH
# directions: a future reader who mistakes the `or None` for a fail-open bug and
# "fixes" it would silently break the documented behaviour and every caller of
# it, while a reader who widens the bound axis would break hard isolation.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_without_an_agent_id_scans_every_agent(clean_db):
    """The documented cross-agent scan: no agent_id means no filter."""
    await _seed(clean_db, AGENT_A, ["from a"])
    await _seed(clean_db, AGENT_B, ["from b"])

    result = await admin_handlers.do_list_memories("", 10)

    returned = {m["content"] for m in result["memories"]}
    assert returned == {"from a", "from b"}, (
        "docs/architecture.md ('Isolation axes') states that list_memories with no "
        "agent_id returns rows belonging to every agent. It returned "
        f"{sorted(returned)}. If the narrowing is intended, the page is now wrong — "
        "fix the sentence and this test together."
    )


@pytest.mark.asyncio
async def test_listing_with_an_agent_id_never_crosses_agents(clean_db):
    """The other half of the same table: a bound agent_id is hard isolation, no union.

    Without this, the test above passes just as well against a build that
    ignores agent_id entirely — a cross-agent scan and a broken filter look
    identical from one direction.
    """
    await _seed(clean_db, AGENT_A, ["from a"])
    await _seed(clean_db, AGENT_B, ["from b"])

    result = await admin_handlers.do_list_memories(AGENT_A, 10)

    returned = {m["content"] for m in result["memories"]}
    assert returned == {"from a"}, (
        "docs/architecture.md ('Isolation axes') states that agents never share rows "
        f"when agent_id is bound. Listing for {AGENT_A} returned {sorted(returned)}."
    )


# --------------------------------------------------------------------------------------
# skills/cpersona-memory/SKILL.md, "Session end" and the mandatory-trigger list:
#
#   "Pass the REAL history — it sets the episode's start/end times from the turns'
#    timestamps, but never reaches the embedding, which comes from `summary` alone."
#
# The page said the opposite until now — that the history "drives timestamps and the
# episode embedding". That version had a running cost rather than a latent one: the
# skill ships inside the wheel and instructs every calling agent, so each archive_episode
# carried a full transcript bought for a retrieval benefit that does not exist. The
# corrected sentence is worth a lock precisely because the wrong one is the intuitive
# guess, and the next person to edit this paragraph will guess it again.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_embedding_ignores_the_conversation_history(clean_db, fake_embedding_client):
    """Only the summary reaches the encoder; history sets the time span and nothing else."""
    summary = "the session settled on the cheaper of the two designs"
    turns = [
        {"role": "user", "content": "which design do we ship", "timestamp": "2026-08-01T00:00:00"},
        {"role": "assistant", "content": "the cheaper one", "timestamp": "2026-08-02T00:00:00"},
    ]

    await memory_handlers.do_archive_episode(AGENT_A, turns, summary=summary)
    await memory_handlers.do_archive_episode(AGENT_B, [], summary=summary)

    rows = await clean_db.execute_fetchall(
        "SELECT agent_id, embedding, start_time, end_time FROM episodes "
        "WHERE agent_id IN (?, ?) ORDER BY agent_id",
        (AGENT_A, AGENT_B),
    )
    assert len(rows) == 2, f"expected one episode per agent, got {len(rows)}"
    with_history, without_history = rows

    assert with_history[1] == without_history[1], (
        "SKILL.md states that the episode embedding comes from `summary` alone. Archiving "
        "the same summary with and without conversation history produced different "
        "embeddings, so history now reaches the encoder — say so on the page, because the "
        "current wording tells agents the transcript is not worth sending."
    )
    assert with_history[1] is not None, (
        "both embeddings are NULL — the encoder never ran, so this test proves nothing. "
        "Check that the fake embedding client is installed."
    )
    assert (with_history[2], with_history[3]) == ("2026-08-01T00:00:00", "2026-08-02T00:00:00"), (
        "history is documented as the source of the episode's start and end times"
    )
    assert (without_history[2], without_history[3]) == (None, None), (
        "an empty history is documented as leaving the time span unset"
    )


# --------------------------------------------------------------------------------------
# skills/cpersona-memory/SKILL.md, tool reference:
#
#   "`channel` on `store` / `recall`, and `source_id` on `recall`"
#
# The table said `source_id` was an argument of both. It is not on `store`, so an agent
# following the row called store(..., source_id=…) and the argument was dropped on the
# floor — a silent no-op, which is the worst shape for a wrong instruction. Per-user
# attribution on a write travels inside `message.source.id`.
#
# Argument names in a shipped instruction file are checkable against the schema the
# server actually advertises, so they should never be settled by reading alone.
# --------------------------------------------------------------------------------------


def test_source_id_is_a_recall_argument_and_not_a_store_argument():
    """The axis table in the shipped skill must not name arguments the tool lacks."""
    from cpersona import server

    tools = {t.name: t for t in server.registry._tools}
    store_args = set(tools["store"].inputSchema.get("properties", {}))
    recall_args = set(tools["recall"].inputSchema.get("properties", {}))

    assert "source_id" not in store_args, (
        "`store` now advertises source_id. The shipped skill tells agents that a write "
        "carries its producer in message.source.id instead — update the tool-reference "
        "table in skills/cpersona-memory/SKILL.md before this becomes the wrong advice."
    )
    assert "source_id" in recall_args, (
        "`recall` no longer advertises source_id, which the shipped skill names as the "
        "per-user read filter."
    )
    assert "channel" in store_args and "channel" in recall_args, (
        "the same table names `channel` on both tools"
    )
