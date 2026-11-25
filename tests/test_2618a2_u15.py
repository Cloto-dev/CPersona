"""Regression tests for two test-gate holes.

bug-840: tests/behaviour_252.py pinned its hermetic environment with
os.environ.setdefault, so the capture script (a bare process where conftest never
runs) kept a CPERSONA_DB_PATH exported in the shell, and observe() then emptied
that store.

bug-841: Gate 13 (excluded vendored modules must be unreachable) seeded its
import walk with a non-recursive glob, so cpersona/dashboard/ was never read.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _child_env(tmp_path, **overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CPERSONA_")}
    env.update(
        CPERSONA_EMBEDDING_MODE="none",
        CPERSONA_OPERATING_CONTEXT="off",
        CPERSONA_DASHBOARD_STORES_PATH=str(tmp_path / "stores.json"),
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
    )
    env.update(overrides)
    return env


def _run_child(tmp_path, code, env):
    script = tmp_path / "child.py"
    script.write_text(code, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(script), str(ROOT)],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, r.stderr[-3000:]
    return r.stdout


# Creates the stand-in store through cpersona itself, without importing behaviour_252.
_SEED_CHILD = r'''
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from cpersona import database
async def main():
    db = await database.get_db()
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('victim', 'precious', '{}', '2026-01-01T00:00:00+00:00')"
    )
    await db.commit()
    # aiosqlite threads are non-daemon: close every cached connection or the child hangs.
    for attr in ("_read_db", "_bg_read_db", "_side_read_db", "_db"):
        conn = getattr(database, attr, None)
        if conn is not None:
            await conn.close()
            setattr(database, attr, None)
asyncio.run(main())
'''


_OBSERVE_CHILD = r'''
import asyncio, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/tests")
import behaviour_252 as b
from cpersona import config
print("DB_PATH=" + config.DB_PATH)
async def main():
    await b.observe(b.SCENARIOS[0])
    await b.close_db()
asyncio.run(main())
'''


def test_bug_840_capture_import_leaves_exported_store_untouched(tmp_path):
    # Stands in for a developer's real store, exported in the shell.
    victim = tmp_path / "real-store.db"
    env = _child_env(tmp_path, CPERSONA_DB_PATH=str(victim))
    _run_child(tmp_path, _SEED_CHILD, env)

    out = _run_child(tmp_path, _OBSERVE_CHILD, env)

    con = sqlite3.connect(victim)
    rows = con.execute("SELECT agent_id, content FROM memories").fetchall()
    con.close()
    assert rows == [("victim", "precious")], "observe() rewrote the exported store"
    db_line = next(line for line in out.splitlines() if line.startswith("DB_PATH="))
    assert db_line != f"DB_PATH={victim}", "the capture import kept the exported database path"


_ENV_CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/tests")
import behaviour_252  # noqa: F401
from cpersona import config
print("EMBEDDING_MODE=" + config.EMBEDDING_MODE)
print("OPERATING_CONTEXT=" + os.environ["CPERSONA_OPERATING_CONTEXT"])
'''


def test_bug_840_capture_import_overrides_exported_embedding_and_context(tmp_path):
    # Import only: nothing is embedded or sent, the child just reports what it resolved.
    env = _child_env(
        tmp_path,
        CPERSONA_DB_PATH=str(tmp_path / "exported.db"),
        CPERSONA_EMBEDDING_MODE="http",
        CPERSONA_EMBEDDING_URL="http://127.0.0.1:9/embed",
        CPERSONA_OPERATING_CONTEXT="on",
    )
    out = _run_child(tmp_path, _ENV_CHILD, env)
    assert "EMBEDDING_MODE=none" in out.splitlines(), out
    assert "OPERATING_CONTEXT=off" in out.splitlines(), out


def _gates_module():
    spec = importlib.util.spec_from_file_location(
        "structural_gates_u15", ROOT / "tests" / "test_structural_gates.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _tree_with(tmp_path, rel, transform):
    shutil.copytree(
        ROOT / "cpersona",
        tmp_path / "cpersona",
        ignore=shutil.ignore_patterns("__pycache__", "static"),
    )
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    target = tmp_path / "cpersona" / rel
    target.write_text(transform(target.read_text(encoding="utf-8")), encoding="utf-8")
    return tmp_path / "cpersona"


_MODULE_LEVEL = "from cpersona._vendored_mcp_common import semantic_cache  # noqa\n"
_LAZY = (
    "\n\ndef _planted():\n"
    "    from cpersona._vendored_mcp_common.search import hybrid_search  # noqa\n"
)


@pytest.mark.parametrize(
    ("rel", "transform"),
    [
        ("dashboard/local.py", lambda s: _MODULE_LEVEL + s),
        ("dashboard/store.py", lambda s: s + _LAZY),
    ],
    ids=["module-level-in-local", "lazy-in-store"],
)
def test_bug_841_gate13_sees_dashboard_imports(tmp_path, rel, transform):
    gates = _gates_module()
    gates.PKG = _tree_with(tmp_path, rel, transform)
    with pytest.raises(AssertionError, match="which cpersona imports"):
        gates.test_excluded_vendored_modules_are_unreachable()


def test_bug_841_gate13_unmodified_copy_is_green(tmp_path):
    # Control for the planted cases above: the copy alone passes, so their failure is the
    # planted import and not the copy.
    gates = _gates_module()
    gates.PKG = _tree_with(tmp_path, "dashboard/local.py", lambda s: s)
    gates.test_excluded_vendored_modules_are_unreachable()
