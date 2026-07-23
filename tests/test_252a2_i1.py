"""Regression tests for fix group I1 (C8, C9) in cpersona.checkup.

C8: the ``--db`` help text advertised a default (``$CPERSONA_DB_PATH`` or
``~/.claude/cpersona.db``) that the code never implements — the real fallback is
the relative ``data/cpersona.db`` from cpersona.config. The help must describe
the true resolution order, and an unconfigured run (no ``--db`` and no
``CPERSONA_DB_PATH``) must print the resolved absolute path to stderr so a cron
run cannot silently monitor the wrong database.

C9: ``_run`` had no try/finally around ``close_db()``. An exception raised while
checking a corrupt/foreign DB — precisely the failure class this monitoring CLI
exists to catch — skipped ``close_db()``, leaving aiosqlite's non-daemon worker
thread alive so the process hung forever instead of exiting nonzero.
"""

import os
import sqlite3
import subprocess
import sys

import pytest

from cpersona import checkup


# --------------------------------------------------------------------------
# C8 — help text no longer advertises a default the code never implements
# --------------------------------------------------------------------------


def test_c8_help_states_true_resolution_order():
    help_text = checkup._build_parser().format_help()
    # The phantom '~/.claude/cpersona.db' default must be gone: no code path
    # ever resolves the DB to that location.
    assert "~/.claude/cpersona.db" not in help_text
    # The real config fallback must be named.
    assert "data/cpersona.db" in help_text
    assert "CPERSONA_DB_PATH" in help_text


# --------------------------------------------------------------------------
# C8 — unconfigured run prints the resolved absolute DB path to stderr
# --------------------------------------------------------------------------


def test_c8_unconfigured_run_announces_resolved_db_path(monkeypatch, capsys):
    # Neither --db nor CPERSONA_DB_PATH: the run must not proceed silently.
    monkeypatch.delenv("CPERSONA_DB_PATH", raising=False)

    ran = {}

    def _fake_run(coro):
        # C8 is a pre-run notice; do not actually execute the checks here.
        coro.close()  # avoid "coroutine was never awaited" warning
        ran["called"] = True
        return 0

    monkeypatch.setattr(checkup.asyncio, "run", _fake_run)

    rc = checkup.main([])

    from cpersona.config import DB_PATH

    expected = os.path.abspath(DB_PATH)
    err = capsys.readouterr().err
    assert expected in err, f"resolved DB path {expected!r} not announced on stderr: {err!r}"
    assert rc == 0
    assert ran.get("called") is True


def test_c8_explicit_db_does_not_emit_unconfigured_notice(monkeypatch, capsys, tmp_path):
    # When --db is passed, the operator configured the target explicitly: no
    # unconfigured-default notice should appear.
    db = tmp_path / "explicit.db"
    sqlite3.connect(str(db)).close()

    monkeypatch.setattr(checkup.asyncio, "run", lambda coro: (coro.close(), 0)[1])
    rc = checkup.main(["--db", str(db)])

    err = capsys.readouterr().err
    assert "default database" not in err
    assert rc == 0


# --------------------------------------------------------------------------
# C9 — a DB missing the `memories` table exits nonzero instead of hanging
# --------------------------------------------------------------------------


def test_c9_foreign_db_exits_nonzero_instead_of_hanging(tmp_path):
    # A valid SQLite file with no `memories` table stands in for the corrupt /
    # foreign / stale DB this monitoring CLI is deployed to catch. Report-only
    # mode reaches the stats COUNT(*) FROM memories, which raises. Before the
    # fix the exception skipped close_db(), so the non-daemon aiosqlite worker
    # thread kept the process alive and it hung forever.
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(str(foreign))
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    env = dict(os.environ, CPERSONA_EMBEDDING_MODE="none")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "cpersona.checkup", "--db", str(foreign)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "checkup CLI hung (did not exit within 20s) on a DB missing the "
            "'memories' table — close_db() was skipped and the non-daemon "
            "aiosqlite worker thread blocked interpreter shutdown"
        )

    assert proc.returncode != 0, (
        f"expected a nonzero exit on a foreign DB, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert "error" in combined, f"expected a readable error, got stdout+stderr={combined!r}"
