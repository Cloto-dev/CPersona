"""Declared folders (2.6.14a1, CPERSONA_DECLARED_FOLDERS, off by default).

An agent declares folders under a project and files each record into at most one
of them. These tests pin the rules the server enforces -- the parent must exist, a
record sits in one folder, the depth limit, the record's agent and project, empty
folders only for delete, no cycles -- that a locked record is filed without being
written, that deleting a record or a folder takes its membership, and that with the
setting off the tool list and the store tool's definition are what they were.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from cpersona import config, folders
from cpersona.database import get_db

ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in (
        "memories",
        "episodes",
        "folders",
        "folder_members",
        "pending_memory_tasks",
    ):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _memory(db, agent="fa", project="p1", locked=0, content=None):
    cursor = await db.execute(
        "INSERT INTO memories (agent_id, project_id, content, timestamp, locked) VALUES (?, ?, ?, ?, ?)",
        (
            agent,
            project,
            content or f"note {agent} {project} {locked} {os.urandom(4).hex()}",
            "2026-10-23T00:00:00+00:00",
            locked,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def _organize(*ops, agent="fa", project="p1"):
    return await folders.organize(agent, list(ops), project_id=project)


def _create(*path):
    return {"op": "create", "path": list(path)}


@pytest.mark.asyncio
async def test_a_folder_needs_its_parent_and_names_what_exists(clean_db):
    report = await _organize(
        _create("design"), _create("design", "recall"), _create("release", "notes")
    )
    assert [a["folder"] for a in report["applied"]] == [
        ["design"],
        ["design", "recall"],
    ]
    assert len(report["dropped"]) == 1
    dropped = report["dropped"][0]
    assert dropped["item"] == "operations[2]"
    assert dropped["missing"] == "release" and dropped["existing"] == ["design"]


@pytest.mark.asyncio
async def test_names_compare_by_normalized_form(clean_db):
    report = await _organize(_create("Design"), _create("ｄｅｓｉｇｎ "))
    assert [a.get("created") for a in report["applied"]] == [True, False]
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM folders")
    assert rows[0][0] == 1


@pytest.mark.asyncio
async def test_a_record_sits_in_one_folder_and_filing_again_moves_it(clean_db):
    mem = await _memory(clean_db)
    await _organize(_create("a"), _create("b"))
    await _organize({"op": "file", "ref": f"mem:{mem}", "path": ["a"]})
    await _organize({"op": "file", "ref": f"mem:{mem}", "path": ["b"]})
    found = await folders.folders_of(clean_db, [f"mem:{mem}"])
    assert found == {f"mem:{mem}": ["b"]}
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM folder_members")
    assert rows[0][0] == 1


@pytest.mark.asyncio
async def test_depth_limit_refuses_create_file_and_move(clean_db, monkeypatch):
    monkeypatch.setattr(config, "FOLDER_MAX_DEPTH", 2)
    report = await _organize(_create("a"), _create("a", "b"), _create("a", "b", "c"))
    assert [a["folder"] for a in report["applied"]] == [["a"], ["a", "b"]]
    assert "deeper than the limit 2" in report["dropped"][0]["reason"]
    # a two-level subtree cannot move below a depth-1 folder: it would reach depth 3
    report = await _organize(
        _create("x"), {"op": "move", "path": ["a"], "parent": ["x"]}
    )
    assert (
        report["dropped"]
        and "deeper than the limit 2" in report["dropped"][0]["reason"]
    )
    # a folder made legal under a higher limit stays readable but takes no filing
    monkeypatch.setattr(config, "FOLDER_MAX_DEPTH", 3)
    await _organize(_create("a", "b", "c"))
    monkeypatch.setattr(config, "FOLDER_MAX_DEPTH", 2)
    mem = await _memory(clean_db)
    report = await _organize(
        {"op": "file", "ref": f"mem:{mem}", "path": ["a", "b", "c"]}
    )
    assert (
        report["dropped"]
        and "deeper than the limit 2" in report["dropped"][0]["reason"]
    )
    report = await _organize({"op": "file", "ref": f"mem:{mem}", "path": ["a", "b"]})
    assert report["applied"] and report["applied"][0]["folder"] == ["a", "b"]


@pytest.mark.asyncio
async def test_a_record_files_only_into_its_own_agent_and_project(clean_db):
    other_agent = await _memory(clean_db, agent="fb")
    other_project = await _memory(clean_db, project="p2")
    await _organize(_create("a"))
    report = await _organize(
        {"op": "file", "ref": f"mem:{other_agent}", "path": ["a"]},
        {"op": "file", "ref": f"mem:{other_project}", "path": ["a"]},
        {"op": "file", "ref": "mem:999999", "path": ["a"]},
    )
    assert not report["applied"]
    reasons = [d["reason"] for d in report["dropped"]]
    assert "not a record of this agent" in reasons[0]
    assert "belongs to project 'p2'" in reasons[1]
    assert "not a record of this agent" in reasons[2]


@pytest.mark.asyncio
async def test_a_locked_record_is_filed_without_being_written(clean_db):
    mem = await _memory(clean_db, locked=1, content="locked rule")
    before = await clean_db.execute_fetchall(
        "SELECT * FROM memories WHERE id = ?", (mem,)
    )
    await _organize(
        _create("rules"), {"op": "file", "ref": f"mem:{mem}", "path": ["rules"]}
    )
    after = await clean_db.execute_fetchall(
        "SELECT * FROM memories WHERE id = ?", (mem,)
    )
    assert before == after
    assert await folders.folders_of(clean_db, [f"mem:{mem}"]) == {
        f"mem:{mem}": ["rules"]
    }


@pytest.mark.asyncio
async def test_rename_keeps_membership_and_refuses_a_sibling_clash(clean_db):
    mem = await _memory(clean_db)
    await _organize(
        _create("a"), _create("b"), {"op": "file", "ref": f"mem:{mem}", "path": ["a"]}
    )
    members_before = await clean_db.execute_fetchall(
        "SELECT ref, folder_id FROM folder_members"
    )
    report = await _organize(
        {"op": "rename", "path": ["a"], "name": "alpha"},
        {"op": "rename", "path": ["b"], "name": "ALPHA"},
    )
    assert report["applied"][0]["folder"] == ["alpha"]
    assert "already there" in report["dropped"][0]["reason"]
    assert (
        await clean_db.execute_fetchall("SELECT ref, folder_id FROM folder_members")
        == members_before
    )
    assert await folders.folders_of(clean_db, [f"mem:{mem}"]) == {
        f"mem:{mem}": ["alpha"]
    }


@pytest.mark.asyncio
async def test_move_refuses_a_cycle(clean_db):
    report = await _organize(
        _create("a"),
        _create("a", "b"),
        {"op": "move", "path": ["a"], "parent": ["a", "b"]},
    )
    assert "into itself or below itself" in report["dropped"][0]["reason"]
    report = await _organize({"op": "move", "path": ["a", "b"], "parent": []})
    assert report["applied"][0]["folder"] == ["b"]


@pytest.mark.asyncio
async def test_delete_takes_only_an_empty_folder(clean_db):
    mem = await _memory(clean_db)
    await _organize(
        _create("a"),
        _create("a", "b"),
        _create("c"),
        {"op": "file", "ref": f"mem:{mem}", "path": ["c"]},
    )
    report = await _organize(
        {"op": "delete", "path": ["a"]},
        {"op": "delete", "path": ["c"]},
        {"op": "delete", "path": ["a", "b"]},
    )
    assert [d["reason"] for d in report["dropped"]] == [
        "the folder has folders below it",
        "the folder holds 1 records",
    ]
    assert report["applied"][0]["folder"] == ["a", "b"]
    report = await _organize(
        {"op": "unfile", "ref": f"mem:{mem}"}, {"op": "delete", "path": ["c"]}
    )
    assert len(report["applied"]) == 2


@pytest.mark.asyncio
async def test_deleting_a_record_takes_its_membership(clean_db):
    mem = await _memory(clean_db)
    await _organize(_create("a"), {"op": "file", "ref": f"mem:{mem}", "path": ["a"]})
    await clean_db.execute("DELETE FROM memories WHERE id = ?", (mem,))
    await clean_db.commit()
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM folder_members")
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_an_episode_can_be_filed(clean_db):
    cur = await clean_db.execute(
        "INSERT INTO episodes (agent_id, project_id, summary, keywords) VALUES ('fa', 'p1', 's', '')"
    )
    await clean_db.commit()
    report = await _organize(
        _create("a"), {"op": "file", "ref": f"ep:{cur.lastrowid}", "path": ["a"]}
    )
    assert report["applied"][1]["ref"] == f"ep:{cur.lastrowid}"


@pytest.mark.asyncio
async def test_file_stored_leaves_a_missing_folder_unfiled_and_names_the_existing(
    clean_db,
):
    mem = await _memory(clean_db)
    await _organize(_create("design"))
    missing = await folders.file_stored("fa", "p1", f"mem:{mem}", ["desgin"])
    assert missing["filed"] is False and missing["existing"] == ["design"]
    assert await folders.folders_of(clean_db, [f"mem:{mem}"]) == {}
    filed = await folders.file_stored("fa", "p1", f"mem:{mem}", ["design"])
    assert filed == {"filed": True, "folder": ["design"]}
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM folders")
    assert rows[0][0] == 1


def _tools_with(env: dict) -> dict:
    code = (
        "import json; from cpersona import server; "
        "t = {x.name: x for x in server.registry._tools}; "
        "print(json.dumps({'names': sorted(t), 'store': t['store'].inputSchema, "
        "'store_desc': t['store'].description}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_off_leaves_the_tools_as_they_were_and_on_adds_them(tmp_path):
    base = {"CPERSONA_DB_PATH": str(tmp_path / "x.db")}
    off = _tools_with({**base, "CPERSONA_DECLARED_FOLDERS": "false"})
    on = _tools_with({**base, "CPERSONA_DECLARED_FOLDERS": "true"})
    assert "organize_folders" not in off["names"]
    assert "folder" not in off["store"]["properties"]
    assert set(on["names"]) - set(off["names"]) == {"organize_folders"}
    assert "folder" in on["store"]["properties"]
    assert (
        on["store_desc"].startswith(off["store_desc"])
        and on["store_desc"] != off["store_desc"]
    )


# --- check_health ----------------------------------------------------------------

from cpersona import admin_handlers, checks  # noqa: E402


@pytest.mark.asyncio
async def test_unfiled_is_counted_only_where_folders_exist(clean_db):
    filed = await _memory(clean_db)
    await _memory(clean_db)
    await _memory(clean_db)
    await _memory(clean_db, project="p2")  # p2 has no folder: not listed
    await _organize(_create("a"), {"op": "file", "ref": f"mem:{filed}", "path": ["a"]})
    issues = await checks.check_folder_unfiled(clean_db, "fa", False)
    assert issues == [
        {
            "type": "folder_unfiled",
            "agent_id": "fa",
            "project_id": "p1",
            "unfiled": 2,
            "filed": 1,
        }
    ]
    assert await checks.check_folder_unfiled(clean_db, "fb", False) == []


@pytest.mark.asyncio
async def test_integrity_removes_only_mechanical_faults(clean_db, monkeypatch):
    mem = await _memory(clean_db)
    moved = await _memory(clean_db)
    await _organize(
        _create("a"),
        _create("a", "b"),
        _create("empty"),
        {"op": "file", "ref": f"mem:{mem}", "path": ["a"]},
        {"op": "file", "ref": f"mem:{moved}", "path": ["a", "b"]},
    )
    # a record that left the project behind its filing, and a membership whose record is gone
    await clean_db.execute(
        "UPDATE memories SET project_id = 'p9' WHERE id = ?", (moved,)
    )
    await clean_db.execute(
        "INSERT INTO folder_members (ref, folder_id, declared_by, declared_at) "
        "SELECT 'mem:987654', id, 'agent', 'x' FROM folders WHERE name = 'a'"
    )
    await clean_db.commit()
    monkeypatch.setattr(config, "FOLDER_MAX_DEPTH", 1)
    issues = {
        i["type"]: i for i in await checks.check_folder_integrity(clean_db, "fa", False)
    }
    assert issues["folder_member_dangling"]["refs"] == ["mem:987654"]
    assert issues["folder_member_scope"]["refs"] == [f"mem:{moved}"]
    assert (
        issues["folder_too_deep"]["count"] == 1
        and issues["folder_too_deep"]["repairable"] == 0
    )
    assert (
        issues["folder_empty"]["count"] == 1
        and issues["folder_empty"]["severity"] == "info"
    )
    await checks.check_folder_integrity(clean_db, "fa", True)
    left = await clean_db.execute_fetchall("SELECT ref FROM folder_members")
    assert left == [(f"mem:{mem}",)]
    assert await clean_db.execute_fetchall("SELECT COUNT(*) FROM folders") == [(3,)]
    assert await checks.check_folder_integrity(clean_db, "fb", False) == []


@pytest.mark.asyncio
async def test_a_cycle_is_critical_and_not_repaired(clean_db):
    await _organize(_create("a"), _create("a", "b"))
    ids = dict(await clean_db.execute_fetchall("SELECT name, id FROM folders"))
    await clean_db.execute(
        "UPDATE folders SET parent_id = ? WHERE id = ?", (ids["b"], ids["a"])
    )
    await clean_db.commit()
    issues = {
        i["type"]: i for i in await checks.check_folder_integrity(clean_db, "fa", True)
    }
    assert (
        issues["folder_cycle"]["severity"] == "critical"
        and issues["folder_cycle"]["count"] == 2
    )
    assert await clean_db.execute_fetchall("SELECT COUNT(*) FROM folders") == [(2,)]


def test_the_checks_register_only_with_the_setting_on(tmp_path):
    code = "import json; from cpersona import checks; print(json.dumps(checks.HEALTH_CHECK_NAMES))"
    names = {}
    for value in ("false", "true"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={
                **os.environ,
                "CPERSONA_DB_PATH": str(tmp_path / "x.db"),
                "CPERSONA_DECLARED_FOLDERS": value,
            },
        )
        names[value] = json.loads(out.stdout.strip().splitlines()[-1])
    assert set(names["true"]) - set(names["false"]) == {
        "folder_unfiled",
        "folder_integrity",
    }
    assert [n for n in names["true"] if n in names["false"]] == names["false"]


# --- export / import / merge / delete --------------------------------------------


@pytest.mark.asyncio
async def test_export_carries_folders_and_import_restores_them(
    clean_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("CPERSONA_EXPORT_DIR", str(tmp_path))
    mem = await _memory(clean_db, content="filed note")
    await _memory(clean_db, content="unfiled note")
    await _organize(
        _create("design"),
        _create("design", "recall"),
        {"op": "file", "ref": f"mem:{mem}", "path": ["design", "recall"]},
    )
    out = tmp_path / "x.jsonl"
    exported = await admin_handlers.do_export_memories("fa", str(out))
    assert exported["folders"] == {"folders": 2, "members": 1}
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert (
        lines[0]["version"] == "cpersona-export/1.2" and lines[0]["folder_count"] == 2
    )
    imported = await admin_handlers.do_import_memories(str(out), target_agent_id="fz")
    assert imported["folders"] == {"folders": 2, "members": 1}, imported
    new_id = (
        await clean_db.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = 'fz' AND content = 'filed note'"
        )
    )[0][0]
    assert await folders.folders_of(clean_db, [f"mem:{new_id}"]) == {
        f"mem:{new_id}": ["design", "recall"]
    }


@pytest.mark.asyncio
async def test_an_export_without_folders_is_unchanged(clean_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_EXPORT_DIR", str(tmp_path))
    await _memory(clean_db, content="plain note")
    out = tmp_path / "y.jsonl"
    exported = await admin_handlers.do_export_memories("fa", str(out))
    header = json.loads(out.read_text().splitlines()[0])
    assert "folders" not in exported
    assert header["version"] == "cpersona-export/1.1" and "folder_count" not in header


@pytest.mark.asyncio
async def test_a_truncated_folder_section_is_refused(clean_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_EXPORT_DIR", str(tmp_path))
    mem = await _memory(clean_db, content="cut note")
    await _organize(_create("a"), {"op": "file", "ref": f"mem:{mem}", "path": ["a"]})
    out = tmp_path / "z.jsonl"
    await admin_handlers.do_export_memories("fa", str(out))
    lines = out.read_text().splitlines()
    out.write_text("\n".join(lines[:-1]) + "\n")  # drop the membership line
    result = await admin_handlers.do_import_memories(str(out), target_agent_id="fy")
    assert result["ok"] is False and "folder members" in result["error"]


@pytest.mark.asyncio
async def test_merge_moves_folders_to_the_target(clean_db):
    mem = await _memory(clean_db, content="merged note")
    await _organize(_create("a"), {"op": "file", "ref": f"mem:{mem}", "path": ["a"]})
    result = await admin_handlers.do_merge_memories("fa", "fm", mode="move")
    assert result["folders"]["members"] == 1, result
    target = (
        await clean_db.execute_fetchall("SELECT id FROM memories WHERE agent_id = 'fm'")
    )[0][0]
    assert await folders.folders_of(clean_db, [f"mem:{target}"]) == {
        f"mem:{target}": ["a"]
    }
    assert await clean_db.execute_fetchall("SELECT agent_id FROM folders") == [("fm",)]


@pytest.mark.asyncio
async def test_delete_agent_data_takes_the_folders(clean_db):
    mem = await _memory(clean_db)
    await _organize(_create("a"), {"op": "file", "ref": f"mem:{mem}", "path": ["a"]})
    await admin_handlers.do_delete_agent_data("fa")
    assert await clean_db.execute_fetchall("SELECT COUNT(*) FROM folders") == [(0,)]
    assert await clean_db.execute_fetchall("SELECT COUNT(*) FROM folder_members") == [
        (0,)
    ]


# --- store's `folder` ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_files_the_new_memory_and_keeps_it_when_the_folder_is_missing(
    clean_db,
):
    from types import SimpleNamespace

    from cpersona import server

    await _organize(_create("design"))
    stub = SimpleNamespace(
        _tools=list(server.registry._tools), _handlers=dict(server.registry._handlers)
    )
    stub.auto_tool = lambda *a, **k: None
    original = stub._handlers["store"]
    stub._handlers["store"] = getattr(original, "__wrapped__", original)
    server._enable_declared_folders(stub)
    filed = await stub._handlers["store"](
        {
            "agent_id": "fa",
            "project_id": "p1",
            "content": "a filed memory",
            "folder": ["design"],
        }
    )
    assert filed["result"] == "stored" and filed["folder"] == {
        "filed": True,
        "folder": ["design"],
    }
    missing = await stub._handlers["store"](
        {
            "agent_id": "fa",
            "project_id": "p1",
            "content": "a misfiled memory",
            "folder": ["desgin"],
        }
    )
    assert missing["result"] == "stored"
    assert missing["folder"]["filed"] is False and missing["folder"]["existing"] == [
        "design"
    ]
    plain = await stub._handlers["store"](
        {"agent_id": "fa", "project_id": "p1", "content": "no folder"}
    )
    assert "folder" not in plain
