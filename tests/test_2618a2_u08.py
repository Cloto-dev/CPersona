"""Operating context on the folder view and on an unreadable sidecar path.

bug-835: list_memories view:"folders" must resolve '@auto' and echo
resolved_project_id / operating_context_warning like every other listing.
bug-830: a sidecar path that cannot be stat'ed for a reason other than absence
must leave a check_health finding instead of reading as "never configured".
"""

import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio

from cpersona import checks, folders, operating_context, server, session
from cpersona.database import get_db

FOLDER_SIDECAR = (
    'version = 1\ncontext_revision = "r1"\n[registry]\nproject_ids = ["p1"]\n'
    'enforce = "warn"\n[defaults]\nfa = "p1"\n'
)

REJECT_SIDECAR = """
version = 1
context_revision = "r1"
[registry]
project_ids = ["", "alpha"]
enforce = "reject"
"""


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "folders", "folder_members", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


@pytest.fixture
def folder_context(tmp_path, monkeypatch):
    path = tmp_path / "oc.toml"
    path.write_text(FOLDER_SIDECAR, encoding="utf-8")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "on")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(path))
    return path


def _folder_listing():
    stub = SimpleNamespace(_tools=list(server.registry._tools), _handlers=dict(server.registry._handlers))
    stub.auto_tool = lambda *a, **k: None
    for name in ("store", "list_memories"):
        handler = stub._handlers[name]
        stub._handlers[name] = getattr(handler, "__wrapped__", handler)
    server._enable_declared_folders(stub)
    return stub._handlers["list_memories"]


@pytest.mark.asyncio
async def test_bug_835_folder_view_resolves_auto_and_echoes_the_gate(clean_db, folder_context):
    session.reset_pauses_for_tests()
    await clean_db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp) "
        "VALUES ('fa','p1','a note','2026-10-23T00:00:00+00:00')"
    )
    await clean_db.commit()
    await folders.organize("fa", [{"op": "create", "path": ["design"]}], project_id="p1")
    listing = _folder_listing()

    project = await listing({"agent_id": "fa", "project_id": "@auto", "view": "folders", "path": ["fa", "p1"]})
    assert project.get("ok", True) is not False, project
    assert project.get("resolved_project_id") == "p1", project
    assert project.get("memory_count") == 1, project
    assert [c.get("name") for c in project.get("children", [])] == ["design"], project

    agent = await listing({"agent_id": "fa", "project_id": "@auto", "view": "folders", "path": ["fa"]})
    assert agent.get("resolved_project_id") == "p1", agent
    assert agent.get("memory_count") == 1, agent
    assert [c.get("name") for c in agent.get("children", [])] == ["p1"], agent

    unknown = await listing({"agent_id": "fa", "project_id": "zz", "view": "folders", "path": ["fa"]})
    assert unknown.get("operating_context_warning") == "project_id 'zz' not in registry (rev r1)", unknown
    assert "resolved_project_id" not in unknown, unknown


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_bug_830_unsearchable_sidecar_directory_leaves_a_finding(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "operating-context.toml"
    path.write_text(REJECT_SIDECAR, encoding="utf-8")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "on")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(path))
    # Readable: the write gate bites.
    assert operating_context.check_project_id("beta", "a", True)[2] is not None

    os.chmod(home, 0o600)  # the file is still there, but os.stat now raises PermissionError
    try:
        state = operating_context.load_state()
        findings = asyncio.run(checks.check_operating_context_parse(None))
    finally:
        os.chmod(home, 0o700)

    assert state["present"] is True, state
    assert state["parse_error"], state
    assert [f["type"] for f in findings] == ["operating_context_parse_error"], findings

    # Readable again: the cached failure does not outlive the cause.
    assert operating_context.check_project_id("beta", "a", True)[2] is not None
    assert asyncio.run(checks.check_operating_context_parse(None)) == []


def test_bug_830_absent_sidecar_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "on")
    for missing in (tmp_path / "nope.toml", tmp_path / "nodir" / "nope.toml"):
        monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(missing))
        state = operating_context.load_state()
        assert state["present"] is False and state["parse_error"] is None, state
        assert asyncio.run(checks.check_operating_context_parse(None)) == []
    # A parent that is a regular file is still "no sidecar here".
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(blocker / "oc.toml"))
    state = operating_context.load_state()
    assert state["present"] is False and state["parse_error"] is None, state
    assert asyncio.run(checks.check_operating_context_parse(None)) == []
