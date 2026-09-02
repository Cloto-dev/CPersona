"""`python -m cpersona.vector_index build|status` — the operator's only handle on the index.

The index is a derived file the server reads but never writes: nothing in the
running server builds it, so the documented way to get one — and to learn
whether the one on disk is usable, or how far behind the database it is — is
this entry point. It had shipped untested, with no way to name the database and
no status command; the documentation that tells an operator to run it needs
the four outcomes it can have pinned: built, declined, absent, unusable.

A subprocess round-trip on an isolated database, the way the checkup CLI is
tested: `--db` is then exercised for real (it has to take effect before the
database module is imported), the exit codes are the process's, and the shared
test connection is never closed under the other tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from tests.conftest import fake_embed_one

_VEC = np.array(fake_embed_one("cli row"), dtype=np.float32).tobytes()


def _cli(db_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CPERSONA_EMBEDDING_MODE="none")
    env.pop("CPERSONA_DB_PATH", None)  # --db must be the only thing naming the database
    return subprocess.run(
        [sys.executable, "-m", "cpersona.vector_index", "--db", db_path, "--json", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _seed(db_path: str, n: int, start: int = 0) -> None:
    """Create the database if needed and add n embedded rows, in a subprocess."""
    script = (
        "import asyncio, sys\n"
        "from cpersona.database import get_db, close_db\n"
        "vec = sys.stdin.buffer.read()\n"
        "async def s():\n"
        "    db = await get_db()\n"
        f"    for i in range({start}, {start + n}):\n"
        "        await db.execute("
        "'INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',"
        " ('cli.agent', '', '', f'row {i}', '{}', '2026-03-01T00:00:00+00:00',"
        " f'2026-03-01 00:00:{i:02d}', vec))\n"
        "    await db.commit()\n"
        "    await close_db()\n"
        "asyncio.run(s())\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        input=_VEC,
        env=dict(os.environ, CPERSONA_DB_PATH=db_path, CPERSONA_EMBEDDING_MODE="none"),
        capture_output=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr.decode()


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "index-cli-corpus")
    _seed(path, 0)  # an empty, migrated database
    return path


def test_the_four_outcomes_and_their_exit_codes(db_path):
    index_path = db_path + ".memories.vecindex"

    absent = _cli(db_path, "status")
    assert absent.returncode == 1, absent.stderr
    assert json.loads(absent.stdout)["present"] is False

    declined = _cli(db_path, "build")
    assert declined.returncode == 1, declined.stderr
    assert json.loads(declined.stdout)["built"] is False
    assert not os.path.exists(index_path), "a declined build must leave no file"

    _seed(db_path, 5)
    built = _cli(db_path, "build")
    assert built.returncode == 0, built.stderr
    result = json.loads(built.stdout)
    assert result["built"] is True and result["count"] == 5
    assert result["path"] == index_path and os.path.exists(index_path)

    status = _cli(db_path, "status")
    assert status.returncode == 0, status.stderr
    report = json.loads(status.stdout)
    assert report["usable"] is True
    assert report["rows"] == 5 and report["watermark"] == result["watermark"]
    assert report["rows_since_build"] == 0

    # Rows written after the build are what the status is for: the number the
    # scan reads exactly until the next build.
    _seed(db_path, 3, start=5)
    behind = json.loads(_cli(db_path, "status").stdout)
    assert behind["rows_since_build"] == 3

    with open(index_path, "r+b") as fh:
        fh.write(b"XXXXXXXX")  # clobber the magic
    unusable = _cli(db_path, "status")
    assert unusable.returncode == 2, unusable.stderr
    report = json.loads(unusable.stdout)
    assert report["present"] is True and report["usable"] is False
    assert "delete" in report["hint"]


def test_human_output_names_the_path_and_the_outcome(db_path):
    _seed(db_path, 2)
    env = dict(os.environ, CPERSONA_EMBEDDING_MODE="none")
    env.pop("CPERSONA_DB_PATH", None)

    def plain(*args):
        return subprocess.run(
            [sys.executable, "-m", "cpersona.vector_index", "--db", db_path, *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    built = plain("build")
    assert built.returncode == 0, built.stderr
    assert built.stdout.startswith("built ") and db_path in built.stdout
    status = plain("status")
    assert status.returncode == 0
    assert "2 rows" in status.stdout and "0 rows written since the build" in status.stdout


def test_a_missing_database_path_is_exit_2_without_creating_it(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    proc = _cli(missing, "status")
    assert proc.returncode == 2
    assert "database not found" in proc.stderr
    assert not os.path.exists(missing)
