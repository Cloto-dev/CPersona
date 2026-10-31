"""A desktop shortcut that opens one store's dashboard with a double-click.

``cpersona dashboard --install-shortcut`` makes it, ``--remove-shortcut`` takes it
away. What it runs is ``python -m cpersona dashboard --db <the store>`` with no
window of its own: the first run starts the dashboard, which then keeps running
(``local``), and a later one opens the running dashboard in the default browser.
The store is fixed when the shortcut is made, so a shortcut always opens the same
one. The command holds the interpreter's path and the store's, never a secret.

- Windows: a ``.lnk`` on the desktop and in the Start menu, made by PowerShell's
  WScript.Shell (no extra package), running ``pythonw.exe`` so no console opens.
- macOS: a small ``.app`` in ``~/Applications`` with a link to it on the desktop.
  Its script starts the dashboard in the background and returns, so the app does
  not stay open in the Dock.
- Linux: a ``.desktop`` file among the applications and a copy on the desktop,
  marked as trusted where GNOME asks for it.

Reinstalling CPersona somewhere else moves the interpreter: run
``--install-shortcut`` again, which replaces the shortcut.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from importlib import resources

from cpersona import fileperms
from cpersona.dashboard import local

NAME = "CPersona Dashboard"


def _name(db_path: str) -> str:
    """The shortcut's name: the store's file name is added unless it is the usual one,
    so two stores' shortcuts do not replace each other."""
    stem = os.path.splitext(os.path.basename(db_path))[0]
    return NAME if stem in ("", "cpersona") else f"{NAME} ({stem})"


def _slug(db_path: str) -> str:
    stem = os.path.splitext(os.path.basename(db_path))[0]
    keep = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem.lower())
    return "cpersona-dashboard" if keep in ("", "cpersona") else f"cpersona-dashboard-{keep}"


def _icon(name: str) -> str:
    return str(resources.files("cpersona.dashboard").joinpath("static", "icons", name))


def interpreter(windowless: bool = False) -> str:
    """The Python that runs CPersona now; on Windows its windowless twin when asked."""
    exe = sys.executable
    if windowless and os.name == "nt":
        twin = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(twin):
            return twin
    return exe


def arguments(db_path: str, port: int = local.DEFAULT_PORT) -> list[str]:
    args = ["-m", "cpersona", "dashboard", "--db", os.path.abspath(db_path)]
    return args + ([] if port == local.DEFAULT_PORT else ["--port", str(port)])


# ---- Windows


def _ps(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def windows_script(db_path: str, port: int = local.DEFAULT_PORT, remove: bool = False) -> str:
    """The PowerShell that makes (or removes) the desktop and Start menu shortcuts.
    The folders are asked of Windows, so a desktop moved to OneDrive is found."""
    file = _name(db_path) + ".lnk"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$paths = @((Join-Path ([Environment]::GetFolderPath('Desktop')) {_ps(file)}), "
        f"(Join-Path ([Environment]::GetFolderPath('Programs')) {_ps(file)}))",
    ]
    if remove:
        lines.append("foreach ($p in $paths) { if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p } ; $p }")
        return "\n".join(lines) + "\n"
    args = subprocess.list2cmdline(arguments(db_path, port))
    lines += [
        "$shell = New-Object -ComObject WScript.Shell",
        "foreach ($p in $paths) {",
        "  $s = $shell.CreateShortcut($p)",
        f"  $s.TargetPath = {_ps(interpreter(windowless=True))}",
        f"  $s.Arguments = {_ps(args)}",
        f"  $s.WorkingDirectory = {_ps(os.path.dirname(os.path.abspath(db_path)))}",
        f"  $s.IconLocation = {_ps(_icon('cpersona.ico'))}",
        f"  $s.Description = {_ps('Open the CPersona dashboard of ' + os.path.abspath(db_path))}",
        "  $s.Save()",
        "  $p",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _run_powershell(script: str) -> list[str]:
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script, capture_output=True, text=True, check=True,
    )
    return [line for line in done.stdout.splitlines() if line.strip()]


# ---- macOS


def mac_app(db_path: str, home: str, port: int = local.DEFAULT_PORT) -> dict[str, bytes | str]:
    """The files of the ``.app`` bundle, by their path under ``home``."""
    app = os.path.join(home, "Applications", _name(db_path) + ".app")
    command = " ".join(shlex.quote(a) for a in [interpreter(), *arguments(db_path, port)])
    script = (
        "#!/bin/sh\n"
        "# Opens the CPersona dashboard of one database; made by `cpersona dashboard --install-shortcut`.\n"
        "# Started in the background so this app returns at once instead of staying in the Dock.\n"
        f"nohup {command} >/dev/null 2>&1 &\n"
    )
    info = plistlib.dumps({
        "CFBundleName": _name(db_path),
        "CFBundleDisplayName": _name(db_path),
        "CFBundleIdentifier": f"dev.cloto.{_slug(db_path)}",
        "CFBundleExecutable": "cpersona-dashboard",
        "CFBundleIconFile": "cpersona.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "LSUIElement": True,
    })
    return {
        os.path.join(app, "Contents", "Info.plist"): info,
        os.path.join(app, "Contents", "MacOS", "cpersona-dashboard"): script,
        os.path.join(app, "Contents", "Resources", "cpersona.icns"): resources.files("cpersona.dashboard")
        .joinpath("static", "icons", "cpersona.icns").read_bytes(),
    }


# ---- Linux


def _desktop_quote(arg: str) -> str:
    """One argument of a desktop entry's Exec key (the freedesktop rules)."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    if any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        escaped = f'"{escaped}"'
    return escaped.replace("%", "%%")


def linux_entry(db_path: str, port: int = local.DEFAULT_PORT) -> str:
    command = " ".join(_desktop_quote(a) for a in [interpreter(), *arguments(db_path, port)])
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_name(db_path)}\n"
        f"Comment=Open the CPersona dashboard of {os.path.abspath(db_path)}\n"
        f"Exec={command}\n"
        f"Icon={_icon('icon-512.png')}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )


def _linux_desktop_dir(home: str) -> str:
    try:
        found = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        found = ""
    return found if found and found != home else os.path.join(home, "Desktop")


# ---- making and removing


def install(db_path: str, port: int = local.DEFAULT_PORT, home: str | None = None, system: str | None = None) -> list[str]:
    """Make the shortcuts; returns where they are."""
    home = home or os.path.expanduser("~")
    system = system or sys.platform
    if system == "win32":
        return _run_powershell(windows_script(db_path, port))
    if system == "darwin":
        files = mac_app(db_path, home, port)
        for path, body in files.items():
            fileperms.makedirs_private(os.path.dirname(path))
            with fileperms.open_private(path, "wb") as fh:
                fh.write(body if isinstance(body, bytes) else body.encode())
            if path.endswith(os.path.join("MacOS", "cpersona-dashboard")):
                os.chmod(path, 0o700)  # run by its owner only, as everything else here is read
        app = os.path.join(home, "Applications", _name(db_path) + ".app")
        made = [app]
        desktop = os.path.join(home, "Desktop")
        if os.path.isdir(desktop):
            link = os.path.join(desktop, _name(db_path) + ".app")
            if os.path.islink(link):
                os.remove(link)
            if not os.path.exists(link):
                os.symlink(app, link)
                made.append(link)
        return made
    entry = linux_entry(db_path, port)
    apps = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "applications")
    made = []
    for folder in (apps, _linux_desktop_dir(home)):
        if folder != apps and not os.path.isdir(folder):
            continue
        fileperms.makedirs_private(folder)
        path = os.path.join(folder, _slug(db_path) + ".desktop")
        with fileperms.open_private(path, "w", encoding="utf-8") as fh:
            fh.write(entry)
        os.chmod(path, 0o700)  # a launcher on the desktop must be executable
        if folder != apps:
            # GNOME runs a launcher on the desktop only once it is marked trusted.
            try:
                subprocess.run(["gio", "set", path, "metadata::trusted", "true"], capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        made.append(path)
    return made


def remove(db_path: str, home: str | None = None, system: str | None = None) -> list[str]:
    """Take the shortcuts away; returns the paths that were removed."""
    home = home or os.path.expanduser("~")
    system = system or sys.platform
    if system == "win32":
        return _run_powershell(windows_script(db_path, remove=True))
    if system == "darwin":
        targets = [os.path.join(home, "Desktop", _name(db_path) + ".app"), os.path.join(home, "Applications", _name(db_path) + ".app")]
    else:
        apps = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "applications")
        targets = [os.path.join(apps, _slug(db_path) + ".desktop"), os.path.join(_linux_desktop_dir(home), _slug(db_path) + ".desktop")]
    gone = []
    for path in targets:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
            gone.append(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
            gone.append(path)
    return gone
