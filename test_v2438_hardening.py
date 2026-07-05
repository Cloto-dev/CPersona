"""Regression tests for the v2.4.38 structural-hardening line (Goal #156).

Covers the fixes filed from the task#150 full audit (qa/issue-registry.json
bug-014..039). Each test pins one fixed defect so verify-issues.sh's "pattern
absent" and these behavioural assertions move together.
"""

import json

import pytest
import pytest_asyncio

from cpersona import admin_handlers, maintenance_handlers, server
from cpersona._vendored_mcp_common import no_persist
from cpersona.config import MAX_CONTENT_LENGTH
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    """A freshly-truncated DB for the DB-backed hardening tests."""
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# bug-017: HTTP transport must fail closed (no unauthenticated public bind).
# ---------------------------------------------------------------------------


def test_http_bind_public_without_token_refuses_to_start():
    """auth off + non-loopback host must raise (fail closed), not silently expose."""
    with pytest.raises(SystemExit):
        server._assert_safe_http_bind("", "0.0.0.0")


def test_http_bind_external_ip_without_token_refuses_to_start():
    with pytest.raises(SystemExit):
        server._assert_safe_http_bind("", "192.168.0.10")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_http_bind_loopback_without_token_is_allowed(host):
    """Loopback bind without a token is a local-dev convenience — allowed (warns)."""
    server._assert_safe_http_bind("", host)  # must not raise


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "127.0.0.1"])
def test_http_bind_with_token_is_allowed_anywhere(host):
    """With a token set, any bind is fine — auth is enforced by the middleware."""
    server._assert_safe_http_bind("s3cret", host)  # must not raise


# ---------------------------------------------------------------------------
# bug-015 / bug-029 / bug-030: fix=True must never delete or alter a locked row.
# The bug-007 "never touch locked" invariant, enforced across the destructive
# check registry. Each locked row is paired with a distinct-content unlocked
# control (distinct content so duplicate_content does not collapse them) to
# prove the repair still fires for unlocked data.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locked_rows_survive_destructive_maintenance(clean_db):
    db = clean_db
    A = "agent-s2lock"
    over_big = "x" * (MAX_CONTENT_LENGTH + 500)
    over_big2 = "y" * (MAX_CONTENT_LENGTH + 500)

    async def ins(content, locked):
        cur = await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
            "VALUES (?, ?, '{}', '2026-01-01T00:00:00Z', ?)",
            (A, content, 1 if locked else 0),
        )
        return cur.lastrowid

    over_locked = await ins(over_big, True)       # oversized (bug-029, truncate)
    empty_locked = await ins("", True)            # empty    (bug-030, delete)
    short_locked = await ins("hi", True)          # short    (bug-015, deep delete)
    over_unlocked = await ins(over_big2, False)
    empty_unlocked = await ins(" ", False)
    short_unlocked = await ins("yo", False)
    await db.commit()

    await maintenance_handlers.do_check_health(agent_id=A, fix=True)
    await maintenance_handlers.do_deep_check(agent_id=A, fix=True)

    async def exists(mid):
        return bool(await db.execute_fetchall("SELECT 1 FROM memories WHERE id=?", (mid,)))

    async def content_of(mid):
        r = await db.execute_fetchall("SELECT content FROM memories WHERE id=?", (mid,))
        return r[0][0] if r else None

    # locked rows survive untouched
    assert await content_of(over_locked) == over_big, "locked oversized row was truncated"
    assert await exists(empty_locked), "locked empty row was deleted"
    assert await content_of(short_locked) == "hi", "locked short row was deleted"

    # unlocked controls are still repaired/removed (the fix works for unlocked data)
    assert len(await content_of(over_unlocked)) <= MAX_CONTENT_LENGTH, "unlocked oversized not truncated"
    assert not await exists(empty_unlocked), "unlocked empty not deleted"
    assert not await exists(short_unlocked), "unlocked short not deleted"


# ---------------------------------------------------------------------------
# bug-014: duplicate_content collapses across channels (intended cleanup) but
# NOT across the hard project_id isolation axis. Same content under project ''
# and project 'X' are distinct rows with different visibility and must survive.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_content_does_not_collapse_across_projects(clean_db):
    db = clean_db
    A = "agent-dupproj"

    async def ins(content, project_id, channel):
        cur = await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp) "
            "VALUES (?, ?, ?, ?, '{}', '2026-01-01T00:00:00Z')",
            (A, project_id, channel, content),
        )
        return cur.lastrowid

    # same content, DIFFERENT project → legitimately distinct, must both survive
    g = await ins("shared text", "", "")            # global project
    x = await ins("shared text", "proj-x", "")       # project 'proj-x'
    # same content, SAME project, different channels → intended cross-channel dedup
    c1 = await ins("chan dup", "", "ch1")
    c2 = await ins("chan dup", "", "ch2")
    await db.commit()

    from cpersona import maintenance_handlers as mh
    await mh.do_check_health(agent_id=A, fix=True)

    async def exists(mid):
        return bool(await db.execute_fetchall("SELECT 1 FROM memories WHERE id=?", (mid,)))

    # cross-project rows both preserved (bug-014 fix)
    assert await exists(g), "global-project copy was deleted (cross-project collapse)"
    assert await exists(x), "project-x copy was deleted (cross-project collapse)"
    # cross-channel duplicates within one project still collapse to one survivor
    survivors = {c1, c2} & {
        r[0] for r in await db.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id=? AND content='chan dup'", (A,)
        )
    }
    assert survivors == {c1}, "cross-channel dedup should keep MIN(id) survivor only"


# ---------------------------------------------------------------------------
# bug-016 / bug-020 / bug-022: export/import and merge must carry the
# project_id / channel / locked axes + embedding, and treat a dedup collision
# as a counted skip rather than an uncaught IntegrityError that half-writes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_import_roundtrip_preserves_axes_and_embedding(clean_db, tmp_path):
    db = clean_db
    A = "agent-rt"
    emb = bytes(range(16))
    await db.execute(
        "INSERT INTO memories "
        "(agent_id, project_id, channel, msg_id, content, source, timestamp, metadata, embedding, locked) "
        "VALUES (?, 'proj', 'chan', 'm1', 'hello world', '{}', '2026-01-01T00:00:00Z', '{}', ?, 1)",
        (A, emb),
    )
    await db.commit()

    out = str(tmp_path / "export.jsonl")
    r = await admin_handlers.do_export_memories(A, out, include_embeddings=True)
    assert r["ok"] and r["memories"] == 1

    await db.execute("DELETE FROM memories")
    await db.commit()

    ri = await admin_handlers.do_import_memories(out, target_agent_id="")
    assert ri["ok"] and ri["imported_memories"] == 1

    row = (
        await db.execute_fetchall(
            "SELECT project_id, channel, locked, embedding FROM memories "
            "WHERE agent_id=? AND content='hello world'",
            (A,),
        )
    )[0]
    assert row[0] == "proj"          # project_id carried (bug-016)
    assert row[1] == "chan"          # channel carried (bug-016)
    assert row[2] == 1               # locked carried (bug-016)
    assert bytes(row[3]) == emb      # embedding carried (bug-016)


@pytest.mark.asyncio
async def test_import_content_collision_skips_not_aborts(clean_db, tmp_path):
    db = clean_db
    A = "agent-coll"
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES (?, 'dup text', '{}', '2026-01-01T00:00:00Z')",
        (A,),
    )
    await db.commit()

    path = str(tmp_path / "imp.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_type": "memory", "agent_id": A, "content": "dup text"}) + "\n")
        f.write(json.dumps({"_type": "memory", "agent_id": A, "content": "fresh text"}) + "\n")

    r = await admin_handlers.do_import_memories(path, target_agent_id="")
    assert r["ok"] is True
    assert r["skipped_memories"] == 1     # collider IGNOREd, not crashed
    assert r["imported_memories"] == 1    # the row AFTER the collision still imported
    assert await db.execute_fetchall(
        "SELECT 1 FROM memories WHERE agent_id=? AND content='fresh text'", (A,)
    )


@pytest.mark.asyncio
async def test_merge_preserves_project_locked_and_skips_collision(clean_db):
    db = clean_db
    SRC, TGT = "agent-src", "agent-tgt"
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, source, timestamp, locked) "
        "VALUES (?, 'p1', 'unique src', '{}', '2026-01-01T00:00:00Z', 1)",
        (SRC,),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES (?, 'shared', '{}', '2026-01-01T00:00:00Z')",
        (SRC,),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES (?, 'shared', '{}', '2026-01-01T00:00:00Z')",
        (TGT,),
    )
    await db.commit()

    r = await admin_handlers.do_merge_memories(SRC, TGT, strategy="skip", mode="copy")
    assert r["ok"] is True
    assert r["skipped_memories"] == 1     # 'shared' collides with target -> skipped
    assert r["merged_memories"] == 1      # 'unique src' merged

    row = (
        await db.execute_fetchall(
            "SELECT project_id, locked FROM memories WHERE agent_id=? AND content='unique src'",
            (TGT,),
        )
    )[0]
    assert row[0] == "p1"   # project_id preserved (bug-020)
    assert row[1] == 1      # locked preserved (bug-022)
