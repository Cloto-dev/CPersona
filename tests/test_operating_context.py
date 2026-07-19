"""Server-served operating context (2.5.1) — design §9 test plan.

Hermetic throughout: the sidecar lives in a per-test tmp_path and is selected
via CPERSONA_OPERATING_CONTEXT_PATH; conftest pins the kill switch off for the
rest of the suite, and each test here opts back in explicitly. No live backend,
no DB — the boundary integration tests stub the underlying handlers.
"""

import os

import pytest

from cpersona import operating_context

VALID_SIDECAR = """
version = 1
context_revision = "2026-07-18.1"

[instructions]
summary = \"\"\"
CPersona operating context (rev 2026-07-18.1).
project_id registry: "" (global), "cycia-mc". Pass "@auto" to resolve your default.
\"\"\"

[registry]
project_ids = ["", "cycia-mc"]
enforce = "warn"

[defaults]
"claude-code" = "cycia-mc"
"agent.global" = ""

[[doctrine]]
name = "recall-discipline"
body = "recall: limit<=5 outside session-start; use exclude_contents."

[[doctrine]]
name = "agent-id-conventions"
body = "agent_id: 'claude-code' for Claude Code sessions."
"""


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """Enable the feature against a tmp sidecar; returns a writer/updater."""
    path = tmp_path / "operating-context.toml"

    def write(text: str = VALID_SIDECAR) -> str:
        path.write_text(text, encoding="utf-8")
        # mtime granularity is filesystem-dependent; force a visible change so
        # the lazy-reload key (path, mtime_ns) always differs between writes.
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        return str(path)

    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "on")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(path))
    return write


# ---------------------------------------------------------------------------
# §9-1 — dormant states: absent / kill switch / invalid file => zero deltas
# ---------------------------------------------------------------------------


def test_absent_file_is_fully_dormant(sidecar):
    # sidecar fixture set the path but never wrote the file
    assert operating_context.get_context() is None
    assert operating_context.instructions_text() is None
    state = operating_context.load_state()
    assert state["enabled"] and not state["present"] and state["parse_error"] is None
    # passthrough for every caller value — including the literal sentinel
    for pid in (None, "", "cycia-mc", "unknown", "@auto"):
        for write in (True, False):
            assert operating_context.check_project_id(pid, "claude-code", write) == (pid, None, None)


def test_kill_switch_wins_over_a_valid_file(sidecar, monkeypatch):
    sidecar()
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "off")
    assert operating_context.get_context() is None
    assert operating_context.instructions_text() is None
    assert not operating_context.load_state()["enabled"]
    assert operating_context.check_project_id("@auto", "claude-code", True) == ("@auto", None, None)


def test_invalid_toml_is_dormant_not_fatal(sidecar):
    sidecar("version = [broken")
    assert operating_context.get_context() is None
    assert operating_context.instructions_text() is None
    assert operating_context.load_state()["parse_error"]
    assert operating_context.check_project_id("unknown", "a", True) == ("unknown", None, None)


@pytest.mark.parametrize(
    "text",
    [
        "version = 2",  # unsupported file-format version
        'version = 1\n[registry]\nenforce = "bogus"',
        'version = 1\n[registry]\nproject_ids = [1, 2]',
        'version = 1\n[instructions]\nsummary = 3',
        'version = 1\n[defaults]\nx = 1',
        'version = 1\n[[doctrine]]\nname = "a"',  # missing body
        'version = 1\n[[doctrine]]\nname = "a"\nbody = "x"\n[[doctrine]]\nname = "a"\nbody = "y"',
    ],
)
def test_schema_violations_are_dormant_with_retained_error(sidecar, text):
    sidecar(text)
    assert operating_context.get_context() is None
    assert operating_context.load_state()["parse_error"]


# ---------------------------------------------------------------------------
# §9-2 — instructions threading
# ---------------------------------------------------------------------------


def test_instructions_text_serves_the_summary(sidecar):
    sidecar()
    text = operating_context.instructions_text()
    assert text is not None and "rev 2026-07-18.1" in text


def test_tool_registry_threads_instructions_into_initialize():
    from cpersona._vendored_mcp_common.mcp_utils import ToolRegistry

    options = ToolRegistry("t", instructions="hello doctrine").server.create_initialization_options()
    assert options.instructions == "hello doctrine"
    # default stays None — absent sidecar must not invent an instructions field
    assert ToolRegistry("t").server.create_initialization_options().instructions is None


# ---------------------------------------------------------------------------
# §9-3 — registry validation mode matrix
# ---------------------------------------------------------------------------


def _with_enforce(mode: str) -> str:
    return VALID_SIDECAR.replace('enforce = "warn"', f'enforce = "{mode}"')


@pytest.mark.parametrize("write", [True, False])
@pytest.mark.parametrize("mode", ["off", "warn", "reject"])
def test_known_empty_and_omitted_always_pass(sidecar, mode, write):
    sidecar(_with_enforce(mode))
    for pid in (None, "", "cycia-mc"):
        assert operating_context.check_project_id(pid, "a", write) == (pid, None, None)


@pytest.mark.parametrize("write", [True, False])
def test_unknown_id_off_mode_is_silent(sidecar, write):
    sidecar(_with_enforce("off"))
    assert operating_context.check_project_id("unknown", "a", write) == ("unknown", None, None)


@pytest.mark.parametrize("write", [True, False])
def test_unknown_id_warn_mode_warns_but_passes(sidecar, write):
    sidecar(_with_enforce("warn"))
    resolved, warning, error = operating_context.check_project_id("unknown", "a", write)
    assert resolved == "unknown" and error is None
    assert "not in registry" in warning and "2026-07-18.1" in warning


def test_unknown_id_reject_mode_rejects_writes_only(sidecar):
    sidecar(_with_enforce("reject"))
    resolved, warning, error = operating_context.check_project_id("unknown", "a", write=True)
    assert resolved is None and warning is None and "not in registry" in error
    # reads still warn rather than reject (§5.1 damage asymmetry)
    resolved, warning, error = operating_context.check_project_id("unknown", "a", write=False)
    assert resolved == "unknown" and "not in registry" in warning and error is None


# ---------------------------------------------------------------------------
# §9-4 — @auto sentinel
# ---------------------------------------------------------------------------


def test_auto_resolves_the_mapped_default(sidecar):
    sidecar()
    assert operating_context.check_project_id("@auto", "claude-code", True) == ("cycia-mc", None, None)


def test_auto_mapped_to_global_skips_registry_validation(sidecar):
    # "agent.global" maps to "", which is always valid even if "" were missing
    # from project_ids — assert via a registry without the empty entry.
    sidecar(VALID_SIDECAR.replace('project_ids = ["", "cycia-mc"]', 'project_ids = ["cycia-mc"]'))
    assert operating_context.check_project_id("@auto", "agent.global", True) == ("", None, None)


def test_auto_unmapped_warn_mode_resolves_to_global_with_warning(sidecar):
    sidecar()
    resolved, warning, error = operating_context.check_project_id("@auto", "agent.unknown", True)
    assert resolved == "" and error is None
    assert "no [defaults] mapping" in warning


def test_auto_unmapped_off_mode_resolves_silently(sidecar):
    sidecar(_with_enforce("off"))
    assert operating_context.check_project_id("@auto", "agent.unknown", True) == ("", None, None)


def test_auto_unmapped_reject_mode_is_an_error(sidecar):
    sidecar(_with_enforce("reject"))
    resolved, warning, error = operating_context.check_project_id("@auto", "agent.unknown", True)
    assert resolved is None and "no [defaults] mapping" in error


def test_auto_resolved_value_is_registry_validated(sidecar):
    # mapping points outside the registry -> validated like an explicit value
    sidecar(VALID_SIDECAR.replace('"claude-code" = "cycia-mc"', '"claude-code" = "ghost"'))
    resolved, warning, error = operating_context.check_project_id("@auto", "claude-code", False)
    assert resolved == "ghost" and "not in registry" in warning and error is None


def test_explicit_values_are_never_rewritten(sidecar):
    sidecar()
    # a caller with a [defaults] mapping still gets its explicit value untouched
    assert operating_context.check_project_id("", "claude-code", True) == ("", None, None)
    resolved, _, _ = operating_context.check_project_id("cycia-mc", "claude-code", True)
    assert resolved == "cycia-mc"


# ---------------------------------------------------------------------------
# §9-4 (boundary integration) — resolution + annotation through server.py,
# with the underlying handlers stubbed (no DB).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_boundary_resolves_auto_and_echoes(sidecar, monkeypatch):
    from cpersona import server

    sidecar()
    seen = {}

    async def fake_store(agent_id, message, channel="", project_id=""):
        seen["project_id"] = project_id
        return {"ok": True, "id": 1}

    monkeypatch.setattr(server, "do_store", fake_store)
    result = await server.do_store_boundary("claude-code", {"content": "x"}, project_id="@auto")
    assert seen["project_id"] == "cycia-mc"
    assert result["resolved_project_id"] == "cycia-mc"
    assert "operating_context_warning" not in result


@pytest.mark.asyncio
async def test_store_boundary_reject_blocks_before_the_handler(sidecar, monkeypatch):
    from cpersona import server

    sidecar(_with_enforce("reject"))

    async def fake_store(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("handler called despite reject")

    monkeypatch.setattr(server, "do_store", fake_store)
    result = await server.do_store_boundary("claude-code", {"content": "x"}, project_id="ghost")
    assert result["ok"] is False and "not in registry" in result["error"]
    assert result["operating_context_revision"] == "2026-07-18.1"


@pytest.mark.asyncio
async def test_recall_boundary_warns_but_serves(sidecar, monkeypatch):
    from cpersona import server

    async def fake_recall(agent_id, query, limit, **kwargs):
        return {"messages": [], "requested_project_id": kwargs.get("project_id")}

    monkeypatch.setattr(server, "do_recall", fake_recall)
    sidecar(_with_enforce("reject"))
    result = await server.do_recall_boundary("claude-code", "q", 5, False, "", [], "ghost", "")
    assert result["requested_project_id"] == "ghost"
    assert "not in registry" in result["operating_context_warning"]


@pytest.mark.asyncio
async def test_boundaries_are_transparent_when_dormant(sidecar, monkeypatch):
    from cpersona import server

    async def fake_store(agent_id, message, channel="", project_id=""):
        return {"ok": True, "id": 2}

    monkeypatch.setattr(server, "do_store", fake_store)
    result = await server.do_store_boundary("claude-code", {"content": "x"}, project_id="anything")
    assert result == {"ok": True, "id": 2}  # no additive fields, no validation


# ---------------------------------------------------------------------------
# §9-5 — mtime-based lazy reload
# ---------------------------------------------------------------------------


def test_operator_edits_are_picked_up_live(sidecar):
    sidecar()
    assert operating_context.get_context().enforce == "warn"
    sidecar(_with_enforce("reject").replace("2026-07-18.1", "2026-07-18.2"))
    context = operating_context.get_context()
    assert context.enforce == "reject" and context.revision == "2026-07-18.2"


def test_unchanged_file_uses_the_cache(sidecar):
    sidecar()
    assert operating_context.get_context() is operating_context.get_context()


def test_a_broken_edit_degrades_and_a_fix_recovers(sidecar):
    sidecar()
    assert operating_context.get_context() is not None
    sidecar("version = [broken")
    assert operating_context.get_context() is None
    sidecar()
    assert operating_context.get_context() is not None
    assert operating_context.load_state()["parse_error"] is None


# ---------------------------------------------------------------------------
# §9-6 — health checks (§8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_parse_check_fires_on_unusable_sidecar(sidecar):
    from cpersona.checks import check_operating_context_parse

    sidecar("version = [broken")
    issues = await check_operating_context_parse(None)
    assert issues and issues[0]["type"] == "operating_context_parse_error"


@pytest.mark.asyncio
async def test_health_checks_are_quiet_on_valid_or_absent(sidecar):
    from cpersona.checks import check_operating_context_parse, check_operating_context_size

    assert await check_operating_context_parse(None) == []  # absent file
    assert await check_operating_context_size(None) == []
    sidecar()
    assert await check_operating_context_parse(None) == []
    assert await check_operating_context_size(None) == []


@pytest.mark.asyncio
async def test_health_size_check_fires_over_budget(sidecar):
    from cpersona.checks import check_operating_context_size

    big = VALID_SIDECAR.replace(
        "CPersona operating context (rev 2026-07-18.1).",
        "x" * (operating_context.SUMMARY_WARN_CHARS + 100),
    )
    sidecar(big)
    issues = await check_operating_context_size(None)
    assert issues and issues[0]["type"] == "operating_context_summary_oversized"
    assert issues[0]["summary_len"] > operating_context.SUMMARY_WARN_CHARS


@pytest.mark.asyncio
async def test_health_registry_contains_the_two_checks():
    from cpersona.checks import HEALTH_CHECKS

    by_name = {c.name: c for c in HEALTH_CHECKS}
    assert by_name["operating_context_parse"].base_severity == "warn"
    assert not by_name["operating_context_parse"].fix_capable
    assert by_name["operating_context_size"].base_severity == "info"
    assert not by_name["operating_context_size"].fix_capable


# ---------------------------------------------------------------------------
# get_operating_context tool surface (§6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_operating_context_preview_tier(sidecar):
    from cpersona.server import do_get_operating_context

    sidecar()
    result = await do_get_operating_context()
    assert result["ok"] is True
    assert result["context_revision"] == "2026-07-18.1"
    assert result["registry"] == {"project_ids": ["", "cycia-mc"], "enforce": "warn"}
    assert result["defaults"] == {"claude-code": "cycia-mc", "agent.global": ""}
    assert result["doctrine_sections"] == ["agent-id-conventions", "recall-discipline"]
    # preview tier: section bodies are NOT in the no-args response
    assert "recall: limit<=5" not in str(result)


@pytest.mark.asyncio
async def test_get_operating_context_section_body(sidecar):
    from cpersona.server import do_get_operating_context

    sidecar()
    result = await do_get_operating_context("recall-discipline")
    assert result["ok"] is True and "limit<=5" in result["body"]
    missing = await do_get_operating_context("nope")
    assert missing["ok"] is False and "unknown doctrine section" in missing["error"]
    assert missing["doctrine_sections"] == ["agent-id-conventions", "recall-discipline"]


@pytest.mark.asyncio
async def test_get_operating_context_reports_dormant_reason(sidecar, monkeypatch):
    from cpersona.server import do_get_operating_context

    result = await do_get_operating_context()  # absent file
    assert result["ok"] is False and "no sidecar file" in result["error"]
    sidecar("version = [broken")
    result = await do_get_operating_context()
    assert result["ok"] is False and "sidecar unusable" in result["error"]
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "off")
    result = await do_get_operating_context()
    assert result["ok"] is False and "disabled" in result["error"]
