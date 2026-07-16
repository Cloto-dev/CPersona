"""Standalone health-checkup CLI: ``python -m cpersona.checkup``.

Runs the same check registry as the ``check_health`` / ``deep_check`` MCP
tools against a database file, without needing an MCP session — built for
cron / systemd timers and CI.

Exit codes (gate semantics; see cpersona.checks.exit_code):
  0  no issues, or warn/info only without ``--strict``
  1  warn issues present and ``--strict`` was passed
  2  critical issues present (always gates)

Defaults are deliberately safe for unattended runs: read-only (no ``--fix``)
and non-strict (only critical failures flip the exit code). Automatic repair
is intentionally not meant for timers — detect unattended, repair deliberately.

Note on no-persist: the MCP server's no-persist pause is per-process state and
does not extend to this CLI. That is by design — the pause guards against
agent-driven write contamination, while a CLI ``--fix`` is an operator's
explicit maintenance action.
"""

import argparse
import asyncio
import json
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cpersona.checkup",
        description="Run CPersona health checks against a database file.",
    )
    parser.add_argument(
        "--db",
        help="Path to the cpersona SQLite database (default: $CPERSONA_DB_PATH or ~/.claude/cpersona.db)",
    )
    parser.add_argument(
        "--agent", default="", help="Restrict agent-scoped checks to one agent_id (default: all)"
    )
    parser.add_argument(
        "--fix", action="store_true", help="Apply automatic repairs (default: read-only report)"
    )
    parser.add_argument(
        "--strict", action="store_true", help="Also gate on warn-severity issues (exit 1)"
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Additionally run deep checks per agent (heuristic analysis; slower)",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full report as JSON")
    return parser


async def _run(args) -> int:
    # Imported here, after --db has been exported, because cpersona.config
    # resolves CPERSONA_DB_PATH at import time.
    from cpersona import database
    from cpersona.checks import exit_code
    from cpersona.database import close_db, connection
    from cpersona.maintenance_handlers import do_check_health, do_deep_check

    if not args.fix:
        # bug-105: a report-only run must not write — without this, get_db()'s
        # boot path silently migrated a version-stale DB from a monitoring cron
        # and crashed outright on a read-only file/filesystem.
        database.SKIP_BOOT_MIGRATIONS = True

    report = await do_check_health(agent_id=args.agent, fix=args.fix)

    if args.deep:
        if args.agent:
            agents = [args.agent]
        else:
            from cpersona.isolation import isolation_where

            iso_all = isolation_where(agent_id=None)  # deliberate corpus-wide scan
            async with connection() as db:
                agents = [
                    r[0]
                    for r in await db.execute_fetchall(
                        f"SELECT DISTINCT agent_id FROM memories{iso_all.where}", iso_all.params
                    )
                ]
        report["deep"] = {}
        for agent in agents:
            report["deep"][agent] = await do_deep_check(agent, fix=args.fix)

    await close_db()

    code = exit_code(report["severity_summary"], strict=args.strict)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["severity_summary"]
        print(
            f"cpersona checkup: {report['total_memories']} memories, "
            f"{summary['critical']} critical / {summary['warn']} warn / {summary['info']} info"
            + (" (fix applied)" if args.fix else "")
        )
        for issue in report["issues"]:
            extras = {
                k: v for k, v in issue.items() if k not in ("type", "severity", "check")
            }
            print(f"  [{issue['severity']:<8}] {issue['type']}: {extras}")
        if args.deep:
            for agent, deep in report["deep"].items():
                findings = {
                    name: res
                    for name, res in deep["results"].items()
                    if res.get("count") or res.get("pairs") or res.get("status") not in (None, "ok", "not_applicable")
                }
                if findings:
                    print(f"  deep[{agent}]: {json.dumps(findings, ensure_ascii=False)[:400]}")
        print(f"exit={code} ({'strict' if args.strict else 'non-strict'} gate)")
    return code


def main(argv: list | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.db:
        if not os.path.exists(args.db):
            print(f"error: database not found: {args.db}", file=sys.stderr)
            return 2
        os.environ["CPERSONA_DB_PATH"] = args.db
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
