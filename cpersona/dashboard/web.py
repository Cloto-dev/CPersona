"""Serve the dashboard on the loopback interface.

Who can read it:

- It listens on 127.0.0.1 only, so another machine cannot connect.
- The data is behind a token made for each start. The page is opened at
  ``http://127.0.0.1:<port>/#t=<token>``: the part after ``#`` is never sent to a
  server, so the token is not in a request line, a log or a Referer. The page keeps
  it for the tab and sends it as ``Authorization: Bearer``; without it the JSON
  endpoints answer 401. Another program or user on the same machine can reach the
  port, but not the data.
- The Host header must name the loopback address and this port. A web page cannot
  then read the data by pointing its own host name at 127.0.0.1 (DNS rebinding).
- Only GET is answered, and the page loads nothing from anywhere else (the CSP).

The page, its script and its style carry no data, so they are served without the token.
"""

import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cpersona.dashboard import graph, overall, store

HOST = "127.0.0.1"

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def _static(name: str) -> bytes:
    return resources.files("cpersona.dashboard").joinpath("static", name).read_bytes()


def make_handler(
    db_path: str,
    token: str,
    port_ref: list[int],
    health: Callable[[], dict] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """A request handler bound to one database and one token.

    ``port_ref`` holds the listening port, filled in once the socket is bound
    (the port may be chosen by the OS), so the Host check knows it.

    ``health`` reports the store's health. Only the running server can judge it
    (whether a long record should have nodes depends on the embedding server's
    token counts, and whether a missing vector is wrong depends on its settings),
    so the dashboard started from a terminal has none and ``/api/health`` says so.
    """
    name = Path(db_path).name

    class Handler(BaseHTTPRequestHandler):
        server_version = "cpersona-dashboard"
        sys_version = ""

        def log_message(self, format, *args):  # noqa: A002 - the stdlib's name
            pass  # a request log would print the paths people looked at

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in _HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value) -> None:
            self._send(code, json.dumps(value).encode(), "application/json")

        def _host_ok(self) -> bool:
            port = port_ref[0]
            return self.headers.get("Host", "") in (
                f"{HOST}:{port}",
                f"localhost:{port}",
            )

        def _token_ok(self) -> bool:
            given = self.headers.get("Authorization", "")
            return hmac.compare_digest(given.encode(), f"Bearer {token}".encode())

        def do_GET(self):  # noqa: N802 - the stdlib's name
            if not self._host_ok():
                return self._json(
                    421,
                    {
                        "error": "This dashboard answers only on its own loopback address."
                    },
                )
            url = urlparse(self.path)
            if url.path in _STATIC:
                file, ctype = _STATIC[url.path]
                return self._send(200, _static(file), ctype)
            if not url.path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not self._token_ok():
                return self._json(
                    401, {"error": "Open the dashboard from the address it printed."}
                )
            if url.path == "/api/health":
                if health is None:
                    return self._json(
                        404,
                        {
                            "error": "Store health is shown when an agent opens the dashboard."
                        },
                    )
                try:
                    return self._json(200, health())
                except Exception as e:  # noqa: BLE001 - reported to the page, not raised
                    return self._json(
                        502, {"error": f"The health check did not finish: {e}"}
                    )
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                path = json.loads(q.get("path") or "[]")
                if not isinstance(path, list):
                    raise ValueError("path is a list")
                conn = store.open_store(db_path)
            except store.StoreError as e:
                return self._json(409, {"error": str(e)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            try:
                with conn:
                    if url.path == "/api/overview":
                        out = store.overview(conn, name)
                    elif url.path == "/api/tree":
                        out = store.tree(conn, path)
                    elif url.path == "/api/memories":
                        out = store.memories(
                            conn,
                            q.get("q", ""),
                            path,
                            q.get("month", ""),
                            int(q.get("limit", 50)),
                            int(q.get("offset", 0)),
                        )
                    elif url.path == "/api/search":
                        out = store.search(
                            conn,
                            q.get("q", ""),
                            q.get("case") == "1",
                            q.get("sort", "new"),
                            int(q.get("limit", 50)),
                            int(q.get("offset", 0)),
                        )
                    elif url.path == "/api/memory":
                        out = store.memory(conn, int(q.get("id", 0)))
                        if out is None:
                            return self._json(404, {"error": "No memory with that id."})
                    elif url.path == "/api/graph" and q.get("around"):
                        out = graph.around(
                            conn,
                            db_path,
                            int(q["around"]),
                            int(q.get("hops", graph.DEFAULT_HOPS)),
                        )
                        if out is None:
                            return self._json(404, {"error": "No memory with that id."})
                    elif url.path == "/api/graph" and q.get("cluster_of"):
                        out = overall.cluster_of(conn, db_path, int(q["cluster_of"]))
                        if out is None:
                            return self._json(
                                404, {"error": "That memory is in no group."}
                            )
                    elif url.path == "/api/graph" and q.get("entity"):
                        out = graph.mentioning(conn, int(q["entity"]))
                        if out is None:
                            return self._json(404, {"error": "No entity with that id."})
                    elif url.path == "/api/graph":
                        out = graph.folder(
                            conn, path, q.get("month", ""),
                            # Prototype switches, for comparing the picture's shape.
                            mutual=q.get("mutual") == "1", hubs=q.get("hubs") == "1",
                        )
                    elif url.path == "/api/map":
                        lens = q.get("lens", "tree")
                        if lens == "tree":
                            out = overall.tree(conn)
                        elif lens == "entities":
                            out = overall.entities(conn)
                        elif lens == "clusters":
                            out = overall.clusters(conn, db_path)
                        else:
                            raise ValueError(
                                f"lens is one of {', '.join(overall.LENSES)}"
                            )
                    else:
                        return self._json(404, {"error": "not found"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except sqlite3.Error as e:
                return self._json(
                    500, {"error": f"The database could not be read: {e}"}
                )
            finally:
                conn.close()
            return self._json(200, out)

        def _refuse(self):
            self._json(405, {"error": "This dashboard only reads."})

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

    return Handler


def make_server(
    db_path: str,
    port: int = 0,
    token: str | None = None,
    health: Callable[[], dict] | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    """Bind the dashboard; returns the server and the address to open (with its token)."""
    token = token or secrets.token_urlsafe(32)
    port_ref = [port]
    server = ThreadingHTTPServer(
        (HOST, port), make_handler(db_path, token, port_ref, health)
    )
    server.daemon_threads = True
    port_ref[0] = server.server_address[1]
    return server, f"http://{HOST}:{port_ref[0]}/#t={token}"
