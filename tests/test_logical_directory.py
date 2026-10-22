"""The logical directory (a trial in the 2.6 line, CPERSONA_LOGICAL_DIRECTORY, off by default).

list_memories, with the setting on, takes a `path` and answers with one level of
a read-only folder view: agent / project / channel / UTC year / UTC month. These
tests pin the counts against the rows the fixture placed (written out by hand, not
recomputed), the UTC month across mixed offsets, the isolation of projects, the
order of the refs, and -- the reason it can ship switched off -- that off leaves
the tool's definition and its answers exactly as they were.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from cpersona import admin_handlers, server
from cpersona.database import get_db

ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _memory(db, agent, timestamp, project="", channel="", locked=0):
    cursor = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, timestamp, locked) VALUES (?, ?, ?, ?, ?, ?)",
        (agent, project, channel, f"note at {timestamp}", timestamp, locked),
    )
    return cursor.lastrowid


async def _episode(db, agent, start_time, created_at, project="", channel=""):
    cursor = await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, start_time, created_at) "
        "VALUES (?, ?, ?, 's', '', ?, ?)",
        (agent, project, channel, start_time, created_at),
    )
    return cursor.lastrowid


async def _seed(db):
    """dirA: project p1 / channel c1 holds 3 memories in 2026-09 and 2 in 2026-10 (one
    locked); the global pool / no channel holds 1 memory and 1 episode in 2026-10 (the
    episode has no start_time, so its time is created_at). dirB: p2 holds 2 memories."""
    for day in (1, 2, 3):
        await _memory(db, "dirA", f"2026-09-0{day}T10:00:00+00:00", "p1", "c1")
    await _memory(db, "dirA", "2026-10-01T10:00:00+00:00", "p1", "c1", locked=1)
    await _memory(db, "dirA", "2026-10-02T10:00:00+00:00", "p1", "c1")
    await _memory(db, "dirA", "2026-10-03T10:00:00+00:00")
    await _episode(db, "dirA", None, "2026-10-05 00:00:00")
    await _memory(db, "dirB", "2026-10-04T10:00:00+00:00", "p2")
    await _memory(db, "dirB", "2026-10-05T10:00:00+00:00", "p2")
    await db.commit()


def _children(result):
    return {c["name"]: (c["memory_count"], c["episode_count"]) for c in result["children"]}


@pytest.mark.asyncio
async def test_each_level_counts_what_the_fixture_placed(clean_db):
    await _seed(clean_db)

    top = await admin_handlers.do_list_directory("", 100, [])
    assert top["level"] == "agent"
    assert _children(top) == {"dirA": (6, 1), "dirB": (2, 0)}
    assert (top["memory_count"], top["episode_count"], top["locked"]) == (8, 1, 1)

    projects = await admin_handlers.do_list_directory("", 100, ["dirA"])
    assert projects["level"] == "project"
    assert _children(projects) == {"": (1, 1), "p1": (5, 0)}

    channels = await admin_handlers.do_list_directory("", 100, ["dirA", "p1"])
    assert _children(channels) == {"c1": (5, 0)}

    years = await admin_handlers.do_list_directory("", 100, ["dirA", "p1", "c1"])
    assert years["level"] == "year"
    assert _children(years) == {"2026": (5, 0)}

    months = await admin_handlers.do_list_directory("", 100, ["dirA", "p1", "c1", "2026"])
    assert months["level"] == "month"
    assert [c["name"] for c in months["children"]] == ["10", "09"]  # newest first
    assert _children(months) == {"10": (2, 0), "09": (3, 0)}
    assert months["locked"] == 1
    assert months["first"] == "2026-09-01T10:00:00+00:00"
    assert months["last"] == "2026-10-02T10:00:00+00:00"


@pytest.mark.asyncio
async def test_children_add_up_to_their_parent_everywhere(clean_db):
    await _seed(clean_db)

    async def walk(path):
        node = await admin_handlers.do_list_directory("", 500, path)
        if "children" not in node:
            assert len(node["refs"]) == node["memory_count"] + node["episode_count"]
            return
        assert sum(c["memory_count"] for c in node["children"]) == node["memory_count"]
        assert sum(c["episode_count"] for c in node["children"]) == node["episode_count"]
        for child in node["children"]:
            await walk([*path, child["name"]])

    await walk([])


@pytest.mark.asyncio
async def test_the_month_is_the_utc_month_whatever_offset_the_time_was_written_with(clean_db):
    late = await _memory(clean_db, "dirT", "2026-10-31T23:30:00-01:00")  # 2026-11-01 00:30 UTC
    early = await _memory(clean_db, "dirT", "2026-11-01T08:00:00+09:00")  # 2026-10-31 23:00 UTC
    naive = await _memory(clean_db, "dirT", "2026-10-15 12:00:00")  # naive is UTC (bug-114)
    unreadable = await _memory(clean_db, "dirT", "not a time")
    await clean_db.commit()

    years = await admin_handlers.do_list_directory("", 100, ["dirT", "", ""])
    assert [c["name"] for c in years["children"]] == ["2026", ""]  # unreadable last
    november = await admin_handlers.do_list_directory("", 100, ["dirT", "", "", "2026", "11"])
    october = await admin_handlers.do_list_directory("", 100, ["dirT", "", "", "2026", "10"])
    unknown = await admin_handlers.do_list_directory("", 100, ["dirT", "", "", "", ""])
    assert november["refs"] == [f"mem:{late}"]
    assert set(october["refs"]) == {f"mem:{early}", f"mem:{naive}"}
    assert unknown["refs"] == [f"mem:{unreadable}"]
    assert unknown["first"] is None and unknown["last"] is None


@pytest.mark.asyncio
async def test_a_project_the_caller_cannot_read_is_neither_listed_nor_counted(clean_db):
    await _memory(clean_db, "dirP", "2026-10-01T00:00:00+00:00", "x")
    await _memory(clean_db, "dirP", "2026-10-02T00:00:00+00:00", "y")
    await _memory(clean_db, "dirP", "2026-10-03T00:00:00+00:00", "y")
    await _memory(clean_db, "dirP", "2026-10-04T00:00:00+00:00", "")
    await clean_db.commit()

    seen_from_x = await admin_handlers.do_list_directory("dirP", 100, ["dirP"], project_id="x")
    assert _children(seen_from_x) == {"": (1, 0), "x": (1, 0)}  # x and the global pool, not y
    assert seen_from_x["memory_count"] == 2
    asked_for_y = await admin_handlers.do_list_directory("dirP", 100, ["dirP", "y"], project_id="x")
    assert asked_for_y["memory_count"] == 0 and asked_for_y["children"] == []
    other_agent = await admin_handlers.do_list_directory("someone-else", 100, [])
    assert other_agent["children"] == []


@pytest.mark.asyncio
async def test_a_month_lists_refs_newest_first_and_limit_cuts_from_the_oldest(clean_db):
    older = await _memory(clean_db, "dirR", "2026-10-01T00:00:00+00:00")
    episode = await _episode(clean_db, "dirR", "2026-10-02T00:00:00+00:00", "2026-10-09 00:00:00")
    newer = await _memory(clean_db, "dirR", "2026-10-03T00:00:00+00:00")
    await clean_db.commit()

    month = await admin_handlers.do_list_directory("", 100, ["dirR", "", "", "2026", "10"])
    assert month["level"] == "records"
    assert month["refs"] == [f"mem:{newer}", f"ep:{episode}", f"mem:{older}"]  # start_time wins over created_at
    cut = await admin_handlers.do_list_directory("", 2, ["dirR", "", "", "2026", "10"])
    assert cut["refs"] == [f"mem:{newer}", f"ep:{episode}"]
    assert cut["memory_count"] + cut["episode_count"] == 3  # the count says the list was cut


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [["a", "b", "c", "d", "e", "f"], ["a", 1], "dirA"])
async def test_a_malformed_path_is_refused_in_the_documented_shape(clean_db, path):
    result = await admin_handlers.do_list_directory("", 100, path)
    assert "error" in result and result["children"] == []


def _tool_in_fresh_process(setting: str | None) -> tuple[dict, str]:
    env = {k: v for k, v in os.environ.items() if k != "CPERSONA_LOGICAL_DIRECTORY"}
    if setting is not None:
        env["CPERSONA_LOGICAL_DIRECTORY"] = setting
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cpersona import config, server; "
            "t = [t for t in server.registry._tools if t.name == 'list_memories'][0]; "
            "print(json.dumps({'on': config.LOGICAL_DIRECTORY, 'description': t.description, "
            "'schema': t.inputSchema}))",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


def test_off_is_the_tool_as_it_was_and_on_adds_only_path():
    off, _ = _tool_in_fresh_process(None)
    on, _ = _tool_in_fresh_process("on")
    assert off["on"] is False and on["on"] is True
    assert "path" not in off["schema"]["properties"]
    assert on["description"] == off["description"] + server._LIST_MEMORIES_PATH_DESCRIPTION
    added = dict(on["schema"]["properties"])
    assert added.pop("path") == server._LIST_MEMORIES_PATH_PROPERTY
    assert {**on["schema"], "properties": added} == off["schema"]


def test_an_unreadable_setting_stays_off_and_says_so():
    result, stderr = _tool_in_fresh_process("onn")
    assert result["on"] is False
    assert "path" not in result["schema"]["properties"]
    assert "CPERSONA_LOGICAL_DIRECTORY" in stderr


@pytest.mark.asyncio
async def test_off_the_registered_tool_ignores_path(clean_db):
    await _seed(clean_db)
    answer = await server.registry._handlers["list_memories"]({"agent_id": "dirA", "path": []})
    assert "children" not in answer
    assert answer["count"] == 6


@pytest.mark.asyncio
async def test_on_path_opens_a_folder_and_no_path_is_still_the_list(clean_db):
    await _seed(clean_db)
    tool = [t for t in server.registry._tools if t.name == "list_memories"][0]
    stub = SimpleNamespace(_tools=[tool], _handlers={"list_memories": server.registry._handlers["list_memories"]})
    server._enable_logical_directory(stub)
    handler = stub._handlers["list_memories"]

    folder = await handler({"agent_id": "dirA", "path": ["dirA"]})
    assert _children(folder) == {"": (1, 1), "p1": (5, 0)}
    listing = await handler({"agent_id": "dirA"})
    assert listing["count"] == 6 and "children" not in listing
    assert "path" in stub._tools[0].inputSchema["properties"]
    assert "path" not in tool.inputSchema["properties"]  # the shared definition was copied, not edited
