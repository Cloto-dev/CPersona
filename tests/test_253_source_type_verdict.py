"""bug-187: an unattributed memory is not a broken one (2.5.3, Task #428).

``store`` records an omitted or null ``source`` as the anonymous ``{}``. That is
the write path's own normalisation and a supported way to say "producer
unknown", but ``check_invalid_source_type`` counted it as a type defect. The
check carries ``warn``, ``warn`` decides the single ``status`` verdict, and
``fix=true`` cannot clear it because ``normalize_source`` rightly refuses to
invent a discriminator — so the verdict was ``degraded``, permanently, for a DB
that had done nothing wrong.

Both directions are pinned here. A test that only proved anonymous rows are
clean would be satisfied by deleting the detector.
"""

import json

import pytest
import pytest_asyncio

from cpersona import checks, maintenance_handlers
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

AGENT = "bug187-agent"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _insert(conn, source: str | None, content: str):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        (AGENT, content, source, "2026-07-29T00:00:00+00:00"),
    )
    await conn.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["{}", "{ }", "null"])
async def test_anonymous_source_is_not_an_invalid_type(db, source):
    """The blessed anonymous shapes produce no finding at all."""
    await _insert(db, source, f"anonymous via {source}")

    assert await checks.check_invalid_source_type(db, AGENT, fix=False) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        '{"type":"assistant"}',
        '{"type":"migration","id":"x"}',
        '{"id":"x","name":"someone"}',
        '"claude-code"',
    ],
)
async def test_genuinely_wrong_source_type_is_still_reported(db, source):
    """The other direction: excluding {} must not blunt the detector.

    Each of these claimed a producer and got the contract wrong — an object with
    keys but no recognised type, or a bare string. Dropping these would turn the
    bug-187 fix into a deleted check.
    """
    await _insert(db, source, f"invalid via {source}")

    found = await checks.check_invalid_source_type(db, AGENT, fix=False)

    assert len(found) == 1
    assert found[0]["type"] == "invalid_source_type"
    assert found[0]["count"] == 1


@pytest.mark.asyncio
async def test_fixer_processes_exactly_the_rows_the_check_counted(db):
    """The predicate has two callers and they must not drift (bug-195 class).

    The reported ledger is where a drift shows: the check promises (bug-139)
    that ``mapped + unmapped + locked`` reconciles with ``count``. A fixer that
    selects rows the COUNT excluded inflates ``unmapped`` and drives ``locked``
    negative, so assert the reconciliation rather than the SQL text — an earlier
    version of this test compared the two predicate strings, and a mutation that
    reverted the fixer's SELECT to the old clause sailed straight through it.
    """
    for i, source in enumerate(["{}", "null", '{"type":"assistant","id":"a1"}']):
        await _insert(db, source, f"row {i}")

    found = await checks.check_invalid_source_type(db, AGENT, fix=True)

    issue = found[0]
    assert issue["count"] == 1, "the two anonymous rows are not type defects"
    assert issue["mapped"] == 1
    assert issue["unmapped"] == 0, "an anonymous row reaching the fixer is a drift"
    assert issue["locked"] == 0
    assert issue["mapped"] + issue["unmapped"] + issue["locked"] == issue["count"]


@pytest.mark.asyncio
async def test_predicate_helper_is_the_single_source_for_both_callers(db):
    """Cheap structural pin beside the behavioural one above.

    Kept because the behavioural test can only see drift that changes a number;
    this one fails the moment either call site stops using the shared helper.
    """
    import inspect

    source = inspect.getsource(checks.check_invalid_source_type)

    assert source.count("invalid_source_type_where(canonical_types)") == 2
    assert "json_extract(source, '$.type') NOT IN" not in source, (
        "the predicate was re-inlined at a call site instead of using the helper"
    )


@pytest.mark.asyncio
async def test_health_status_is_healthy_for_an_unattributed_corpus(db):
    """The verdict, end to end — this is what an operator actually reads."""
    for i in range(3):
        await _insert(db, "{}", f"unattributed memory {i}")

    health = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    types = [issue["type"] for issue in health["issues"]]
    assert "invalid_source_type" not in types
    assert health["status"] == "healthy", health["issues"]


@pytest.mark.asyncio
async def test_health_status_still_degrades_on_a_real_type_defect(db):
    """And the paired direction at the same altitude."""
    await _insert(db, '{"type":"assistant"}', "legacy lowercase type")

    health = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    assert "invalid_source_type" in [issue["type"] for issue in health["issues"]]
    assert health["status"] == "degraded"


@pytest.mark.asyncio
async def test_fix_leaves_anonymous_rows_untouched(db):
    """fix=true must not reach for the rows it stopped reporting."""
    await _insert(db, "{}", "anonymous")
    await _insert(db, '{"type":"assistant","id":"a1"}', "mappable legacy")

    await checks.check_invalid_source_type(db, AGENT, fix=True)

    stored = {
        content: source
        for content, source in await db.execute_fetchall(
            "SELECT content, source FROM memories WHERE agent_id = ?", (AGENT,)
        )
    }
    assert stored["anonymous"] == "{}"
    assert json.loads(stored["mappable legacy"])["type"] == "Agent"
