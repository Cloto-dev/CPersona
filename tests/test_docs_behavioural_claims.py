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

from cpersona import admin_handlers
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
