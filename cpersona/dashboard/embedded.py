"""The dashboard run inside the MCP server, for the open_dashboard tool.

The server knows which database it serves (most installs set CPERSONA_DB_PATH in
the MCP client's configuration, where a terminal cannot see it), so an agent can
open the dashboard for the person without anyone looking the path up. The page is
served from a thread of the server process, reads through its own read-only
connections, and keeps running until the process exits. Asking again returns the
same address.
"""

import os
import threading
from collections.abc import Callable
from http.server import ThreadingHTTPServer

from cpersona.dashboard import store, web

_lock = threading.Lock()
_running: dict[str, tuple[ThreadingHTTPServer, str]] = {}


def open_for(db_path: str, health: Callable[[], dict] | None = None) -> str:
    """Start the dashboard for ``db_path`` once, and return its address."""
    path = os.path.abspath(db_path)
    with _lock:
        if path in _running:
            return _running[path][1]
        # Refuse a store it cannot read before binding a port.
        store.open_store(path).close()
        server, url = web.make_server(path, 0, health=health)
        threading.Thread(
            target=server.serve_forever, name="cpersona-dashboard", daemon=True
        ).start()
        _running[path] = (server, url)
        return url


def stop_all() -> None:
    """Stop every dashboard this process started (tests, and a clean shutdown)."""
    with _lock:
        for server, _url in _running.values():
            server.shutdown()
            server.server_close()
        _running.clear()
