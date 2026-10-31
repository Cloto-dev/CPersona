"""The dashboard opened from the command line (2.6.16): which store it opens, the
fixed port it is found again on, the sign-in that replaces the per-start token, and
the requests only the store's owner can make.

Each check is made against the wrong answer it guards: a store list read in the
order it was written, an instance check that names the path, a code or a stop
granted without a proof, a code that opens two sessions, a cookie of one store
taken by another's dashboard.
"""

import http.client
import json
import os
import socket
import threading
import time

import pytest
import pytest_asyncio

from cpersona import database
from cpersona import known_stores as stores
from cpersona.dashboard import local, remote, web
from cpersona.dashboard.__main__ import main as dashboard_main


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    # The suite's store, made with its schema: each test copies it.
    await database.get_db()


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_DASHBOARD_STORES_PATH", str(tmp_path / "stores.json"))
    monkeypatch.delenv("CPERSONA_DB_PATH", raising=False)
    remote.clear()
    yield
    remote.clear()


def _db(tmp_path, name="a.db"):
    import sqlite3

    path = tmp_path / name
    src = sqlite3.connect(database.DB_PATH)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    return str(path)


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    r = conn.getresponse()
    body = r.read()
    out = (r.status, json.loads(body) if body else {}, r.getheader("Set-Cookie"))
    conn.close()
    return out


@pytest.fixture
def served(tmp_path):
    db = _db(tmp_path)
    sessions = local.Sessions(db)
    server, _url = web.make_server(db, 0, token="tok", local=sessions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield db, server.server_address[1], sessions, thread
    server.shutdown()
    server.server_close()


# ---- the store list


def test_the_list_keeps_the_latest_store_first_and_leaves_out_one_that_is_gone(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    stores.remember(a)
    time.sleep(1.1)  # the times are kept to the second
    stores.remember(b)
    assert [r["path"] for r in stores.known()] == [b, a]
    stores.remember(a)  # opened again: first, and not listed twice
    assert [r["path"] for r in stores.known()] == [a, b]
    os.remove(b)
    assert [r["path"] for r in stores.known()] == [a]
    # Owner-only, and paths and times only.
    file = stores.path()
    assert oct(os.stat(file).st_mode & 0o777) == "0o600"
    assert set(json.load(open(file))["stores"][0]) == {"path", "last_used"}


def test_a_note_that_cannot_be_written_never_stops_the_server(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("CPERSONA_DASHBOARD_STORES_PATH", str(blocker / "stores.json"))
    stores.remember(_db(tmp_path))  # no exception
    assert stores.known() == []


def test_a_relative_path_is_noted_as_the_file_the_server_opens(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.chdir(tmp_path)
    stores.remember("a.db")
    assert stores.known()[0]["path"] == db


# ---- the dashboard's own routes


def test_the_instance_check_names_the_store_by_a_digest_not_its_path(served):
    db, port, sessions, _t = served
    status, body, _c = _get(port, "/api/instance")
    assert status == 200 and body["app"] == local.APP and body["store"] == local.store_id(db)
    assert db not in json.dumps(body) and os.path.basename(db) not in json.dumps(body)
    assert local.probe(port) == body


def test_a_code_or_a_stop_needs_the_owners_proof(served):
    db, port, _s, thread = served
    for action in ("code", "stop"):
        assert _get(port, f"/api/{action}")[0] == 403
        assert _get(port, f"/api/{action}", {"X-Dashboard-Proof": "1.forged"})[0] == 403
        # A proof for one action does not do the other, and an old one is refused.
        other = "stop" if action == "code" else "code"
        assert _get(port, f"/api/{action}", {"X-Dashboard-Proof": local.proof(db, other)})[0] == 403
        stale = local.proof(db, action, now=time.time() - local.PROOF_SECONDS - 5)
        assert _get(port, f"/api/{action}", {"X-Dashboard-Proof": stale})[0] == 403
    assert thread.is_alive()
    status, body = local.ask(port, "code", db)
    assert status == 200 and body["code"]


def test_a_code_signs_in_once_and_the_cookie_reads_the_data(served):
    db, port, sessions, _t = served
    assert _get(port, "/api/overview")[0] == 401
    code = local.ask(port, "code", db)[1]["code"]
    status, _b, cookie = _get(port, "/api/session", {"X-Dashboard-Code": code})
    assert status == 204 and cookie.startswith(sessions.cookie + "=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    # Used up.
    assert _get(port, "/api/session", {"X-Dashboard-Code": code})[0] == 410
    pair = cookie.split(";")[0]
    assert _get(port, "/api/overview", {"Cookie": pair})[0] == 200
    # Another store's dashboard does not take this store's cookie, even renamed.
    other = local.Sessions(os.path.join(os.path.dirname(db), "b.db"))
    assert other.cookie != sessions.cookie
    assert _get(port, "/api/overview", {"Cookie": f"{other.cookie}={pair.split('=', 1)[1]}"})[0] == 401
    # The token still works, as the agent's way in.
    assert _get(port, "/api/overview", {"Authorization": "Bearer tok"})[0] == 200
    # Signing out clears the cookie.
    assert "Max-Age=0" in _get(port, "/api/logout", {"Cookie": pair})[2]


def test_a_stop_with_the_proof_stops_the_dashboard(served):
    db, port, _s, thread = served
    assert local.ask(port, "stop", db)[0] == 202
    thread.join(timeout=5)
    assert not thread.is_alive()


# ---- the command line


def test_with_no_database_named_it_opens_the_one_a_server_opened_last(tmp_path, monkeypatch, capsys):
    assert dashboard_main(["--no-browser", "--port", "0", "--list"]) == 0
    assert dashboard_main(["--no-browser", "--stop"]) == 2  # nothing noted yet: which database?
    db = _db(tmp_path)
    stores.remember(db)
    seen = {}
    monkeypatch.setattr(web, "make_server", lambda path, port, **kw: seen.setdefault("db", path) and (_ for _ in ()).throw(OSError("stop here")))
    assert dashboard_main(["--no-browser", "--port", "0"]) == 2
    assert seen["db"] == db


def test_a_second_run_for_the_same_store_opens_the_running_one(served, monkeypatch, capsys):
    db, port, _s, _t = served
    monkeypatch.setattr(web, "make_server", lambda *a, **kw: pytest.fail("a second dashboard started"))
    assert dashboard_main(["--db", db, "--port", str(port), "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{port}/#c=" in out


def test_a_port_held_by_another_store_or_program_is_reported_not_shared(served, tmp_path, capsys):
    _db_a, port, _s, _t = served
    b = _db(tmp_path, "b.db")
    assert dashboard_main(["--db", b, "--port", str(port), "--no-browser"]) == 2
    assert "another database" in capsys.readouterr().err
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    try:
        busy = other.getsockname()[1]
        threading.Thread(target=lambda: other.accept()[0].close(), daemon=True).start()
        assert dashboard_main(["--db", b, "--port", str(busy), "--no-browser"]) == 2
        assert "another program" in capsys.readouterr().err
    finally:
        other.close()


def test_stop_from_the_command_line_stops_only_this_stores_dashboard(served, tmp_path, capsys):
    db, port, _s, thread = served
    b = _db(tmp_path, "b.db")
    assert dashboard_main(["--db", b, "--port", str(port), "--stop"]) == 1
    assert thread.is_alive()
    assert dashboard_main(["--db", db, "--port", str(port), "--stop"]) == 0
    thread.join(timeout=5)
    assert not thread.is_alive()
