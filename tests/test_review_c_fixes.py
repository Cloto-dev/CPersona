"""Health-check and repair-path correctness fixes (review batch C).

One theme runs through the batch: a maintenance run must never leave the
database — or the operator's picture of it — worse than it found it. Each test
below fails on the pre-fix code.

  bug-224  a drifted UNIQUE index is not dropped before it is known to be
           re-creatable, and a failed CREATE restores what it dropped
  bug-225  fix-outcome fields survive to the do_check_health caller
  bug-226  the LIKE detector and the regex repair express one predicate
  bug-227  deep_anonymous_source survives a legacy non-JSON source row
  bug-228  its `fixed` counts writes, not intentions
  bug-229  the invalid-timestamp repair writes the canonical aware form
  bug-230  an unknown check name is rejected instead of reported healthy
  bug-236  empty_content's count is agent-scoped like its DELETE
  bug-238  a scalar `doctrine` leaves the sidecar dormant, not the server dead
  bug-239  malformed metadata is a counted remainder, not an abort
  bug-240  a notification gets no JSON-RPC reply
  bug-243  a scoped check_health does not disclose other agents' buckets
  bug-245  the episode start_time backfill writes the canonical aware form
"""

import datetime
import json

import pytest
import pytest_asyncio

from cpersona import checks, maintenance_handlers, operating_context, proxy_stdio
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

AGENT = "review-c"
OTHER = "review-c-other"
UTC_TS = "2026-08-01T00:00:00+00:00"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


async def _mem(
    conn,
    content,
    *,
    agent_id=AGENT,
    locked=0,
    source="{}",
    metadata="{}",
    timestamp=UTC_TS,
    channel="",
    embedding=None,
    created_at="2026-08-10 12:00:00",
):
    cur = await conn.execute(
        """INSERT INTO memories
           (agent_id, content, source, metadata, timestamp, locked, channel, embedding, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, content, source, metadata, timestamp, locked, channel, embedding, created_at),
    )
    await conn.commit()
    return cur.lastrowid


async def _object_sql(conn, name):
    rows = await conn.execute_fetchall(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    )
    return rows[0][0] if rows else None


# ---------------------------------------------------------------------------
# bug-224 — check_schema_objects must not drop what it cannot recreate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug224_drifted_unique_index_is_kept_when_duplicates_block_the_create(db):
    """A UNIQUE index the corpus cannot admit is refused, not dropped.

    Pre-fix the repair issued the DROP first and only then discovered the
    CREATE could not succeed, so the run committed a database with no dedup
    index at all — strictly worse than the drift it was repairing.
    """
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_content")
    await db.execute(
        "CREATE INDEX idx_memories_dedup_content "
        "ON memories(agent_id, project_id, channel, content)"
    )
    await db.commit()
    # Two locked rows the canonical UNIQUE index cannot admit.
    await _mem(db, "same content", locked=1)
    await _mem(db, "same content", locked=1)

    issues = await checks.check_schema_objects(db, AGENT, fix=True)
    drift = [i for i in issues if i["object"] == "idx_memories_dedup_content"]
    assert drift, "the drifted index must still be reported"
    assert drift[0]["fixed"] is False
    assert "duplicate" in drift[0]["fix_error"], drift[0]["fix_error"]

    surviving = await _object_sql(db, "idx_memories_dedup_content")
    assert surviving is not None, (
        "the repair dropped an index it could not recreate — the write-time dedup "
        "guarantee this check calls critical was destroyed by its own fix"
    )
    assert "UNIQUE" not in surviving.upper(), "the drifted definition is what survives"


@pytest.mark.asyncio
async def test_bug224_failed_create_restores_the_previous_definition(db, monkeypatch):
    """When the CREATE fails after a DROP, the captured definition goes back."""
    monkeypatch.setitem(
        checks._EXPECTED_OBJECTS,
        "idx_memories_agent",
        {
            "kind": "index",
            "severity": "warn",
            # Canonical DDL that cannot be created: the column does not exist.
            "sql": "CREATE INDEX idx_memories_agent ON memories(no_such_column)",
        },
    )
    before = await _object_sql(db, "idx_memories_agent")
    assert before is not None

    issues = await checks.check_schema_objects(db, AGENT, fix=True)
    drift = [i for i in issues if i["object"] == "idx_memories_agent"][0]
    assert drift["fixed"] is False
    assert drift["fix_error"], "the CREATE failure must be reported"
    assert drift.get("restored") is True and "restore_error" not in drift

    assert await _object_sql(db, "idx_memories_agent") == before, (
        "a failed CREATE after a DROP must put the previous definition back"
    )


# ---------------------------------------------------------------------------
# bug-225 — do_check_health(fix=True) keeps the fix run's outcome fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug225_fix_outcome_fields_reach_the_do_check_health_caller(db, monkeypatch):
    """`fixed` / `mapped` / `remaining` must survive the residual re-run.

    The MCP tool and the checkup CLI are the only surfaces operators use, and
    both read this envelope. Pre-fix it was rebound to the fix=False re-run, so
    a capped repair (bug-210's `remaining`) and a failed one (`fix_error`) were
    indistinguishable from a repair that never ran.
    """
    monkeypatch.setattr(checks, "INVALID_SOURCE_CLASSIFY_CAP", 2)
    for i in range(5):
        await _mem(db, f"mappable {i}", source='{"type":"assistant"}')

    result = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["invalid_source_type"]
    )
    issue = [i for i in result["issues"] if i["type"] == "invalid_source_type"][0]
    assert issue["mapped"] == 2
    assert issue["remaining"] == 3, "a capped run must not read as convergence"
    assert "run fix again" in issue["hint"]
    # The verdict still comes from the residual state (bug-059).
    assert result["severity_summary"]["warn"] >= 1
    assert result["status"] == "degraded"


# ---------------------------------------------------------------------------
# bug-226 — detection and repair are one predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug226_like_only_match_keeps_its_embedding_and_is_not_repairable(db):
    """A row the regex does not rewrite is not counted, not written, not blanked.

    Pre-fix both rows were rewritten to byte-identical text with
    `embedding = NULL` on every run: the finding never cleared and each
    maintenance pass destroyed a vector for content that did not change.
    """
    blob = b"\x00" * 16
    await _mem(db, "discussing the [Memory from format literally", embedding=blob)
    await _mem(db, "ping <@&9999> the role", embedding=blob)
    # ...and one genuine match of each, which must still be repaired.
    await _mem(db, "[Memory from bob] hello", embedding=blob)
    await _mem(db, "<@1234> hi there", embedding=blob)

    annotation = (await checks.check_memory_annotation(db, AGENT, fix=True))[0]
    mention = (await checks.check_discord_mention(db, AGENT, fix=True))[0]
    assert annotation["count"] == 2 and annotation["repairable"] == 1
    assert mention["count"] == 2 and mention["repairable"] == 1

    rows = await db.execute_fetchall(
        "SELECT content, embedding FROM memories ORDER BY id"
    )
    kept = {r[0]: r[1] for r in rows}
    assert kept["discussing the [Memory from format literally"] == blob, (
        "a no-op rewrite must not NULL the embedding"
    )
    assert kept["ping <@&9999> the role"] == blob
    assert "hello" in kept and kept["hello"] is None, "the genuine match is still repaired"
    assert "hi there" in kept and kept["hi there"] is None


# ---------------------------------------------------------------------------
# bug-227 / bug-228 — deep_anonymous_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug227_malformed_source_does_not_abort_deep_anonymous_source(db):
    """One legacy non-JSON source row must not take the whole deep check down."""
    await _mem(db, "legacy row", source="not json at all")
    await _mem(db, "[alice] hello", source=json.dumps({"type": "User", "id": "", "name": ""}))

    result = await checks.deep_anonymous_source(db, AGENT, fix=True)

    assert result["recoverable"] == 1 and result["fixed"] == 1
    rows = await db.execute_fetchall(
        "SELECT source FROM memories WHERE content = '[alice] hello'"
    )
    assert json.loads(rows[0][0])["name"] == "alice", (
        "the recoverable row is repaired instead of the scan raising 'malformed JSON', "
        "which do_deep_check converts to an {'error': ...} the checkup filter hides"
    )


@pytest.mark.asyncio
async def test_bug228_locked_recoverable_row_is_not_counted_as_fixed(db):
    """`fixed` counts UPDATE rowcounts; the locked remainder is reported."""
    await _mem(
        db,
        "[alice] hello",
        locked=1,
        source=json.dumps({"type": "User", "id": "", "name": ""}),
    )

    result = await checks.deep_anonymous_source(db, AGENT, fix=True)

    assert result["recoverable"] == 1
    assert result["fixed"] == 0, "the UPDATE carries `locked = 0` and wrote nothing"
    assert result["unfixable_locked"] == 1, "recoverable = fixed + locked must reconcile"
    rows = await db.execute_fetchall(
        "SELECT source FROM memories WHERE content = '[alice] hello'"
    )
    assert json.loads(rows[0][0])["name"] == ""


# ---------------------------------------------------------------------------
# bug-229 / bug-245 — repairs write the canonical aware form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug229_invalid_timestamp_repair_is_not_flagged_as_naive_drift(db):
    """The repair must not mint the drift the very next check refuses to fix."""
    await _mem(db, "good row", timestamp=UTC_TS)
    await _mem(db, "bad row", timestamp="garbage", created_at="2026-08-10 12:00:00")

    await checks.check_invalid_timestamp(db, AGENT, fix=True)

    rows = await db.execute_fetchall(
        "SELECT timestamp FROM memories WHERE content = 'bad row'"
    )
    written = rows[0][0]
    assert written == "2026-08-10T12:00:00+00:00"
    assert datetime.datetime.fromisoformat(written).tzinfo is not None

    drift = await checks.check_timestamp_format_drift(db, AGENT, fix=False)
    assert drift == [], (
        "the repaired row was classified 'naive' — a permanently unfixable finding "
        f"manufactured by a sibling repair: {drift}"
    )


@pytest.mark.asyncio
async def test_bug245_episode_start_time_backfill_writes_an_aware_timestamp(db):
    """`episodes.start_time` holds one lexical convention after the backfill."""
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, start_time, created_at) VALUES (?, ?, ?, ?)",
        (AGENT, "no start", None, "2026-08-10 12:00:00"),
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, start_time, created_at) VALUES (?, ?, ?, ?)",
        (AGENT, "recorded start", "2026-08-01T09:00:00+00:00", "2026-08-10 12:00:00"),
    )
    await db.commit()

    await checks.check_missing_episode_start_time(db, AGENT, fix=True)

    rows = await db.execute_fetchall("SELECT summary, start_time FROM episodes ORDER BY id")
    by_summary = dict(rows)
    assert by_summary["no start"] == "2026-08-10T12:00:00+00:00"
    for summary, start in by_summary.items():
        assert datetime.datetime.fromisoformat(start).tzinfo is not None, (
            f"{summary!r} holds a naive spelling; ' ' < 'T' inverts string ordering "
            "against the recorded rows and no health check covers episodes"
        )


# ---------------------------------------------------------------------------
# bug-230 — an unknown check name is a caller error, not a clean bill of health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug230_unknown_check_name_is_rejected(db):
    await _mem(db, "   ")  # something a real run would report

    result = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=False, checks=["empty_contnet"]
    )

    assert result["ok"] is False
    assert "empty_contnet" in result["error"]
    assert "empty_content" in result["error"], "the valid names must be named"
    assert result["unknown_checks"] == ["empty_contnet"]
    assert "status" not in result, "a rejected call must not answer with a verdict"


@pytest.mark.asyncio
async def test_bug230_a_served_run_echoes_the_checks_it_ran(db):
    await _mem(db, "   ")

    subset = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=False, checks=["empty_content"]
    )
    assert subset["checks_run"] == ["empty_content"]

    full = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)
    assert full["checks_run"] == checks.HEALTH_CHECK_NAMES


# ---------------------------------------------------------------------------
# bug-236 — empty_content counts what it can delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug236_empty_content_count_is_agent_scoped(db):
    """AND binds tighter than OR: the unparenthesised count went corpus-wide.

    A scoped fix run could then never drive its own finding to zero — the count
    kept reporting another agent's rows, which this call is not allowed to touch.
    """
    await _mem(db, "   ")
    await _mem(db, "  ", agent_id=OTHER)
    await _mem(db, " ", agent_id=OTHER)

    mine = (await checks.check_empty_content(db, AGENT, fix=False))[0]
    assert mine["count"] == 1 and mine["repairable"] == 1

    assert await checks.check_empty_content(db, AGENT, fix=True) == [] or True
    assert await checks.check_empty_content(db, AGENT, fix=False) == [], (
        "a scoped fix run must converge instead of reporting other agents' rows"
    )
    # The other agent's rows are untouched and still visible on a global sweep.
    corpus = (await checks.check_empty_content(db, "", fix=False))[0]
    assert corpus["count"] == 2


# ---------------------------------------------------------------------------
# bug-243 — a scoped run does not disclose other agents' buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug243_scoped_axis_hygiene_does_not_leak_other_agents_buckets(db):
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) VALUES (?, ?, ?, ?)",
        (OTHER, "mizprism", "theirs a", UTC_TS),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) VALUES (?, ?, ?, ?)",
        (OTHER, "MIZPRISM", "theirs b", UTC_TS),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) VALUES (?, ?, ?, ?)",
        (AGENT, "kirari-site", "mine", UTC_TS),
    )
    await db.commit()

    scoped = await checks.check_axis_hygiene(db, AGENT, fix=False)
    assert scoped == [], (
        "a per-agent check_health returned another agent's project_id names and "
        f"row counts in `issues`: {scoped}"
    )

    sweep = await checks.check_axis_hygiene(db, "", fix=False)
    assert sweep and sweep[0]["type"] == "project_id_naming_drift", (
        "the CLI global sweep must still see cross-bucket drift"
    )


@pytest.mark.asyncio
async def test_bug243_scoped_run_still_reports_the_agents_own_drift(db):
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) VALUES (?, ?, ?, ?)",
        (AGENT, "data-ops", "mine a", UTC_TS),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) VALUES (?, ?, ?, ?)",
        (AGENT, "dataops", "mine b", UTC_TS),
    )
    await db.commit()

    scoped = await checks.check_axis_hygiene(db, AGENT, fix=False)
    spellings = {m["project_id"] for cluster in scoped[0]["clusters"] for m in cluster}
    assert spellings == {"data-ops", "dataops"}


# ---------------------------------------------------------------------------
# bug-239 — migrate_channel_axis survives malformed metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug239_malformed_metadata_is_a_counted_remainder_not_an_abort(db):
    """json_extract RAISES on non-JSON metadata; one locked row killed the tool.

    Both the dry run — whose whole purpose is to report before mutating — and
    the real run went down with `sqlite3.OperationalError: malformed JSON`.
    """
    await _mem(db, "recoverable", channel="discord", metadata='{"session_id":"123:9:0"}')
    bad_id = await _mem(db, "malformed", channel="discord", metadata="", locked=1)

    dry = await maintenance_handlers.do_migrate_channel_axis(agent_id=AGENT, dry_run=True)
    assert dry["recoverable_total"] == 1
    assert dry["invalid_metadata_total"] == 1
    assert dry["invalid_metadata_ids"] == [bad_id]
    assert dry["unrecoverable_total"] == 1

    real = await maintenance_handlers.do_migrate_channel_axis(agent_id=AGENT, dry_run=False)
    assert real["migrated"] == 1
    rows = await db.execute_fetchall(
        "SELECT content, channel FROM memories ORDER BY id"
    )
    by_content = dict(rows)
    assert by_content["recoverable"] == "123"
    assert by_content["malformed"] == "discord", "the malformed row is left where it was"


@pytest.mark.asyncio
async def test_bug239_globalize_still_sweeps_the_unrecoverable_bucket(db):
    """The complement predicate keeps its NULL-session_id and malformed members."""
    await _mem(db, "no session", channel="discord", metadata='{"other":"x"}')
    await _mem(db, "malformed", channel="discord", metadata="{oops")
    await _mem(db, "recoverable", channel="discord", metadata='{"session_id":"777:1:0"}')

    result = await maintenance_handlers.do_migrate_channel_axis(
        agent_id=AGENT, dry_run=False, globalize_unrecoverable=True
    )
    assert result["migrated"] == 1
    assert result["globalized"] == 2

    rows = await db.execute_fetchall("SELECT content, channel FROM memories ORDER BY id")
    by_content = dict(rows)
    assert by_content["no session"] == "" and by_content["malformed"] == ""
    assert by_content["recoverable"] == "777"


# ---------------------------------------------------------------------------
# bug-238 — a config typo leaves the feature dormant, never the server dead
# ---------------------------------------------------------------------------


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    path = tmp_path / "operating-context.toml"

    def write(text: str) -> str:
        path.write_text(text, encoding="utf-8")
        return str(path)

    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "on")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(path))
    return write


@pytest.mark.parametrize("value", ["5", "true", '"string"'])
@pytest.mark.asyncio
async def test_bug238_scalar_doctrine_is_dormant_with_a_health_finding(sidecar, value):
    """`doctrine = 5` reached enumerate() and raised TypeError past the guard.

    server.py calls instructions_text() at module scope, so the typo escaped at
    import and the server never started — with no operating_context_parse
    finding anywhere, because check_health could not run either.
    """
    sidecar(f'version = 1\ndoctrine = {value}\n')

    assert operating_context.get_context() is None
    assert operating_context.instructions_text() is None
    state = operating_context.load_state()
    assert state["present"] and state["parse_error"]

    found = await checks.check_operating_context_parse(None, "", False)
    assert found and found[0]["type"] == "operating_context_parse_error"


def test_bug238_get_context_degrades_on_any_unchecked_shape(sidecar, monkeypatch):
    """The dormancy guard must not depend on _parse being exhaustive."""
    sidecar("version = 1\n")

    def _boom(raw):
        raise TypeError("'int' object is not iterable")

    monkeypatch.setattr(operating_context, "_parse", _boom)
    monkeypatch.setattr(operating_context, "_cached_key", None)

    assert operating_context.get_context() is None
    assert operating_context.load_state()["parse_error"]


# ---------------------------------------------------------------------------
# bug-240 — the proxy never answers a notification
# ---------------------------------------------------------------------------


def test_bug240_notification_gets_no_jsonrpc_reply(monkeypatch):
    """JSON-RPC 2.0: a server MUST NOT reply to a notification.

    The `id: null` error belongs to bug-135's unparseable line, where the client
    IS waiting; sending it for a fire-and-forget notification hands the client a
    response for a request it never made.
    """
    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)

    proxy_stdio._write_error(
        '{"jsonrpc":"2.0","method":"notifications/initialized"}', "remote failed"
    )

    assert written == [], f"a notification was answered: {written}"


@pytest.mark.parametrize(
    ("request_line", "expected_id"),
    [
        ('{"jsonrpc":"2.0","id":42,"method":"tools/call"}', 42),
        ('{"jsonrpc":"2.0","id":null,"method":"tools/call"}', None),
        ("{malformed", None),
        ('[{"jsonrpc":"2.0","id":1}]', None),
    ],
)
def test_bug240_requests_still_receive_their_error(monkeypatch, request_line, expected_id):
    """Everything a client can be waiting on still gets an answer."""
    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)

    proxy_stdio._write_error(request_line, "remote failed")

    assert len(written) == 1
    assert json.loads(written[0]) == {
        "jsonrpc": "2.0",
        "id": expected_id,
        "error": {"code": -32000, "message": "remote failed"},
    }
