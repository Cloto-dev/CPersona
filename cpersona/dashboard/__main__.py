"""``python -m cpersona.dashboard`` (also ``cpersona dashboard``): open the dashboard.

The database is the one ``--db`` names, else ``CPERSONA_DB_PATH``. There is no
fallback to the server's relative default (``data/cpersona.db``): from a terminal
that resolves against whatever directory the command runs in, and would open the
wrong file or none. Most installs set the path in the MCP client's configuration,
not in the shell, so the message says where to find it.

Exit codes: 0 after Ctrl-C, 2 when the database cannot be read.
"""

import argparse
import os
import sys
import webbrowser

from cpersona.dashboard import store, web


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cpersona dashboard",
        description="Open a read-only dashboard of a CPersona database in the browser.",
    )
    p.add_argument("--db", help="the database file (default: $CPERSONA_DB_PATH)")
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="the port on 127.0.0.1 (default: any free port)",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="print the address without opening a browser",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = args.db or os.environ.get("CPERSONA_DB_PATH", "")
    if not db:
        print(
            "Which database? Pass --db <path>, or set CPERSONA_DB_PATH.\n"
            "If CPersona runs from an MCP client, the path is the CPERSONA_DB_PATH in that "
            "client's MCP server configuration.",
            file=sys.stderr,
        )
        return 2
    try:
        store.open_store(db).close()
    except store.StoreError as e:
        print(e, file=sys.stderr)
        return 2
    server, url = web.make_server(db, args.port)
    print(f"CPersona dashboard (read-only): {url}", flush=True)
    print("Press Ctrl-C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
