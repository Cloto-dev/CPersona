"""Maintenance and deep-check handlers for CPersona.

Tools: do_check_health / do_deep_check — thin dispatch wrappers over the
check registry in ``cpersona.checks`` (v2.4.37). The registry is the single
implementation shared by the MCP tools, the pytest fixtures, and the
``python -m cpersona.checkup`` CLI; check semantics, severities and fix
behaviour live there, response envelopes live here.
"""

import importlib.metadata
import logging

from cpersona import checks as checks_registry
from cpersona import config
from cpersona import findings as findings_seam
from cpersona import session
from cpersona import vector
from cpersona.database import connection, transaction
from cpersona.isolation import isolation_where
from cpersona.session import resolve_session_key
from cpersona.utils import error_response

logger = logging.getLogger(__name__)


async def do_check_health(
    agent_id: str = "", fix: bool = False, checks: list | None = None, session_key: str = ""
) -> dict:
    """Check and optionally fix memory database health issues.

    Runs the full check registry (or the subset named in ``checks``); every
    issue carries ``severity`` (critical / warn / info) and ``check``. The
    ``severity_summary`` counts feed the checkup CLI's gate exit code.
    """
    # bug-230: an unrecognised name selects NOTHING, so the run used to answer
    # status='healthy' with an empty issues list after executing zero checks —
    # a typo ('empty_contnet') read as a clean bill of health, and the response
    # carried no record of what actually ran. Reject the call instead, and echo
    # `checks_run` the way deep_check does so a caller can always tell.
    unknown = [name for name in (checks or []) if name not in checks_registry.HEALTH_CHECK_NAMES]
    if unknown:
        return error_response(
            f"unknown check name(s): {', '.join(unknown)}. Valid names: "
            f"{', '.join(checks_registry.HEALTH_CHECK_NAMES)}",
            unknown_checks=unknown,
            valid_checks=list(checks_registry.HEALTH_CHECK_NAMES),
        )
    checks_run = list(checks) if checks else list(checks_registry.HEALTH_CHECK_NAMES)

    # Under no-persist, downgrade fix=True to fix=False so the diagnostic
    # still runs but no rows are mutated. Clear no-persist and re-run to repair.
    key, _declared = resolve_session_key(session_key)
    repairs_skipped = bool(fix and session.is_paused_for(key))
    if repairs_skipped:
        fix = False

    # bug-072: pre-compute the null-embedding re-embeddings OUTSIDE the write seam. Those
    # two checks do up to ~1000 sequential embedding HTTP calls; holding the shared write
    # lock across them stalled every other writer — do_store, the queue drain,
    # import/merge — for the entire re-embed. The transaction below covers only the DB
    # writes+commit; the network I/O happens here, unlocked.
    # bug-083: the dimension probe embed rides in the same unlocked phase (as
    # embedding_cache["expected_dim"]) so check_embedding_dimension no longer embeds
    # under the lock either.
    embedding_cache = None
    if fix:
        async with connection() as db:
            embedding_cache = await checks_registry.prefetch_null_embeddings(db, agent_id)
        embedding_cache["expected_dim"] = await checks_registry.probe_embedding_dim()

    # bug-254: the REPORT-ONLY whole-database scan leaves the write seam for the
    # same reason the embedding round-trips did. check_sqlite_integrity runs
    # PRAGMA quick_check over the whole file — O(database), fix_capable=False,
    # it can never write — yet under fix=True it executed inside transaction(),
    # so every other writer — do_store, the queue drain, import/merge — waited
    # on a scan that could not have needed the lock.
    #
    # check_fts_integrity, the other whole-database scan, deliberately STAYS in
    # the locked run: it is fix-capable, and its detection must be atomic with
    # its repair. Deciding "needs no repair" from an unlocked pre-scan opens a
    # window the fix run itself then falls into — the content-rewriting repairs
    # (memory_annotation, oversized_content, ...) run before fts_integrity in
    # registry order, and on a database whose FTS triggers are missing they
    # CREATE the drift after a clean pre-scan said there was nothing to repair;
    # the corruption would then be counted by the residual severity re-run but
    # repaired by nothing and named by no issue. One always-locked scan is the
    # price of never shipping that contradiction.
    scan_issues: list[dict] = []
    locked_checks = checks_run
    if fix:
        scan_names = [
            n
            for n in checks_run
            if n in checks_registry.WHOLE_DB_SCAN_CHECKS and not checks_registry.is_fix_capable(n)
        ]
        if scan_names:
            async with connection() as db:
                scan_issues, _ = await checks_registry.run_health_checks(
                    db, agent_id=agent_id, fix=False, checks=scan_names
                )
            locked_checks = [n for n in checks_run if n not in scan_names]

    # bug-042/043: a fix run's writes + commit are serialised by transaction() so a
    # concurrent import/merge cannot flush check_health's partial repairs (and vice
    # versa). The read-only (fix=False) path goes through the plain read seam.
    issues: list[dict] = []
    # Same key order run_health_checks emits, so a caller reading the serialised
    # response sees one shape whichever branch produced it.
    severity_summary = {"critical": 0, "warn": 0, "info": 0}
    # Empty only when every requested check was a scan already settled above; an
    # empty `checks` list means "everything" to run_health_checks, so this must
    # not reach it as one.
    if locked_checks:
        async with (transaction() if fix else connection()) as db:
            issues, severity_summary = await checks_registry.run_health_checks(
                db, agent_id=agent_id, fix=fix, checks=locked_checks, embedding_cache=embedding_cache
            )
    if scan_issues:
        # Registry order, as if one run had produced them. The scan findings are
        # NOT added to severity_summary here: the split only happens under
        # fix=True, and the residual re-run below unconditionally rebinds the
        # summary before anything can observe it — a report-only finding is by
        # definition still true then, so the residual count includes it.
        issues = checks_registry.merge_issues(issues, scan_issues)

    # bug-083 second pass: rows NULLed DURING the locked run (embedding_dimension NULLs
    # mismatched blobs; memory_annotation / discord_mention / oversized_content rewrite
    # content and NULL the embedding) are absent from the round-1 prefetch, and the
    # locked re-embed deliberately no longer live-embeds on a cache miss. Repair them
    # here: embed the CURRENT (post-rewrite) text outside the write seam, then write
    # under a short transaction with the text revalidated inside the UPDATE itself
    # (bug-077), so a single fix run still converges without ever holding the lock
    # across HTTP.
    if fix and vector._embedding_client:
        async with connection() as db:
            second_pass = await checks_registry.prefetch_null_embeddings(db, agent_id)
        if second_pass["memories"] or second_pass["episodes"]:
            async with transaction() as db:
                await checks_registry.apply_embedding_cache(db, second_pass)

    # bug-059: after a fix run, re-derive status/severity_summary from the RESIDUAL
    # state (read-only, post-commit) rather than from the issues that were FOUND.
    # Runners are inconsistent about stamping issue['fixed'] (schema_object_drift
    # does, stale_pending_tasks deletes without a marker), so filtering on 'fixed'
    # is unreliable; a fix=False re-run reports true residual uniformly, so a clean
    # auto-repair is no longer reported as unhealthy (and the checkup CLI no longer
    # exits nonzero after a successful fix).
    # bug-225: only the SEVERITY comes from that residual run. Rebinding `issues`
    # to it as well discarded every field a runner emits only under fix=True —
    # `fixed` / `fix_error`, `mapped` / `unmapped` / `remaining` (bug-210's
    # non-convergence signal), `re_embedded`, `normalized` — so the MCP tool and
    # the checkup CLI, the only surfaces operators use, could not tell a failed
    # repair from one that was never attempted, nor a capped run from a converged
    # one. Tests that pin those fields call the runner directly and never saw it.
    #
    # bug-254: this re-run repeats the sqlite_integrity scan the unlocked
    # pre-phase already did, and that is not redundancy — the two answer
    # different questions. The pre-phase says what is broken going in; this one
    # says what is still true after the repairs committed, which is the only
    # thing the verdict may be derived from — including damage the fix run's own
    # writes introduced. Both copies run on the read seam; the locked copy of
    # this report-only scan is the one bug-254 removed. (fts_integrity is not
    # part of the split at all: detection and repair stay atomic under the lock,
    # see the dispatch above.)
    if fix:
        async with connection() as db:
            _residual_issues, severity_summary = await checks_registry.run_health_checks(
                db, agent_id=agent_id, fix=False, checks=checks
            )

    iso = isolation_where(agent_id=agent_id or None)
    async with connection() as db:
        total = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories{iso.where}", iso.params
            )
        )[0][0]

        try:
            page_info = await db.execute_fetchall("PRAGMA page_count")
            page_size_info = await db.execute_fetchall("PRAGMA page_size")
            db_size_bytes = page_info[0][0] * page_size_info[0][0]
        except Exception:
            db_size_bytes = 0

        # bug-058: scope episodes / profiles / pending_tasks to the requested agent
        # when agent_id is set, so every count under an unprefixed stats key is
        # consistent with `memories` (which is agent-scoped via `total`). Before this,
        # check_health(agent_id='A') returned agent-scoped memories but corpus-wide
        # episodes/profiles, so a dashboard reading stats.episodes saw every agent's
        # episodes. Empty agent_id keeps the corpus-wide totals.
        stats = {
            "db_size_bytes": db_size_bytes,
            "memories": total,
            "episodes": (
                await db.execute_fetchall(
                    f"SELECT COUNT(*) FROM episodes{iso.where}", iso.params
                )
            )[0][0],
            "profiles": (
                await db.execute_fetchall(
                    f"SELECT COUNT(*) FROM profiles{iso.where}", iso.params
                )
            )[0][0],
            "pending_tasks": (
                await db.execute_fetchall(
                    f"SELECT COUNT(*) FROM pending_memory_tasks{iso.where}", iso.params
                )
            )[0][0],
            # Axis distributions are observations, not issues (rare != wrong).
            # bug-062: pass agent_id so a per-agent run does not leak other agents' buckets.
            "axes": await checks_registry.axis_distribution(db, agent_id),
        }
        if agent_id:
            stats["agent_memories"] = total
            stats["agent_episodes"] = (
                await db.execute_fetchall(
                    "SELECT COUNT(*) FROM episodes WHERE agent_id = ?", (agent_id,)
                )
            )[0][0]

    # 2.5.2b1: ``status`` is the only verdict. The legacy
    # ``healthy`` boolean was ``len(issues) == 0``, which reported False for an
    # info-only database — an observation, not a defect (the bug-009 lesson) —
    # while ``status`` already said 'healthy' for the same run. Two verdicts
    # disagreeing by construction is worse than one, and a caller wanting the
    # old meaning still has ``issues`` and ``severity_summary`` verbatim.
    #
    # bug-225: the two halves of a fix run's response answer different questions
    # and come from different runs. ``issues`` is what the FIX run did (its
    # fix-only fields intact); ``severity_summary`` / ``status`` are the RESIDUAL
    # verdict measured after the commit (bug-059), so a repaired database is not
    # reported unhealthy for the issues it just fixed. On a fix=False run the two
    # are the same run.
    result = {
        "total_memories": total,
        "issues": issues,
        "severity_summary": severity_summary,
        "status": checks_registry.health_status(severity_summary),
        "fixed": fix,
        "checks_run": checks_run,
        "stats": stats,
    }
    if repairs_skipped:
        result["repairs_skipped"] = True
        result["repairs_skip_reason"] = "no-persist mode active — fix downgraded to fix=False"
    return result


async def do_deep_check(
    agent_id: str, fix: bool = False, checks: list | None = None, session_key: str = ""
) -> dict:
    """Deep heuristic analysis of memory data quality for a specific agent."""
    key, _declared = resolve_session_key(session_key)
    repairs_skipped = bool(fix and session.is_paused_for(key))
    if repairs_skipped:
        fix = False
    selected = checks if checks else checks_registry.DEEP_CHECK_NAMES
    results: dict[str, dict] = {}

    # bug-042/043: a fix run's writes + commit are serialised by transaction() so a
    # concurrent import/merge cannot flush this run's partial repairs. The read-only
    # (fix=False) path goes through the plain read seam.
    async with (transaction() if fix else connection()) as db:
        for name in selected:
            runner = checks_registry.DEEP_CHECKS.get(name)
            if runner is None:
                continue  # unknown names are silently skipped (pre-registry behaviour)
            try:
                results[name] = await runner(db, agent_id, fix)
            except Exception as e:
                logger.warning("deep check %s crashed: %s", name, e)
                results[name] = {"error": str(e)}

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
# bug-239: the json_extract is guarded by json_valid(metadata), the bug-144
# hazard checks.py already documents — json_extract RAISES 'malformed JSON' (it
# does not return NULL) on a non-JSON metadata value, so a single
# channel='discord' row with unparseable metadata (one check_invalid_json can
# leave behind forever when the row is locked) aborted every statement in this
# tool, including the dry run whose whole purpose is to report before mutating.
#
# The guard is a CASE rather than checks.py's leading `json_valid(x) AND …`
# because it must hold in EVERY position, not only as a top-level WHERE
# conjunct: measured on SQLite 3.50, `SELECT json_valid(m) AND json_extract(m,
# '$.k')` still raises (the AND short-circuit is a WHERE-clause property), while
# CASE is defined to evaluate only the selected branch. A malformed row
# therefore reads as "no session_id", which is exactly what it is.
_SESSION_ID_EXPR = "CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.session_id') END"
_SNOWFLAKE_SESSION_GLOB = "[0-9]*:*"

_RECOVERABLE_WHERE = f"channel = 'discord' AND {_SESSION_ID_EXPR} GLOB ?"
# The complement: no session_id at all (including the malformed rows, which are
# unrecoverable by definition) or one that is not a snowflake.
_UNRECOVERABLE_WHERE = (
    f"channel = 'discord' AND ({_SESSION_ID_EXPR} IS NULL OR NOT ({_SESSION_ID_EXPR} GLOB ?))"
)
_INVALID_METADATA_WHERE = (
    "channel = 'discord' AND metadata IS NOT NULL AND json_valid(metadata) = 0"
)
_INVALID_METADATA_SAMPLE_LIMIT = 10


async def do_migrate_channel_axis(
    agent_id: str = "",
    dry_run: bool = True,
    globalize_unrecoverable: bool = False,
    session_key: str = "",
) -> dict:
    """Re-channel bridge-type memories to their concrete channel (knob2 v2).

    Prepares the knob2 v2 default flip. Under the historical default
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

    Rows whose metadata is not valid JSON are unrecoverable by definition and
    are additionally reported on their own (`invalid_metadata_total` /
    `invalid_metadata_ids`, bug-239) — they used to abort the whole tool.
    """
    # Under no-persist, force a report-only run so nothing mutates.
    key, _declared = resolve_session_key(session_key)
    paused = session.is_paused_for(key)
    effective_dry_run = dry_run or paused

    iso = isolation_where(agent_id=agent_id or None)

    sid = _SESSION_ID_EXPR
    recovered_expr = f"substr({sid}, 1, instr({sid}, ':') - 1)"

    async with connection() as db:
        # Recoverable rows, grouped by the channel they would be moved to.
        recoverable_rows = await db.execute_fetchall(
            f"""SELECT {recovered_expr} AS recovered_channel, COUNT(*) AS n
               FROM memories
               WHERE {_RECOVERABLE_WHERE}{iso.and_clause}
               GROUP BY recovered_channel
               ORDER BY n DESC""",
            (_SNOWFLAKE_SESSION_GLOB, *iso.params),
        )
        recoverable_total = sum(r[1] for r in recoverable_rows)
        by_channel = [{"channel": r[0], "count": r[1]} for r in recoverable_rows]

        # Total bridge-type rows; unrecoverable = total − recoverable (this captures
        # NULL session_id rows too, which a `NOT (sid GLOB ?)` filter would drop).
        total_row = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE channel = 'discord'{iso.and_clause}",
            iso.params,
        )
        total_discord = total_row[0][0] if total_row else 0
        unrecoverable_total = total_discord - recoverable_total

        # A few samples for inspection in dry-run.
        sample_rows = await db.execute_fetchall(
            f"""SELECT id, {recovered_expr}, {sid}
               FROM memories
               WHERE {_RECOVERABLE_WHERE}{iso.and_clause}
               LIMIT 5""",
            (_SNOWFLAKE_SESSION_GLOB, *iso.params),
        )
        samples = [{"id": r[0], "recovered_channel": r[1], "session_id": r[2]} for r in sample_rows]

        # bug-239: the malformed-metadata rows are reported as their own counted,
        # listed remainder (they are inside `unrecoverable_total` too). Naming
        # them is the difference between "this tool cannot run, find the row by
        # hand" and "these ids need an operator".
        invalid_metadata_total = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories WHERE {_INVALID_METADATA_WHERE}{iso.and_clause}",
                iso.params,
            )
        )[0][0]
        invalid_metadata_ids = [
            r[0]
            for r in await db.execute_fetchall(
                f"""SELECT id FROM memories
                   WHERE {_INVALID_METADATA_WHERE}{iso.and_clause}
                   ORDER BY id LIMIT {_INVALID_METADATA_SAMPLE_LIMIT}""",
                iso.params,
            )
        ]

    migrated = 0
    globalized = 0
    if not effective_dry_run:
        # bug-042/043: transaction() serialises the whole migrate behind the shared
        # lock so its commit cannot flush a concurrent import/merge's partial rows.
        # bug-068: its auto-rollback discards a partial migrate on failure, so a later
        # committer on the shared connection cannot flush half-written rows.
        async with transaction() as db:
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
                   WHERE {_RECOVERABLE_WHERE}{iso.and_clause}""",
                (_SNOWFLAKE_SESSION_GLOB, *iso.params),
            )
            # UPDATE OR IGNORE's changes() counts only rows actually updated, so a full
            # collision (every recovered row's target content already exists → all
            # skipped) legitimately reports 0 — that is NOT a "rowcount unavailable"
            # signal. Only fall back to the recoverable estimate when the driver gives
            # no count at all (None / negative); a genuine 0 must be reported as 0,
            # otherwise the full-collision case over-reports recoverable_total migrated.
            migrated = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else recoverable_total
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
                    f"WHERE {_UNRECOVERABLE_WHERE}{iso.and_clause}",
                    (_SNOWFLAKE_SESSION_GLOB, *iso.params),
                )
                # Same rowcount semantics as `migrated` above: a real 0 (full collision)
                # is authoritative; only None/negative means "count unavailable".
                globalized = cur2.rowcount if cur2.rowcount is not None and cur2.rowcount >= 0 else unrecoverable_total

    out = {
        "agent_id": agent_id,
        "dry_run": effective_dry_run,
        "recoverable_total": recoverable_total,
        "recoverable_by_channel": by_channel,
        "unrecoverable_total": unrecoverable_total,
        "invalid_metadata_total": invalid_metadata_total,
        "invalid_metadata_ids": invalid_metadata_ids,
        "globalize_unrecoverable": globalize_unrecoverable,
        "migrated": migrated,
        "globalized": globalized,
        "samples": samples,
    }
    if paused and not dry_run:
        out["repairs_skipped"] = True
        out["repairs_skip_reason"] = "no-persist mode active — dry_run forced"
    return out


async def do_get_session_findings(
    session_key: str = "", per_kind_limit: int = findings_seam.DEFAULT_PER_KIND_LIMIT, include_summary: bool = True
) -> dict:
    """Pull the storage-integrity findings on demand (SuperAuditor v1 seam).

    The same detector ``check_health`` runs — ``checks.run_health_checks`` over
    the whole database with ``fix=False`` — delivered as findings with the
    static per-kind ``severity`` and honest per-kind caps that
    ``docs/SUPERAUDITOR_STANDARD.md`` specifies. Read-only: nothing is
    repaired, nothing is written, and the call cannot corrupt anything.

    It is not free, though, and "read-only" is easy to read as "cheap". The
    registry is run unfiltered, which includes the two probes it names itself
    as whole-database reads (``checks.WHOLE_DB_SCAN_CHECKS``): ``fts_integrity``
    runs the FTS5 integrity-check over both indexes and ``sqlite_integrity``
    runs ``PRAGMA quick_check`` over the file. Both are O(database) on every
    pull, and this is a channel meant to be pulled once a session. There is
    deliberately no cheap subset: a caller that could ask for one would be
    choosing which forgotten state stays forgotten, which is the opposite of
    what the channel is for (standard §7). Scope by cost with call frequency,
    not by narrowing the probe set.

    Findings are NOT scoped to an agent or a project (standard §7): the
    channel exists to surface forgotten state, and slicing it by the bucket
    the caller happens to be reading would hide exactly the rows that were
    forgotten. Scope a repair with ``check_health(agent_id=...)`` instead.

    ``session_key`` is an opaque partition hint, accepted so a caller can
    declare itself; this server carries no session-scoped probes, so the key
    changes nothing but the honesty flag: on a shared transport with no key
    declared the response carries ``identity_shared: true`` rather than
    pretending it can tell sessions apart.
    """
    if per_kind_limit < 1:
        return error_response(
            f"per_kind_limit must be at least 1 (got {per_kind_limit})", per_kind_limit=per_kind_limit
        )
    async with connection() as db:
        issues, _ = await checks_registry.run_health_checks(db, agent_id="", fix=False)
    delivered = findings_seam.deliver_issues(issues, per_kind_limit)
    if include_summary:
        delivered["summary"] = findings_seam.render_summary(delivered)
    if config.transport() != "stdio" and not session_key:
        delivered["identity_shared"] = True
    delivered["_meta"] = {"server_version": _server_version()}
    return delivered


def _server_version() -> str:
    try:
        return importlib.metadata.version("cpersona")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
        return "unknown"
