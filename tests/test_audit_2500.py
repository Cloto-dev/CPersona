"""Regression tests for the v2.5.0 deep-audit line (continuation of the 2.4.39 audit).

Covers the behavioural fixes filed from the second, deeper audit pass
(qa/issue-registry.json bug-061..072). Each test pins one fixed defect so
verify-issues.sh's pattern check and these assertions move together. The deferred
findings (bug-073 export streaming, bug-074 episode-prefix heuristic, bug-075 remote
vector channel) are intentionally not covered here.
"""

import json
import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, database  # noqa: F401
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db


def _blob(dim=64):
    """A well-formed float32 embedding blob (length always a multiple of 4)."""
    return np.zeros(dim, dtype=np.float32).tobytes()


@pytest_asyncio.fixture
async def clean_db():
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-061: a malformed embedding blob (byte length not a multiple of 4) must not
# crash calibration / the frombuffer decode; it must be rejected at ingestion.
# ---------------------------------------------------------------------------


def test_safe_frombuffer_rejects_corrupt_blob():
    assert admin_handlers._safe_frombuffer(b"\x00\x00\x00") is None  # 3 bytes: not a float32
    assert admin_handlers._safe_frombuffer(b"") is None
    assert admin_handlers._safe_frombuffer(None) is None
    ok = admin_handlers._safe_frombuffer(_blob())
    assert ok is not None and ok.shape[0] == 64


def test_decode_embedding_rejects_non_multiple_of_four():
    import base64

    # A validly-base64'd 3-byte payload decodes fine but is a poison float32 blob.
    poison = {"embedding_b64": base64.b64encode(b"\x01\x02\x03").decode()}
    assert admin_handlers._decode_embedding(poison) is None
    good = {"embedding_b64": base64.b64encode(_blob()).decode()}
    assert admin_handlers._decode_embedding(good) is not None


@pytest.mark.asyncio
async def test_calibrate_survives_a_poison_blob(clean_db):
    db = clean_db
    # 10 valid embeddings + 1 corrupt row: calibration must not raise.
    for i in range(10):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, '', ?)",
            ("poison-agent", f"m{i}", _blob()),
        )
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, '', ?)",
        ("poison-agent", "bad", b"\x01\x02\x03"),
    )
    await db.commit()
    res = await admin_handlers.do_calibrate_threshold("poison-agent")  # must not raise
    assert isinstance(res, dict)


# ---------------------------------------------------------------------------
# bug-062: axis_distribution must scope to the requested agent (no cross-agent leak).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_axis_distribution_scoped_to_agent(clean_db):
    db = clean_db
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) VALUES ('A', 'proj-a', 'x', '')"
    )
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) VALUES ('B', 'proj-b-secret', 'y', '')"
    )
    await db.commit()
    axes = await checks.axis_distribution(db, "A")
    assert "proj-a" in axes["project_id"]
    assert "proj-b-secret" not in axes["project_id"]  # other agent's bucket must not leak
    # Empty agent_id keeps the corpus-wide view.
    axes_all = await checks.axis_distribution(db, "")
    assert "proj-b-secret" in axes_all["project_id"]


# ---------------------------------------------------------------------------
# bug-066: calibrate percentile/z_factor validation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibrate_rejects_out_of_range_percentile(clean_db):
    # 200 -> /100 = 2.0, still >1 -> a clear validation error (not an opaque numpy stack).
    res = await admin_handlers.do_calibrate_threshold("x", percentile=200)
    assert res.get("ok") is False and "percentile must be in" in res.get("error", "")


# ---------------------------------------------------------------------------
# bug-070: import dry_run must count an intra-file duplicate as skipped (parity).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_dry_run_counts_intra_file_duplicate(clean_db, tmp_path):
    rec = {"_type": "memory", "agent_id": "imp", "content": "same text", "msg_id": "", "project_id": "", "channel": ""}
    path = os.path.join(tmp_path, "dup.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
        f.write(json.dumps(rec) + "\n")  # identical duplicate in the same file
    res = await admin_handlers.do_import_memories(path, dry_run=True)
    assert res["imported_memories"] == 1, res
    assert res["skipped_memories"] == 1, res


# ---------------------------------------------------------------------------
# bug-071: merge dry_run must count an intra-batch duplicate-summary episode as skipped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_dry_run_counts_intra_batch_duplicate_episode(clean_db):
    db = clean_db
    # Source agent holds two episodes with the SAME summary (episodes have no summary
    # uniqueness constraint, so this is legitimate).
    for _ in range(2):
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, keywords) VALUES ('src', 'dup summary', '')"
        )
    await db.commit()
    res = await admin_handlers.do_merge_memories("src", "dst", dry_run=True)
    assert res["merged_episodes"] == 1, res
    assert res["skipped_episodes"] == 1, res


# ---------------------------------------------------------------------------
# bug-072: prefetch_null_embeddings computes embeddings for NULL rows outside the lock.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefetch_null_embeddings_populates_cache(clean_db, fake_embedding_client):
    db = clean_db
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('pf', 'needs embedding', '')"
    )
    mem_id = cur.lastrowid
    cur2 = await db.execute("INSERT INTO episodes (agent_id, summary) VALUES ('pf', 'ep summary')")
    ep_id = cur2.lastrowid
    await db.commit()
    cache = await checks.prefetch_null_embeddings(db, "pf")
    # bug-077: cache values are (text, blob) so the write path can refuse to attach a
    # blob to content that changed after prefetch.
    assert mem_id in cache["memories"]
    text, blob = cache["memories"][mem_id]
    assert text == "needs embedding" and isinstance(blob, (bytes, bytearray))
    assert ep_id in cache["episodes"]


@pytest.mark.asyncio
async def test_prefetch_empty_without_embedding_client(clean_db, monkeypatch):
    from cpersona import vector

    monkeypatch.setattr(vector, "_embedding_client", None)
    db = clean_db
    await db.execute("INSERT INTO memories (agent_id, content, timestamp) VALUES ('pf', 'x', '')")
    await db.commit()
    cache = await checks.prefetch_null_embeddings(db, "pf")
    assert cache == {"memories": {}, "episodes": {}}


# ---------------------------------------------------------------------------
# bug-072 behavioural: check_health(fix=True) still fills NULL embeddings (via the
# prefetch cache path), i.e. the refactor preserved the repair.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_health_fills_null_embeddings_via_cache(clean_db, fake_embedding_client):
    from cpersona import maintenance_handlers

    db = clean_db
    await db.execute("INSERT INTO memories (agent_id, content, timestamp) VALUES ('h', 'fill me', '')")
    await db.commit()
    await maintenance_handlers.do_check_health(agent_id="h", fix=True)
    row = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = 'h' AND content = 'fill me'"
    )
    assert row[0][0] is not None  # the null embedding was repaired
