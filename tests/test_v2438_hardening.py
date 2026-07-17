"""Regression tests for the v2.4.38 structural-hardening line (Goal #156).

Covers the fixes filed from the task#150 full audit (qa/issue-registry.json
bug-014..039). Each test pins one fixed defect so verify-issues.sh's "pattern
absent" and these behavioural assertions move together.
"""

import json

import pytest
import pytest_asyncio

from cpersona import (
    admin_handlers,
    checks,
    maintenance_handlers,
    memory_handlers,
    server,
    vector,
)
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


# ---------------------------------------------------------------------------
# v2.4.38 batch #2 (task#171): the 11 MEDIUM/LOW fixes 博士 approved for 2.4.38.
# Each test pins one landed fix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_pending_is_agent_scoped(clean_db):
    """bug-031: check_health(fix) must not delete OTHER agents' pending tasks."""
    db = clean_db
    for agent in ("agent-A", "agent-B"):
        await db.execute(
            "INSERT INTO pending_memory_tasks (task_type, agent_id, payload, created_at) "
            "VALUES ('store', ?, '{}', datetime('now', '-2 hours'))",
            (agent,),
        )
    await db.commit()

    await maintenance_handlers.do_check_health(agent_id="agent-A", fix=True)

    remaining = {
        r[0] for r in await db.execute_fetchall("SELECT agent_id FROM pending_memory_tasks")
    }
    assert "agent-B" in remaining, "check_health for A deleted B's pending task (cross-agent loss)"
    assert "agent-A" not in remaining, "A's own stale task was not cleaned"


@pytest.mark.asyncio
async def test_recall_negative_limit_is_clamped(clean_db):
    """bug-032: a negative limit must not become SQLite LIMIT -1 (unbounded scan)."""
    db = clean_db
    A = "agent-lim"
    for i in range(5):
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            "VALUES (?, ?, '{}', '2026-01-01T00:00:00Z')",
            (A, f"row {i}"),
        )
    await db.commit()

    res = await memory_handlers.do_recall(A, "", -1)
    assert "error" not in res
    assert len(res["messages"]) == 0  # clamped to 0, not the whole corpus


def test_like_escape_contains_escapes_wildcards():
    """bug-034: % and _ in a keyword query are literals, not LIKE wildcards."""
    assert memory_handlers._like_escape_contains("a_b") == "%a\\_b%"
    assert memory_handlers._like_escape_contains("50%") == "%50\\%%"
    assert memory_handlers._like_escape_contains("a\\b") == "%a\\\\b%"


@pytest.mark.asyncio
async def test_delete_agent_data_clears_calibration(clean_db):
    """bug-036: deleting an agent must drop its in-process calibration state."""
    A = "agent-cal-del"
    vector._agent_thresholds[A] = 0.42
    vector._agent_betas[A] = 1.5
    vector._agent_fused_gates[A] = 0.3
    try:
        await admin_handlers.do_delete_agent_data(A)
        assert A not in vector._agent_thresholds, "stale threshold survived delete_agent_data"
        assert A not in vector._agent_betas, "stale beta survived delete_agent_data"
        assert A not in vector._agent_fused_gates, "stale fused gate survived delete_agent_data"
    finally:
        vector._agent_thresholds.pop(A, None)
        vector._agent_betas.pop(A, None)
        vector._agent_fused_gates.pop(A, None)


@pytest.mark.asyncio
async def test_calibrate_survives_mixed_embedding_dims(clean_db, monkeypatch):
    """bug-025: a mixed 768d/1024d corpus must not raise a ragged-array ValueError."""
    import numpy as np

    monkeypatch.setattr(admin_handlers, "_save_calibration_state", lambda *a, **k: None)
    db = clean_db
    A = "agent-dim"
    for i in range(12):
        blob = (np.arange(768, dtype=np.float32) + i + 1).tobytes()
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp, embedding) "
            "VALUES (?, ?, '{}', '2026-01-01T00:00:00Z', ?)",
            (A, f"a{i}", blob),
        )
    for i in range(3):
        blob = (np.arange(1024, dtype=np.float32) + i + 1).tobytes()
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp, embedding) "
            "VALUES (?, ?, '{}', '2026-01-01T00:00:00Z', ?)",
            (A, f"b{i}", blob),
        )
    await db.commit()

    try:
        res = await admin_handlers.do_calibrate_threshold(agent_id=A)
        assert res.get("ok") is True, res  # modal dim (768) used, no ValueError
    finally:
        vector._agent_thresholds.pop(A, None)


@pytest.mark.asyncio
async def test_migrate_channel_axis_completes_on_collision(clean_db):
    """bug-021: a dedup-index collision must be IGNOREd, not ABORT the migration.

    Setup: a row already on channel '123' plus a 'discord' row whose session_id
    recovers to the same channel '123' with identical content — migrating the
    discord row collides on idx_memories_dedup_content. A bare UPDATE raises
    IntegrityError (ABORT) and the whole migration fails; UPDATE OR IGNORE skips
    the colliding row and completes.
    """
    db = clean_db
    A = "agent-mig"
    await db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, metadata) "
        "VALUES (?, '123', 'dup', '{}', '2026-01-01T00:00:00Z', '{}')",
        (A,),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, metadata) "
        "VALUES (?, 'discord', 'dup', '{}', '2026-01-01T00:00:00Z', ?)",
        (A, json.dumps({"session_id": "123:u1:0"})),
    )
    await db.commit()

    # Must not raise (a bare UPDATE would IntegrityError here).
    res = await maintenance_handlers.do_migrate_channel_axis(agent_id=A, dry_run=False)
    assert res["dry_run"] is False

    rows = {
        (r[0], r[1])
        for r in await db.execute_fetchall("SELECT channel, content FROM memories WHERE agent_id=?", (A,))
    }
    assert ("123", "dup") in rows, "pre-existing row lost"
    # the discord row could not migrate (collision) so it is skipped, left on 'discord'
    assert ("discord", "dup") in rows, "colliding row was dropped/migrated instead of skipped"


@pytest.mark.asyncio
async def test_content_rewrite_nulls_stale_embedding(clean_db):
    """bug-028: stripping content in a fixer must NULL the now-stale embedding."""
    import numpy as np

    db = clean_db
    A = "agent-emb"
    blob = np.ones(8, dtype=np.float32).tobytes()
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, embedding) "
        "VALUES (?, '[Memory from X] hello', '{}', '2026-01-01T00:00:00Z', ?)",
        (A, blob),
    )
    mid = cur.lastrowid
    await db.commit()

    await checks.check_memory_annotation(db, A, fix=True)

    row = (await db.execute_fetchall("SELECT content, embedding FROM memories WHERE id=?", (mid,)))[0]
    assert "[Memory from" not in row[0], "annotation not stripped"
    assert row[1] is None, "embedding not NULLed after content rewrite (stale vector, bug-028)"


@pytest.mark.asyncio
async def test_recall_does_not_bump_recall_count_under_no_persist(clean_db, monkeypatch):
    """bug-038: recall must not mutate recall_count while no-persist is active.

    The bump only fires under CONFIDENCE_ENABLED (which populates recall_counts),
    so we force it on; the quality gate is stubbed to identity so the hermetic
    (embedding=none) FTS hit reaches the bump path deterministically.
    """
    db = clean_db
    A = "agent-np"
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES (?, 'findme raspberry sentinel', '{}', '2026-01-01T00:00:00Z')",
        (A,),
    )
    mid = cur.lastrowid
    await db.commit()

    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "_apply_quality_gate", lambda results, *a, **k: results)

    # sanity: a normal recall reaches the bump path (proves the test isn't vacuous)
    await memory_handlers.do_recall(A, "raspberry", 10)
    rc = (await db.execute_fetchall("SELECT recall_count FROM memories WHERE id=?", (mid,)))[0][0]
    assert rc >= 1, "recall did not bump recall_count even without no-persist (setup issue)"

    await db.execute("UPDATE memories SET recall_count = 0 WHERE id = ?", (mid,))
    await db.commit()

    no_persist.pause(1800)
    try:
        await memory_handlers.do_recall(A, "raspberry", 10)
    finally:
        no_persist.resume()

    rc2 = (await db.execute_fetchall("SELECT recall_count FROM memories WHERE id=?", (mid,)))[0][0]
    assert rc2 == 0, "recall bumped recall_count under no-persist (bug-038)"


@pytest.mark.asyncio
async def test_locked_row_survives_admin_delete_and_update(clean_db):
    """bug-024: the admin delete/update path must never destroy a locked row."""
    db = clean_db
    A = "agent-adminlock"
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
        "VALUES (?, 'protected', '{}', '2026-01-01T00:00:00Z', 1)",
        (A,),
    )
    mid = cur.lastrowid
    await db.commit()

    dr = await admin_handlers.do_delete_memory(mid, agent_id=A)
    assert "error" in dr
    ur = await admin_handlers.do_update_memory(mid, "new text", agent_id=A)
    assert "error" in ur

    row = (await db.execute_fetchall("SELECT content, locked FROM memories WHERE id=?", (mid,)))[0]
    assert row[0] == "protected" and row[1] == 1, "locked row was destroyed/edited via admin path (bug-024)"


@pytest.mark.asyncio
async def test_remote_search_honors_min_similarity_argument(clean_db, monkeypatch):
    """bug-027: the remote /search branch must send the caller's min_similarity."""
    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    class _FakeHTTP:
        async def post(self, url, json=None, **kwargs):
            captured["json"] = json
            captured["kwargs"] = kwargs
            return _FakeResp()

    class _FakeClient:
        _http_url = "http://x/embed"
        _client = _FakeHTTP()

    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _FakeClient())

    db = clean_db
    await vector._search_vector(db, "agent-x", "q", 10, min_similarity=0.123)
    assert captured["json"]["min_similarity"] == 0.123, "remote /search ignored min_similarity (bug-027)"
    # bug-033: the recall hot-path POST must carry a bounded per-call timeout, not
    # inherit the client's 30s default.
    assert captured["kwargs"].get("timeout") == vector.REMOTE_SEARCH_TIMEOUT_SECS, (
        "remote /search POST did not pass a dedicated timeout (bug-033)"
    )


# ---------------------------------------------------------------------------
# bug-019 / bug-039: an omitted project_id (spec ("project_id", str, None)) must
# reach the handler as None (= all projects), not "" (= global pool only). The
# root fix is in the vendored auto_tool _handler; bug-039 is the missing
# end-to-end test that drives the registered handler with the arg absent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_tool_passes_explicit_none_default():
    from cpersona._vendored_mcp_common.mcp_utils import ToolRegistry

    received = {}

    async def handler(agent_id, project_id):
        received["agent_id"] = agent_id
        received["project_id"] = project_id
        return {"ok": True}

    reg = ToolRegistry("test-none-default")
    reg.auto_tool("t", "d", {}, handler, [("agent_id", str), ("project_id", str, None)])

    # project_id omitted → the explicit None default must win (bug-019)
    await reg._handlers["t"]({"agent_id": "a"})
    assert received["project_id"] is None, "omitted project_id reached the handler as '' (global-only), not None (all projects)"

    # an explicit JSON null also resolves to None (not "")
    await reg._handlers["t"]({"agent_id": "a", "project_id": None})
    assert received["project_id"] is None

    # a present value still passes straight through
    await reg._handlers["t"]({"agent_id": "a", "project_id": "proj-x"})
    assert received["project_id"] == "proj-x"


@pytest.mark.asyncio
async def test_auto_tool_two_tuple_still_uses_validator_default():
    """The fix must not disturb 2-tuple specs (no default) — validator '' stands."""
    from cpersona._vendored_mcp_common.mcp_utils import ToolRegistry

    got = {}

    async def handler(agent_id):
        got["agent_id"] = agent_id
        return {"ok": True}

    reg = ToolRegistry("test-two-tuple")
    reg.auto_tool("t2", "d", {}, handler, [("agent_id", str)])
    await reg._handlers["t2"]({})  # agent_id omitted
    assert got["agent_id"] == "", "2-tuple no-default spec should still fall to the validator's '' default"
