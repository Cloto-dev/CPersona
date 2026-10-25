"""The dashboard over HTTP (2.6.15): one-time links, sessions, and each viewer's reach.

Pinned on real requests through the server's own app (``_build_http_app``), so the
mount, the bearer pass-through and the dashboard's own session check are tested as
they are wired, not as classes alone. A viewer's reach is pinned on two agents whose
records name each other, so a view that leaked one row, or one id in a line, fails.
"""

import contextlib
import json

import numpy as np
import pytest
import pytest_asyncio

from cpersona import acl, config, database, server
from cpersona.dashboard import graph, overall, remote, store, web
from cpersona.dashboard.scope import Scope
from cpersona.database import get_db

TOKEN = "bearer-for-tests"


@pytest_asyncio.fixture
async def db(monkeypatch):
    conn = await get_db()
    for table in ("memories", "episodes", "entities", "entity_aliases", "entity_mentions", "relations", "record_nodes", "record_blocks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    remote.clear()
    graph.clear_cache()
    overall.clear_cache()
    monkeypatch.setattr(config, "DB_PATH", database.DB_PATH)
    yield conn
    remote.clear()
    graph.clear_cache()
    overall.clear_cache()


def _vec(*xs) -> bytes:
    return np.asarray(xs, dtype=np.float32).tobytes()


async def _two_agents(conn):
    """a's memory, b's memory, near each other; an entity of a mentioning both; a
    relation of a joining both memories."""
    ids = {}
    for agent, text, vec in (("a", "alpha note", _vec(1, 0, 0, 0)), ("b", "bravo secret", _vec(0.99, 0.1, 0, 0))):
        cur = await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, '2026-10-01T00:00:00+00:00', ?)",
            (agent, text, vec),
        )
        ids[agent] = cur.lastrowid
    cur = await conn.execute(
        "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES ('a', 'Thing', 'thing', 'a', 'x')"
    )
    ent = cur.lastrowid
    for ref in (f"mem:{ids['a']}", f"mem:{ids['b']}"):
        await conn.execute("INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'a', 'x')", (ent, ref))
    await conn.execute(
        "INSERT INTO relations (agent_id, subject_kind, subject_id, predicate, object_kind, object_id, declared_by, declared_at) "
        "VALUES ('a', 'mem', ?, 'cites', 'mem', ?, 'a', 'x')",
        (ids["a"], ids["b"]),
    )
    await conn.commit()
    return ids, ent


def _answer(route, scope, **q):
    return web.answer(database.DB_PATH, route, {k: str(v) for k, v in q.items()}, "t.db", scope)


# ---- Reach

@pytest.mark.asyncio
async def test_a_viewer_sees_only_the_agents_they_read(db):
    ids, _ = await _two_agents(db)
    only_a = Scope({"a": acl.PERM_READ})
    code, ov = _answer("/api/overview", only_a)
    assert code == 200 and ov["counts"]["memories"] == 1
    assert [r["id"] for r in _answer("/api/memories", only_a)[1]["rows"]] == [ids["a"]]
    assert _answer("/api/search", only_a, q="secret")[1]["total"] == 0
    assert _answer("/api/memory", only_a, id=ids["b"])[0] == 404
    assert [k["key"] for k in _answer("/api/tree", only_a)[1]] == ["a"]
    # Everyone else (no ACL) still sees both.
    assert _answer("/api/overview", Scope(None))[1]["counts"]["memories"] == 2


@pytest.mark.asyncio
async def test_a_hidden_record_leaves_no_id_in_mentions_relations_or_neighbours(db):
    ids, ent = await _two_agents(db)
    only_a = Scope({"a": acl.PERM_READ})
    hidden = f"m:{ids['b']}"
    for route, q in (("/api/graph", {}), ("/api/graph", {"around": ids["a"]}), ("/api/graph", {"entity": ent})):
        code, out = _answer(route, only_a, **q)
        assert code == 200, (route, q, out)
        text = json.dumps(out)
        assert hidden not in text and "bravo" not in text, (route, q)
    # With both readable, the same pictures do carry b: the checks above can fail.
    both = Scope({"*": acl.PERM_READ})
    assert hidden in json.dumps(_answer("/api/graph", both, around=ids["a"])[1])
    assert hidden in json.dumps(_answer("/api/graph", both, entity=ent)[1])


@pytest.mark.asyncio
async def test_a_named_exception_hides_that_agent_from_a_wildcard(db):
    ids, _ = await _two_agents(db)
    code, out = _answer("/api/memories", Scope({"*": acl.PERM_READ, "b": acl.PERM_NONE}))
    assert [r["id"] for r in out["rows"]] == [ids["a"]]


@pytest.mark.asyncio
async def test_computed_pictures_are_not_shared_across_viewers(db):
    ids, _ = await _two_agents(db)
    wide = _answer("/api/map", Scope({"*": acl.PERM_READ}), lens="clusters")[1]
    narrow = _answer("/api/map", Scope({"a": acl.PERM_READ}), lens="clusters")[1]
    assert wide["total"] == 2 and narrow["total"] == 1
    assert "bravo" not in json.dumps(narrow)


# ---- Links and sessions

def test_a_link_opens_once():
    code = remote.issue_link(Scope(None))
    assert remote.redeem(code) is not None
    assert remote.redeem(code) is None
    assert remote.redeem("never-issued") is None


def test_a_link_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    code = remote.issue_link(Scope(None))
    now[0] += remote.LINK_SECONDS + 1
    assert remote.redeem(code) is None


def test_a_session_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    sid = remote.redeem(remote.issue_link(Scope(None)))
    assert remote.session(sid) is not None
    now[0] += remote.SESSION_SECONDS + 1
    assert remote.session(sid) is None


# ---- Wiring through the server's app

def _app(monkeypatch, on=True):
    monkeypatch.setattr(config, "DASHBOARD_REMOTE", on)
    monkeypatch.setattr(config, "DASHBOARD", True)
    reached = []

    async def endpoint(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"mcp"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    return server._build_http_app(TOKEN, endpoint, lifespan), reached


async def _get(app, path, query=b"", headers=(), method="GET"):
    messages = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8402),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        messages.append(m)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start.get("headers", [])}, body


@pytest.mark.asyncio
async def test_a_link_becomes_a_session_cookie_that_reads_the_viewers_agents(db, monkeypatch):
    ids, _ = await _two_agents(db)
    app, reached = _app(monkeypatch)
    code = remote.issue_link(Scope({"a": acl.PERM_READ}))
    status, headers, _ = await _get(app, "/dashboard/api/session", headers=[("x-dashboard-code", code)])
    assert status == 204
    cookie = headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/dashboard/"):
        assert flag in cookie
    sid = cookie.split(";")[0]
    status, _, body = await _get(app, "/dashboard/api/overview", headers=[("cookie", sid)])
    assert status == 200 and json.loads(body)["counts"]["memories"] == 1
    # The same code again is refused, and so is none at all.
    assert (await _get(app, "/dashboard/api/session", headers=[("x-dashboard-code", code)]))[0] == 410
    assert (await _get(app, "/dashboard/api/session"))[0] == 410
    assert reached == []


@pytest.mark.asyncio
async def test_without_a_session_the_dashboard_data_is_refused(db, monkeypatch):
    await _two_agents(db)
    app, reached = _app(monkeypatch)
    assert (await _get(app, "/dashboard/api/overview"))[0] == 401
    assert (await _get(app, "/dashboard/api/overview", headers=[("cookie", f"{remote.COOKIE}=forged")]))[0] == 401
    # The page itself carries no data and opens without one.
    status, _, body = await _get(app, "/dashboard/")
    assert status == 200 and b"app.js" in body
    assert (await _get(app, "/dashboard/api/overview", method="POST"))[0] == 405
    assert reached == []


@pytest.mark.asyncio
async def test_only_the_dashboard_prefix_skips_the_bearer_check(db, monkeypatch):
    app, reached = _app(monkeypatch)
    for path in ("/mcp", "/", "/dashboardx", "/dashboard-api", "/mcp/dashboard"):
        assert (await _get(app, path, method="POST"))[0] == 401, path
    # A path that only starts like the dashboard is routed to it, never to MCP.
    assert (await _get(app, "/dashboard/../mcp", method="POST"))[0] == 405
    assert reached == []
    # With a bearer token, MCP is reached as before.
    assert (await _get(app, "/mcp", method="POST", headers=[("authorization", f"Bearer {TOKEN}")]))[0] == 200
    assert reached == ["/mcp"]


@pytest.mark.asyncio
async def test_with_the_setting_off_there_is_no_dashboard(db, monkeypatch):
    app, reached = _app(monkeypatch, on=False)
    assert (await _get(app, "/dashboard/"))[0] == 401
    assert reached == []


# ---- The tool

@pytest.mark.asyncio
async def test_the_tool_needs_a_public_address_and_returns_a_one_time_link(db, monkeypatch):
    monkeypatch.setattr(config, "transport", lambda: "streamable-http")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    out = await server.do_open_dashboard()
    assert "CPERSONA_DASHBOARD_PUBLIC_URL" in json.dumps(out)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://memory.example.com")
    out = await server.do_open_dashboard()
    # The code is after the "#", so no request line (and no access log) carries it.
    assert out["ok"] and out["url"].startswith("https://memory.example.com/dashboard/#c=")
    code = out["url"].split("#c=", 1)[1]
    assert remote.redeem(code) is not None


@pytest.mark.asyncio
async def test_a_per_subject_connection_is_refused(db, monkeypatch):
    monkeypatch.setattr(config, "transport", lambda: "streamable-http")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://memory.example.com")
    cfg = acl.AclConfig(grants_by_client={"p": {"*": acl.PERM_READ}}, token_entries=(), per_subject_clients=frozenset({"p"}))
    acl.activate(cfg)
    tok = acl.set_principal(acl.Principal(client_id="p"))
    try:
        out = await server.do_open_dashboard()
    finally:
        acl.reset_principal(tok)
        acl.activate(None)
    assert not out.get("ok") and "per-subject" in json.dumps(out)


@pytest.mark.asyncio
async def test_rows_tying_records_together_are_kept_only_when_every_end_is_visible(db):
    ids, ent = await _two_agents(db)
    await db.execute(
        "INSERT INTO record_nodes (parent_kind, parent_id, node_index, start_char, end_char, token_count, window) VALUES ('mem', ?, 0, 0, 5, 2, 512)",
        (ids["b"],),
    )
    await db.commit()
    conn = store.open_store(database.DB_PATH)
    Scope({"a": acl.PERM_READ}).apply(conn)
    try:
        refs = [r[0] for r in conn.execute("SELECT ref FROM entity_mentions")]
        assert refs == [f"mem:{ids['a']}"]  # the mention of b's memory is gone
        assert conn.execute("SELECT count(*) FROM relations").fetchone()[0] == 0  # it names b's memory
        assert conn.execute("SELECT count(*) FROM record_nodes").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM main.relations").fetchone()[0] == 1
    finally:
        conn.close()
