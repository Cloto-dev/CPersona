"""Regression tests for the 2.6.11 audit's follow-up findings.

  bug-794  A per-user ``source_id`` filter read ``json_extract(source, '$.id')``
           with no ``json_valid`` guard, so one row in scope whose ``source`` is
           not JSON made every source-scoped scan raise "malformed JSON" and the
           whole call fail. Such rows exist: the JSON repair leaves locked ones
           alone. bug-743 guarded the same read in the index builder.
  bug-802  tests/test_recall_precision.py restored a calibration sidecar whose
           global threshold (0.5) stayed in config.VECTOR_MIN_SIMILARITY after
           the module, so a recall test that ran after it in the same process
           scored against the wrong floor. The full suite runs the files in
           alphabetical order, which never takes that order.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

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


# ==========================================================================
# bug-802: the precision module leaves no calibration state behind it
# ==========================================================================

_REPO = Path(__file__).resolve().parent.parent
_AFTER_PRECISION = "tests/test_docs_behavioural_claims.py::test_deep_relaxes_the_quality_gate_rather_than_a_time_horizon"


def test_bug802_a_recall_test_after_the_precision_module_still_passes():
    """Run the order the alphabetical full suite never takes, in a fresh process.

    The child gets no CPERSONA_* variable from this process: several test modules
    set them at import, and the child must start the way a plain `pytest` does."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CPERSONA_")}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_recall_precision.py", _AFTER_PRECISION],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=50,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1000:]
    assert " passed" in run.stdout and " failed" not in run.stdout, run.stdout[-1000:]


# ==========================================================================
# bug-804: no test reads a file another test left beside the shared database
# ==========================================================================

_LEAVES_A_BLOCK_INDEX = "tests/test_blocks_retrieval.py::test_hydrating_the_hits_stops_at_its_bound"
_READS_BESIDE_THE_SHARED_PATH = (
    "tests/test_equivalence_252.py::test_behaviour_matches_the_pre_refactor_golden[m1-recall-cue-sure-rrf-trace]"
)
_PASSED_ON_A_LEFTOVER = "tests/test_blocks_retrieval.py::test_changing_the_count_alone_leaves_the_examined_set_unchanged"


def _child_pytest(*node_ids: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CPERSONA_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *node_ids],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=50,
    )


def test_bug804_a_golden_scenario_after_a_test_that_built_a_block_index_still_passes():
    """The order a narrowed run takes: the first test builds a block index beside
    the process's shared database path, and the golden scenario then reads that
    path. With the file left there, it recorded `logging_off` where the golden
    holds `no_file`."""
    run = _child_pytest(_LEAVES_A_BLOCK_INDEX, _READS_BESIDE_THE_SHARED_PATH)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1000:]
    assert "2 passed" in run.stdout, run.stdout[-1000:]


def test_bug804_the_count_invariant_holds_without_a_leftover_index():
    """Alone, with no earlier test's file to fall back past."""
    run = _child_pytest(_PASSED_ON_A_LEFTOVER)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1000:]
    assert "1 passed" in run.stdout, run.stdout[-1000:]
