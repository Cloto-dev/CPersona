"""Regression tests for six health and deep-check defects.

  msg_id repair      The dedup index repair keeps one row per colliding
                     (agent_id, project_id, msg_id) group. Keeping the newest
                     row even when an older one is locked left both standing:
                     the locked row may not be blanked, the newest was kept,
                     and the CREATE failed on every run.
  fresh connection   quick_check is confirmed on a connection opened from a
                     SQLite URI. A database path holding '%' or '?' has to be
                     escaped into that URI, or the confirmation never opens and
                     a stale-cache false positive stands as a critical finding.
  query failure      invalid_source_type and anonymous_source let a failed
                     count propagate, so the runner reports a crashed check
                     instead of a clean corpus.
  speaker prefix     deep_check anonymous_source recovers a speaker only from a
                     bracket that can hold a name: not a checkbox, a date, a log
                     level or a label CPersona itself writes.
  scan cap           A deep scan that read exactly the cap read everything, and
                     says so.
  index scope        An agent-scoped vector_index / coarse_index finding reports
                     that agent's rows, not every agent's.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x08.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import json  # noqa: E402

import aiosqlite  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import checks, coarse_index, config, database, vector, vector_index  # noqa: E402
from cpersona.database import get_db  # noqa: E402


@pytest_asyncio.fixture
async def private_db(tmp_path):
    """A database of this test's own, so dropping an index or seeding collisions
    cannot leak into the connection the rest of the suite shares."""
    conn = await aiosqlite.connect(str(tmp_path / "private.db"))
    await database._init_schema(conn)
    yield conn
    await conn.close()


async def _insert_memory(db, agent, content, *, msg_id="", locked=0, source="{}"):
    cur = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " msg_id, locked) VALUES (?, '', '', ?, ?, '2026-03-01T00:00:00+00:00', ?, ?)",
        (agent, content, source, msg_id, locked),
    )
    await db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# msg_id repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_msg_id_repair_keeps_the_locked_collider_and_blanks_the_newer_one(private_db):
    db = private_db
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_msg_id")
    locked_id = await _insert_memory(db, "x08.msgid", "older, locked", msg_id="m1", locked=1)
    newer_id = await _insert_memory(db, "x08.msgid", "newer, unlocked", msg_id="m1")
    assert newer_id > locked_id

    [issue] = await checks.check_dedup_msg_id_index(db, "", True)
    await db.commit()

    assert issue["fixed"] is True, issue
    rows = await db.execute_fetchall("SELECT id, msg_id, locked FROM memories ORDER BY id")
    # The locked row is untouched (bug-098); the unlocked newer row gave up its
    # msg_id, which is the one write that lets the index exist.
    assert rows == [(locked_id, "m1", 1), (newer_id, "", 0)], rows
    assert await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memories_dedup_msg_id'"
    )


@pytest.mark.asyncio
async def test_msg_id_repair_still_keeps_the_newest_when_nothing_is_locked(private_db):
    """Control: without a lock the survivor is still the newest row."""
    db = private_db
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_msg_id")
    first = await _insert_memory(db, "x08.msgid", "first", msg_id="m1")
    second = await _insert_memory(db, "x08.msgid", "second", msg_id="m1")
    third = await _insert_memory(db, "x08.msgid", "third", msg_id="m1")
    other_agent = await _insert_memory(db, "x08.msgid.other", "same msg_id, other agent", msg_id="m1")

    [issue] = await checks.check_dedup_msg_id_index(db, "", True)
    await db.commit()

    assert issue["fixed"] is True, issue
    rows = dict(await db.execute_fetchall("SELECT id, msg_id FROM memories"))
    assert rows == {first: "", second: "", third: "m1", other_agent: "m1"}, rows


@pytest.mark.asyncio
async def test_msg_id_repair_with_two_locked_colliders_refuses_without_touching_them(private_db):
    """Two locked rows cannot both keep the msg_id; neither may be rewritten, so the
    CREATE fails and says so -- but the unlocked collider is still released."""
    db = private_db
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_msg_id")
    a = await _insert_memory(db, "x08.msgid", "locked a", msg_id="m1", locked=1)
    b = await _insert_memory(db, "x08.msgid", "unlocked b", msg_id="m1")
    c = await _insert_memory(db, "x08.msgid", "locked c", msg_id="m1", locked=1)

    [issue] = await checks.check_dedup_msg_id_index(db, "", True)
    await db.commit()

    assert issue["fixed"] is False and "UNIQUE" in issue["fix_error"], issue
    rows = dict(await db.execute_fetchall("SELECT id, msg_id FROM memories"))
    assert rows == {a: "m1", b: "", c: "m1"}, rows


# ---------------------------------------------------------------------------
# fresh connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", ["pct%41", "q?mark", "hash#x", "sp ace"])
async def test_quick_check_confirmation_opens_a_path_that_needs_escaping(
    tmp_path, monkeypatch, directory
):
    path = tmp_path / directory / "cpersona.db"
    path.parent.mkdir()
    conn = await aiosqlite.connect(str(path))
    await conn.execute("CREATE TABLE t (x)")
    await conn.commit()
    await conn.close()
    monkeypatch.setattr(database, "DB_PATH", str(path))

    # None is "could not open a confirming connection", which keeps a stale
    # cache's false positive as a critical finding.
    assert await checks._quick_check_on_a_fresh_connection() == ["ok"]
    # An unescaped '?' cuts the path short: SQLite then opens (and, since the
    # mode parameter went with the cut, creates) a different file, whose
    # quick_check vouches for nothing.
    assert sorted(p.name for p in tmp_path.iterdir()) == [directory]


@pytest.mark.asyncio
async def test_quick_check_confirmation_does_not_open_the_decoded_path(tmp_path, monkeypatch):
    """'%41' is 'A' once decoded. The confirmation must check the file the
    server uses, not a sibling whose name is the decoded spelling."""
    real = tmp_path / "pct%41" / "cpersona.db"
    real.parent.mkdir()
    decoded = tmp_path / "pctA" / "cpersona.db"
    decoded.parent.mkdir()
    decoded.write_bytes(b"this is not a database, and it is not the one in use")
    conn = await aiosqlite.connect(str(real))
    await conn.execute("CREATE TABLE t (x)")
    await conn.commit()
    await conn.close()
    monkeypatch.setattr(database, "DB_PATH", str(real))

    assert await checks._quick_check_on_a_fresh_connection() == ["ok"]


@pytest.mark.asyncio
async def test_quick_check_confirmation_declines_an_in_memory_database(monkeypatch):
    """A second ':memory:' connection is a new, empty database; its 'ok' would
    clear a finding about the one in use."""
    monkeypatch.setattr(database, "DB_PATH", ":memory:")
    assert await checks._quick_check_on_a_fresh_connection() is None


# ---------------------------------------------------------------------------
# query failure
# ---------------------------------------------------------------------------


class _FailingReads:
    """A connection whose every read fails the way a locked or damaged store does."""

    def __init__(self, inner):
        self._inner = inner

    async def execute_fetchall(self, sql, params=()):
        raise aiosqlite.OperationalError("database is locked")

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("check_name", ["invalid_source_type", "anonymous_source"])
async def test_a_failed_count_is_reported_as_a_crashed_check(private_db, check_name):
    issues, summary = await checks.run_health_checks(
        _FailingReads(private_db), "x08.failing", False, checks=[check_name]
    )
    # A failure returned as [] is a verified-clean answer for an axis that was
    # never evaluated; the runner can only say otherwise if the error reaches it.
    assert [(i["type"], i.get("check_name")) for i in issues] == [("check_crashed", check_name)], issues
    assert "database is locked" in issues[0]["detail"]
    assert summary["warn"] == 1


# ---------------------------------------------------------------------------
# speaker prefix
# ---------------------------------------------------------------------------

_ANON = json.dumps({"type": "User", "id": "", "name": ""})

_NOT_SPEAKERS = [
    "[x] renew the passport before november",
    "[X] done item",
    "[ ] open item",
    "[2026-10-01] standup: nothing blocked",
    "[2026-10-01T09:00Z] standup in the morning",
    "[12:30] lunch",
    "[1] see the footnote",
    "[WARN] disk on the build host is at 91%",
    "[error] build failed",
    "[Episode] notes copied from the retro",
    "[Profile] prefers tea",
    "[Memory from bob] a recalled note",
    "[!NOTE] an admonition",
]

_SPEAKERS = {
    "[alice] I prefer tea in the morning": "alice",
    "[Bob Smith] meeting moved": "Bob Smith",
    "[田中] こんにちは": "田中",
    "[user_42] hi": "user_42",
}


@pytest.mark.asyncio
async def test_deep_anonymous_source_recovers_names_but_not_other_brackets(private_db):
    db = private_db
    agent = "x08.speaker"
    ids = {}
    for content in [*_NOT_SPEAKERS, *_SPEAKERS]:
        ids[content] = await _insert_memory(db, agent, content, source=_ANON)

    result = await checks.deep_anonymous_source(db, agent, True)
    await db.commit()

    recovered = {
        content: json.loads(src)["name"]
        for content, src in await db.execute_fetchall(
            "SELECT content, source FROM memories WHERE agent_id = ?", (agent,)
        )
        if json.loads(src)["name"]
    }
    assert recovered == _SPEAKERS, recovered
    assert result["recoverable"] == len(_SPEAKERS), result
    assert result["unrecoverable"] == len(_NOT_SPEAKERS), result
    assert result["fixed"] == len(_SPEAKERS), result


# ---------------------------------------------------------------------------
# scan cap
# ---------------------------------------------------------------------------

_CAP = 3


async def _seed_scan_rows(db, agent, count):
    for n in range(count):
        blob = np.full(4, 0.5, dtype=np.float32).tobytes()  # a unit vector
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " embedding) VALUES (?, '', '', ?, '{}', '2026-03-01T00:00:00+00:00', ?)",
            (agent, f"row {n}", blob),
        )
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, embedding)"
            " VALUES (?, ?, '', '2026-03-01T00:00:00+00:00', '2026-03-01T00:00:00+00:00', ?)",
            (agent, f"episode {n}", blob),
        )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deep", "cap_name"),
    [
        (checks.deep_unnormalized_content, "NORMALIZATION_SCAN_CAP"),
        (checks.deep_embedding_norm, "_NORM_SAMPLE_ROWS"),
    ],
)
async def test_a_scan_of_exactly_the_cap_is_complete(private_db, monkeypatch, deep, cap_name):
    monkeypatch.setattr(checks, cap_name, _CAP)
    await _seed_scan_rows(private_db, "x08.scan", _CAP)
    result = await deep(private_db, "x08.scan", False)
    assert result["rows_scanned"] == {"memories": _CAP, "episodes": _CAP}, result
    assert result["complete"] is True, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deep", "cap_name"),
    [
        (checks.deep_unnormalized_content, "NORMALIZATION_SCAN_CAP"),
        (checks.deep_embedding_norm, "_NORM_SAMPLE_ROWS"),
    ],
)
async def test_a_scan_past_the_cap_is_incomplete_and_reads_only_the_cap(
    private_db, monkeypatch, deep, cap_name
):
    monkeypatch.setattr(checks, cap_name, _CAP)
    await _seed_scan_rows(private_db, "x08.scan", _CAP + 1)
    result = await deep(private_db, "x08.scan", False)
    assert result["rows_scanned"] == {"memories": _CAP, "episodes": _CAP}, result
    assert result["complete"] is False, result


# ---------------------------------------------------------------------------
# index scope
# ---------------------------------------------------------------------------

AGENT_A = "x08.index.a"
AGENT_B = "x08.index.b"
DIM = 8
WINDOW = 10


def _blob(seed: int) -> bytes:
    return np.random.default_rng(seed).standard_normal(DIM).astype(np.float32).tobytes()


async def _insert_embedded(db, agent, count, *, start):
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, '{}', ?, ?, ?)",
        [
            (
                agent, f"row {agent} {start + n}", "2026-03-01T00:00:00+00:00",
                f"2026-03-01 {(start + n) // 3600 % 24:02d}:{(start + n) // 60 % 60:02d}"
                f":{(start + n) % 60:02d}",
                _blob(start + n),
            )
            for n in range(count)
        ],
    )
    await db.commit()


def _clean_indexes():
    for path in (vector_index.index_path("memories"), coarse_index.index_path("memories")):
        for p in (path, path + ".tmp"):
            if os.path.exists(p):
                os.unlink(p)


@pytest_asyncio.fixture
async def shared_db(monkeypatch):
    monkeypatch.setattr(vector, "MAX_MEMORIES", WINDOW)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 5)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")
    conn = await get_db()
    _clean_indexes()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    _clean_indexes()


@pytest.mark.asyncio
async def test_agent_scoped_vector_index_finding_counts_that_agents_rows(shared_db):
    db = shared_db
    await _insert_embedded(db, AGENT_A, 10, start=0)
    await _insert_embedded(db, AGENT_B, 40, start=100)
    await vector_index.start_logging()
    await vector_index.build_index(db, "memories")
    await _insert_embedded(db, AGENT_A, 11, start=1000)  # 11 > 50 * INDEX_TAIL_RATIO

    [issue] = await checks.check_vector_index(db, AGENT_A, False)
    assert issue["type"] == "vector_index_tail_grown"
    # Ten of the fifty indexed rows are A's; the other forty are a size of B's
    # corpus that a caller holding read on A alone has no grant to learn.
    assert issue["indexed_rows"] == 10, issue
    assert issue["rows_past_watermark"] == issue["rows_read_exactly"] == 11, issue

    # Control: the operator's whole-store call still reports the whole file.
    [whole] = await checks.check_vector_index(db, "", False)
    assert whole["indexed_rows"] == 50, whole


@pytest.mark.asyncio
async def test_agent_scoped_coarse_index_finding_counts_that_agents_rows(shared_db):
    db = shared_db
    await _insert_embedded(db, AGENT_A, 20, start=0)
    await _insert_embedded(db, AGENT_B, 40, start=100)
    await vector_index.start_logging()
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]
    await _insert_embedded(db, AGENT_A, 13, start=1000)  # 13 > 60 * INDEX_TAIL_RATIO
    await _insert_embedded(db, AGENT_B, 5, start=2000)

    [issue] = await checks.check_coarse_index(db, AGENT_A, False)
    assert issue["type"] == "coarse_index_tail_grown"
    assert issue["indexed_rows"] == 20, issue
    assert issue["rows_read_exactly"] == 13, issue

    [whole] = await checks.check_coarse_index(db, "", False)
    assert whole["indexed_rows"] == 60 and whole["rows_read_exactly"] == 18, whole
