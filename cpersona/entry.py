"""The ``cpersona`` command: decide what this process is before importing the server.

Importing ``cpersona.server`` takes about a second (it builds the tool registry and
reads the configuration). A stdio launch that only relays to the local service
(cpersona/service.py) needs none of it, so the choice is made here, from the
arguments and the environment alone.

- ``cpersona dashboard [...]`` opens the read-only dashboard.
- ``cpersona service list|stop [KEY]`` lists or stops local services.
- ``cpersona`` under stdio (the default transport) relays to the local service,
  unless ``CPERSONA_LOCAL_SERVICE=off``.
- Anything else starts the server in this process, as before.
"""

import json
import os
import sys


def _service_command(args: list[str]) -> int:
    from cpersona import service

    if args[:1] == ["list"] or not args:
        rows = service.known_services()
        print(json.dumps(rows, indent=1) if rows else "No local services recorded.")
        return 0
    if args[:1] == ["stop"]:
        asked = service.stop(args[1] if len(args) > 1 else None)
        print(f"Asked {len(asked)} service(s) to stop." if asked else "No running local service to stop.")
        return 0
    print("usage: cpersona service [list | stop [KEY]]", file=sys.stderr)
    return 2


def run() -> None:
    """Entry point for the ``cpersona`` console script and ``python -m cpersona``."""
    if sys.argv[1:2] == ["dashboard"]:
        from cpersona.dashboard.__main__ import main as dashboard_main

        sys.exit(dashboard_main(sys.argv[2:]))
    if sys.argv[1:2] == ["service"]:
        sys.exit(_service_command(sys.argv[2:]))
    from cpersona import service

    if os.environ.get("CPERSONA_TRANSPORT", "stdio") == "stdio" and service.enabled():
        service.run_relay()
        return
    from cpersona.server import serve

    serve()
