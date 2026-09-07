"""Tests for the v2.4.37 check registry, severity model and checkup CLI.

Fixture round-trip pattern: a healthy fixture must produce zero issues (warn
included), and each deliberately broken fixture must fire exactly the check
that owns that failure class. The golden-DDL test pins checks._EXPECTED_OBJECTS
to what database.py actually creates, so the two definitions cannot drift.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest
import pytest_asyncio

# Hermetic DB + embeddings-off before importing any cpersona module.
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_v2437.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import session  # noqa: E402
from cpersona import checks  # noqa: E402
from cpersona import checkup  # noqa: E402
from cpersona import database  # noqa: E402
from cpersona import maintenance_handlers  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402
from cpersona.database import get_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.execute("DELETE FROM profiles")
    await db.execute("DELETE FROM pending_memory_tasks")
    await db.commit()
    saved_client = vector._embedding_client
    vector._embedding_client = None
    yield
    vector._embedding_client = saved_client
    session.reset_pauses_for_tests()


async def _insert(db, agent_id="agent-h", content="fine content", **cols):
    defaults = {
        "source": '{"type":"User","id":"u","name":"n"}',
        "timestamp": "2026-07-01T00:00:00+00:00",
        "channel": "",
        "project_id": "",
    }
    defaults.update(cols)
    keys = ["agent_id", "content", *defaults.keys()]
    sql = f"INSERT INTO memories ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})"
    cur = await db.execute(sql, (agent_id, content, *defaults.values()))
    await db.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------
# healthy fixture → zero issues (strict: warn counts as failure here)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_fixture_has_zero_issues():
    db = await get_db()
    await _insert(db, content="a perfectly ordinary memory")
    await db.execute(
        "INSERT INTO profiles (agent_id, content) VALUES ('agent-h', 'profile text')"
    )
    await db.commit()

    result = await maintenance_handlers.do_check_health()

    # mode=none: null embeddings are info (expected steady state), never warn.
    non_info = [i for i in result["issues"] if i["severity"] != "info"]
    assert non_info == []
    assert result["severity_summary"]["critical"] == 0
    assert result["severity_summary"]["warn"] == 0


# --------------------------------------------------------------------------
# golden DDL — pins _EXPECTED_OBJECTS to database.py reality
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_golden_ddl_matches_fresh_database():
    db = await get_db()
    issues = await checks.check_schema_objects(db, "", fix=False)
    assert issues == []  # every expected object exists with the canonical definition


# --------------------------------------------------------------------------
# schema_objects — the v12 silent-fail class
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_dedup_index_is_critical_and_fixable():
    db = await get_db()
    await db.execute("DROP INDEX idx_memories_dedup_content")
    await db.commit()

    issues = await checks.check_schema_objects(db, "", fix=False)
    assert any(
        i["object"] == "idx_memories_dedup_content"
        and i["state"] == "missing"
        and i["severity"] == "critical"
        for i in issues
    )

    fixed = await checks.check_schema_objects(db, "", fix=True)
    await db.commit()
    assert any(i.get("fixed") is True for i in fixed)
    assert await checks.check_schema_objects(db, "", fix=False) == []


@pytest.mark.asyncio
async def test_drifted_trigger_definition_is_detected_and_restored():
    db = await get_db()
    await db.execute("DROP TRIGGER memories_fts_au")
    # A plausible-but-wrong replacement: delete-only (the pre-v11 bug shape).
    await db.execute(
        """CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
           INSERT INTO memories_fts(memories_fts, rowid, content)
           VALUES ('delete', old.id, old.content); END"""
    )
    await db.commit()

    issues = await checks.check_schema_objects(db, "", fix=True)
    await db.commit()
    drift = [i for i in issues if i["object"] == "memories_fts_au"]
    assert drift and drift[0]["state"] == "definition_drift" and drift[0]["severity"] == "critical"
    assert drift[0].get("fixed") is True
    assert await checks.check_schema_objects(db, "", fix=False) == []


# --------------------------------------------------------------------------
# fts_integrity — count desync + rebuild
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fts_ghost_row_fires_integrity_failure_and_rebuild_fixes():
    db = await get_db()
    await _insert(db, content="indexed once")
    # Inject a stray index-only row (no matching content row). Note: a row
    # count comparison can never see this — COUNT(*) on an external-content
    # FTS5 table proxies to the content table (why the old check was removed).
    await db.execute("INSERT INTO memories_fts(rowid, content) VALUES (999999, 'ghost entry')")
    await db.commit()

    issues = await checks.check_fts_integrity(db, "", fix=False)
    assert issues and issues[0]["type"] == "fts_integrity_failure"
    assert issues[0]["severity"] == "critical"

    fixed = await checks.check_fts_integrity(db, "", fix=True)
    await db.commit()
    assert fixed[0].get("fixed") is True
    assert await checks.check_fts_integrity(db, "", fix=False) == []


@pytest.mark.asyncio
async def test_fts_stale_content_fires_integrity_failure():
    """The bug-008 shape: indexed text no longer matches the content table."""
    db = await get_db()
    mem_id = await _insert(db, content="photosynthesis chloroplast")
    # Bypass the AU trigger by rewriting content with triggers momentarily
    # dropped, then restoring them — simulating a pre-v11 stale index.
    await db.execute("DROP TRIGGER memories_fts_au")
    await db.execute(
        "UPDATE memories SET content = 'quantum entanglement' WHERE id = ?", (mem_id,)
    )
    await db.commit()

    issues = await checks.check_fts_integrity(db, "", fix=True)
    await db.commit()
    assert issues and issues[0]["type"] == "fts_integrity_failure"
    assert issues[0].get("fixed") is True

    # Restore the canonical trigger for the rest of the suite.
    await checks.check_schema_objects(db, "", fix=True)
    await db.commit()
    assert await checks.check_schema_objects(db, "", fix=False) == []


# --------------------------------------------------------------------------
# sqlite_integrity — healthy path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_integrity_ok_on_healthy_db():
    db = await get_db()
    assert await checks.check_sqlite_integrity(db, "", fix=False) == []


# --------------------------------------------------------------------------
# axis_hygiene — naming drift clusters, report-only
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_axis_hygiene_clusters_normalized_equal_project_ids():
    db = await get_db()
    await _insert(db, content="row one", project_id="data-ops-audit")
    await _insert(db, content="row two", project_id="dataops-audit")
    await _insert(db, content="row three", project_id="unrelated")

    issues = await checks.check_axis_hygiene(db, "", fix=False)
    assert len(issues) == 1
    clusters = issues[0]["clusters"]
    assert len(clusters) == 1
    ids = {m["project_id"] for m in clusters[0]}
    assert ids == {"data-ops-audit", "dataops-audit"}  # 'unrelated' not flagged


@pytest.mark.asyncio
async def test_axis_hygiene_silent_on_distinct_buckets():
    db = await get_db()
    await _insert(db, content="row one", project_id="alpha")
    await _insert(db, content="row two", project_id="beta")
    assert await checks.check_axis_hygiene(db, "", fix=False) == []


# --------------------------------------------------------------------------
# timestamp_format_drift — aware→UTC lossless fix, naive untouched
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timestamp_drift_normalizes_aware_keeps_naive():
    db = await get_db()
    id_jst = await _insert(db, content="jst row", timestamp="2026-07-02T09:00:00+09:00")
    id_utc = await _insert(db, content="utc row", timestamp="2026-07-02T00:00:00Z")
    id_naive = await _insert(db, content="naive row", timestamp="2026-07-02T03:00:00")

    issues = await checks.check_timestamp_format_drift(db, "", fix=True)
    await db.commit()
    assert issues and issues[0]["type"] == "timestamp_format_drift"
    assert issues[0]["unfixable_naive"] == 1
    assert issues[0]["normalized"] == 1

    rows = dict(
        await db.execute_fetchall(
            "SELECT id, timestamp FROM memories WHERE id IN (?, ?, ?)", (id_jst, id_utc, id_naive)
        )
    )
    assert rows[id_jst] == "2026-07-02T00:00:00+00:00"  # same instant, UTC form
    assert rows[id_utc] == "2026-07-02T00:00:00Z"  # already UTC — untouched
    assert rows[id_naive] == "2026-07-02T03:00:00"  # unknowable zone — never rewritten


# --------------------------------------------------------------------------
# null_embedding context-dependent severity
# --------------------------------------------------------------------------


class _StubClient:
    _http_url = None

    async def embed(self, texts):
        return [[0.5, 0.5, 0.5, 0.5] for _ in texts]

    pack_embedding = staticmethod(EmbeddingClient.pack_embedding)


@pytest.mark.asyncio
async def test_null_embedding_severity_ladder():
    db = await get_db()
    await _insert(db, content="row without vector")

    # mode=none → info (NULL is the expected steady state)
    issues = await checks.check_null_embedding(db, "", fix=False)
    assert issues[0]["severity"] == "info"

    # client configured, 1/1 NULL (> 50%) → critical (pipeline down)
    vector._embedding_client = _StubClient()
    issues = await checks.check_null_embedding(db, "", fix=False)
    assert issues[0]["severity"] == "critical"

    # client configured, 1/3 NULL (< 50%) → warn
    blob = EmbeddingClient.pack_embedding([0.1, 0.2, 0.3, 0.4])
    await _insert(db, content="embedded row a", embedding=blob)
    await _insert(db, content="embedded row b", embedding=blob)
    issues = await checks.check_null_embedding(db, "", fix=False)
    assert issues[0]["severity"] == "warn"


# --------------------------------------------------------------------------
# checks subset parameter
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_health_subset_runs_only_selected():
    db = await get_db()
    # A condition duplicate_content would flag (cross-channel exact dupes)...
    await _insert(db, content="dup", channel="c1")
    await _insert(db, content="dup", channel="c2")

    result = await maintenance_handlers.do_check_health(checks=["axis_hygiene"])
    # ...must NOT be reported when only axis_hygiene was selected.
    assert all(i["check"] == "axis_hygiene" for i in result["issues"])


# --------------------------------------------------------------------------
# exit-code gate semantics
# --------------------------------------------------------------------------


def test_exit_code_gate_semantics():
    assert checks.exit_code({"critical": 1, "warn": 0, "info": 0}) == 2
    assert checks.exit_code({"critical": 1, "warn": 5, "info": 5}, strict=True) == 2
    assert checks.exit_code({"critical": 0, "warn": 3, "info": 0}) == 0  # default: no gate
    assert checks.exit_code({"critical": 0, "warn": 3, "info": 0}, strict=True) == 1
    assert checks.exit_code({"critical": 0, "warn": 0, "info": 9}, strict=True) == 0


# --------------------------------------------------------------------------
# deep checks — near_duplicate + calibration_staleness
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_near_duplicate_reports_close_pair_only():
    db = await get_db()
    a = EmbeddingClient.pack_embedding([1.0, 0.0, 0.0, 0.01])
    b = EmbeddingClient.pack_embedding([1.0, 0.0, 0.0, 0.02])  # cosine ≈ 1.0 vs a
    c = EmbeddingClient.pack_embedding([0.0, 1.0, 0.0, 0.0])  # orthogonal
    await _insert(db, agent_id="agent-n", content="the cat sat", embedding=a)
    await _insert(db, agent_id="agent-n", content="the cat sat.", embedding=b)
    await _insert(db, agent_id="agent-n", content="unrelated topic", embedding=c)

    result = await checks.deep_near_duplicate(db, "agent-n", fix=False)
    assert result["pairs"] == 1
    assert result["samples"][0]["cosine"] > checks.NEAR_DUPLICATE_COSINE


@pytest.mark.asyncio
async def test_near_duplicate_excludes_exact_duplicates():
    db = await get_db()
    blob = EmbeddingClient.pack_embedding([1.0, 0.0, 0.0, 0.0])
    await _insert(db, agent_id="agent-n", content="same text", channel="c1", embedding=blob)
    await _insert(db, agent_id="agent-n", content="same text", channel="c2", embedding=blob)
    result = await checks.deep_near_duplicate(db, "agent-n", fix=False)
    assert result["pairs"] == 0  # exact dupes belong to duplicate_content


@pytest.mark.asyncio
async def test_calibration_staleness_not_applicable_without_client():
    db = await get_db()
    result = await checks.deep_calibration_staleness(db, "agent-h", fix=False)
    assert result["status"] == "not_applicable"


@pytest.mark.asyncio
async def test_deep_check_includes_new_checks_by_default():
    result = await maintenance_handlers.do_deep_check("agent-h")
    assert "calibration_staleness" in result["checks_run"]
    assert "near_duplicate" in result["checks_run"]


# --------------------------------------------------------------------------
# checkup CLI — subprocess round-trip on an isolated DB
# --------------------------------------------------------------------------


def _cli(db_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CPERSONA_EMBEDDING_MODE="none")
    return subprocess.run(
        [sys.executable, "-m", "cpersona.checkup", "--db", db_path, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_cli_roundtrip_exit_codes():
    import sqlite3 as sq

    cli_dir = tempfile.mkdtemp()
    db_path = os.path.join(cli_dir, "cli.db")
    seed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio\n"
            "from cpersona.database import get_db, close_db\n"
            "async def s():\n"
            "    db = await get_db()\n"
            "    await db.execute(\"INSERT INTO memories (agent_id, content, source, timestamp)"
            " VALUES ('a1', 'hello', '{\\\"type\\\":\\\"User\\\",\\\"id\\\":\\\"u\\\",\\\"name\\\":\\\"n\\\"}',"
            " '2026-07-01T00:00:00+00:00')\")\n"
            "    await db.commit()\n"
            "    await close_db()\n"
            "asyncio.run(s())",
        ],
        env=dict(os.environ, CPERSONA_DB_PATH=db_path, CPERSONA_EMBEDDING_MODE="none"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seed.returncode == 0, seed.stderr

    healthy = _cli(db_path, "--json")
    assert healthy.returncode == 0, healthy.stderr
    report = json.loads(healthy.stdout)
    assert report["severity_summary"]["critical"] == 0

    # Break a load-bearing object → critical → exit 2; --fix repairs → exit 0.
    with sq.connect(db_path) as raw:
        raw.execute("DROP INDEX idx_memories_dedup_content")
    assert _cli(db_path).returncode == 2
    # bug-059: a --fix run that fully repairs the critical must exit 0 — the gate is
    # derived from the RESIDUAL state after the repair commits, not from the issues
    # that were found (which are stamped fixed). A repair cron/CI must not read a
    # clean fix as failure.
    assert _cli(db_path, "--fix").returncode == 0
    assert _cli(db_path).returncode == 0  # repaired

    missing = _cli(os.path.join(cli_dir, "nope.db"))
    assert missing.returncode == 2


@pytest.mark.asyncio
async def test_every_shipped_object_is_in_the_registry():
    """The other direction (bug-414).

    ``check_schema_objects`` walks ``_EXPECTED_OBJECTS`` and asks the database
    about each entry; nothing asked the database what it holds. So an entry
    deleted from the registry made a real, load-bearing object *invisible*
    rather than reported -- the object still existed, and the one-way golden
    test above stayed green because it only asserts that the surviving entries
    are present. This walks the fresh database instead, which is the only
    direction that can notice an object nobody is watching.

    Auto-indexes (``sql IS NULL`` / the ``sqlite_`` prefix) are SQLite's own and
    are excluded: they are not objects this package ships or could repair.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger') "
        "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    )
    shipped = {r[0] for r in rows}
    unwatched = sorted(shipped - set(checks._EXPECTED_OBJECTS))
    assert not unwatched, (
        f"these objects exist in a fresh database but are not in _EXPECTED_OBJECTS, so "
        f"check_schema_objects cannot report them missing on a deployment: {unwatched}"
    )


# --------------------------------------------------------------------------
# bug-324 — a deep fix run held the write lock across the report-only scans.
# --------------------------------------------------------------------------
#
# Only two of the eight deep checks act on fix=True, and which two is already
# declared (checks.DEEP_FIX_CAPABLE, asserted against the AST by the structural
# gates). The runner ignored the declaration and wrapped the whole registry in
# one transaction, so a fix run held the shared write lock across a dense
# pairwise similarity matrix, thousands of row fetches per table, Unicode
# normalisation of row content and a blocking read of the calibration sidecar --
# with every concurrent writer waiting on all of it. The sibling health handler
# was split on that ground earlier (bug-072); the split was not carried across.
#
# The lock is the observable, so the tests read it from inside the runners. The
# corpus-scale cost is not measured here, only the scope.


def _lock_spy(seen: dict):
    def make(name):
        async def runner(db, agent_id, fix):
            seen[name] = database.write_lock().locked()
            return {"count": 0}

        return runner

    return {name: make(name) for name in checks.DEEP_CHECK_NAMES}


@pytest.mark.asyncio
async def test_a_deep_fix_run_keeps_the_report_only_scans_off_the_write_lock(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(checks, "DEEP_CHECKS", _lock_spy(seen))

    await maintenance_handlers.do_deep_check("a1", fix=True)

    held = {n: seen[n] for n in checks.DEEP_REPORT_ONLY}
    assert not any(held.values()), held


@pytest.mark.asyncio
async def test_a_deep_fix_run_still_serialises_the_checks_that_write(monkeypatch):
    """The other half: bug-042/043 must keep holding for the repairing checks."""
    seen: dict = {}
    monkeypatch.setattr(checks, "DEEP_CHECKS", _lock_spy(seen))

    await maintenance_handlers.do_deep_check("a1", fix=True)

    held = {n: seen[n] for n in checks.DEEP_FIX_CAPABLE}
    assert all(held.values()), held


@pytest.mark.asyncio
async def test_a_read_only_deep_run_takes_the_write_lock_for_nothing(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(checks, "DEEP_CHECKS", _lock_spy(seen))

    await maintenance_handlers.do_deep_check("a1", fix=False)

    assert not any(seen.values()), seen


@pytest.mark.asyncio
async def test_the_deep_results_keep_the_requested_order_across_the_two_seams(monkeypatch):
    """Splitting the loop must not reorder the response."""
    monkeypatch.setattr(checks, "DEEP_CHECKS", _lock_spy({}))
    requested = list(checks.DEEP_CHECK_NAMES)

    out = await maintenance_handlers.do_deep_check("a1", fix=True, checks=requested)

    assert list(out["results"]) == requested
    assert out["checks_run"] == requested


# --------------------------------------------------------------------------
# bug-325 — an unknown deep check name read as a clean deep pass.
# --------------------------------------------------------------------------
#
# The loop dropped any name the registry did not know and the schema enumerates
# the legal values in prose without an enum, so a single typo answered
# checks_run: [], results: {}, fixed: true. The sibling health tool has refused
# the same input since bug-230, with the list of valid names; the two handlers
# disagreed on a documented point, in the opposite direction from the one that
# earlier fix recorded.


@pytest.mark.asyncio
async def test_an_unknown_deep_check_name_is_refused_the_way_health_refuses_it():
    result = await maintenance_handlers.do_deep_check("a1", checks=["deep_no_such"])

    assert result["ok"] is False
    assert result["unknown_checks"] == ["deep_no_such"]
    assert result["valid_checks"] == list(checks.DEEP_CHECK_NAMES)
    assert result["checks_run"] == []


@pytest.mark.asyncio
async def test_a_mixed_deep_list_is_refused_rather_than_partly_run():
    """The half that would have run is the half that makes the empty answer plausible."""
    result = await maintenance_handlers.do_deep_check(
        "a1", checks=["short_content", "deep_no_such"]
    )

    assert result["ok"] is False
    assert result["unknown_checks"] == ["deep_no_such"]
    assert "results" not in result


@pytest.mark.asyncio
async def test_a_known_deep_check_name_still_runs():
    result = await maintenance_handlers.do_deep_check("a1", checks=["short_content"])

    assert result["checks_run"] == ["short_content"]
    assert "short_content" in result["results"]


# --------------------------------------------------------------------------
# bug-327 — the all-agent deep sweep swept one of the three tables it reads.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_deep_sweep_reaches_agents_that_hold_no_memories():
    db = await get_db()
    await _insert(db, agent_id="has-memories", content="hello")
    await db.execute(
        "INSERT INTO profiles (agent_id, user_id, content) VALUES (?,?,?)",
        ("profile-only", "", "a stale profile"),
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time) "
        "VALUES (?,?,?,?,?)",
        ("episode-only", "an orphaned episode", "", TS_327, TS_327),
    )
    await db.commit()

    agents = await checkup._deep_sweep_agents(None)

    assert {"has-memories", "profile-only", "episode-only"} <= set(agents), agents


@pytest.mark.asyncio
async def test_a_named_agent_still_sweeps_only_that_agent():
    assert await checkup._deep_sweep_agents("just-this-one") == ["just-this-one"]


TS_327 = "2026-07-01T00:00:00+00:00"


# --------------------------------------------------------------------------
# bug-381 — a deep finding whose counts had other names printed as nothing.
# --------------------------------------------------------------------------
#
# The filter read a hand-written key list; the anonymous-source check reports
# `recoverable` / `unrecoverable`, so a row it found was invisible to every run
# that was not --json. The replacement is a declared set rather than "any number
# in the result": the same results carry threshold, tolerance, rows_scanned and
# age_days, and reading those as findings prints every check on every run.


def test_a_deep_finding_reported_without_a_count_key_is_printed():
    findings = checkup._deep_findings(
        {"anonymous_source": {"recoverable": 1, "unrecoverable": 0}}
    )

    assert "anonymous_source" in findings, findings


def test_a_clean_deep_result_is_still_filtered_out():
    findings = checkup._deep_findings(
        {
            "anonymous_source": {"recoverable": 0, "unrecoverable": 0},
            "near_duplicate": {"pairs": 0, "rows_scanned": 500, "threshold": 0.97},
            "stale_profile": {"count": 0, "threshold_days": 30},
            "calibration_staleness": {"status": "ok", "age_days": 3},
        }
    )

    assert findings == {}, findings


def test_every_deep_check_reports_on_a_key_the_human_readable_filter_reads():
    """So a check added later cannot ship invisible to the operator reading plain output."""
    import ast
    import inspect

    readable = checkup.DEEP_FINDING_KEYS | {"status", "error"}
    for name, runner in checks.DEEP_CHECKS.items():
        keys = set()
        for node in ast.walk(ast.parse(inspect.getsource(runner).lstrip())):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
        assert keys & readable, (
            f"deep check {name!r} reports on none of {sorted(readable)}, so a finding "
            f"from it cannot appear in a non-JSON checkup run; its keys are {sorted(keys)}"
        )


# --------------------------------------------------------------------------
# bug-382 — the echoed check list disagreed with the counts beside it.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repeated_health_check_name_is_echoed_once():
    result = await maintenance_handlers.do_check_health(
        checks=["empty_content", "empty_content"]
    )

    assert result["checks_run"] == ["empty_content"]


@pytest.mark.asyncio
async def test_the_echoed_health_check_list_keeps_the_callers_order():
    result = await maintenance_handlers.do_check_health(
        checks=["duplicate_content", "empty_content", "duplicate_content"]
    )

    assert result["checks_run"] == ["duplicate_content", "empty_content"]


# --------------------------------------------------------------------------
# bug-413 — --strict was pinned at the helper and never end to end.
# --------------------------------------------------------------------------
#
# The flag exists for exactly one database state: warn issues, no critical ones.
# exit_code's semantics were covered by calling it directly, and the subprocess
# round trip exercised only the healthy and the critical cases -- so rewriting the
# CLI's own call to pass strict=False left the suite green while the documented
# contract was broken on the state the flag is for.


def _warn_only_db() -> str:
    import sqlite3 as sq

    db_path = os.path.join(tempfile.mkdtemp(), "warn_only.db")
    seed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio\n"
            "from cpersona.database import get_db, close_db\n"
            "async def s():\n"
            "    db = await get_db()\n"
            "    await db.execute('INSERT INTO memories (agent_id, content, source, "
            "timestamp) VALUES (?,?,?,?)', ('a1', 'hello', "
            "'{\"type\":\"User\",\"id\":\"u\",\"name\":\"n\"}', "
            "'2026-07-01T00:00:00+00:00'))\n"
            "    await db.commit()\n"
            "    await close_db()\n"
            "asyncio.run(s())",
        ],
        env=dict(os.environ, CPERSONA_DB_PATH=db_path, CPERSONA_EMBEDDING_MODE="none"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seed.returncode == 0, seed.stderr
    # A warn-severity registered object: losing it costs recall latency, not answers,
    # and boot recreates it -- which is why it is warn and not critical.
    with sq.connect(db_path) as raw:
        raw.execute("DROP " + "INDEX idx_memories_isolation")
    return db_path


def test_strict_gates_a_warn_only_database_through_the_cli():
    db_path = _warn_only_db()

    summary = json.loads(_cli(db_path, "--json").stdout)["severity_summary"]
    assert summary["critical"] == 0 and summary["warn"] >= 1, summary

    assert _cli(db_path).returncode == 0, "a warn-only database must not gate without --strict"
    assert _cli(db_path, "--strict").returncode == 1, "--strict must gate on warn issues"
