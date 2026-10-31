"""The stores a dashboard can open without being told which.

Most installs set the database path in an MCP client's configuration, where a
terminal cannot see it, and the server's relative default resolves against
whatever directory a command runs in. So each server notes the absolute path of
the database it opens when it starts (and the dashboard notes one named with
``--db``), and ``cpersona dashboard`` run from a terminal or a desktop shortcut
opens the one used last.

The list is ``~/.cpersona/stores.json`` (``CPERSONA_DASHBOARD_STORES_PATH`` moves
it), owner-only, and holds paths and times only: nothing read from a store.
"""

import json
import logging
import os
from datetime import datetime, timezone

from cpersona import fileperms

logger = logging.getLogger(__name__)

DEFAULT_PATH = "~/.cpersona/stores.json"
#: How many stores the list keeps, the most recently used first.
MAX_STORES = 20


def path() -> str:
    return os.path.expanduser(os.environ.get("CPERSONA_DASHBOARD_STORES_PATH") or DEFAULT_PATH)


def _read(file: str) -> list[dict]:
    try:
        with open(file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = data.get("stores") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("path"), str)]


def remember(db_path: str) -> None:
    """Note that a server opens ``db_path`` now. Never raises: a server starts
    whether or not the note can be written."""
    if not db_path or db_path == ":memory:" or db_path.startswith("file:"):
        return
    try:
        full = os.path.abspath(db_path)
        file = path()
        rows = [r for r in _read(file) if r["path"] != full]
        rows.insert(0, {"path": full, "last_used": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        fileperms.makedirs_private(os.path.dirname(file))
        # Written whole and swapped in, so a reader never sees half a list.
        tmp = f"{file}.tmp.{os.getpid()}"
        with fileperms.open_private(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stores": rows[:MAX_STORES]}, fh, indent=1)
        os.replace(tmp, file)
    except Exception as e:  # noqa: BLE001 - a note for the dashboard, never a reason to stop
        logger.debug("could not note the store for the dashboard: %s", e)


def known() -> list[dict]:
    """The stores noted, the most recently used first, leaving out any whose file
    is gone."""
    rows = sorted(_read(path()), key=lambda r: str(r.get("last_used", "")), reverse=True)
    return [r for r in rows if os.path.isfile(r["path"])]
