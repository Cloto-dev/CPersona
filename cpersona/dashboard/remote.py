"""The dashboard served by the HTTP transport, for a server people reach remotely.

Off unless ``CPERSONA_DASHBOARD_REMOTE`` is set. Who can read it:

- A browser carries no bearer token, so the first visit opens from a link the
  ``open_dashboard`` tool returns to an authenticated MCP caller. The link carries a
  code after the "#", which a browser never sends in a request line, so it is in no
  access log and no Referer. The page sends it once, in a header, and gets a session
  cookie for it (HttpOnly, Secure, SameSite=Strict, scoped to the dashboard's path).
  The code works once, within ``LINK_SECONDS``.
- The session is the cookie itself, signed by the server: it names the client the
  link was made for and when it ends. It lasts ``SESSION_SECONDS`` and is renewed
  while it is used, so a bookmark of the page keeps working, and it outlives a
  restart. Its signature also covers the client's current bearer token, so replacing
  that token ends every session it opened; removing the client from the ACL ends
  them too.
- Every request reads the client's grants as the ACL states them now, through a
  connection narrowed to the agents those grants let them read (``scope``).
- Only GET and HEAD are answered, and the page loads nothing from anywhere else.

The signing key is ``CPERSONA_DASHBOARD_SESSION_KEY`` when set, else a file beside
the store made on first use (owner-only). Deleting that file ends every session.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qs

from cpersona import acl, fileperms
from cpersona.dashboard import web
from cpersona.dashboard.scope import Scope

logger = logging.getLogger(__name__)

PREFIX = "/dashboard"
LINK_SECONDS = 600
SESSION_SECONDS = 30 * 86400
COOKIE = "cp_dash"
#: Codes held at most for one client; past it that client's oldest goes first.
#: Counted per client so that one client asking for links again and again pushes
#: out only its own, never another viewer's. The clients are the ones the operator
#: configured, so the whole is bounded too.
MAX_PER_OWNER = 20
_VERSION = "v1"
_DOMAIN = b"cpersona-dashboard-session"

_lock = threading.Lock()
# code -> (expiry, owner)
_links: dict[str, tuple[float, str]] = {}
_key: bytes | None = None


def _prune(now: float, owner: str) -> None:
    for k in [k for k, (until, _o) in _links.items() if until <= now]:
        del _links[k]
    mine = [k for k, (_u, o) in _links.items() if o == owner]
    for k in mine[: max(0, len(mine) - MAX_PER_OWNER + 1)]:
        del _links[k]


def issue_link(owner: str = "") -> str:
    """A code that opens one session for ``owner``, once, within LINK_SECONDS.

    ``owner`` names the client that asked for it (empty without an ACL).
    """
    code = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _prune(now, owner)
        _links[code] = (now + LINK_SECONDS, owner)
    return code


def clear() -> None:
    global _key
    with _lock:
        _links.clear()
        _key = None


def _signing_key(db_path: str) -> bytes:
    """The key sessions are signed with: the setting, else the key file, made once."""
    global _key
    if _key is not None:
        return _key
    from cpersona import config

    if config.DASHBOARD_SESSION_KEY:
        _key = hashlib.sha256(config.DASHBOARD_SESSION_KEY.encode()).digest()
        return _key
    stem = os.path.splitext(os.path.basename(db_path))[0] or "cpersona"
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), f"{stem}-dashboard.key")
    try:
        # Made owner-only and never replaced: an existing key file is the one in use.
        if fileperms.create_private(path):
            with fileperms.open_private(path, "w") as f:
                f.write(secrets.token_hex(32) + "\n")
        with open(path) as f:
            text = f.read().strip()
        if len(text) < 32:
            raise ValueError(f"{path} holds no key")
        _key = hashlib.sha256(text.encode()).digest()
    except (OSError, ValueError) as e:
        # Sessions still work, but end when the process does.
        logger.warning("dashboard sessions will not outlive this process: %s", e)
        _key = secrets.token_bytes(32)
    return _key


def _credential(owner: str, bearer: str) -> bytes | None:
    """What a session of ``owner`` is bound to now, or None when ``owner`` may not
    open one: the client's static tokens under an ACL, the server's token without."""
    config = acl.active_config()
    if config is None:
        return bearer.encode() if owner == "" else None
    if owner not in config.grants_by_client or owner in config.per_subject_clients:
        return None
    tokens = sorted(t for t, c in config.token_entries if c == owner)
    return "\0".join(tokens).encode()


def _viewer(owner: str) -> Scope:
    config = acl.active_config()
    return Scope(None) if config is None else Scope(config.grants_by_client.get(owner, {}))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, owner: str, until: int, credential: bytes) -> str:
    material = b"\0".join(
        (_DOMAIN, _VERSION.encode(), owner.encode(), str(until).encode(), hashlib.sha256(credential).digest())
    )
    return _b64(hmac.new(key, material, hashlib.sha256).digest())


def _sign(db_path: str, owner: str, until: int, bearer: str) -> str | None:
    credential = _credential(owner, bearer)
    if credential is None:
        return None
    return f"{_VERSION}.{_b64(owner.encode())}.{until}.{_mac(_signing_key(db_path), owner, until, credential)}"


def redeem(code: str, db_path: str, bearer: str = "") -> str | None:
    """The session cookie value a code opens, or None for a code unknown, used or
    expired, or for a client that may no longer open one."""
    now = time.time()
    with _lock:
        held = _links.pop(code, None)
    if held is None or held[0] <= now:
        return None
    return _sign(db_path, held[1], int(now) + SESSION_SECONDS, bearer)


def session(value: str, db_path: str, bearer: str = "") -> tuple[Scope, str | None] | None:
    """The viewer a cookie stands for, and a renewed cookie value once half of it
    has passed (else None); None for a cookie that is forged, expired, or bound to a
    token or a client that is gone."""
    try:
        version, owner_b64, until_text, mac = value.split(".")
        if version != _VERSION:
            return None
        owner = _unb64(owner_b64).decode()
        until = int(until_text)
    except ValueError:
        return None
    credential = _credential(owner, bearer)
    if credential is None:
        return None
    if not hmac.compare_digest(mac.encode(), _mac(_signing_key(db_path), owner, until, credential).encode()):
        return None
    now = time.time()
    if until <= now:
        return None
    renewed = None
    if until - now < SESSION_SECONDS / 2:
        renewed = _sign(db_path, owner, int(now) + SESSION_SECONDS, bearer)
    return _viewer(owner), renewed


def _set_cookie(value: str, seconds: int) -> tuple[bytes, bytes]:
    return (
        b"set-cookie",
        f"{COOKIE}={value}; Path={PREFIX}/; Max-Age={seconds}; HttpOnly; Secure; SameSite=Strict".encode(),
    )


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


def make_app(
    db_path: str,
    health: Callable[[asyncio.AbstractEventLoop], Callable[[], dict]] | None = None,
    bearer: str = "",
):
    """The ASGI app for ``PREFIX``; it checks its own session, not the bearer token.

    ``bearer`` is the server's token when it runs without an ACL: a session is bound
    to it as a client's is to the client's.

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

        async def as_json(send, code: int, value, extra: list | None = None) -> None:
            await respond(send, code, json.dumps(value).encode(), "application/json", extra)

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
            value = redeem(code, db_path, bearer)
            if value is None:
                return await as_json(send, 410, {"error": "This link has been used or has expired."})
            return await respond(send, 204, b"", "text/plain", [_set_cookie(value, SESSION_SECONDS)])
        if path == "/api/logout":
            # The session lives in the cookie, so signing out is the browser dropping it.
            return await respond(send, 204, b"", "text/plain", [_set_cookie("", 0)])
        if path in web._STATIC:
            file, ctype = web._STATIC[path]
            return await respond(send, 200, web._static(file), ctype)
        if not path.startswith("/api/"):
            return await as_json(send, 404, {"error": "not found"})
        held = session(_cookie(scope.get("headers", [])), db_path, bearer)
        if held is None:
            return await as_json(send, 401, {"error": "Ask your agent to open the dashboard again."})
        viewer, renewed = held
        renew = [_set_cookie(renewed, SESSION_SECONDS)] if renewed else []
        if path == "/api/health":
            # The store's health counts records of every agent; only a viewer who
            # reads every agent sees it.
            if health is None or not viewer.everything:
                return await as_json(send, 404, {"error": "Store health is not shown here."}, renew)
            try:
                report = health(asyncio.get_running_loop())
                return await as_json(send, 200, await asyncio.to_thread(report), renew)
            except Exception as e:  # noqa: BLE001 - reported to the page, not raised
                return await as_json(send, 502, {"error": f"The health check did not finish: {e}"}, renew)
        q = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode("latin-1")).items()}
        code, out = await asyncio.to_thread(web.answer, db_path, path, q, name, viewer)
        return await as_json(send, code, out, renew)

    return app
