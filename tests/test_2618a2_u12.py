"""Regression tests for the dashboard's owner proofs and sign-in secrets (2.6.18a2).

The dashboard promises that another user of the same machine can reach its port
but not the data. Two ways round that are pinned here:

  bug-914  The proof the command line sends with "code" and "stop" covered only the
           action and a time. Any dashboard of the store took it for a minute, so a
           program that answered the instance check on the port the command line
           asked could pass the proof on to the real dashboard (on another port,
           or on the same port once it had let it go) and get a sign-in code.
  bug-918  The browser was started with the secret-bearing address as an argument
           (on Linux, xdg-open and the browser itself), and other accounts can read
           a process's arguments: the bearer token of open_dashboard, the one-time
           code of the command line.
"""

import http.client
import json
import os
import sqlite3
import stat
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import pytest
import pytest_asyncio

from cpersona import config, database
from cpersona.dashboard import local, remote, web
from cpersona.dashboard.__main__ import main as dashboard_main

SECRET = "TOP SECRET u12 note"


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    await database.get_db()


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_DASHBOARD_STORES_PATH", str(tmp_path / "stores.json"))
    monkeypatch.delenv("CPERSONA_DB_PATH", raising=False)
    remote.clear()
    yield
    remote.clear()


def _db(tmp_path):
    path = tmp_path / "owner.db"
    src = sqlite3.connect(database.DB_PATH)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, timestamp) VALUES (?,?,?,?,?)",
        ("owner", "", "", SECRET, "2026-07-01T00:00:00+00:00"),
    )
    dst.commit()
    dst.close()
    return str(path)


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    r = conn.getresponse()
    body = r.read()
    out = (r.status, body, r.getheader("Set-Cookie"))
    conn.close()
    return out


def _serve(db, port=0):
    server, _url = web.make_server(db, port, local=local.Sessions(db))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(server):
    server.shutdown()
    server.server_close()


def _squatter(db, captured, port=0):
    """A program that answers the instance check as ``db``'s dashboard (the store's
    digest needs no key file) and keeps the proof it is sent."""

    class Squatter(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - the stdlib's name
            if self.path.startswith("/api/instance"):
                return self._json(200, {"app": local.APP, "store": local.store_id(db), "version": "x"})
            if self.path.startswith("/api/code"):
                captured["proof"] = self.headers.get("X-Dashboard-Proof", "")
                return self._json(200, {"code": "decoy"})
            return self._json(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", port), Squatter)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _loot(real_port, proof):
    """What the proof opens on the real dashboard: its status, and the data a session
    made from the code reads (b"" when no code was given)."""
    status, body, _c = _get(real_port, "/api/code", {"X-Dashboard-Proof": proof})
    if status != 200:
        return status, b""
    code = json.loads(body)["code"]
    _s, _b, cookie = _get(real_port, "/api/session", {"X-Dashboard-Code": code})
    return status, _get(real_port, "/api/memories", {"Cookie": cookie.split(";")[0]})[1]


# ---- bug-914: a proof is taken only by the dashboard it was sent to


def test_bug_914_a_proof_relayed_from_another_port_opens_nothing(tmp_path):
    db = _db(tmp_path)
    real = _serve(db)
    real_port = real.server_address[1]
    captured = {}
    squat = _squatter(db, captured)
    try:
        assert dashboard_main(["--db", db, "--port", str(squat.server_address[1]), "--no-browser"]) == 0
        assert captured.get("proof")
        status, data = _loot(real_port, captured["proof"])
        assert SECRET.encode() not in data, "a relayed proof opened a session that reads the store"
        assert status == 403, f"a proof made for port {squat.server_address[1]} was taken on {real_port}"
        # The owner's own command line still gets a code from the real dashboard.
        assert local.ask(real_port, "code", db)[0] == 200
    finally:
        _stop(squat)
        _stop(real)


def test_bug_914_a_proof_made_before_the_dashboard_started_opens_nothing(tmp_path):
    # The program on the port takes the proof, lets the port go, and the owner's
    # next run starts the real dashboard there: the proof names the right port.
    db = _db(tmp_path)
    captured = {}
    squat = _squatter(db, captured)
    port = squat.server_address[1]
    try:
        assert dashboard_main(["--db", db, "--port", str(port), "--no-browser"]) == 0
    finally:
        _stop(squat)
    time.sleep(0.01)  # a proof is stamped to the millisecond
    real = _serve(db, port)
    try:
        status, data = _loot(port, captured["proof"])
        assert SECRET.encode() not in data, "an earlier program's proof opened a session that reads the store"
        assert status == 403, "a proof made before this dashboard started was taken"
        assert local.ask(port, "code", db)[0] == 200
    finally:
        _stop(real)


def test_bug_914_an_old_proof_is_refused_by_a_dashboard_that_started_before_it(tmp_path):
    # The minute a proof lasts is kept apart from the start check: this dashboard
    # has run for an hour, so only the proof's age can refuse it.
    db = _db(tmp_path)
    sessions = local.Sessions(db)
    sessions.started = time.time() - 3600
    stale = local.proof(db, "code", 48701, now=time.time() - local.PROOF_SECONDS - 5)
    assert not sessions.proof_ok("code", stale, 48701)
    assert sessions.proof_ok("code", local.proof(db, "code", 48701), 48701)
    # Nor does a proof for one port pass on another.
    assert not sessions.proof_ok("code", local.proof(db, "code", 48701), 48702)


# ---- bug-918: the browser is never handed the secret on its command line


def _linux_browser(monkeypatch):
    """webbrowser as CPython registers it first on Linux when xdg-open is installed,
    with the process it starts recorded instead of started."""
    spawned = []

    class FakePopen:
        def __init__(self, cmdline, **kw):
            spawned.append(list(cmdline))

        def poll(self):
            return None

    monkeypatch.setattr(webbrowser, "_tryorder", ["xdg-open"])
    monkeypatch.setattr(webbrowser, "_browsers", {"xdg-open": [None, webbrowser.BackgroundBrowser("xdg-open")]})
    monkeypatch.setattr(webbrowser, "_os_preferred_browser", None)
    monkeypatch.setattr(webbrowser.subprocess, "Popen", FakePopen)
    return spawned


def _forwards_to(spawned, address, secret):
    """The browser was given an owner-only local page that sends it on to
    ``address``; the secret is in no argument."""
    assert spawned, "no browser was started"
    assert not any(secret in arg for cmd in spawned for arg in cmd), "the secret is on a child's command line"
    target = spawned[-1][-1]
    assert target.startswith("file://"), target
    page = unquote(urlparse(target).path)
    assert stat.S_IMODE(os.stat(page).st_mode) == 0o600
    text = open(page, encoding="utf-8").read()
    assert f'http-equiv="refresh" content="0;url={address}"' in text
    return page


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
@pytest.mark.asyncio
async def test_bug_918_open_dashboard_keeps_the_token_off_the_command_line(monkeypatch):
    from cpersona import server
    from cpersona.dashboard import embedded

    monkeypatch.setattr(config, "DB_PATH", database.DB_PATH)
    monkeypatch.setattr(web, "FORWARD_SECONDS", 0.2, raising=False)
    spawned = _linux_browser(monkeypatch)
    try:
        out = await server.do_open_dashboard(open_browser=True)
        assert out["ok"] and out["opened"] is True
        page = _forwards_to(spawned, out["url"], out["url"].split("#t=")[1])
        # The page goes once the browser has had time to read it.
        deadline = time.monotonic() + 5
        while os.path.exists(page) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not os.path.exists(page)
    finally:
        embedded.stop_all()


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_bug_918_the_command_line_keeps_the_sign_in_code_off_the_command_line(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)
    real = _serve(db)
    port = real.server_address[1]
    spawned = _linux_browser(monkeypatch)
    try:
        assert dashboard_main(["--db", db, "--port", str(port)]) == 0
        address = capsys.readouterr().out.split("(read-only): ", 1)[1].split()[0]
        assert address.startswith(f"http://127.0.0.1:{port}/#c=")
        _forwards_to(spawned, address, address.split("#c=")[1])
    finally:
        _stop(real)


def test_bug_918_a_page_that_cannot_be_written_leaves_the_secret_out(monkeypatch):
    def refuse(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(web.tempfile, "mkstemp", refuse)
    assert web.browser_address("http://127.0.0.1:48700/#c=one-time") == "http://127.0.0.1:48700/"
