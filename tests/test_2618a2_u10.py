"""Dashboard graph and command line fixes for 2.6.18a2.

- bug-819: the vectors and clusters held for the dashboard are released once older
  than CACHE_SECONDS, and a store whose vectors pass CACHE_MAX_BYTES is not held.
- bug-820: the declared lines of a picture are read in batches, so a folder with more
  memories than SQLite's bound-parameter limit is drawn.
- bug-818: `cpersona dashboard --stop` reports a stop the dashboard refused as a
  failure, not as stopped.
- bug-922: the overview's month histogram escapes the month label in its attributes.
"""

import base64
import hashlib
import hmac
import http.client
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio

from cpersona import database
from cpersona.dashboard import graph, local, overall, remote, store, web
from cpersona.dashboard.__main__ import main as dashboard_main
from cpersona.database import get_db

APP_JS = Path(__file__).resolve().parent.parent / "cpersona" / "dashboard" / "static" / "app.js"


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in ("memories", "entity_mentions", "relations", "entities"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    graph.clear_cache()
    overall.clear_cache()
    yield conn
    graph.clear_cache()
    overall.clear_cache()


async def _seed_vectors(conn, n=20):
    first = None
    for i in range(n):
        v = np.random.default_rng(i).standard_normal(8).astype(np.float32).tobytes()
        cur = await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES ('a', ?, ?, ?)",
            (f"m{i}", f"2026-10-01T00:00:{i:02d}+00:00", v),
        )
        first = first or cur.lastrowid
    await conn.commit()
    return first


# ---- bug-819


@pytest.mark.asyncio
async def test_bug_819_expired_vector_copies_are_released(db, monkeypatch):
    p = await _seed_vectors(db)
    conn = store.open_store(database.DB_PATH)
    try:
        graph.around(conn, f"{database.DB_PATH}#scope=aaa", p, hops=1)
        graph.around(conn, f"{database.DB_PATH}#scope=bbb", p, hops=1)
        assert len(graph._cache) == 2
        real = time.monotonic
        monkeypatch.setattr(graph.time, "monotonic", lambda: real() + 10 * graph.CACHE_SECONDS)
        graph.around(conn, f"{database.DB_PATH}#scope=ccc", p, hops=1)
        keys = sorted(k.rsplit("=", 1)[1] for k in graph._cache)
        assert keys == ["ccc"], f"expired vector copies still held: {keys}"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_bug_819_a_store_past_the_cap_drops_its_held_copy(db, monkeypatch):
    p = await _seed_vectors(db)
    conn = store.open_store(database.DB_PATH)
    try:
        held = graph.around(conn, database.DB_PATH, p, hops=1)
        assert len(graph._cache) == 1
        monkeypatch.setattr(graph, "CACHE_MAX_BYTES", 1)  # the store now exceeds the cap
        chunked = graph.around(conn, database.DB_PATH, p, hops=1)
        assert graph._cache == {}, "an over-cap store's old vector copy is still held"
        assert chunked == held  # read in chunks, the same answer
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_bug_819_expired_clusters_are_released(db, monkeypatch):
    await _seed_vectors(db)
    conn = store.open_store(database.DB_PATH)
    try:
        overall.clusters(conn, f"{database.DB_PATH}#scope=aaa")
        overall.clusters(conn, f"{database.DB_PATH}#scope=bbb")
        assert len(overall._cache) == 2
        real = time.monotonic
        monkeypatch.setattr(overall.time, "monotonic", lambda: real() + 10 * graph.CACHE_SECONDS)
        overall.clusters(conn, f"{database.DB_PATH}#scope=ccc")
        keys = sorted(k.rsplit("=", 1)[1] for k in overall._cache)
        assert keys == ["ccc"], f"expired clusters still held: {keys}"
    finally:
        conn.close()


# ---- bug-820


@pytest.mark.asyncio
async def test_bug_820_declared_lines_are_read_in_batches(db):
    n_mem, n_ent = 1_500, 1_100
    now = "2026-10-24T00:00:00+00:00"
    await db.executemany(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES ('a', ?, ?)",
        [(f"m{i}", now) for i in range(n_mem)],
    )
    await db.executemany(
        "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES ('a', ?, ?, 'a', ?)",
        [(f"E{j}", f"e{j}", now) for j in range(n_ent)],
    )
    await db.commit()
    mems = [r[0] for r in await (await db.execute("SELECT id FROM memories ORDER BY id")).fetchall()]
    ents = [r[0] for r in await (await db.execute("SELECT id FROM entities ORDER BY id")).fetchall()]
    await db.executemany(
        "INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'a', ?)",
        [(ents[j], f"mem:{mems[j]}", now) for j in range(n_ent)],
    )
    # Entity and memory relations interleaved, so their order by id crosses batches.
    rels = []
    for i in range(n_mem - 1):
        rels.append(("mem", mems[i], "follows", "mem", mems[i + 1]))
        if i < n_ent - 1:
            rels.append(("entity", ents[i], "next", "entity", ents[i + 1]))
    await db.executemany(
        "INSERT INTO relations (agent_id, subject_kind, subject_id, predicate, object_kind, object_id, declared_by, declared_at) "
        "VALUES ('a', ?, ?, ?, ?, ?, 'a', ?)",
        [(*r, now) for r in rels],
    )
    await db.commit()
    conn = store.open_store(database.DB_PATH)
    try:
        # The limit of SQLite before 3.32, the one the batch size is chosen under.
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        out = graph.folder(conn, [], "")
    finally:
        conn.close()
    assert out["shown"] == n_mem
    declared = [x for x in out["links"] if x["kind"] == "declared"]
    mentions = sorted((x["s"], x["t"]) for x in declared if x["label"] == "mentions")
    assert mentions == sorted((f"e:{ents[j]}", f"m:{mems[j]}") for j in range(n_ent))
    name = {"entity": "e", "mem": "m"}
    assert [(x["s"], x["t"], x["label"]) for x in declared if x["label"] != "mentions"] == [
        (f"{name[a]}:{s}", f"{name[b]}:{o}", p) for a, s, p, b, o in rels
    ]
    assert sorted(n["entity"] for n in out["nodes"] if n["kind"] == "entity") == ents


# ---- bug-818


@pytest.fixture
def _dashboard_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_DASHBOARD_STORES_PATH", str(tmp_path / "stores.json"))
    monkeypatch.delenv("CPERSONA_DB_PATH", raising=False)
    remote.clear()
    yield
    remote.clear()


async def _copy_db(tmp_path):
    await database.get_db()
    path = tmp_path / "a.db"
    src = sqlite3.connect(database.DB_PATH)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    return str(path)


@pytest.mark.asyncio
async def test_bug_818_a_refused_stop_is_not_reported_as_stopped(tmp_path, monkeypatch, capsys, _dashboard_env):
    db = await _copy_db(tmp_path)
    sessions = local.Sessions(db)
    server, _url = web.make_server(db, 0, token="tok", local=sessions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        # The stopping process signs with another key than the running dashboard.
        other_key = hashlib.sha256(b"another process's key").digest()

        def foreign_proof(db_path, action, now=None):
            stamp = str(int(time.time()))
            raw = hmac.new(other_key, b"\0".join((local._DOMAIN, action.encode(), stamp.encode())), hashlib.sha256)
            return f"{stamp}.{base64.urlsafe_b64encode(raw.digest()).decode().rstrip('=')}"

        monkeypatch.setattr(local, "proof", foreign_proof)
        rc = dashboard_main(["--db", db, "--port", str(port), "--stop"])
        out = capsys.readouterr()
        time.sleep(0.3)
        assert thread.is_alive(), "precondition: the dashboard was not stopped"
        assert rc == 2, f"--stop exited {rc} although the dashboard refused it: {out.out!r}"
        assert "Stopped" not in out.out
        assert "Only the command line of this store's owner" in out.err
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_bug_818_an_accepted_stop_still_stops(tmp_path, capsys, _dashboard_env):
    db = await _copy_db(tmp_path)
    sessions = local.Sessions(db)
    server, _url = web.make_server(db, 0, token="tok", local=sessions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        rc = dashboard_main(["--db", db, "--port", str(port), "--stop"])
        out = capsys.readouterr()
        thread.join(timeout=5)
        assert rc == 0 and f"Stopped the dashboard on port {port}." in out.out
        assert not thread.is_alive()
    finally:
        server.server_close()


@pytest.mark.asyncio
async def test_bug_818_a_dropped_connection_is_not_reported_as_stopped(tmp_path, monkeypatch, capsys, _dashboard_env):
    db = await _copy_db(tmp_path)
    monkeypatch.setattr(local, "probe", lambda port: {"app": local.APP, "store": local.store_id(db)})

    def dropped(port, action, db_path):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(local, "ask", dropped)
    rc = dashboard_main(["--db", db, "--port", "1", "--stop"])
    out = capsys.readouterr()
    assert rc == 2 and "Stopped" not in out.out and out.err


# ---- bug-922


def _hist_template() -> str:
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index('<div class="hist">')
    return text[start : text.index("</div>", start)]


def test_bug_922_histogram_month_is_escaped_in_every_attribute():
    tpl = _hist_template()
    uses = [e for e in re.findall(r"\$\{([^{}]*)\}", tpl) if "m.month" in e]
    assert len(uses) >= 3, f"the histogram template was not found: {uses}"
    raw = [e for e in uses if re.search(r"(?<!esc\()m\.month\b", e)]
    assert raw == [], f"month labels inserted unescaped: {raw}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_bug_922_a_month_with_markup_stays_inside_its_attributes():
    text = APP_JS.read_text(encoding="utf-8")
    esc_line = next(line for line in text.splitlines() if line.startswith("const esc = "))
    tpl = _hist_template()
    item = tpl[tpl.index("o.months.map(m => `") + len("o.months.map(m => `") : tpl.rindex("`).join")]
    script = (
        f"{esc_line}\n"
        "const o = { months: [{ month: '\"><s>xx', count: 1 }] };\n"
        "const max = 1;\n"
        f"process.stdout.write(o.months.map(m => `{item}`).join(''));\n"
    )
    html = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30).stdout
    assert html.startswith("<button") and "<s>" not in html, html
