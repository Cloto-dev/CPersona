"""SuperAuditor v1 conformance for CPersona's ``get_session_findings``.

The standard (docs/SUPERAUDITOR_STANDARD.md §9) lists nine demonstrations,
C1–C9. C1–C5 are proved against the shared fixtures in
``conformance/superauditor/v1/`` — pure functions of (detector output,
per_kind_limit), fed through THIS server's delivery path rather than the
reference script. C6–C8 are proved here against the registry and a live
database. C9 (broadcast retirement) does not apply: CPersona never pushed
findings onto unrelated responses, so there is nothing to retire.

Beyond the letter of the standard, this file pins the two decisions the seam
makes about ``check_health``'s severity model — escalation tiers become their
own kinds, de-escalation does not — and the inventory of runners that
override their severity, so a new escalation rule cannot appear without a
tier entry (the C6 exhaustiveness rule applied to this server's shape).
"""

import ast
import importlib.metadata
import json
from pathlib import Path

import pytest
import pytest_asyncio

from cpersona import acl, checks, findings, maintenance_handlers, server, vector
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "conformance" / "superauditor" / "v1"

AGENT_A = "findings-agent-a"
AGENT_B = "findings-agent-b"


@pytest_asyncio.fixture
async def db(monkeypatch):
    """A clean database with no embedding client, so the null-embedding checks
    sit on their info tier unless a test installs a client."""
    no_persist.resume()
    monkeypatch.setattr(vector, "_embedding_client", None)
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _insert(db, agent_id=AGENT_A, content="fine content", **cols):
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


async def _profile(db, agent_id):
    await db.execute("INSERT INTO profiles (agent_id, content) VALUES (?, 'profile text')", (agent_id,))
    await db.commit()


def _by_kind(response: dict, kind: str) -> list[dict]:
    return [f for f in response["findings"] if f["kind"] == kind]


# ---------------------------------------------------------------------------
# C1–C5: the shared fixtures, through this server's delivery path
# ---------------------------------------------------------------------------


def _fixture_cases():
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for case in doc["cases"]:
            yield pytest.param(case, id=f"{path.name}::{case['name']}")


@pytest.mark.parametrize("case", list(_fixture_cases()))
def test_fixture_case_through_deliver(case):
    got = findings.deliver(case["detector_output"], case["per_kind_limit"], case["severity_map"])
    for key, expected in case["expect"].items():
        assert got[key] == expected, f"{case['name']}: {key}"
    # The invariants the counts exist to carry, independently of the expectations.
    assert got["total"] == len(got["findings"])
    assert sum(got["counts_by_kind"].values()) == got["total"]
    assert sum(got["counts_by_severity"].values()) == got["total"]
    assert all(n <= case["per_kind_limit"] for n in got["counts_by_kind"].values())


def test_fixture_directory_is_not_empty():
    """A glob that matches nothing parametrizes nothing and passes — say so."""
    assert list(_fixture_cases()), f"no fixture cases found under {FIXTURE_DIR}"


# ---------------------------------------------------------------------------
# C4 / C5 / C6: the static severity map
# ---------------------------------------------------------------------------


def test_severity_map_is_exhaustive_over_the_registry():
    """C6: every registered check delivers under a mapped kind."""
    missing = [c.name for c in checks.HEALTH_CHECKS if c.name not in findings.FINDING_SEVERITY]
    assert not missing, f"checks with no finding severity: {missing}"
    assert set(findings.FINDING_SEVERITY.values()) <= set(checks.SEVERITIES)


def test_unmapped_kind_falls_back_to_info():
    """C5: the fallback is the weakest severity — an unmapped probe cannot alarm."""
    assert findings.severity_for_kind("never_registered") == "info"
    delivered = findings.deliver_issues([{"check": "never_registered", "type": "x", "severity": "critical"}], 5)
    assert delivered["findings"][0]["severity"] == "info"


def test_same_kind_always_yields_the_same_severity():
    """C4: severity is read off the kind, never off the instance."""
    issues = [
        {"check": "stale_pending_tasks", "type": "stale_pending_tasks", "severity": "warn", "count": 3},
        {"check": "stale_pending_tasks", "type": "stale_pending_tasks", "severity": "info", "count": 0,
         "repairable": 0, "needs_human_review": True},
    ]
    delivered = findings.deliver_issues(issues, 5)
    assert {f["severity"] for f in delivered["findings"]} == {"warn"}


# ---------------------------------------------------------------------------
# The seam's two decisions about check_health's severity model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "issue, kind, severity",
    [
        ({"check": "null_embedding", "type": "null_embedding", "severity": "info"},
         "null_embedding_expected", "info"),
        ({"check": "null_embedding", "type": "null_embedding", "severity": "warn"},
         "null_embedding", "warn"),
        ({"check": "null_embedding", "type": "null_embedding", "severity": "critical"},
         "null_embedding_pipeline_down", "critical"),
        ({"check": "null_episode_embedding", "type": "null_episode_embedding", "severity": "critical"},
         "null_episode_embedding_pipeline_down", "critical"),
        ({"check": "schema_objects", "type": "schema_object_drift", "severity": "critical"},
         "schema_objects", "critical"),
        ({"check": "schema_objects", "type": "schema_object_drift", "severity": "warn"},
         "schema_objects_perf_index", "warn"),
        # De-escalated perf index (repairable == 0): still the perf-index tier.
        ({"check": "schema_objects", "type": "schema_object_drift", "severity": "info",
          "needs_human_review": True}, "schema_objects_perf_index", "warn"),
        ({"check": "fts_integrity", "type": "fts_integrity_failure", "severity": "critical"},
         "fts_integrity", "critical"),
        ({"check": "empty_content", "type": "check_crashed", "check_name": "empty_content",
          "detail": "boom", "severity": "warn"}, "check_crashed", "warn"),
        ({"check": "missing_profile", "type": "missing_profile", "severity": "info"},
         "missing_profile", "info"),
    ],
)
def test_escalation_tiers_are_their_own_kinds(issue, kind, severity):
    assert findings.finding_kind(issue) == kind
    delivered = findings.deliver_issues([issue], 5)
    assert delivered["findings"][0]["severity"] == severity


def test_an_issue_that_owns_a_kind_key_keeps_it_as_object_kind():
    """schema_objects uses `kind` for the object type; the finding's `kind` is the
    probe. Caught live: the object type was delivered as the finding kind."""
    issue = {"check": "schema_objects", "type": "schema_object_drift", "kind": "index",
             "name": "idx_memories_agent", "severity": "warn", "repairable": 1}
    finding = findings.as_finding(issue)
    assert finding["kind"] == "schema_objects_perf_index"
    assert finding["object_kind"] == "index" and finding["name"] == "idx_memories_agent"
    assert finding["health_severity"] == "warn" and finding["severity"] == "warn"


def test_de_escalation_is_kept_as_health_severity_not_as_the_kind_severity():
    """check_health's repairable policy lowers a warn to info when nothing can be
    repaired; the finding keeps its kind's severity and carries that verdict."""
    issue = {"check": "empty_content", "type": "empty_content", "severity": "info",
             "repairable": 0, "needs_human_review": True, "count": 2}
    finding = findings.deliver_issues([issue], 5)["findings"][0]
    assert finding["severity"] == "warn"
    assert finding["health_severity"] == "info"
    assert finding["needs_human_review"] is True
    assert finding["repairable"] == 0
    assert finding["check"] == "empty_content" and finding["type"] == "empty_content"


def _runners_that_stamp_severity() -> set[str]:
    """Every ``check_*`` runner whose body builds a dict with a ``severity`` key."""
    tree = ast.parse((ROOT / "cpersona" / "checks.py").read_text(encoding="utf-8"))
    stamping = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("check_"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "severity" for k in sub.keys
            ):
                stamping.add(node.name)
                break
    return stamping


# Runners that override the registry default. The first three carry more than
# one severity and are tiered in findings.py; the last two stamp a single
# explicit value (critical) on every finding they emit.
_TIERED_RUNNERS = {"check_null_embedding", "check_null_episode_embedding", "check_schema_objects"}
_SINGLE_VALUE_RUNNERS = {"check_fts_integrity", "check_sqlite_integrity"}


def test_every_severity_overriding_runner_is_inventoried():
    """The C6 rule applied to this server's shape: a runner that starts stamping
    its own severity must be tiered here (or shown to emit one value), or the
    static map silently misreports it."""
    stamping = _runners_that_stamp_severity()
    assert stamping, "the AST scan found no runner stamping severity — the scan is broken"
    assert stamping == _TIERED_RUNNERS | _SINGLE_VALUE_RUNNERS, (
        f"uninventoried: {sorted(stamping - _TIERED_RUNNERS - _SINGLE_VALUE_RUNNERS)}; "
        f"stale: {sorted((_TIERED_RUNNERS | _SINGLE_VALUE_RUNNERS) - stamping)}"
    )
    tiered_checks = {name.removeprefix("check_") for name in _TIERED_RUNNERS}
    assert tiered_checks == set(findings._TIERED_CHECKS)


def test_inventory_gate_has_teeth():
    """The scan must see a runner that stamps severity, and only such runners."""
    source = "async def check_x(db, a, f):\n    return [{'type': 'x', 'severity': 'warn'}]\n"
    tree = ast.parse(source)
    found = [n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    assert found == ["check_x"]
    assert any(
        isinstance(sub, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "severity" for k in sub.keys)
        for sub in ast.walk(tree)
    )
    silent = ast.parse("async def check_y(db, a, f):\n    return [{'type': 'y'}]\n")
    assert not any(
        isinstance(sub, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "severity" for k in sub.keys)
        for sub in ast.walk(silent)
    )


# ---------------------------------------------------------------------------
# The pull tool against a live database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_database_delivers_no_findings_with_the_full_response_shape(db):
    response = await maintenance_handlers.do_get_session_findings()
    assert response["findings"] == [] and response["total"] == 0
    assert response["counts_by_kind"] == {} and response["counts_by_severity"] == {}
    assert response["capped_kinds"] == []
    assert response["per_kind_limit"] == findings.DEFAULT_PER_KIND_LIMIT
    assert response["summary"] == "Storage findings: none."
    assert response["_meta"]["server_version"] == importlib.metadata.version("cpersona")
    assert "identity_shared" not in response  # stdio: sessions are not shared


@pytest.mark.asyncio
async def test_one_detector_the_pull_reports_what_check_health_reports(db):
    """§5.3: the same probes feed both channels, so the two never disagree on
    which checks fired."""
    await _insert(db, timestamp="not-a-date")
    await _insert(db, agent_id=AGENT_B, source="{}")
    health = await maintenance_handlers.do_check_health(fix=False)
    pulled = await maintenance_handlers.do_get_session_findings(per_kind_limit=1000)
    assert {(i["check"], i["type"]) for i in health["issues"]} == {
        (f["check"], f["type"]) for f in pulled["findings"]
    }
    assert pulled["total"] == len(health["issues"])


@pytest.mark.asyncio
async def test_findings_are_not_filtered_by_agent(db):
    """C7: both agents' defects surface; nothing narrows the channel."""
    await _insert(db, agent_id=AGENT_A, timestamp="not-a-date")
    await _insert(db, agent_id=AGENT_B, timestamp="also-not-a-date")
    response = await maintenance_handlers.do_get_session_findings()
    bad = _by_kind(response, "invalid_timestamp")
    assert len(bad) == 1, bad  # one finding for the check, counting both rows
    assert bad[0]["count"] == 2
    assert bad[0]["severity"] == "warn" and bad[0]["health_severity"] == "warn"
    assert bad[0]["check"] == "invalid_timestamp"
    tool = {t.name: t for t in server.registry._tools}["get_session_findings"]
    assert set(tool.inputSchema["properties"]) == {"session_key", "per_kind_limit", "include_summary"}
    assert tool.annotations.readOnlyHint is True


_PERF_INDEXES = ("idx_memories_agent", "idx_memories_msg_id", "idx_episodes_agent")


async def _drop_perf_indexes(db, names):
    """schema_objects reports one finding per drifted object, which is what a
    per-kind cap needs to bite on. These are the warn-tier (performance)
    indexes, so no data guarantee changes while they are gone."""
    for name in names:
        assert checks._EXPECTED_OBJECTS[name]["severity"] == "warn"
        await db.execute(f"DROP INDEX IF EXISTS {name}")
    await db.commit()


async def _restore_perf_indexes(db, names):
    for name in names:
        await db.execute(checks._EXPECTED_OBJECTS[name]["sql"])
    await db.commit()


@pytest.mark.asyncio
async def test_caps_are_observed_at_the_boundary(db):
    """C2: exactly per_kind_limit findings is NOT capped; one more is."""
    await _drop_perf_indexes(db, _PERF_INDEXES)  # three findings of one kind
    try:
        capped = await maintenance_handlers.do_get_session_findings(per_kind_limit=2)
        assert capped["capped_kinds"] == ["schema_objects_perf_index"]
        assert capped["counts_by_kind"] == {"schema_objects_perf_index": 2}
        assert capped["per_kind_limit"] == 2
        assert {f["severity"] for f in capped["findings"]} == {"warn"}
        assert "Capped at 2 per kind: schema_objects_perf_index" in capped["summary"]

        exact = await maintenance_handlers.do_get_session_findings(per_kind_limit=3)
        assert exact["capped_kinds"] == []
        assert exact["counts_by_kind"] == {"schema_objects_perf_index": 3}
        assert "Capped" not in exact["summary"]
    finally:
        await _restore_perf_indexes(db, _PERF_INDEXES)
    clean = await maintenance_handlers.do_get_session_findings()
    assert not _by_kind(clean, "schema_objects_perf_index"), "the indexes were not restored"


@pytest.mark.asyncio
async def test_summary_is_rendered_from_the_trimmed_set_and_is_optional(db):
    await _drop_perf_indexes(db, _PERF_INDEXES[:2])
    try:
        with_summary = await maintenance_handlers.do_get_session_findings(per_kind_limit=1)
        assert with_summary["summary"] == (
            "Storage findings: 1 returned (warn 1); kinds: schema_objects_perf_index 1. "
            "Capped at 1 per kind: schema_objects_perf_index — more exist than were returned."
        )
        without = await maintenance_handlers.do_get_session_findings(per_kind_limit=1, include_summary=False)
        assert "summary" not in without
        assert without["findings"] == with_summary["findings"]
    finally:
        await _restore_perf_indexes(db, _PERF_INDEXES[:2])


@pytest.mark.asyncio
async def test_per_kind_limit_below_one_is_refused(db):
    response = await maintenance_handlers.do_get_session_findings(per_kind_limit=0)
    assert response["ok"] is False and "per_kind_limit" in response["error"]


@pytest.mark.asyncio
async def test_shared_transport_without_a_session_key_is_marked(db, monkeypatch):
    """C8: a non-stdio transport with no declared key says identity_shared."""
    monkeypatch.setenv("CPERSONA_TRANSPORT", "http")
    shared = await maintenance_handlers.do_get_session_findings()
    assert shared["identity_shared"] is True
    declared = await maintenance_handlers.do_get_session_findings(session_key="s-1")
    assert "identity_shared" not in declared
    monkeypatch.setenv("CPERSONA_TRANSPORT", "stdio")
    local = await maintenance_handlers.do_get_session_findings()
    assert "identity_shared" not in local


@pytest.mark.asyncio
async def test_a_crashed_probe_is_a_finding_not_a_failed_pull(db, monkeypatch):
    """A partial result names the probe that could not run; the rest is delivered."""
    await _insert(db, agent_id="p1")  # missing_profile still fires
    victim = next(c for c in checks.HEALTH_CHECKS if c.name == "empty_content")

    async def boom(*args, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(victim, "runner", boom)
    response = await maintenance_handlers.do_get_session_findings()
    crashed = _by_kind(response, "check_crashed")
    assert len(crashed) == 1
    assert crashed[0]["severity"] == "warn" and crashed[0]["check"] == "empty_content"
    assert "probe exploded" in crashed[0]["detail"]
    assert _by_kind(response, "missing_profile"), "the other probes were not delivered"


@pytest.mark.asyncio
async def test_null_embedding_tiers_on_a_live_database(db, monkeypatch, fake_embedding_client):
    """The pipeline-down tier is the critical it is; the expected tier is info."""
    await _profile(db, AGENT_A)
    await _insert(db)  # embedding NULL, client configured → 100% NULL → pipeline down
    monkeypatch.setattr(checks, "_blobs_are_stored", lambda: True)
    response = await maintenance_handlers.do_get_session_findings()
    down = _by_kind(response, "null_embedding_pipeline_down")
    assert len(down) == 1 and down[0]["severity"] == "critical"
    assert down[0]["health_severity"] == "critical" and down[0]["count"] == 1
    assert not _by_kind(response, "null_embedding")

    monkeypatch.setattr(vector, "_embedding_client", None)
    response = await maintenance_handlers.do_get_session_findings()
    expected = _by_kind(response, "null_embedding_expected")
    assert len(expected) == 1 and expected[0]["severity"] == "info"
    assert not _by_kind(response, "null_embedding_pipeline_down")


# ---------------------------------------------------------------------------
# Surface: ACL classification and the shipped documentation
# ---------------------------------------------------------------------------


def test_acl_demands_the_all_agents_read_and_can_say_why():
    demands = acl.ACL_CLASSIFICATION["get_session_findings"]
    assert demands({}) == [(acl.WILDCARD, acl.PERM_READ)]
    assert demands({"agent_id": "x"}) == [(acl.WILDCARD, acl.PERM_READ)]
    assert "whole database" in demands._sweep_cause({})


def test_tools_page_documents_the_pull_tool():
    for page in ("tools.md", "tools.ja.md"):
        text = (ROOT / "docs" / page).read_text(encoding="utf-8")
        assert "`get_session_findings`" in text, f"docs/{page} does not list get_session_findings"
        assert "SUPERAUDITOR_STANDARD.md" in text, f"docs/{page} does not point at the standard"
