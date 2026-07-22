"""Three-level health status derived from severity counts (CSC #282 item 5).

The mapping lives in ``checks.health_status`` (colocated with ``exit_code`` so
gate semantics evolve together) and is surfaced on ``do_check_health``'s
response as ``status`` alongside the legacy ``healthy`` boolean.

Info is an observation, not a gate signal: an info-only DB reports
``status='healthy'`` even though ``healthy=False`` (``len(issues) == 0`` is
False). Both fields are exposed deliberately — this file pins that split.
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Hermetic DB + embeddings-off before importing any cpersona module (same pattern
# as test_v2437_checkup.py — a stray real endpoint would let embed-triggered
# checks flip severities under our feet).
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_health_status.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import checks  # noqa: E402
from cpersona import maintenance_handlers  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona._vendored_mcp_common import no_persist  # noqa: E402
from cpersona.database import get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    no_persist.resume()
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.execute("DELETE FROM profiles")
    await db.execute("DELETE FROM pending_memory_tasks")
    await db.commit()
    saved_client = vector._embedding_client
    vector._embedding_client = None
    yield
    vector._embedding_client = saved_client
    # Restore schema so a preceding test that dropped a canonical index/trigger
    # cannot cascade a "critical" residual into the next test.
    db = await get_db()
    await checks.check_schema_objects(db, "", fix=True)
    await db.commit()
    no_persist.resume()


async def _insert(db, agent_id="agent-h", content="fine content", **cols):
    defaults = {
        "source": '{"type":"User","id":"u","name":"n"}',
        "timestamp": "2026-07-01T00:00:00+00:00",
        "channel": "",
        "project_id": "",
    }
    defaults.update(cols)
    keys = ["agent_id", "content", *defaults.keys()]
    sql = f"INSERT INTO memories ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})"
    cur = await db.execute(sql, (agent_id, content, *defaults.values()))
    await db.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------
# unit: pure mapping from severity summary to status label
# --------------------------------------------------------------------------


def test_health_status_critical_dominates_warn():
    # Precedence: critical wins over warn regardless of the warn count.
    assert checks.health_status({"critical": 1, "warn": 5, "info": 0}) == "unhealthy"


def test_health_status_warn_only_is_degraded():
    assert checks.health_status({"critical": 0, "warn": 3, "info": 0}) == "degraded"


def test_health_status_info_only_is_healthy():
    # Info is an observation, not a gate signal (same stance as exit_code).
    assert checks.health_status({"critical": 0, "warn": 0, "info": 7}) == "healthy"


def test_health_status_all_zero_is_healthy():
    assert checks.health_status({"critical": 0, "warn": 0, "info": 0}) == "healthy"


# --------------------------------------------------------------------------
# integration: do_check_health surfaces status alongside legacy healthy bool
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_health_healthy_on_clean_fixture():
    db = await get_db()
    await _insert(db, content="a perfectly ordinary memory")
    await db.execute(
        "INSERT INTO profiles (agent_id, content) VALUES ('agent-h', 'profile text')"
    )
    await db.commit()

    result = await maintenance_handlers.do_check_health(agent_id="agent-h")
    assert result["severity_summary"]["critical"] == 0
    assert result["severity_summary"]["warn"] == 0
    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_do_check_health_degraded_on_warn_only():
    # Dropping a non-load-bearing (warn-severity) index fires schema_object_drift
    # at warn level without any critical companion.
    db = await get_db()
    await _insert(db, content="a perfectly ordinary memory")
    await db.execute("DROP INDEX idx_memories_agent")
    await db.commit()

    result = await maintenance_handlers.do_check_health(agent_id="agent-h")
    assert result["severity_summary"]["critical"] == 0
    assert result["severity_summary"]["warn"] >= 1
    assert result["status"] == "degraded"
    assert result["healthy"] is False


@pytest.mark.asyncio
async def test_do_check_health_unhealthy_on_critical():
    # Dropping the load-bearing dedup UNIQUE index fires a critical
    # schema_object_drift (see test_v2437_checkup for the underlying pin).
    db = await get_db()
    await _insert(db, content="a perfectly ordinary memory")
    await db.execute("DROP INDEX idx_memories_dedup_content")
    await db.commit()

    result = await maintenance_handlers.do_check_health(agent_id="agent-h")
    assert result["severity_summary"]["critical"] >= 1
    assert result["status"] == "unhealthy"
    assert result["healthy"] is False


@pytest.mark.asyncio
async def test_do_check_health_info_only_is_healthy_but_not_healthy_bool():
    # Under CPERSONA_EMBEDDING_MODE=none, null_embedding is info (NULL is the
    # expected steady state — see test_null_embedding_severity_ladder). So the
    # response should carry status='healthy' (info doesn't gate) while the
    # legacy healthy bool is False (issues list is non-empty). This pins the
    # deliberate split between the two fields.
    db = await get_db()
    await _insert(db, content="row without vector")
    await db.commit()

    result = await maintenance_handlers.do_check_health(agent_id="agent-h")
    non_info = [i for i in result["issues"] if i["severity"] != "info"]
    assert non_info == []  # only info-severity findings
    info_count = result["severity_summary"]["info"]
    assert info_count >= 1
    assert result["status"] == "healthy"
    assert result["healthy"] is False  # len(issues) != 0
