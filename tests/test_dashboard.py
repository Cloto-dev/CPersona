"""The local dashboard (2.6.15): read-only, loopback-only, token-guarded.

These tests pin the read-only contract (``mode=ro`` refuses a write, a schema
outside the supported range is refused before anything is read, a missing file is
not created), who can read it (127.0.0.1 only, the token, the Host check, GET only),
what the views compute (the logical-directory tree and its short labels, the list's
folder / month / search filters and previews, the detail without the vector) and how
it starts (``cpersona dashboard`` and ``python -m cpersona.dashboard``).
"""

import http.client
import json
import sqlite3
import sys
import threading

import pytest
import pytest_asyncio

from cpersona import config, database
from cpersona.dashboard import store, web
from cpersona.dashboard.__main__ import main as dashboard_main
from cpersona.database import get_db


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in (
        "memories",
        "episodes",
        "record_nodes",
        "record_blocks",
        "pending_memory_tasks",
    ):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _add(conn, agent, content, ts, project="", channel="", **extra):
    cols = {
        "agent_id": agent,
        "project_id": project,
        "channel": channel,
        "content": content,
        "timestamp": ts,
        **extra,
    }
    cursor = await conn.execute(
        f"INSERT INTO memories ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )
    await conn.commit()
    return cursor.lastrowid


def _path() -> str:
    return database.DB_PATH


def _schema_file(tmp_path, version):
    path = tmp_path / f"v{version}.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
    conn.commit()
    conn.close()
    return str(path)


# ---- the read-only contract


@pytest.mark.asyncio
async def test_the_connection_refuses_a_write(db):
    await _add(db, "a", "kept as it is", "2026-10-01T00:00:00+00:00")
    conn = store.open_store(_path())
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE memories SET content = 'changed'")
    conn.close()
    rows = await db.execute_fetchall("SELECT content FROM memories")
    assert [r[0] for r in rows] == ["kept as it is"]


def test_a_newer_schema_is_refused(tmp_path):
    with pytest.raises(store.StoreError, match="newer than this dashboard"):
        store.open_store(_schema_file(tmp_path, store.MAX_SCHEMA + 1))


def test_an_older_schema_is_refused(tmp_path):
    with pytest.raises(store.StoreError, match="older than this dashboard"):
        store.open_store(_schema_file(tmp_path, store.MIN_SCHEMA - 1))


def test_the_supported_range_ends_at_this_builds_schema():
    assert store.MAX_SCHEMA == database.SCHEMA_VERSION
    assert store.MIN_SCHEMA <= store.MAX_SCHEMA


def test_a_missing_file_is_reported_and_not_created(tmp_path):
    missing = tmp_path / "none.db"
    with pytest.raises(store.StoreError, match="No database"):
        store.open_store(str(missing))
    assert not missing.exists()


def test_a_file_that_is_not_cpersona_is_refused(tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(store.StoreError, match="not a CPersona database"):
        store.open_store(str(other))


# ---- what the views compute


def test_short_labels_keep_the_part_that_differs():
    keys = [f"locomo_exp_user_{i}_speaker_a_x2611" for i in range(3)]
    assert list(store.distinct_labels(keys).values()) == [
        "…user_0_speaker…",
        "…user_1_speaker…",
        "…user_2_speaker…",
    ]
    assert store.distinct_labels(["claude-code", "agent.karin"]) == {
        "claude-code": "claude-code",
        "agent.karin": "agent.karin",
    }
    assert store.distinct_labels(["solo"]) == {"solo": "solo"}


def test_a_preview_skips_the_stamp_and_centres_on_a_match():
    text = (
        "Session date: 2023-10-22T09:55:00+00:00\n\nuser: hello there\n"
        + "word " * 40
        + "adoption news "
        + "tail " * 60
    )
    assert store.snippet(text).startswith("user: hello there")
    around = store.snippet(text, "adoption")
    assert (
        around.startswith("… ")
        and "adoption news" in around
        and "Session date" not in around
    )


@pytest.mark.asyncio
async def test_the_tree_groups_by_level_with_counts(db):
    for i, (agent, project, ts) in enumerate(
        [
            ("a", "p1", "2026-09-03"),
            ("a", "p1", "2026-10-01"),
            ("a", "", "2026-10-02"),
            ("b", "p2", "2025-01-01"),
        ]
    ):
        await _add(db, agent, f"note {i}", ts + "T00:00:00+00:00", project=project)
    conn = store.open_store(_path())
    assert [(k["key"], k["count"]) for k in store.tree(conn, [])] == [
        ("a", 3),
        ("b", 1),
    ]
    assert [(k["key"], k["count"]) for k in store.tree(conn, ["a"])] == [
        ("p1", 2),
        ("", 1),
    ]
    assert [(k["key"], k["count"]) for k in store.tree(conn, ["a", "p1", ""])] == [
        ("2026", 2)
    ]
    assert [
        (k["key"], k["count"]) for k in store.tree(conn, ["a", "p1", "", "2026"])
    ] == [("10", 1), ("09", 1)]
    assert store.tree(conn, ["a", "p1", "", "2026", "10"]) == []
    conn.close()


@pytest.mark.asyncio
async def test_the_list_filters_by_folder_month_and_search(db):
    await _add(
        db, "a", "the adoption agency called", "2026-10-01T00:00:00+00:00", project="p1"
    )
    await _add(db, "a", "a quiet day", "2026-10-02T00:00:00+00:00", project="p1")
    await _add(db, "b", "記憶の整理をした", "2026-09-05T00:00:00+00:00")
    conn = store.open_store(_path())
    everything = store.memories(conn)
    assert everything["total"] == 3
    assert [r["snippet"] for r in everything["rows"]][
        0
    ] == "a quiet day"  # newest first
    assert store.memories(conn, path=["a", "p1"])["total"] == 2
    assert store.memories(conn, month="2026-09")["total"] == 1
    assert [r["snippet"] for r in store.memories(conn, query="adoption")["rows"]] == [
        "the adoption agency called"
    ]
    # Two characters are below the trigram index; they are found by substring.
    assert store.memories(conn, query="記憶")["total"] == 1
    assert store.memories(conn, query="qu")["total"] == 1
    conn.close()


@pytest.mark.asyncio
async def test_the_detail_lists_the_split_and_never_the_vector(db):
    mid = await _add(
        db,
        "a",
        "x" * 300,
        "2026-10-01T00:00:00+00:00",
        embedding=b"\x00" * 16,
        embedding_model="m",
    )
    await db.execute(
        "INSERT INTO record_nodes (parent_kind, parent_id, node_index, start_char, end_char, token_count, window, embedding_model) "
        "VALUES ('mem', ?, 0, 0, 200, 50, 512, 'm'), ('mem', ?, 1, 200, 300, 25, 512, 'm')",
        (mid, mid),
    )
    await db.execute(
        "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id, channel, start_char, end_char, "
        "forced_boundary, embedding_bits, embedding_model) VALUES ('mem', ?, 0, 'a', '', '', 0, 120, 0, x'00', 'm')",
        (mid,),
    )
    await db.commit()
    conn = store.open_store(_path())
    m = store.memory(conn, mid)
    conn.close()
    assert [(n["start_char"], n["end_char"]) for n in m["nodes"]] == [
        (0, 200),
        (200, 300),
    ]
    assert [(b["start_char"], b["end_char"]) for b in m["blocks"]] == [(0, 120)]
    assert m["has_embedding"] is True
    assert "embedding" not in m
    json.dumps(m)  # nothing binary reaches the page


# ---- who can read it


@pytest.fixture
def served():
    server, url = web.make_server(_path(), 0, token="tok")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _get(server, path, token="tok", host=None, method="GET"):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, headers=headers)
    r = conn.getresponse()
    body = r.read()
    out = (r.status, dict(r.getheaders()), body)
    conn.close()
    return out


def test_it_listens_on_loopback_only(served):
    assert served.server_address[0] == "127.0.0.1"


def test_the_address_carries_the_token_after_the_hash():
    server, url = web.make_server(_path(), 0, token="tok")
    server.server_close()
    assert url.endswith("/#t=tok") and url.startswith("http://127.0.0.1:")


@pytest.mark.asyncio
async def test_data_needs_the_token(db, served):
    await _add(db, "a", "secret note", "2026-10-01T00:00:00+00:00")
    assert _get(served, "/api/overview", token=None)[0] == 401
    assert _get(served, "/api/overview", token="wrong")[0] == 401
    status, _, body = _get(served, "/api/overview")
    assert status == 200 and json.loads(body)["counts"]["memories"] == 1


def test_another_host_name_is_refused(served):
    assert _get(served, "/", host="attacker.example:80")[0] == 421
    assert (
        _get(
            served, "/api/overview", host=f"attacker.example:{served.server_address[1]}"
        )[0]
        == 421
    )


def test_only_get_is_answered(served):
    assert _get(served, "/api/overview", method="POST")[0] == 405
    assert _get(served, "/api/overview", method="DELETE")[0] == 405


def test_the_page_loads_nothing_from_elsewhere(served):
    status, headers, body = _get(served, "/", token=None)
    assert status == 200 and b'<script src="app.js">' in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    for name in ("index.html", "app.js"):
        text = web._static(name).decode()
        assert "onclick=" not in text and "style=" not in text  # the CSP refuses both


@pytest.mark.asyncio
async def test_a_write_by_the_server_is_seen_on_the_next_request(db, served):
    await _add(db, "a", "first", "2026-10-01T00:00:00+00:00")
    assert json.loads(_get(served, "/api/overview")[2])["counts"]["memories"] == 1
    await _add(db, "a", "second", "2026-10-02T00:00:00+00:00")
    assert json.loads(_get(served, "/api/overview")[2])["counts"]["memories"] == 2


# ---- how it starts


def test_no_database_named_is_an_error_not_a_guess(monkeypatch, capsys):
    monkeypatch.delenv("CPERSONA_DB_PATH", raising=False)
    assert dashboard_main(["--no-browser"]) == 2
    assert "CPERSONA_DB_PATH" in capsys.readouterr().err


def test_an_unreadable_database_is_an_error(tmp_path, capsys):
    assert dashboard_main(["--db", str(tmp_path / "none.db"), "--no-browser"]) == 2
    assert "No database" in capsys.readouterr().err


def test_cpersona_dashboard_opens_the_dashboard_not_the_server(monkeypatch):
    from cpersona import server

    seen = {}
    monkeypatch.setattr(
        "cpersona.dashboard.__main__.main",
        lambda argv: seen.setdefault("argv", argv) and 0,
    )
    monkeypatch.setattr(server, "main", lambda: pytest.fail("the server started"))
    monkeypatch.setattr(sys, "argv", ["cpersona", "dashboard", "--db", "x.db"])
    with pytest.raises(SystemExit):
        server.run()
    assert seen["argv"] == ["--db", "x.db"]


def test_config_db_path_is_not_used_as_a_fallback(monkeypatch, capsys):
    # The server's relative default would resolve against the terminal's directory.
    monkeypatch.delenv("CPERSONA_DB_PATH", raising=False)
    monkeypatch.setattr(config, "DB_PATH", _path())
    assert dashboard_main(["--no-browser"]) == 2


# ---- the open_dashboard tool (opened by an agent, inside the server)


@pytest.fixture
def embedded_dashboards():
    from cpersona.dashboard import embedded

    yield embedded
    embedded.stop_all()


def _tools_under(env_extra: dict) -> set[str]:
    import os
    import subprocess

    env = {**os.environ, "CPERSONA_DB_PATH": _path(), **env_extra}
    if "CPERSONA_TRANSPORT" not in env_extra:
        env.pop("CPERSONA_TRANSPORT", None)
    code = "import json; from cpersona import server, acl; print(json.dumps([sorted(t.name for t in server.registry._tools), 'open_dashboard' in acl.ACL_CLASSIFICATION]))"
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    names, classified = json.loads(out.stdout.strip().splitlines()[-1])
    assert (
        "open_dashboard" in names
    ) == classified  # the ACL row exists exactly when the tool does
    return set(names)


def test_the_tool_is_registered_under_stdio_by_default():
    assert "open_dashboard" in _tools_under({})


def test_the_setting_off_removes_the_tool():
    assert "open_dashboard" not in _tools_under({"CPERSONA_DASHBOARD": "false"})


def test_over_http_the_tool_is_not_registered():
    assert "open_dashboard" not in _tools_under(
        {"CPERSONA_TRANSPORT": "streamable-http"}
    )


@pytest.mark.asyncio
async def test_the_tool_opens_the_servers_own_database_once(
    db, monkeypatch, embedded_dashboards
):
    from cpersona import server

    monkeypatch.setattr(config, "DB_PATH", _path())
    monkeypatch.setattr(
        server.webbrowser, "open", lambda url: pytest.fail("opened a browser unasked")
    )
    await _add(db, "a", "visible through the tool", "2026-10-01T00:00:00+00:00")
    first = await server.do_open_dashboard()
    again = await server.do_open_dashboard()
    assert first["ok"] and first["read_only"] and first["opened"] is False
    assert first["url"] == again["url"] and first["url"].startswith("http://127.0.0.1:")
    port = int(first["url"].split(":")[2].split("/")[0])
    token = first["url"].split("#t=")[1]

    def get(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=150)
        conn.request(
            "GET",
            path,
            headers={"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {token}"},
        )
        r = conn.getresponse()
        out = (r.status, json.loads(r.read()))
        conn.close()
        return out

    import asyncio

    status, overview = await asyncio.to_thread(get, "/api/overview")
    assert status == 200 and overview["counts"]["memories"] == 1
    # The health report runs check_health on the server's loop, so the loop must stay free.
    status, health = await asyncio.to_thread(get, "/api/health")
    assert status == 200
    assert health["status"] in ("healthy", "degraded", "critical")
    assert isinstance(health["issues"], list) and "severity_summary" in health


@pytest.mark.asyncio
async def test_the_tool_reports_a_database_it_cannot_read(
    monkeypatch, tmp_path, embedded_dashboards
):
    from cpersona import server

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "none.db"))
    result = await server.do_open_dashboard()
    assert result.get("ok") is not True and "No database" in json.dumps(result)


def test_a_terminal_dashboard_has_no_health(served):
    status, _, body = _get(served, "/api/health")
    assert status == 404 and "agent" in json.loads(body)["error"]


def test_the_plugin_offers_a_dashboard_command():
    from pathlib import Path

    command = (
        Path(__file__).resolve().parents[1] / "plugin" / "commands" / "dashboard.md"
    )
    text = command.read_text(encoding="utf-8")
    assert text.startswith("---\ndescription:") and "`open_dashboard`" in text
