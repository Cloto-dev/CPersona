"""The desktop shortcut that opens one store's dashboard (2.6.16).

Each check is against the shortcut that would open the wrong thing or nothing: a
store path cut at a space or eaten by a shell, the default port written in (so a
later change of default is ignored), a token in the command, a second store's
shortcut replacing the first's, a Windows console left open.
"""

import os
import plistlib
import shlex

import pytest
import pytest_asyncio

from cpersona import database
from cpersona.dashboard import local, shortcut
from cpersona.dashboard.__main__ import main as dashboard_main

ODD = "My Store $HOME 100% `x`.db"  # a space, a shell variable, a percent, a backtick


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    await database.get_db()


@pytest.fixture
def py(monkeypatch):
    monkeypatch.setattr(shortcut, "interpreter", lambda windowless=False: "/opt/py/bin/python3" if not windowless else r"C:\Py\pythonw.exe")


def test_the_command_names_the_store_and_only_a_port_other_than_the_default(tmp_path):
    db = str(tmp_path / "a.db")
    assert shortcut.arguments(db) == ["-m", "cpersona", "dashboard", "--db", db]
    assert shortcut.arguments(db, 48800)[-2:] == ["--port", "48800"]
    assert shortcut.arguments(db, local.DEFAULT_PORT) == shortcut.arguments(db)


def test_two_stores_get_two_shortcuts_and_the_usual_one_a_plain_name(tmp_path):
    assert shortcut._name(str(tmp_path / "cpersona.db")) == "CPersona Dashboard"
    assert shortcut._name(str(tmp_path / "work.db")) == "CPersona Dashboard (work)"
    assert shortcut._slug(str(tmp_path / "My Work.db")) == "cpersona-dashboard-my-work"


def test_the_mac_app_runs_the_store_in_the_background_and_leaves_the_dock(tmp_path, py):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    db = str(tmp_path / ODD)
    made = shortcut.install(db, home=str(home), system="darwin")
    app = home / "Applications" / "CPersona Dashboard (My Store $HOME 100% `x`).app"
    assert made == [str(app), str(home / "Desktop" / app.name)]
    assert os.path.islink(made[1]) and os.readlink(made[1]) == str(app)
    script = (app / "Contents" / "MacOS" / "cpersona-dashboard").read_text()
    line = [ln for ln in script.splitlines() if ln.startswith("nohup ")][0]
    # The shell reads back exactly the command: the odd path is one argument, unexpanded.
    words = shlex.split(line)
    assert words[1:] == ["/opt/py/bin/python3", "-m", "cpersona", "dashboard", "--db", db, ">/dev/null", "2>&1", "&"]
    assert oct((app / "Contents" / "MacOS" / "cpersona-dashboard").stat().st_mode & 0o777) == "0o700"
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "cpersona-dashboard" and info["LSUIElement"] is True
    assert (app / "Contents" / "Resources" / "cpersona.icns").stat().st_size > 1000
    assert "#t=" not in script and "token" not in script.lower()
    # Made again, it replaces; removed, both go.
    assert shortcut.install(db, home=str(home), system="darwin")[0] == str(app)
    assert sorted(shortcut.remove(db, home=str(home), system="darwin")) == sorted(made)
    assert not app.exists() and not os.path.lexists(made[1])


def test_the_linux_entry_keeps_an_odd_path_as_one_argument(tmp_path, py, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(shortcut, "_linux_desktop_dir", lambda h: os.path.join(h, "Desktop"))
    db = str(tmp_path / ODD)
    made = shortcut.install(db, 48800, home=str(home), system="linux")
    assert made == [str(tmp_path / "data" / "applications" / "cpersona-dashboard-my-store--home-100---x-.desktop"),
                    str(home / "Desktop" / "cpersona-dashboard-my-store--home-100---x-.desktop")]
    entry = open(made[0]).read()
    exec_line = [ln for ln in entry.splitlines() if ln.startswith("Exec=")][0][len("Exec="):]
    # The freedesktop rules: %% is a percent, and a quoted argument escapes " ` $ \.
    assert exec_line == (
        '/opt/py/bin/python3 -m cpersona dashboard --db '
        + '"' + db.replace("$", "\\$").replace("`", "\\`").replace("%", "%%") + '" --port 48800'
    )
    assert "Terminal=false" in entry and "Type=Application" in entry
    assert oct(os.stat(made[1]).st_mode & 0o777) == "0o700"
    assert sorted(shortcut.remove(db, home=str(home), system="linux")) == sorted(made)


def test_the_windows_script_uses_the_windowless_python_and_quotes_every_path(tmp_path, py):
    db = str(tmp_path / "it's here.db")
    script = shortcut.windows_script(db)
    assert "GetFolderPath('Desktop')" in script and "GetFolderPath('Programs')" in script
    assert r"$s.TargetPath = 'C:\Py\pythonw.exe'" in script
    # PowerShell single quotes double a quote inside; list2cmdline quotes a spaced path.
    assert "it''s here.db" in script and '--db "' in script
    assert "$s.Save()" in script and "#t=" not in script
    removal = shortcut.windows_script(db, remove=True)
    assert "Remove-Item -LiteralPath $p" in removal and "CreateShortcut" not in removal


def test_the_command_line_makes_the_shortcut_and_starts_no_dashboard(tmp_path, monkeypatch, capsys):
    import sqlite3

    from cpersona.dashboard import web

    db = str(tmp_path / "s.db")
    src, dst = sqlite3.connect(database.DB_PATH), sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()
    monkeypatch.setenv("CPERSONA_DASHBOARD_STORES_PATH", str(tmp_path / "stores.json"))
    seen = {}
    monkeypatch.setattr(shortcut, "install", lambda path, port: seen.setdefault("args", (path, port)) and ["/somewhere/x"])
    monkeypatch.setattr(web, "make_server", lambda *a, **k: pytest.fail("a dashboard started"))
    assert dashboard_main(["--db", db, "--install-shortcut"]) == 0
    assert seen["args"] == (db, local.DEFAULT_PORT)
    assert "/somewhere/x" in capsys.readouterr().out
