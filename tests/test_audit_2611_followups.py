"""Regression tests for the 2.6.11 audit's follow-up findings.

  bug-794  A per-user ``source_id`` filter read ``json_extract(source, '$.id')``
           with no ``json_valid`` guard, so one row in scope whose ``source`` is
           not JSON made every source-scoped scan raise "malformed JSON" and the
           whole call fail. Such rows exist: the JSON repair leaves locked ones
           alone. bug-743 guarded the same read in the index builder.
"""

import json
import os
import sqlite3
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_audit_2611_followups.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import memory_handlers, session  # noqa: E402
from cpersona.database import get_db  # noqa: E402
from cpersona.isolation import source_id_where  # noqa: E402

STAMP = "2026-01-01T00:00:00+00:00"
AGENT = "followups.agent"
_ALICE = "discord:12345"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


async def _insert(db, content, source, *, locked=0) -> int:
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, created_at, locked)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (AGENT, content, source, STAMP, "2026-01-01 00:00:00", locked),
    )
    await db.commit()
    return cur.lastrowid


async def _with_one_malformed_source(db) -> int:
    """Alice's row, another user's row, and one locked row whose source is not JSON."""
    await _insert(db, "alice home address is elm street", json.dumps({"type": "User", "id": _ALICE}))
    await _insert(db, "bob home address is oak avenue", json.dumps({"type": "User", "id": "discord:999"}))
    return await _insert(db, "legacy home address note", "not json at all", locked=1)


# ==========================================================================
# bug-794: a malformed source matches no source_id instead of failing the scan
# ==========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", [_ALICE, "discord:"], ids=["one-id", "namespace"])
async def test_bug794_the_shared_predicate_skips_a_malformed_source(db, source_id):
    """Every source-scoped arm (keyword, recency, vector window, far seats, coarse
    search) reads this one predicate, so it must hold for both of its forms."""
    bad = await _with_one_malformed_source(db)
    src = source_id_where(source_id)
    try:
        cur = await db.execute(f"SELECT id, content FROM memories WHERE agent_id = ? AND {src.clause}",
                               (AGENT, *src.params))
        rows = await cur.fetchall()
    except sqlite3.OperationalError as exc:  # the defect: "malformed JSON"
        pytest.fail(f"a source-scoped scan raised on one malformed source: {exc}")
    assert bad not in {r[0] for r in rows}
    assert "alice home address is elm street" in {r[1] for r in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "home address"], ids=["recency", "keyword"])
async def test_bug794_a_source_scoped_recall_survives_a_malformed_row(db, query):
    await _with_one_malformed_source(db)
    result = await memory_handlers.do_recall(AGENT, query, 10, source_id=_ALICE)
    assert "error" not in result, result
    got = [(m["content"], m["source"]["id"]) for m in result["messages"]]
    assert got == [("alice home address is elm street", _ALICE)], got


@pytest.mark.asyncio
async def test_bug794_an_unscoped_recall_still_returns_the_malformed_row(db):
    """The guard narrows only the source filter: the row stays recallable."""
    await _with_one_malformed_source(db)
    result = await memory_handlers.do_recall(AGENT, "", 10)
    assert "legacy home address note" in {m["content"] for m in result["messages"]}
