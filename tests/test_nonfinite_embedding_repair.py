"""A vector already in the database that holds a NaN or an infinity.

The write path refuses these now, but that only stops new ones. A corpus embedded
before that seam existed -- or by a backend that answered with a NaN -- still holds
them, and reading the row back cannot repair the vector, because the number that
would have been there is gone.

So the repair is the one `embedding_dimension` already uses for a vector it cannot
trust: null it and let the re-embed pass fill it from the content, which is intact.
That is the write side's own judgement -- a missing vector is a gap that can be
filled, and a NaN vector is not a gap -- and it means this check decides nothing
about ranking. It removes the bad input; the ordinary paths then do what they did.

The property these tests exist for is the one a summary hides: detection changes
NOTHING a recall returns. Only a fix pass moves anything, and a fix pass is a
maintenance action a person triggers.
"""

from __future__ import annotations

import math
import struct

import pytest
import pytest_asyncio

from cpersona import checks, session, vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db

AGENT = "nonfinite-agent"
UTC_TS = "2026-08-01T00:00:00+00:00"
DIM = 8


def _vec(*, bad: float | None = None, at: int = 0) -> bytes:
    values = [0.1 * (i + 1) for i in range(DIM)]
    if bad is not None:
        values[at] = bad
    return EmbeddingClient.pack_embedding(values)


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _mem(conn, content, embedding):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
        (AGENT, content, UTC_TS, embedding),
    )
    await conn.commit()


async def _ep(conn, summary, embedding):
    await conn.execute(
        "INSERT INTO episodes (agent_id, summary, start_time, embedding) VALUES (?,?,?,?)",
        (AGENT, summary, UTC_TS, embedding),
    )
    await conn.commit()


async def _blobs(conn, table="memories"):
    rows = await conn.execute_fetchall(
        f"SELECT embedding FROM {table} WHERE agent_id = ?", (AGENT,)
    )
    return [r[0] for r in rows]


# --- the predicate, on bytes the store actually writes ----------------------


@pytest.mark.parametrize(
    "bad,expected",
    [
        (None, True),
        (float("nan"), False),
        (float("inf"), False),
        (float("-inf"), False),
    ],
    ids=["finite", "nan", "plus-inf", "minus-inf"],
)
def test_the_predicate_reads_a_packed_vector(bad, expected):
    assert vector.stored_blob_is_finite(_vec(bad=bad)) is expected


def test_one_bad_element_among_many_is_enough():
    """The corpus that motivated this held vectors with a single NaN element, not
    vectors that were entirely NaN. A check that only caught the all-NaN case
    would have reported the loud half of the fault and left the quiet half."""
    assert vector.stored_blob_is_finite(_vec(bad=float("nan"), at=DIM - 1)) is False


def test_a_blob_of_the_wrong_width_is_not_this_checks_finding():
    """`embedding_dimension` owns that row and has its own repair. Claiming it here
    would have two checks null the same vector, and the width says nothing about
    whether the values are finite."""
    assert vector.stored_blob_is_finite(b"\x00\x00\x00") is True


def test_a_missing_blob_is_not_a_finite_one():
    """NULL is `null_embedding`'s finding. Answering True here would let a caller
    that trusts this predicate treat a missing vector as a usable one."""
    assert vector.stored_blob_is_finite(None) is False


# --- the check ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_corpus_reports_nothing(db):
    await _mem(db, "an ordinary memory", _vec())
    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=False, checks=["nonfinite_embedding"]
    )
    assert issues == []


@pytest.mark.asyncio
async def test_detection_alone_leaves_every_vector_where_it_was(db):
    """The property the whole design rests on: finding the row is not repairing it.
    A check that nulled on sight would change what recall returns for everyone who
    merely ASKED about their corpus's health."""
    await _mem(db, "a poisoned memory", _vec(bad=float("nan")))
    before = await _blobs(db)

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=False, checks=["nonfinite_embedding"]
    )

    assert [i["type"] for i in issues] == ["nonfinite_embedding"]
    assert issues[0]["count"] == 1
    assert await _blobs(db) == before, "a fix=False run wrote to the database"


@pytest.mark.asyncio
async def test_the_fix_nulls_the_vector_and_keeps_the_content(db):
    """Null, not delete. The content is what a re-embed reads, so losing the row
    would turn a repairable vector into a lost memory."""
    await _mem(db, "a poisoned memory", _vec(bad=float("inf")))

    await checks.run_health_checks(db, agent_id=AGENT, fix=True, checks=["nonfinite_embedding"])

    rows = await db.execute_fetchall(
        "SELECT content, embedding FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert len(rows) == 1
    assert rows[0][0] == "a poisoned memory"
    assert rows[0][1] is None


@pytest.mark.asyncio
async def test_the_fix_leaves_the_healthy_vectors_alone(db):
    """A repair that nulled the whole scope would 'fix' the finding and destroy
    every good vector with it -- and the count would look the same."""
    await _mem(db, "healthy", _vec())
    await _mem(db, "poisoned", _vec(bad=float("nan")))

    await checks.run_health_checks(db, agent_id=AGENT, fix=True, checks=["nonfinite_embedding"])

    surviving = sorted(
        (r[0], r[1] is None)
        for r in await db.execute_fetchall(
            "SELECT content, embedding FROM memories WHERE agent_id = ?", (AGENT,)
        )
    )
    assert surviving == [("healthy", False), ("poisoned", True)]


@pytest.mark.asyncio
async def test_episodes_are_repaired_too(db):
    """Episodes carry embeddings and are scored by the same retriever. A check that
    only walked `memories` would leave half the corpus poisoned."""
    await _ep(db, "a poisoned episode", _vec(bad=float("nan")))

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=True, checks=["nonfinite_embedding"]
    )

    assert [i["table"] for i in issues] == ["episodes"]
    assert await _blobs(db, "episodes") == [None]


@pytest.mark.asyncio
async def test_a_scope_reports_only_its_own_rows(db):
    """The isolation predicate every other check carries. Without it, one agent's
    health answer counts another agent's rows -- and a fix run repairs them."""
    await _mem(db, "mine", _vec(bad=float("nan")))
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
        ("someone-else", "theirs", UTC_TS, _vec(bad=float("nan"))),
    )
    await db.commit()

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=True, checks=["nonfinite_embedding"]
    )

    assert issues[0]["count"] == 1
    other = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = 'someone-else'"
    )
    assert other[0][0] is not None, "the repair reached outside the scope it was given"


@pytest.mark.asyncio
async def test_a_corpus_larger_than_one_parameter_batch_is_repaired_whole(db):
    """A backend that goes bad poisons every row it touches, so the widespread case
    is the ordinary one. Binding them all into one IN clause raises past SQLite's
    variable limit -- and the repair for a widespread fault must not be the thing
    that fails."""
    n = checks._NONFINITE_REPAIR_CHUNK * 2 + 7
    for i in range(n):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (AGENT, f"poisoned {i}", UTC_TS, _vec(bad=float("nan"))),
        )
    await db.commit()

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=True, checks=["nonfinite_embedding"]
    )

    assert issues[0]["count"] == n
    remaining = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NOT NULL", (AGENT,)
    )
    assert remaining[0][0] == 0


@pytest.mark.asyncio
async def test_a_value_that_is_finite_only_in_float64_is_caught(db):
    """1e300 is a finite float64 and an infinite float32, and the store is float32.
    The write seam refuses it by asking `struct.pack`; a row that predates the seam
    holds the infinity it became, which is what this reads."""
    packed = struct.pack("<1f", float("inf"))  # what 1e300 becomes on the way in
    assert math.isinf(struct.unpack("<1f", packed)[0])
    await _mem(db, "an overflowed vector", packed)

    issues, _ = await checks.run_health_checks(
        db, agent_id=AGENT, fix=False, checks=["nonfinite_embedding"]
    )
    assert issues[0]["count"] == 1
