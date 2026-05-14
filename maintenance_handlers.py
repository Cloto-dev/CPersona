"""Maintenance and deep-check handlers for CPersona.

Tools: do_check_health (17 checks + auto-repair), do_deep_check (semantic
heuristic analysis).

Accesses `vector._embedding_client` for embedding dimension verification
and null-embedding re-population.
"""

import json
import logging
import re

from mcp_common import no_persist

import vector
from config import MAX_CONTENT_LENGTH
from database import SCHEMA_VERSION, get_db
from utils import _MEMORY_ANNOTATION_PATTERN, _MENTION_PATTERN

logger = logging.getLogger(__name__)

_USERNAME_PREFIX_PATTERN = re.compile(r"^\[(.+?)\]\s")
_DEEP_CHECK_ALL = ["anonymous_source", "short_content", "stale_profile", "orphaned_episodes"]
_SHORT_CONTENT_THRESHOLD = 5
_STALE_PROFILE_DAYS = 30


async def do_check_health(agent_id: str = "", fix: bool = False) -> dict:
    """Check and optionally fix memory database health issues."""
    # Under no-persist, downgrade fix=True to fix=False so the diagnostic
    # still runs but no rows are mutated. Clear no-persist and re-run to repair.
    repairs_skipped = bool(fix and no_persist.is_paused())
    if repairs_skipped:
        fix = False
    db = await get_db()
    issues = []

    agent_clause = "AND agent_id = ?" if agent_id else ""
    agent_params = (agent_id,) if agent_id else ()

    rows = await db.execute_fetchall(
        f"SELECT id, content FROM memories WHERE content LIKE '%[Memory from%' {agent_clause}",
        agent_params,
    )
    if rows:
        issues.append({"type": "memory_annotation", "count": len(rows)})
        if fix:
            for row_id, content in rows:
                cleaned = _MEMORY_ANNOTATION_PATTERN.sub("", content).strip()
                await db.execute("UPDATE memories SET content = ? WHERE id = ?", (cleaned, row_id))

    rows = await db.execute_fetchall(
        f"SELECT id, content FROM memories WHERE content LIKE '%<@%' {agent_clause}",
        agent_params,
    )
    if rows:
        issues.append({"type": "discord_mention", "count": len(rows)})
        if fix:
            for row_id, content in rows:
                cleaned = _MENTION_PATTERN.sub("", content).strip()
                await db.execute("UPDATE memories SET content = ? WHERE id = ?", (cleaned, row_id))

    dup_rows = await db.execute_fetchall(
        f"""SELECT content, COUNT(*) as cnt FROM memories
            WHERE 1=1 {agent_clause}
            GROUP BY agent_id, content HAVING cnt > 1""",
        agent_params,
    )
    if dup_rows:
        total_dupes = sum(r[1] - 1 for r in dup_rows)
        issues.append({"type": "duplicate_content", "groups": len(dup_rows), "total_extra": total_dupes})
        if fix:
            await db.execute(
                "DELETE FROM memories WHERE id NOT IN (SELECT MIN(id) FROM memories GROUP BY agent_id, content)"
            )

    rows = await db.execute_fetchall(
        f"SELECT id, length(content) as len FROM memories WHERE length(content) > ? {agent_clause}",
        (MAX_CONTENT_LENGTH, *agent_params),
    )
    if rows:
        issues.append({"type": "oversized_content", "count": len(rows), "max_len": max(r[1] for r in rows)})
        if fix:
            for row_id, _ in rows:
                await db.execute(
                    "UPDATE memories SET content = SUBSTR(content, 1, ?) WHERE id = ?",
                    (MAX_CONTENT_LENGTH, row_id),
                )

    count = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE channel = '' {agent_clause}",
            agent_params,
        )
    )[0][0]
    if count > 0:
        issues.append({"type": "empty_channel", "count": count})
        if fix:
            await db.execute(
                f"UPDATE memories SET channel = 'chat' WHERE channel = '' {agent_clause}",
                agent_params,
            )

    if vector._embedding_client:
        try:
            test_emb = await vector._embedding_client.embed(["test"])
            if test_emb and test_emb[0]:
                expected_bytes = len(test_emb[0]) * 4
                mismatched_mem = (
                    await db.execute_fetchall(
                        f"""SELECT COUNT(*) FROM memories
                        WHERE embedding IS NOT NULL AND length(embedding) != ?
                        {agent_clause}""",
                        (expected_bytes, *agent_params),
                    )
                )[0][0]
                mismatched_ep = (
                    await db.execute_fetchall(
                        f"""SELECT COUNT(*) FROM episodes
                        WHERE embedding IS NOT NULL AND length(embedding) != ?
                        {agent_clause}""",
                        (expected_bytes, *agent_params),
                    )
                )[0][0]
                mismatched = mismatched_mem + mismatched_ep
                if mismatched > 0:
                    issues.append(
                        {
                            "type": "embedding_dimension_mismatch",
                            "count": mismatched,
                            "memories": mismatched_mem,
                            "episodes": mismatched_ep,
                            "expected_dim": len(test_emb[0]),
                        }
                    )
                    if fix:
                        # NULL out mismatched BLOBs so the null_embedding fixer re-embeds them
                        if mismatched_mem > 0:
                            await db.execute(
                                f"""UPDATE memories SET embedding = NULL
                                WHERE embedding IS NOT NULL AND length(embedding) != ?
                                {agent_clause}""",
                                (expected_bytes, *agent_params),
                            )
                        if mismatched_ep > 0:
                            await db.execute(
                                f"""UPDATE episodes SET embedding = NULL
                                WHERE embedding IS NOT NULL AND length(embedding) != ?
                                {agent_clause}""",
                                (expected_bytes, *agent_params),
                            )
        except Exception as e:
            logger.warning("Embedding dimension check failed: %s", e)

    # Null embeddings (memories)
    null_count = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE embedding IS NULL {agent_clause}",
            agent_params,
        )
    )[0][0]
    if null_count > 0:
        issues.append({"type": "null_embedding", "count": null_count})

    # Null embedding auto-repair for memories (batch limit: 500)
    if null_count > 0 and fix and vector._embedding_client:
        rows = await db.execute_fetchall(
            f"SELECT id, content FROM memories WHERE embedding IS NULL {agent_clause} LIMIT 500",
            agent_params,
        )
        re_embedded = 0
        for row_id, content in rows:
            try:
                emb = await vector._embedding_client.embed([content])
                if emb and emb[0]:
                    blob = vector._embedding_client.pack_embedding(emb[0])
                    await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (blob, row_id))
                    re_embedded += 1
            except Exception:
                pass
        if re_embedded > 0:
            for issue in issues:
                if issue["type"] == "null_embedding":
                    issue["re_embedded"] = re_embedded
                    break

    # Null embeddings (episodes) — auto-repair with the same batch limit
    null_ep_count = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM episodes WHERE embedding IS NULL {agent_clause}",
            agent_params,
        )
    )[0][0]
    if null_ep_count > 0:
        issues.append({"type": "null_episode_embedding", "count": null_ep_count})

    if null_ep_count > 0 and fix and vector._embedding_client:
        ep_rows = await db.execute_fetchall(
            f"SELECT id, summary FROM episodes WHERE embedding IS NULL {agent_clause} LIMIT 500",
            agent_params,
        )
        ep_re_embedded = 0
        for row_id, summary in ep_rows:
            try:
                emb = await vector._embedding_client.embed([summary])
                if emb and emb[0]:
                    blob = vector._embedding_client.pack_embedding(emb[0])
                    await db.execute("UPDATE episodes SET embedding = ? WHERE id = ?", (blob, row_id))
                    ep_re_embedded += 1
            except Exception:
                pass
        if ep_re_embedded > 0:
            for issue in issues:
                if issue["type"] == "null_episode_embedding":
                    issue["re_embedded"] = ep_re_embedded
                    break

    try:
        mem_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories"))[0][0]
        mem_fts_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memories_fts"))[0][0]
        if mem_count != mem_fts_count:
            issues.append({"type": "fts_memories_desync", "memories": mem_count, "fts": mem_fts_count})
            if fix:
                await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

        ep_count = (await db.execute_fetchall("SELECT COUNT(*) FROM episodes"))[0][0]
        ep_fts_count = (await db.execute_fetchall("SELECT COUNT(*) FROM episodes_fts"))[0][0]
        if ep_count != ep_fts_count:
            issues.append({"type": "fts_episodes_desync", "episodes": ep_count, "fts": ep_fts_count})
            if fix:
                await db.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('rebuild')")
    except Exception:
        pass

    try:
        db_version = (await db.execute_fetchall("SELECT MAX(version) FROM schema_version"))[0][0]
        if db_version != SCHEMA_VERSION:
            issues.append(
                {
                    "type": "schema_version_mismatch",
                    "db_version": db_version,
                    "expected": SCHEMA_VERSION,
                }
            )
    except Exception:
        pass

    try:
        bad_source = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories WHERE json_valid(source) = 0 {agent_clause}",
                agent_params,
            )
        )[0][0]
        bad_metadata = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories WHERE json_valid(metadata) = 0 {agent_clause}",
                agent_params,
            )
        )[0][0]
        if bad_source + bad_metadata > 0:
            issues.append(
                {
                    "type": "invalid_json",
                    "bad_source": bad_source,
                    "bad_metadata": bad_metadata,
                }
            )
            if fix:
                await db.execute(
                    f"UPDATE memories SET source = '{{}}' WHERE json_valid(source) = 0 {agent_clause}",
                    agent_params,
                )
                await db.execute(
                    f"UPDATE memories SET metadata = '{{}}' WHERE json_valid(metadata) = 0 {agent_clause}",
                    agent_params,
                )
    except Exception:
        pass

    bad_ts = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE datetime(timestamp) IS NULL AND timestamp != '' {agent_clause}",
            agent_params,
        )
    )[0][0]
    if bad_ts > 0:
        issues.append({"type": "invalid_timestamp", "count": bad_ts})
        if fix:
            await db.execute(
                f"UPDATE memories SET timestamp = created_at WHERE datetime(timestamp) IS NULL AND timestamp != '' {agent_clause}",
                agent_params,
            )

    stale_tasks = (
        await db.execute_fetchall(
            "SELECT COUNT(*) FROM pending_memory_tasks WHERE created_at < datetime('now', '-1 hour')"
        )
    )[0][0]
    if stale_tasks > 0:
        issues.append({"type": "stale_pending_tasks", "count": stale_tasks})
        if fix:
            await db.execute("DELETE FROM pending_memory_tasks WHERE created_at < datetime('now', '-1 hour')")

    missing = await db.execute_fetchall(
        """SELECT DISTINCT m.agent_id FROM memories m
           LEFT JOIN profiles p ON m.agent_id = p.agent_id
           WHERE p.id IS NULL"""
    )
    if missing:
        agents = [r[0] for r in missing]
        issues.append({"type": "missing_profile", "count": len(agents), "agents": agents})

    empty_content = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE TRIM(content) = '' OR content IS NULL {agent_clause}",
            agent_params,
        )
    )[0][0]
    if empty_content > 0:
        issues.append({"type": "empty_content", "count": empty_content})
        if fix:
            await db.execute(
                f"DELETE FROM memories WHERE (TRIM(content) = '' OR content IS NULL) {agent_clause}",
                agent_params,
            )

    try:
        bad_source_type = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE (json_extract(source, '$.type') NOT IN ('User', 'Agent', 'System')
                    OR json_extract(source, '$.type') IS NULL)
                    {agent_clause}""",
                agent_params,
            )
        )[0][0]
        if bad_source_type > 0:
            issues.append({"type": "invalid_source_type", "count": bad_source_type})
            if fix:
                await db.execute(
                    f"""UPDATE memories SET source = '{{"type":"User","id":"","name":""}}'
                        WHERE (json_extract(source, '$.type') NOT IN ('User', 'Agent', 'System')
                        OR json_extract(source, '$.type') IS NULL) {agent_clause}""",
                    agent_params,
                )
    except Exception:
        pass

    try:
        anon_source = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE json_extract(source, '$.type') = 'User'
                    AND json_extract(source, '$.id') = ''
                    AND json_extract(source, '$.name') = ''
                    {agent_clause}""",
                agent_params,
            )
        )[0][0]
        if anon_source > 0:
            issues.append(
                {
                    "type": "anonymous_source",
                    "count": anon_source,
                    "hint": "Use deep_check with fix=true to recover names from content",
                }
            )
    except Exception:
        pass

    if fix:
        await db.commit()

    total = (await db.execute_fetchall(f"SELECT COUNT(*) FROM memories WHERE 1=1 {agent_clause}", agent_params))[0][0]

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
        "pending_tasks": (await db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks"))[0][0],
    }
    if agent_id:
        stats["agent_memories"] = total
        stats["agent_episodes"] = (
            await db.execute_fetchall("SELECT COUNT(*) FROM episodes WHERE agent_id = ?", (agent_id,))
        )[0][0]

    result = {
        "total_memories": total,
        "issues": issues,
        "healthy": len(issues) == 0,
        "fixed": fix,
        "stats": stats,
    }
    if repairs_skipped:
        result["repairs_skipped"] = True
        result["repairs_skip_reason"] = "no-persist mode active — fix downgraded to fix=False"
    return result


async def do_deep_check(agent_id: str, fix: bool = False, checks: list | None = None) -> dict:
    """Deep semantic analysis of memory data quality for a specific agent."""
    repairs_skipped = bool(fix and no_persist.is_paused())
    if repairs_skipped:
        fix = False
    db = await get_db()
    selected = checks if checks else _DEEP_CHECK_ALL
    results: dict[str, dict] = {}

    if "anonymous_source" in selected:
        rows = await db.execute_fetchall(
            """SELECT id, content FROM memories
               WHERE agent_id = ?
               AND json_extract(source, '$.type') = 'User'
               AND json_extract(source, '$.id') = ''
               AND json_extract(source, '$.name') = ''""",
            (agent_id,),
        )
        recoverable = []
        unrecoverable = []
        for row_id, content in rows:
            match = _USERNAME_PREFIX_PATTERN.match(content)
            if match:
                recoverable.append({"id": row_id, "recovered_name": match.group(1)})
            else:
                unrecoverable.append({"id": row_id, "content_preview": content[:60]})

        fixed_count = 0
        if fix and recoverable:
            for item in recoverable:
                new_source = json.dumps({"type": "User", "id": "", "name": item["recovered_name"]})
                await db.execute("UPDATE memories SET source = ? WHERE id = ?", (new_source, item["id"]))
            fixed_count = len(recoverable)

        result = {"recoverable": len(recoverable), "unrecoverable": len(unrecoverable)}
        if fix:
            result["fixed"] = fixed_count
        if recoverable:
            result["samples"] = recoverable[:5]
        if unrecoverable:
            result["unrecoverable_samples"] = unrecoverable[:5]
        results["anonymous_source"] = result

    if "short_content" in selected:
        rows = await db.execute_fetchall(
            "SELECT id, content FROM memories WHERE agent_id = ? AND LENGTH(TRIM(content)) <= ?",
            (agent_id, _SHORT_CONTENT_THRESHOLD),
        )
        fixed_count = 0
        if fix and rows:
            ids = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids))
            await db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
            fixed_count = len(ids)

        result = {"count": len(rows)}
        if fix:
            result["fixed"] = fixed_count
        if rows:
            result["samples"] = [{"id": r[0], "content": r[1]} for r in rows[:10]]
        results["short_content"] = result

    if "stale_profile" in selected:
        rows = await db.execute_fetchall(
            """SELECT id, updated_at FROM profiles
               WHERE agent_id = ? AND user_id = ''
               AND updated_at < datetime('now', ?)""",
            (agent_id, f"-{_STALE_PROFILE_DAYS} days"),
        )
        result = {"count": len(rows), "threshold_days": _STALE_PROFILE_DAYS}
        if rows:
            result["last_updated"] = rows[0][1]
        results["stale_profile"] = result

    if "orphaned_episodes" in selected:
        rows = await db.execute_fetchall(
            """SELECT e.id, e.summary, e.start_time, e.end_time FROM episodes e
               WHERE e.agent_id = ?
               AND e.start_time IS NOT NULL AND e.end_time IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM memories m
                   WHERE m.agent_id = e.agent_id
                   AND m.timestamp >= e.start_time AND m.timestamp <= e.end_time
               )""",
            (agent_id,),
        )
        result = {"count": len(rows)}
        if rows:
            result["samples"] = [{"id": r[0], "summary": r[1][:80], "start": r[2], "end": r[3]} for r in rows[:5]]
        results["orphaned_episodes"] = result

    if fix:
        await db.commit()

    out = {
        "agent_id": agent_id,
        "checks_run": selected,
        "results": results,
        "fixed": fix,
    }
    if repairs_skipped:
        out["repairs_skipped"] = True
        out["repairs_skip_reason"] = "no-persist mode active — fix downgraded to fix=False"
    return out
