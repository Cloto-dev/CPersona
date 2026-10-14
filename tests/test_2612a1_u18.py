"""The suite's environment is hermetic even when the shell exports CPersona settings.

``tests/conftest.py`` pins the database, the embedding mode and the operating
context before ``cpersona`` is imported. Pinned with ``setdefault``, a value the
developer's shell already exported won: ``CPERSONA_DB_PATH`` pointing at a real
store bound ``config.DB_PATH`` to it, and the fixtures' ``DELETE FROM memories``
then wiped that store; an exported ``CPERSONA_EMBEDDING_MODE=http`` reached the
suite as well.

Both tests run in a subprocess with a controlled environment and touch only files
under ``tmp_path``.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFTEST = REPO / "tests" / "conftest.py"
SENTINEL = "exported-store-sentinel-memory"


def _env_without_cpersona_settings() -> dict:
    """The current environment minus every CPersona setting, so each test decides
    exactly what the 'developer shell' exports."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CPERSONA_") and k not in ("EMBEDDING_MODE", "EMBEDDING_HTTP_URL")
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _seed_store(db: Path) -> None:
    """Create a store through cpersona's own schema, then add one memory that
    belongs to a real-looking agent."""
    env = _env_without_cpersona_settings()
    env.update(
        CPERSONA_DB_PATH=str(db),
        CPERSONA_EMBEDDING_MODE="none",
        CPERSONA_OPERATING_CONTEXT="off",
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio\n"
            "from cpersona.database import get_db\n"
            "async def m():\n"
            "    c = await get_db(); await c.commit(); await c.close()\n"
            "asyncio.run(m())\n",
        ],
        cwd=REPO,
        env=env,
        check=True,
        timeout=50,
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES ('real-user', ?, '')",
            (SENTINEL,),
        )
        conn.commit()
    finally:
        conn.close()


def _sentinels(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM memories WHERE content = ?", (SENTINEL,)).fetchone()[0]
    finally:
        conn.close()


def test_conftest_overrides_exported_settings(tmp_path):
    """Loading conftest the way pytest does, before any cpersona import, must leave
    config bound to the conftest's own temp database and to no embedding endpoint,
    whatever the shell exported."""
    exported_db = tmp_path / "exported.db"
    env = _env_without_cpersona_settings()
    env.update(
        CPERSONA_DB_PATH=str(exported_db),
        CPERSONA_EMBEDDING_MODE="http",
        CPERSONA_EMBEDDING_HTTP_URL="http://127.0.0.1:9/embed",  # discard port; never contacted
        CPERSONA_OPERATING_CONTEXT="on",
    )
    probe = (
        "import os, runpy, sys\n"
        "runpy.run_path(sys.argv[1])\n"
        "from cpersona import config\n"
        "print('DB_PATH=' + config.DB_PATH)\n"
        "print('EMBEDDING_MODE=' + config.EMBEDDING_MODE)\n"
        "print('OPERATING_CONTEXT=' + os.environ.get('CPERSONA_OPERATING_CONTEXT', ''))\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe, str(CONFTEST)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert got["DB_PATH"] != str(exported_db), "conftest let the exported CPERSONA_DB_PATH through"
    assert got["DB_PATH"].endswith("conftest.db"), got["DB_PATH"]
    assert got["EMBEDDING_MODE"] == "none", f"conftest let CPERSONA_EMBEDDING_MODE={got['EMBEDDING_MODE']} through"
    assert got["OPERATING_CONTEXT"] == "off", got["OPERATING_CONTEXT"]


def test_suite_run_leaves_an_exported_store_untouched(tmp_path):
    """End to end: a real pytest run of a module whose fixture wipes ``memories``,
    with CPERSONA_DB_PATH exported at a store holding one memory. The memory must
    survive."""
    store = tmp_path / "user-store.db"
    _seed_store(store)
    assert _sentinels(store) == 1

    env = _env_without_cpersona_settings()
    env["CPERSONA_DB_PATH"] = str(store)
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_bug439_quick_check_stale_fts_cache.py",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=55,
    )
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    after = _sentinels(store)
    assert after == 1, f"the suite's fixtures wiped the exported store: sentinel rows before=1 after={after}"
