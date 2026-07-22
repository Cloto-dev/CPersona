"""Coverage the 2.5.2 refactor seams need before the code moves (CSC Task #285).

`scripts/mutation-proof.py` breaks one behaviour at a time and asserts the suite
goes red. Four of its mutations initially SURVIVED — the suite stayed green
against deliberately broken code — which meant the 2.5.2 split would have been
unguarded exactly where it cuts. Each test here closes one of those holes, and
is named for the mutation it makes fail.

The gaps were not random. Three of them share a shape: the behaviour is asserted
through the *response* the handler returns, while the thing that actually
matters is a side effect on the database (or the absence of one). A handler can
report `imported: 0` while writing rows, and every count assertion still passes.
So these tests read the DB back.
"""

import json

import pytest
import pytest_asyncio

from cpersona import admin_handlers, memory_handlers, vector
from cpersona._vendored_mcp_common import no_persist
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _count(db, table: str) -> int:
    return (await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]


# ---------------------------------------------------------------------------
# M06 — the most serious of the four. `dry_run` exists to answer "what would
# this import do?" without doing it, but every dry_run test asserted only the
# returned counts. Flipping `if not dry_run:` to `if True:` (i.e. making the
# preview write for real) left the whole suite green.
#
# The existing tests could not have caught it even in principle: their fixture
# records are duplicates, so they `continue` at the dedup check and never reach
# the INSERT. This one imports FRESH records — the only shape that reaches the
# write — and asserts the table is untouched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_dry_run_writes_nothing_to_the_database(clean_db, tmp_path):
    db = clean_db
    path = str(tmp_path / "fresh.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        # Fresh content, no msg_id: nothing dedups these away, so a dry_run that
        # leaked writes would land all three rows.
        for i in range(3):
            f.write(json.dumps({"_type": "memory", "agent_id": "a-dry", "content": f"fresh {i}"}) + "\n")
        f.write(
            json.dumps(
                {"_type": "episode", "agent_id": "a-dry", "summary": "ep summary", "keywords": ["k"]}
            )
            + "\n"
        )

    before_mem = await _count(db, "memories")
    before_ep = await _count(db, "episodes")

    preview = await admin_handlers.do_import_memories(path, target_agent_id="a-dry", dry_run=True)

    assert preview["ok"] is True
    assert preview["imported_memories"] == 3, "preview must still report what a real run would import"
    assert await _count(db, "memories") == before_mem, "dry_run import wrote memory rows"
    assert await _count(db, "episodes") == before_ep, "dry_run import wrote episode rows"


@pytest.mark.asyncio
async def test_merge_dry_run_writes_nothing_to_the_database(clean_db):
    """Same promise on the merge path, which shares the dry_run idiom."""
    db = clean_db
    await memory_handlers.do_store("m-src", {"content": "only in source", "source": {}, "timestamp": "t"})
    before = await _count(db, "memories")

    preview = await admin_handlers.do_merge_memories("m-src", "m-dst", dry_run=True)

    assert preview["merged_memories"] == 1
    assert await _count(db, "memories") == before, "dry_run merge copied rows for real"
    remaining = await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'm-src'")
    assert remaining[0][0] == 1, "dry_run merge removed the source rows"


# ---------------------------------------------------------------------------
# M04 — import dedups by msg_id before touching the DB, but every import test
# used records without a msg_id, so that pre-check was dead code as far as the
# suite was concerned. Content collisions were covered (the UNIQUE index catches
# those via INSERT OR IGNORE); identity collisions were not.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_skips_rows_whose_msg_id_already_exists(clean_db, tmp_path):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, msg_id) "
        "VALUES ('a-mid', 'original text', '{}', '2026-01-01T00:00:00Z', 'mid-1')"
    )
    await db.commit()

    path = str(tmp_path / "byid.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        # Same msg_id, DIFFERENT content: only the msg_id pre-check can catch
        # this — the content-uniqueness index sees two distinct strings.
        f.write(
            json.dumps({"_type": "memory", "agent_id": "a-mid", "msg_id": "mid-1", "content": "edited text"})
            + "\n"
        )
        f.write(
            json.dumps({"_type": "memory", "agent_id": "a-mid", "msg_id": "mid-2", "content": "new text"})
            + "\n"
        )

    result = await admin_handlers.do_import_memories(path, target_agent_id="a-mid")

    assert result["skipped_memories"] == 1, "an existing msg_id must be skipped, not re-imported"
    assert result["imported_memories"] == 1
    rows = await db.execute_fetchall("SELECT content FROM memories WHERE msg_id = 'mid-1'")
    assert len(rows) == 1 and rows[0][0] == "original text", "the stored row was overwritten by the import"


# ---------------------------------------------------------------------------
# M08 — calibration has two sample floors: one on the raw row count, and one on
# the count that survives the ragged-dimension filter. Fixtures only ever
# tripped the first, so the second (the one that protects against a corpus whose
# embeddings are mixed-model) was unpinned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibrate_rejects_when_dim_filter_drops_below_the_floor(clean_db):
    db = clean_db
    # 14 rows total — comfortably past the raw-count floor — but split across two
    # dimensions so neither group reaches 10 on its own.
    for i in range(8):
        blob = EmbeddingClient.pack_embedding([float((i + j) % 5) - 2.0 for j in range(8)])
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            ("a-ragged", f"dim8 {i}", "2026-05-14T00:00:00Z", blob),
        )
    for i in range(6):
        blob = EmbeddingClient.pack_embedding([float((i + j) % 5) - 2.0 for j in range(16)])
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            ("a-ragged", f"dim16 {i}", "2026-05-14T00:00:00Z", blob),
        )
    await db.commit()

    result = await admin_handlers.do_calibrate_threshold("a-ragged")

    assert result["ok"] is False, "calibrated a threshold from fewer than 10 same-dimension vectors"
    assert "same-dimension" in result["error"]
    assert "a-ragged" not in vector._agent_thresholds, "a rejected calibration still mutated the threshold"


# ---------------------------------------------------------------------------
# M03 — the remote vector path is the primary extraction target of the 2.5.2
# split, and the only remote test made the service return zero results, so the
# by-id fetch that turns remote hits into rows never ran. Its isolation
# predicates (bug-046/075/100: the fetch must fail closed on agent/project/
# channel) were therefore asserted by nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_by_id_fetch_refuses_rows_outside_the_isolation_axes(clean_db, monkeypatch):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, source, timestamp) "
        "VALUES ('agent-r', 'proj-a', 'row in project a', '{}', '2026-01-01T00:00:00Z')"
    )
    await db.commit()
    mem_id = (await db.execute_fetchall("SELECT id FROM memories LIMIT 1"))[0][0]

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            # The index claims a hit on a row that belongs to another project —
            # exactly the desync the fetch predicate is meant to survive.
            return {"results": [{"id": f"mem:{mem_id}", "score": 0.99}]}

    class _FakeHTTP:
        async def post(self, url, json=None, **kwargs):
            return _FakeResp()

    class _FakeClient:
        _http_url = "http://x/embed"
        _client = _FakeHTTP()

    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _FakeClient())

    # Querying a DIFFERENT project must not surface the proj-a row even though
    # the remote index handed us its id.
    leaked = await vector._search_vector(db, "agent-r", "q", 10, project_id="proj-b")
    assert all(r.get("id") != mem_id for r in leaked), (
        "remote by-id fetch returned a row outside the requested project (bug-046/075/100)"
    )

    # Same axes as the row: the fetch must still work, or the predicate is simply
    # blocking everything and the assertion above would be vacuous.
    found = await vector._search_vector(db, "agent-r", "q", 10, project_id="proj-a")
    assert any(r.get("id") == mem_id for r in found), "the fetch predicate blocks legitimate rows too"
