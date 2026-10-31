"""The dashboard opened from the command line or a desktop shortcut.

It listens on a fixed port on 127.0.0.1, so a shortcut, a bookmark or an installed
web app finds it again, and it keeps running until it is stopped. A browser signs
in the way the remote dashboard's does (``remote``): a one-time code after the "#"
is sent once, in a header, for a signed session cookie, so the page opens again
without a new address.

A second ``cpersona dashboard`` for the same store starts nothing: it asks the
running one for a code and opens the browser on it. That request, and the one that
stops the dashboard, carry a proof made with the store's session key, a file only
its owner can read, so another user of the machine can reach the port but can do
neither.
"""

import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import time

from cpersona.dashboard import remote

HOST = "127.0.0.1"
DEFAULT_PORT = 48700
#: What the instance check answers, so a dashboard is told from another program.
APP = "cpersona-dashboard"
#: How long a proof made by the command line is taken.
PROOF_SECONDS = 60
_DOMAIN = b"cpersona-dashboard-local"


def store_id(db_path: str) -> str:
    """The store's identity on the instance check: a digest of its path, not the path."""
    return hashlib.sha256(os.path.abspath(db_path).encode()).hexdigest()[:16]


def _mac(db_path: str, action: str, stamp: str) -> str:
    raw = hmac.new(remote._signing_key(db_path), b"\0".join((_DOMAIN, action.encode(), stamp.encode())), hashlib.sha256)
    return base64.urlsafe_b64encode(raw.digest()).decode().rstrip("=")


def proof(db_path: str, action: str, now: float | None = None) -> str:
    stamp = str(int(time.time() if now is None else now))
    return f"{stamp}.{_mac(db_path, action, stamp)}"


def proof_ok(db_path: str, action: str, value: str, now: float | None = None) -> bool:
    try:
        stamp, mac = value.split(".")
        age = (time.time() if now is None else now) - int(stamp)
    except ValueError:
        return False
    return 0 <= age <= PROOF_SECONDS and hmac.compare_digest(mac.encode(), _mac(db_path, action, stamp).encode())


class Sessions:
    """Sign-in for one store's dashboard on 127.0.0.1.

    The cookie is named for the store: cookies are kept per host, not per port, so
    two stores' dashboards on two ports would otherwise overwrite each other's.
    It cannot be Secure (the page is plain HTTP on the loopback address)."""

    app = APP

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.store = store_id(db_path)
        self.cookie = f"cp_dash_{self.store[:12]}"

    def proof_ok(self, action: str, value: str) -> bool:
        return proof_ok(self.db_path, action, value)

    def issue(self) -> str:
        return remote.issue_link("")

    def _set_cookie(self, value: str, seconds: int) -> str:
        return f"{self.cookie}={value}; Path=/; Max-Age={seconds}; HttpOnly; SameSite=Strict"

    def redeem(self, code: str) -> str | None:
        """The Set-Cookie header a code opens, or None for one used, unknown or expired."""
        value = remote.redeem(code, self.db_path)
        return None if value is None else self._set_cookie(value, remote.SESSION_SECONDS)

    def logout(self) -> str:
        return self._set_cookie("", 0)

    def check(self, cookie_header: str) -> tuple[bool, str | None]:
        """Whether the request's cookie is a session of this store, and a Set-Cookie
        header renewing it once half of it has passed."""
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.cookie and value:
                held = remote.session(value, self.db_path)
                if held is not None:
                    renewed = held[1]
                    return True, None if renewed is None else self._set_cookie(renewed, remote.SESSION_SECONDS)
        return False, None


def _get(port: int, path: str, headers: dict | None = None, timeout: float = 2.0) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": f"{HOST}:{port}", **(headers or {})})
        r = conn.getresponse()
        body = r.read()
        try:
            return r.status, json.loads(body or b"{}")
        except ValueError:
            return r.status, {}
    finally:
        conn.close()


def probe(port: int) -> dict | str | None:
    """What listens on ``port``: the instance check of a dashboard, "busy" for some
    other program, or None when nothing does."""
    try:
        socket.create_connection((HOST, port), timeout=1.0).close()
    except OSError:
        return None  # nothing accepts a connection there
    try:
        status, body = _get(port, "/api/instance", timeout=1.0)
    except (OSError, http.client.HTTPException):
        return "busy"  # something accepted it, but does not answer as a dashboard
    return body if status == 200 and body.get("app") == APP else "busy"


def ask(port: int, action: str, db_path: str) -> tuple[int, dict]:
    """Ask the running dashboard of ``db_path`` for a code ("code") or to stop ("stop")."""
    return _get(port, f"/api/{action}", {"X-Dashboard-Proof": proof(db_path, action)})
