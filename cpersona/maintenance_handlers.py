"""Maintenance and deep-check handlers for CPersona.

Tools: do_check_health / do_deep_check — thin dispatch wrappers over the
check registry in ``cpersona.checks`` (v2.4.37). The registry is the single
implementation shared by the MCP tools, the pytest fixtures, and the
``python -m cpersona.checkup`` CLI; check semantics, severities and fix
behaviour live there, response envelopes live here.
"""

import logging

from cpersona._vendored_mcp_common import no_persist

from cpersona import checks as checks_registry
from cpersona.database import get_db

logger = logging.getLogger(__name__)


async def do_check_health(agent_id: str = "", fix: bool = False, checks: list | None = None) -> dict:
    """Check and optionally fix memory database health issues.

    Runs the full check registry (or the subset named in ``checks``); every
    issue carries ``severity`` (critical / warn / info) and ``check``. The
    ``severity_summary`` counts feed the checkup CLI's gate exit code.
    """
    # Under no-persist, downgrade fix=True to fix=False so the diagnostic
    # still runs but no rows are mutated. Clear no-persist and re-run to repair.
    repairs_skipped = bool(fix and no_persist.is_paused())
    if repairs_skipped:
        fix = False
    db = await get_db()

    issues, severity_summary = await checks_registry.run_health_checks(
        db, agent_id=agent_id, fix=fix, checks=checks
    )
    if fix:
        await db.commit()

    agent_clause = "AND agent_id = ?" if agent_id else ""
    agent_params = (agent_id,) if agent_id else ()
    total = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE 1=1 {agent_clause}", agent_params
        )
    )[0][0]

    try:
        page_info = await db.execute_fetchall("PRAGMA page_count")
        page_size_info = await db.execute_fetchall("PRAGMA page_size")
        db_size_bytes = page_info[0][0] * page_size_info[0][0]
    except Exception:
        db_size_bytes = 0

    stats = {
        "db_size_bytes": db_size_bytes,
        "memories": total,
        "episodes": (await db.execute_fetchall("SELECT COUNT(*) FROM episodes"))[0][0],
        "profiles": (await db.execute_fetchall("SELECT COUNT(*) FROM profiles"))[0][0],
        "pending_tasks": (
            await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
        )[0][0],
        # Axis distributions are observations, not issues (rare != wrong).
        "axes": await checks_registry.axis_distribution(db),
    }
    if agent_id:
        stats["agent_memories"] = total
        stats["agent_episodes"] = (
            await db.execute_fetchall(
                "SELECT COUNT(*) FROM episodes WHERE agent_id = ?", (agent_id,)
            )
        )[0][0]

    result = {
        "total_memories": total,
        "issues": issues,
        "severity_summary": severity_summary,
        "healthy": len(issues) == 0,
        "fixed": fix,
        "stats": stats,
    }
    if repairs_skipped:
        result["repairs_skipped"] = True
        result["repairs_skip_reason"] = "no-persist mode active — fix downgraded to fix=False"
    return result


async def do_deep_check(agent_id: str, fix: bool = False, checks: list | None = None) -> dict:
    """Deep heuristic analysis of memory data quality for a specific agent."""
    repairs_skipped = bool(fix and no_persist.is_paused())
    if repairs_skipped:
        fix = False
    db = await get_db()
    selected = checks if checks else checks_registry.DEEP_CHECK_NAMES
    results: dict[str, dict] = {}

    for name in selected:
        runner = checks_registry.DEEP_CHECKS.get(name)
        if runner is None:
            continue  # unknown names are silently skipped (pre-registry behaviour)
        try:
            results[name] = await runner(db, agent_id, fix)
        except Exception as e:
            logger.warning("deep check %s crashed: %s", name, e)
            results[name] = {"error": str(e)}

    if fix:
        await db.commit()

    out = {
        "agent_id": agent_id,
        "checks_run": [n for n in selected if n in checks_registry.DEEP_CHECKS],
        "results": results,
        "fixed": fix,
    }
    if repairs_skipped:
        out["repairs_skipped"] = True
        out["repairs_skip_reason"] = "no-persist mode active — fix downgraded to fix=False"
    return out


# Discord bridge session_id = "{channel_id}:{user_id}:{chunk}" (bridge.rs) or
# "{channel_id}:shared" (thread, main.rs). channel_id is a numeric snowflake, so
# the concrete channel is the substring before the first ':'. The kernel stores
# it at metadata.session_id (system.rs), persisted into the memories.metadata
# JSON column, so json_extract recovers it deterministically.
_SESSION_ID_EXPR = "json_extract(metadata, '$.session_id')"
_SNOWFLAKE_SESSION_GLOB = "[0-9]*:*"


async def do_migrate_channel_axis(
    agent_id: str = "",
    dry_run: bool = True,
    globalize_unrecoverable: bool = False,
) -> dict:
    """Re-channel bridge-type memories to their concrete channel (knob2 v2).

    Prepares the knob2 v2 default flip (Goal #120). Under the historical default
    the kernel filed PerUser memories under the bridge *type* ("discord") rather
    than the concrete channel, so once recall starts filtering by the concrete
    channel those memories can no longer be matched. This tool recovers the
    concrete channel from the stored session_id
    (metadata.session_id = "{channel_id}:{user_id}:{chunk}" | "{channel_id}:shared")
    and rewrites each affected memory's channel in place.

    Non-destructive: only the `channel` column changes; content, embedding,
    source and metadata are untouched. Idempotent: once a row's channel is the
    concrete id it no longer matches the channel='discord' scope, so re-running
    is a no-op. dry_run (default True) reports the counts and the channels that
    would be recovered without mutating anything.

    Two buckets are reported:
      - recoverable:   channel='discord' rows whose session_id is a snowflake
                       (channel_id deterministically recoverable).
      - unrecoverable: channel='discord' rows with no snowflake session_id
                       (e.g. session_id missing). These cannot be re-channelled.
                       With globalize_unrecoverable=True they are instead moved
                       to channel='' (global), which the v2 recall change makes
                       match every channel-scoped recall, so they are not
                       orphaned by the flip. Default False (report only).
    """
    db = await get_db()

    # Under no-persist, force a report-only run so nothing mutates.
    paused = no_persist.is_paused()
    effective_dry_run = dry_run or paused

    agent_clause = " AND agent_id = ?" if agent_id else ""
    agent_params = (agent_id,) if agent_id else ()

    sid = _SESSION_ID_EXPR
    recovered_expr = f"substr({sid}, 1, instr({sid}, ':') - 1)"

    # Recoverable rows, grouped by the channel they would be moved to.
    recoverable_rows = await db.execute_fetchall(
        f"""SELECT {recovered_expr} AS recovered_channel, COUNT(*) AS n
           FROM memories
           WHERE channel = 'discord' AND {sid} GLOB ?{agent_clause}
           GROUP BY recovered_channel
           ORDER BY n DESC""",
        (_SNOWFLAKE_SESSION_GLOB, *agent_params),
    )
    recoverable_total = sum(r[1] for r in recoverable_rows)
    by_channel = [{"channel": r[0], "count": r[1]} for r in recoverable_rows]

    # Total bridge-type rows; unrecoverable = total − recoverable (this captures
    # NULL session_id rows too, which a `NOT (sid GLOB ?)` filter would drop).
    total_row = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM memories WHERE channel = 'discord'{agent_clause}",
        agent_params,
    )
    total_discord = total_row[0][0] if total_row else 0
    unrecoverable_total = total_discord - recoverable_total

    # A few samples for inspection in dry-run.
    sample_rows = await db.execute_fetchall(
        f"""SELECT id, {recovered_expr}, {sid}
           FROM memories
           WHERE channel = 'discord' AND {sid} GLOB ?{agent_clause}
           LIMIT 5""",
        (_SNOWFLAKE_SESSION_GLOB, *agent_params),
    )
    samples = [{"id": r[0], "recovered_channel": r[1], "session_id": r[2]} for r in sample_rows]

    migrated = 0
    globalized = 0
    if not effective_dry_run:
        # bug-021: OR IGNORE — a recovered (agent_id, project_id, channel, content)
        # can collide with an existing row on the v12 idx_memories_dedup_content
        # UNIQUE index. A bare UPDATE would ABORT+rollback the whole statement
        # (migrated=0), and because the collision is data-deterministic every
        # re-run re-collides, so the migration could never complete. OR IGNORE
        # skips the colliding row (its target content already exists) and lets the
        # rest migrate; the docstring's idempotency claim is only true with it.
        cur = await db.execute(
            f"""UPDATE OR IGNORE memories
               SET channel = {recovered_expr}
               WHERE channel = 'discord' AND {sid} GLOB ?{agent_clause}""",
            (_SNOWFLAKE_SESSION_GLOB, *agent_params),
        )
        migrated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else recoverable_total
        if globalize_unrecoverable and unrecoverable_total:
            # bug-037: globalize ONLY genuinely-unrecoverable rows (NULL session_id
            # or a non-snowflake session_id). The earlier "whatever is still
            # 'discord' is the unrecoverable bucket" assumption breaks across the
            # await boundary: a do_store landing a fresh snowflake 'discord' row in
            # the window would be swept to channel='' (a silent scope-broadening
            # leak). Excluding snowflake rows leaves such a row on 'discord' for the
            # next migration pass instead. (OR IGNORE for symmetry with the above.)
            cur2 = await db.execute(
                f"UPDATE OR IGNORE memories SET channel = '' "
                f"WHERE channel = 'discord' AND ({sid} IS NULL OR NOT ({sid} GLOB ?)){agent_clause}",
                (_SNOWFLAKE_SESSION_GLOB, *agent_params),
            )
            globalized = cur2.rowcount if cur2.rowcount and cur2.rowcount > 0 else unrecoverable_total
        await db.commit()

    out = {
        "agent_id": agent_id,
        "dry_run": effective_dry_run,
        "recoverable_total": recoverable_total,
        "recoverable_by_channel": by_channel,
        "unrecoverable_total": unrecoverable_total,
        "globalize_unrecoverable": globalize_unrecoverable,
        "migrated": migrated,
        "globalized": globalized,
        "samples": samples,
    }
    if paused and not dry_run:
        out["repairs_skipped"] = True
        out["repairs_skip_reason"] = "no-persist mode active — dry_run forced"
    return out
