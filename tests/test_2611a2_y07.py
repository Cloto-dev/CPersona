"""merge_memories and export: carried-through columns, multi-chunk move counts, and a
move that leaves rows at the source.

Three behaviours the shipped code gets right but that no test would notice breaking:

* A merge copies an episode's ``resolved`` flag and a profile's ``project_id``, and an
  export writes the profile's ``project_id``. Losing the profile's project moves a
  project-scoped profile into the global '' bucket, where every project's reads see it;
  losing ``resolved`` lands every merged episode unresolved.
* A move deletes the copied rows in chunks of ``_MOVE_DELETE_CHUNK`` and reports the
  per-table total. With one chunk, keeping only the last chunk's rowcount and summing
  them agree, so only a move larger than one chunk can tell the two apart.
* A move that leaves a row behind (a skipped locked memory) leaves a live agent: its
  calibration, its remote namespace and its queued pending tasks must all survive, and
  only the moved rows' vectors are removed.
"""

import json

import pytest
import pytest_asyncio

from cpersona import admin_handlers, session, vector
from cpersona.database import get_db

SRC = "y07.src"
DST = "y07.dst"
_TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks")


async def _clear(db) -> None:
    for agent in (SRC, DST):
        for table in _TABLES:
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))  # noqa: S608
    await db.commit()


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    await _clear(conn)
    yield conn
    await _clear(conn)


# ---------------------------------------------------------------------------
# Carried-through columns: episode resolved, profile project_id (merge and export).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_keeps_episode_resolved_flag(db):
    # One resolved and one unresolved episode, so a merge that writes a constant
    # (either 0 or 1) gets one of them wrong.
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, resolved) VALUES (?, 'a finished session', 1)",
        (SRC,),
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, resolved) VALUES (?, 'an open session', 0)",
        (SRC,),
    )
    await db.commit()

    result = await admin_handlers.do_merge_memories(SRC, DST)
    assert result["ok"] and result["merged_episodes"] == 2, result

    rows = await db.execute_fetchall(
        "SELECT summary, resolved FROM episodes WHERE agent_id = ? ORDER BY summary", (DST,)
    )
    assert [(s, int(r)) for s, r in rows] == [
        ("a finished session", 1),
        ("an open session", 0),
    ], f"the merged episodes did not keep their resolved flag: {rows}"


@pytest.mark.asyncio
async def test_merge_keeps_profile_project_id(db):
    await db.execute(
        "INSERT INTO profiles (agent_id, project_id, user_id, content, updated_at)"
        " VALUES (?, 'proj-y07', 'u1', 'a project-scoped profile', '2026-01-01 00:00:00')",
        (SRC,),
    )
    await db.commit()

    result = await admin_handlers.do_merge_memories(SRC, DST)
    assert result["ok"] and result["profile_copied"] is True, result

    rows = await db.execute_fetchall(
        "SELECT project_id FROM profiles WHERE agent_id = ? AND user_id = 'u1'", (DST,)
    )
    assert rows and rows[0][0] == "proj-y07", (
        f"the merged profile left its project and landed in {rows!r}; a '' project_id is "
        "the global bucket every project's reads see"
    )


@pytest.mark.asyncio
async def test_export_then_import_keeps_profile_project_id(db, tmp_path):
    await db.execute(
        "INSERT INTO profiles (agent_id, project_id, user_id, content, updated_at)"
        " VALUES (?, 'proj-y07', 'u1', 'a project-scoped profile', '2026-01-01 00:00:00')",
        (SRC,),
    )
    await db.commit()

    path = str(tmp_path / "y07-export.jsonl")
    exported = await admin_handlers.do_export_memories(SRC, path)
    assert exported["ok"], exported

    with open(path, encoding="utf-8") as fh:
        profiles = [r for r in map(json.loads, fh) if r.get("_type") == "profile"]
    assert [p["project_id"] for p in profiles] == ["proj-y07"], (
        f"the exported profile record does not carry its project: {profiles}"
    )

    # The restore: the row is gone, and the file alone has to put it back where it was.
    await db.execute("DELETE FROM profiles WHERE agent_id = ?", (SRC,))
    await db.commit()
    imported = await admin_handlers.do_import_memories(path)
    assert imported["ok"], imported

    rows = await db.execute_fetchall(
        "SELECT project_id FROM profiles WHERE agent_id = ? AND user_id = 'u1'", (SRC,)
    )
    assert rows and rows[0][0] == "proj-y07", (
        f"the restored profile left its project and landed in {rows!r}"
    )


# ---------------------------------------------------------------------------
# A move larger than one delete chunk reports every deleted row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_over_one_delete_chunk_reports_the_whole_count(db):
    chunk = admin_handlers._MOVE_DELETE_CHUNK
    # One full chunk plus a partial one, so the last chunk's rowcount alone is wrong.
    n_mem = chunk + 200
    n_ep = chunk + 37
    await db.executemany(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, '{}', 't')",
        [(SRC, f"moved memory {i}") for i in range(n_mem)],
    )
    await db.executemany(
        "INSERT INTO episodes (agent_id, summary) VALUES (?, ?)",
        [(SRC, f"moved episode {i}") for i in range(n_ep)],
    )
    await db.commit()

    result = await admin_handlers.do_merge_memories(SRC, DST, mode="move")
    assert result["ok"], result
    assert result["merged_memories"] == n_mem and result["merged_episodes"] == n_ep, result

    deleted = result["source_deleted"]
    assert deleted["deleted_memories"] == n_mem, (
        f"{n_mem} memories moved, {deleted['deleted_memories']} reported deleted: {deleted}"
    )
    assert deleted["deleted_episodes"] == n_ep, (
        f"{n_ep} episodes moved, {deleted['deleted_episodes']} reported deleted: {deleted}"
    )
    # The report is checked against the table, not against itself.
    for table in ("memories", "episodes"):
        left = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM {table} WHERE agent_id = ?", (SRC,)  # noqa: S608
        )
        assert left[0][0] == 0, f"{table} still holds source rows after the move"
    assert result["left_at_source"] == {"memories": 0, "episodes": 0, "profiles": 0}, result


# ---------------------------------------------------------------------------
# A move that leaves a row behind keeps the source agent live.
# ---------------------------------------------------------------------------


class _Response:
    status_code = 200

    def json(self):
        return {}


class _RecordingHttp:
    def __init__(self):
        self.posts = []

    async def post(self, url, json=None, **kwargs):  # noqa: A002
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return _Response()


@pytest.fixture
def remote(monkeypatch):
    http = _RecordingHttp()

    class _Client:
        _http_url = "http://fake.invalid/embed"
        _client = http
        mode = "remote"

        async def embed(self, texts):
            return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(admin_handlers, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _Client())
    return http


async def _seed_partial_move(db) -> int:
    """A source that a move cannot empty, plus one row it does move.

    The locked source memory collides with an unlocked target row, so the copy pass
    skips it and the move leaves it where it is. Returns the id of the row that moves.
    """
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked)"
        " VALUES (?, 'shared content', '{}', 't', 1)",
        (SRC,),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked)"
        " VALUES (?, 'shared content', '{}', 't', 0)",
        (DST,),
    )
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp)"
        " VALUES (?, 'only in source', '{}', 't')",
        (SRC,),
    )
    moved_id = cur.lastrowid
    await db.execute(
        "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES ('store', ?, '{}')",
        (SRC,),
    )
    await db.commit()
    return moved_id


@pytest.mark.asyncio
async def test_partial_move_keeps_the_live_source_intact(db, remote, monkeypatch):
    moved_id = await _seed_partial_move(db)
    monkeypatch.setitem(vector._agent_betas, SRC, 0.37)
    monkeypatch.setitem(vector._agent_thresholds, SRC, 0.61)

    result = await admin_handlers.do_merge_memories(SRC, DST, mode="move")
    assert result["ok"] and result["merged_memories"] == 1 and result["skipped_memories"] == 1, result
    assert result["left_at_source"]["memories"] == 1, result

    src_ns = f"cpersona:{SRC}"
    src_posts = [(ep, body) for ep, body in remote.posts if body.get("namespace") == src_ns]
    purges = [body for ep, body in src_posts if ep == "purge"]
    assert not purges, (
        f"the namespace of a source that still holds a memory was purged: {remote.posts}"
    )
    removed = {i for ep, body in src_posts if ep == "remove" for i in body.get("ids") or []}
    assert removed == {f"mem:{moved_id}"}, (
        f"the moved row's vector was not removed from {src_ns} by id: {remote.posts}"
    )

    assert vector._agent_betas.get(SRC) == 0.37, "the live source's beta was purged"
    assert vector._agent_thresholds.get(SRC) == 0.61, "the live source's threshold was purged"

    assert result["source_deleted"]["deleted_pending_tasks"] == 0, result
    pending = await db.execute_fetchall(
        "SELECT COUNT(*) FROM pending_memory_tasks WHERE agent_id = ?", (SRC,)
    )
    assert pending[0][0] == 1, "the live source's queued pending task was drained"


@pytest.mark.asyncio
async def test_emptying_move_tears_the_source_down(db, remote, monkeypatch):
    """The other side of the same gates: a move that empties the source does drop its
    calibration, its namespace and its queue, so the test above is not passing only
    because the teardown never runs."""
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp)"
        " VALUES (?, 'only in source', '{}', 't')",
        (SRC,),
    )
    assert cur.lastrowid
    await db.execute(
        "INSERT INTO pending_memory_tasks (task_type, agent_id, payload) VALUES ('store', ?, '{}')",
        (SRC,),
    )
    await db.commit()
    monkeypatch.setitem(vector._agent_betas, SRC, 0.37)

    result = await admin_handlers.do_merge_memories(SRC, DST, mode="move")
    assert result["ok"] and not any(result["left_at_source"].values()), result

    purges = [b for ep, b in remote.posts if ep == "purge" and b.get("namespace") == f"cpersona:{SRC}"]
    assert purges, f"an emptied source's namespace was not purged: {remote.posts}"
    assert SRC not in vector._agent_betas, "an emptied source kept its calibration"
    assert result["source_deleted"]["deleted_pending_tasks"] == 1, result
