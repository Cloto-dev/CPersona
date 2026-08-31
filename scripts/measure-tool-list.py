#!/usr/bin/env python3
"""Measure what a parameter costs on the tool list every client loads.

`docs/SESSION_IDENTITY_DESIGN.md` §6 promises that the *measured* size of the
tool list, not a preference, decides whether a parameter is threaded onto more
tools. A promise nobody can re-run is a promise that gets quoted from memory, so
this is the thing that runs.

    python3 scripts/measure-tool-list.py                    # totals
    python3 scripts/measure-tool-list.py --per-tool         # and the breakdown
    python3 scripts/measure-tool-list.py --param project_id # cost of another one

What is measured: `json.dumps(Tool.model_dump(exclude_none=True))` over the
registry, with compact separators — what `tools/list` puts on the wire. A run
that reports a different number probably used the default separators.

How the cost is isolated: the named property is deleted from the *live* payload
and the difference taken. Do NOT measure by diffing a merge commit against its
parent — every unrelated description change that rode along in the same commit
lands in the number. That mistake put 334 characters into a stage 1 figure, and
a later projection was quoted from a text that did not contain the clause the
option existed to keep. Both were caught by measuring this way instead.

Reports only. Nothing here fails a build: what the number should be is a
judgement, and encoding today's answer as a threshold would freeze it.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

os.environ.setdefault("CPERSONA_DB_PATH", "/tmp/cpersona-measure-tool-list.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cpersona.server import registry  # noqa: E402


def ser(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def properties(tool: dict) -> dict:
    return (tool.get("inputSchema") or {}).get("properties") or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--param", default="session_key", help="parameter to price (default: session_key)")
    ap.add_argument("--per-tool", action="store_true", help="show the per-tool breakdown")
    args = ap.parse_args()

    full = [t.model_dump(exclude_none=True) for t in registry._tools]
    ablated = copy.deepcopy(full)
    carriers = []
    for tool in ablated:
        if properties(tool).pop(args.param, None) is not None:
            carriers.append(tool["name"])
            required = (tool.get("inputSchema") or {}).get("required")
            if required and args.param in required:
                required.remove(args.param)

    with_param, without = len(ser(full)), len(ser(ablated))
    print(f"tools                     {len(full)}")
    print(f"carrying {args.param:<16} {len(carriers)}")
    print(f"tool list as shipped      {with_param}")
    print(f"without {args.param:<17} {without}")
    if not carriers:
        print(f"\nNo tool declares {args.param!r} — nothing to price.")
        return 0
    delta = with_param - without
    print(f"the parameter             +{delta} ({delta / without * 100:.1f}% of the list without it)")

    if args.per_tool:
        print(f"\n{'tool':<26} {'chars':>6}")
        for tool in full:
            if tool["name"] not in carriers:
                continue
            stripped = json.loads(ser(tool))
            stripped["inputSchema"]["properties"].pop(args.param, None)
            print(f"{tool['name']:<26} {len(ser(tool)) - len(ser(stripped)):>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
