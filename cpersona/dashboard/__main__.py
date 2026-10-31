"""``python -m cpersona.dashboard`` (also ``cpersona dashboard``): open the dashboard.

The database is the one ``--db`` names, else ``CPERSONA_DB_PATH``, else the one a
CPersona server opened last (``cpersona.known_stores``: each server notes its
database when it starts). There is no fallback to the server's relative default
(``data/cpersona.db``): from a terminal that resolves against whatever directory
the command runs in, and would open the wrong file or none.

It listens on 127.0.0.1 at a fixed port (``--port``, default 48700) and runs until
it is stopped (``--stop``) so that a shortcut or a bookmark finds it again. Run a
second time for the same store, it starts nothing: the running dashboard gives a
sign-in code and the browser opens on it.

Exit codes: 0 when the dashboard ran, was opened or was stopped; 1 when ``--stop``
found none of this store; 2 when the database cannot be read or the port is taken.
"""

import argparse
import os
import sys
import webbrowser

from cpersona import known_stores as stores
from cpersona.dashboard import local, store, web


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cpersona dashboard",
        description="Open a read-only dashboard of a CPersona database in the browser.",
    )
    p.add_argument(
        "--db",
        help="the database file (default: $CPERSONA_DB_PATH, else the one a CPersona server opened last)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=local.DEFAULT_PORT,
        help=f"the port on 127.0.0.1 (default: {local.DEFAULT_PORT}; 0 for any free port)",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="print the address without opening a browser",
    )
    p.add_argument("--list", action="store_true", help="list the databases CPersona servers have opened, and exit")
    p.add_argument("--stop", action="store_true", help="stop the dashboard of this database on the port, and exit")
    return p


def _database(args) -> str:
    if args.db or os.environ.get("CPERSONA_DB_PATH"):
        return args.db or os.environ["CPERSONA_DB_PATH"]
    seen = stores.known()
    return seen[0]["path"] if seen else ""


def _open(url: str, no_browser: bool) -> None:
    print(f"CPersona dashboard (read-only): {url}", flush=True)
    if not no_browser:
        webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        for row in stores.known():
            print(f"{row['path']}\t(last opened {row.get('last_used', '?')})")
        return 0
    db = _database(args)
    if not db:
        print(
            "Which database? Pass --db <path>, or set CPERSONA_DB_PATH.\n"
            "A CPersona server notes the database it opens when it starts; once one has "
            "run, `cpersona dashboard` opens that one (`--list` shows them).",
            file=sys.stderr,
        )
        return 2
    try:
        store.open_store(db).close()
    except store.StoreError as e:
        print(e, file=sys.stderr)
        return 2
    stores.remember(db)  # named once with --db, it opens without it next time
    found = local.probe(args.port) if args.port else None
    mine = isinstance(found, dict) and found.get("store") == local.store_id(db)
    if args.stop:
        if not mine:
            print(f"No dashboard of {db} is running on port {args.port}.", file=sys.stderr)
            return 1
        local.ask(args.port, "stop", db)
        print(f"Stopped the dashboard on port {args.port}.")
        return 0
    if mine:
        status, body = local.ask(args.port, "code", db)
        if status != 200:
            print(f"The dashboard on port {args.port} did not open a session: {body.get('error', status)}", file=sys.stderr)
            return 2
        _open(f"http://{local.HOST}:{args.port}/#c={body['code']}", args.no_browser)
        return 0
    if found is not None:
        what = "the dashboard of another database" if isinstance(found, dict) else "another program"
        print(f"Port {args.port} is used by {what}. Pass --port <n> to use another.", file=sys.stderr)
        return 2
    sessions = local.Sessions(db)
    try:
        server, url = web.make_server(db, args.port, local=sessions)
    except OSError as e:
        print(f"Cannot listen on port {args.port}: {e}. Pass --port <n> to use another.", file=sys.stderr)
        return 2
    port = server.server_address[1]
    # The token address stays valid while this process runs; the browser opens on a
    # one-time code instead, which signs it in for later visits.
    print(f"Token address (for scripts): {url}", flush=True)
    _open(f"http://{local.HOST}:{port}/#c={sessions.issue()}", args.no_browser)
    stop = "cpersona dashboard --stop" + ("" if args.port == local.DEFAULT_PORT else f" --port {port}")
    print(f"It runs until stopped: {stop} (or Ctrl-C here).", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
