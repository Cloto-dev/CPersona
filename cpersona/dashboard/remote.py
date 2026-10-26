"""The dashboard served by the HTTP transport, for a server people reach remotely.

Off unless ``CPERSONA_DASHBOARD_REMOTE`` is set. Who can read it:

- A browser carries no bearer token, so the page is opened from a link the
  ``open_dashboard`` tool returns to an authenticated MCP caller. The link carries a
  code after the "#", which a browser never sends in a request line, so it is in no
  access log and no Referer. The page sends it once, in a header, and gets a session
  cookie for it (HttpOnly, Secure, SameSite=Strict, scoped to the dashboard's path,
  ``SESSION_SECONDS`` long). The code works once, within ``LINK_SECONDS``.
- The session holds the caller's grants, and every request reads through a
  connection narrowed to the agents those grants let them read (``scope``).
- Only GET and HEAD are answered, and the page loads nothing from anywhere else.

Codes and sessions live in this process: a restart ends every session, and the
person asks the agent for a new link.
"""

import asyncio
import json
import secrets
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qs

from cpersona.dashboard import web
from cpersona.dashboard.scope import Scope

PREFIX = "/dashboard"
LINK_SECONDS = 120
SESSION_SECONDS = 8 * 3600
COOKIE = "cp_dash"
#: Codes, and sessions, held at most for one client; past it that client's oldest
#: goes first. Counted per client so that one client asking for links again and
#: again pushes out only its own, never another viewer's. The clients are the ones
#: the operator configured, so the whole is bounded too.
MAX_PER_OWNER = 20

_lock = threading.Lock()
# code or session id -> (scope, expiry, owner)
_links: dict[str, tuple[Scope, float, str]] = {}
_sessions: dict[str, tuple[Scope, float, str]] = {}


def _prune(table: dict, now: float, owner: str) -> None:
    for k in [k for k, (_, until, _o) in table.items() if until <= now]:
        del table[k]
    mine = [k for k, (_, _u, o) in table.items() if o == owner]
    for k in mine[: max(0, len(mine) - MAX_PER_OWNER + 1)]:
        del table[k]


def issue_link(scope: Scope, owner: str = "") -> str:
    """A code that opens one session with ``scope``, once, within LINK_SECONDS.

    ``owner`` names the client that asked for it (empty without an ACL).
    """
    code = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        _prune(_links, now, owner)
        _links[code] = (scope, now + LINK_SECONDS, owner)
    return code


def redeem(code: str) -> str | None:
    """The session a code opens, or None for a code unknown, used or expired."""
    now = time.monotonic()
    with _lock:
        held = _links.pop(code, None)
        if held is None or held[1] <= now:
            return None
        scope, _, owner = held
        _prune(_sessions, now, owner)
        sid = secrets.token_urlsafe(32)
        _sessions[sid] = (scope, now + SESSION_SECONDS, owner)
        return sid


def session(sid: str) -> Scope | None:
    """The scope of a live session, or None."""
    now = time.monotonic()
    with _lock:
        held = _sessions.get(sid)
        if held is None:
            return None
        if held[1] <= now:
            del _sessions[sid]
            return None
        return held[0]


def clear() -> None:
    with _lock:
        _links.clear()
        _sessions.clear()


def _cookie(headers: list) -> str:
    """The session cookie's value, or "" when there is none or more than one.

    Two cookies of this name mean one was set from elsewhere (a sibling host, a
    narrower path), and which of them the browser sent first is not ours to trust.
    """
    found = []
    for name, value in headers:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    found.append(v)
    return found[0] if len(found) == 1 else ""


def make_app(db_path: str, health: Callable[[asyncio.AbstractEventLoop], Callable[[], dict]] | None = None):
    """The ASGI app for ``PREFIX``; it checks its own session, not the bearer token.

    ``health`` makes the store's health report for the server's running loop; the
    report waits on that loop, so it is called from a worker thread.
    """
    name = db_path.rsplit("/", 1)[-1]

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        head = scope.get("method") == "HEAD"

        async def respond(send, code: int, body: bytes, ctype: str, extra: list | None = None) -> None:
            headers = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]
            headers += [(k.lower().encode(), v.encode()) for k, v in web._HEADERS.items()]
            headers += extra or []
            await send({"type": "http.response.start", "status": code, "headers": headers})
            # A HEAD answer states the length of the body it leaves out.
            await send({"type": "http.response.body", "body": b"" if head else body})

        async def as_json(send, code: int, value) -> None:
            await respond(send, code, json.dumps(value).encode(), "application/json")

        path = scope.get("path", "")
        # Whether the router strips the mount point or not, read the path below it.
        if path.startswith(PREFIX):
            path = path[len(PREFIX):]
        if scope.get("method") not in ("GET", "HEAD"):
            return await as_json(send, 405, {"error": "This dashboard only reads."})
        if path == "/api/session":
            # Opening a session uses the code up, which a HEAD request must not do.
            if head:
                return await as_json(send, 405, {"error": "Open a session with GET."})
            code = dict(scope.get("headers", [])).get(b"x-dashboard-code", b"").decode("latin-1")
            sid = redeem(code)
            if sid is None:
                return await as_json(send, 410, {"error": "This link has been used or has expired."})
            cookie = f"{COOKIE}={sid}; Path={PREFIX}/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict"
            return await respond(send, 204, b"", "text/plain", [(b"set-cookie", cookie.encode())])
        if path in web._STATIC:
            file, ctype = web._STATIC[path]
            return await respond(send, 200, web._static(file), ctype)
        if not path.startswith("/api/"):
            return await as_json(send, 404, {"error": "not found"})
        viewer = session(_cookie(scope.get("headers", [])))
        if viewer is None:
            return await as_json(send, 401, {"error": "Ask your agent to open the dashboard again."})
        if path == "/api/health":
            # The store's health counts records of every agent; only a viewer who
            # reads every agent sees it.
            if health is None or not viewer.everything:
                return await as_json(send, 404, {"error": "Store health is not shown here."})
            try:
                report = health(asyncio.get_running_loop())
                return await as_json(send, 200, await asyncio.to_thread(report))
            except Exception as e:  # noqa: BLE001 - reported to the page, not raised
                return await as_json(send, 502, {"error": f"The health check did not finish: {e}"})
        q = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode("latin-1")).items()}
        code, out = await asyncio.to_thread(web.answer, db_path, path, q, name, viewer)
        return await as_json(send, code, out)

    return app
