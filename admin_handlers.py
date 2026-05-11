"""Administrative tool handlers for CPersona.

Tools: profile (get/update), list, delete, update, lock/unlock, agent data wipe,
threshold calibration, episode delete, export/import, merge, queue status.

Accesses `vector._embedding_client` (remote vector index sync) and
`tasks._task_queue` (queue status) as module attributes.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone

import config
import tasks
import vector
from config import (
    CALIBRATE_FLOOR,
    CALIBRATE_SAMPLE_SIZE,
    CALIBRATE_Z_FACTOR,
    FTS_ENABLED,
    TASK_QUEUE_ENABLED,
    VECTOR_SEARCH_MODE,
)
from database import get_db
from utils import _clamp_limit, _try_parse_json

logger = logging.getLogger(__name__)


async def do_get_profile(agent_id: str) -> dict:
    """Get the current profile for an agent."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ? AND user_id = '' LIMIT 1",
        (agent_id,),
    )
    return {"profile": rows[0][0] if rows else ""}


async def do_update_profile(agent_id: str, profile: str = "") -> dict:
    """Update agent profile with pre-computed content."""
    db = await get_db()

    if not profile:
        return {"ok": True, "profiles_updated": 0}

    await db.execute(
        """INSERT INTO profiles (agent_id, user_id, content, updated_at)
           VALUES (?, '', ?, datetime('now'))
           ON CONFLICT(agent_id, user_id) DO UPDATE SET
               content = excluded.content,
               updated_at = excluded.updated_at""",
        (agent_id, profile),
    )
    await db.commit()
    return {"ok": True, "profiles_updated": 1}


async def do_list_memories(agent_id: str, limit: int) -> dict:
    """List recent memories for dashboard display."""
    db = await get_db()
    if agent_id:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, msg_id, content, source, timestamp, created_at, locked "
            "FROM memories WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, _clamp_limit(limit, 500)),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, msg_id, content, source, timestamp, created_at, locked "
            "FROM memories ORDER BY created_at DESC LIMIT ?",
            (_clamp_limit(limit, 500),),
        )
    memories = []
    for row in rows:
        source = {}
        try:
            source = json.loads(row[4]) if row[4] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        memories.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "content": row[3],
                "source": source,
                "timestamp": row[5],
                "created_at": row[6],
                "locked": bool(row[7]),
            }
        )
    return {"memories": memories, "count": len(memories)}


async def do_list_episodes(agent_id: str, limit: int) -> dict:
    """List archived episodes for dashboard display."""
    db = await get_db()
    if agent_id:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, summary, keywords, start_time, end_time, created_at "
            "FROM episodes WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, _clamp_limit(limit, 200)),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, summary, keywords, start_time, end_time, created_at "
            "FROM episodes ORDER BY created_at DESC LIMIT ?",
            (_clamp_limit(limit, 200),),
        )
    episodes = []
    for row in rows:
        episodes.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "summary": row[2],
                "keywords": row[3],
                "start_time": row[4],
                "end_time": row[5],
                "created_at": row[6],
            }
        )
    return {"episodes": episodes, "count": len(episodes)}


async def do_delete_memory(memory_id: int, agent_id: str = "") -> dict:
    """Delete a single memory by ID.

    When agent_id is provided (non-empty), enforces ownership.
    """
    db = await get_db()
    row = await db.execute_fetchone("SELECT locked FROM memories WHERE id = ?", (memory_id,))
    if row is None:
        return {"error": f"Memory {memory_id} not found"}
    if row[0]:
        return {"error": f"Memory {memory_id} is locked and cannot be deleted"}

    if agent_id:
        cursor = await db.execute(
            "DELETE FROM memories WHERE id = ? AND agent_id = ?",
            (memory_id, agent_id),
        )
    else:
        cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    await db.commit()
    if cursor.rowcount == 0:
        return {"error": f"Memory {memory_id} not found or not owned by agent"}

    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        ns = f"cpersona:{agent_id}" if agent_id else "cpersona:"
        try:
            base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
            await vector._embedding_client._client.post(
                f"{base_url}/remove",
                json={"namespace": ns, "ids": [f"mem:{memory_id}"]},
            )
        except Exception as e:
            logger.debug("Remote remove failed (non-fatal): %s", e)

    return {"ok": True, "deleted_id": memory_id}


async def do_update_memory(memory_id: int, content: str, agent_id: str = "") -> dict:
    """Update memory content by ID. Rejects if memory is locked."""
    if not content or not content.strip():
        return {"error": "Content cannot be empty"}

    db = await get_db()
    row = await db.execute_fetchone("SELECT locked, agent_id FROM memories WHERE id = ?", (memory_id,))
    if row is None:
        return {"error": f"Memory {memory_id} not found"}
    if row[0]:
        return {"error": f"Memory {memory_id} is locked and cannot be edited"}
    if agent_id and row[1] != agent_id:
        return {"error": f"Memory {memory_id} not owned by agent {agent_id}"}

    content = content.strip()
    await db.execute("UPDATE memories SET content = ? WHERE id = ?", (content, memory_id))

    if FTS_ENABLED:
        try:
            await db.execute("UPDATE memories_fts SET content = ? WHERE rowid = ?", (content, memory_id))
        except Exception:
            pass

    await db.commit()
    return {"ok": True, "updated_id": memory_id}


async def do_lock_memory(memory_id: int, agent_id: str = "") -> dict:
    """Lock a memory to prevent deletion and editing."""
    db = await get_db()
    row = await db.execute_fetchone("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if row is None:
        return {"error": f"Memory {memory_id} not found"}
    if agent_id and row[0] != agent_id:
        return {"error": f"Memory {memory_id} not owned by agent {agent_id}"}

    await db.execute("UPDATE memories SET locked = 1 WHERE id = ?", (memory_id,))
    await db.commit()
    return {"ok": True, "locked_id": memory_id}


async def do_unlock_memory(memory_id: int, agent_id: str = "") -> dict:
    """Unlock a memory to allow deletion and editing."""
    db = await get_db()
    row = await db.execute_fetchone("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if row is None:
        return {"error": f"Memory {memory_id} not found"}
    if agent_id and row[0] != agent_id:
        return {"error": f"Memory {memory_id} not owned by agent {agent_id}"}

    await db.execute("UPDATE memories SET locked = 0 WHERE id = ?", (memory_id,))
    await db.commit()
    return {"ok": True, "unlocked_id": memory_id}


async def do_delete_agent_data(agent_id: str) -> dict:
    """Delete ALL data for a specific agent (memories, profiles, episodes)."""
    if not agent_id:
        return {"error": "agent_id is required for bulk deletion"}

    db = await get_db()
    mem_cursor = await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent_id,))
    prof_cursor = await db.execute("DELETE FROM profiles WHERE agent_id = ?", (agent_id,))
    ep_cursor = await db.execute("DELETE FROM episodes WHERE agent_id = ?", (agent_id,))
    await db.commit()

    if VECTOR_SEARCH_MODE == "remote" and vector._embedding_client and vector._embedding_client._http_url:
        try:
            base_url = vector._embedding_client._http_url.rsplit("/", 1)[0]
            await vector._embedding_client._client.post(
                f"{base_url}/purge",
                json={"namespace": f"cpersona:{agent_id}"},
            )
        except Exception as e:
            logger.debug("Remote purge failed (non-fatal): %s", e)

    result = {
        "ok": True,
        "agent_id": agent_id,
        "deleted_memories": mem_cursor.rowcount,
        "deleted_profiles": prof_cursor.rowcount,
        "deleted_episodes": ep_cursor.rowcount,
    }
    logger.info(
        "Deleted agent data for %s: %d memories, %d profiles, %d episodes",
        agent_id,
        mem_cursor.rowcount,
        prof_cursor.rowcount,
        ep_cursor.rowcount,
    )
    return result


async def do_calibrate_threshold(agent_id: str, sample_size: int = 0, z_factor: float = 0) -> dict:
    """Auto-calibrate VECTOR_MIN_SIMILARITY based on embedding distribution.

    Uses null distribution of pairwise cosine similarities (mostly unrelated pairs).
    Mutates `config.VECTOR_MIN_SIMILARITY` in-place.
    """
    import numpy as np

    db = await get_db()
    sample_n = sample_size or CALIBRATE_SAMPLE_SIZE
    z = z_factor or CALIBRATE_Z_FACTOR

    rows = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = ? AND embedding IS NOT NULL ORDER BY RANDOM() LIMIT ?",
        (agent_id, sample_n),
    )

    if len(rows) < 10:
        return {"ok": False, "error": f"Need at least 10 embeddings, found {len(rows)}"}

    vecs = []
    for (blob,) in rows:
        vec = np.frombuffer(blob, dtype=np.float32).copy()
        vecs.append(vec)
    vecs = np.array(vecs)

    sim_matrix = vecs @ vecs.T

    n = len(vecs)
    triu_indices = np.triu_indices(n, k=1)
    pairwise_sims = sim_matrix[triu_indices]

    num_pairs = len(pairwise_sims)
    old_threshold = config.VECTOR_MIN_SIMILARITY

    sim_mean = float(np.mean(pairwise_sims))
    sim_std = float(np.std(pairwise_sims))
    sim_median = float(np.median(pairwise_sims))

    z_threshold = sim_mean - z * sim_std
    new_threshold = max(z_threshold, CALIBRATE_FLOOR)

    config.VECTOR_MIN_SIMILARITY = round(new_threshold, 4)

    result = {
        "ok": True,
        "agent_id": agent_id,
        "sampled_embeddings": n,
        "num_pairs": num_pairs,
        "z_factor": z,
        "distribution": {
            "mean": round(sim_mean, 4),
            "std": round(sim_std, 4),
            "median": round(sim_median, 4),
        },
        "old_threshold": old_threshold,
        "new_threshold": config.VECTOR_MIN_SIMILARITY,
    }
    logger.info(
        "Calibrated VECTOR_MIN_SIMILARITY: %.4f → %.4f (z=%.1f of %d pairs, mean=%.4f, std=%.4f)",
        old_threshold,
        config.VECTOR_MIN_SIMILARITY,
        z,
        num_pairs,
        sim_mean,
        sim_std,
    )
    return result


async def do_delete_episode(episode_id: int, agent_id: str = "") -> dict:
    """Delete a single episode by ID (FTS5 triggers handle index cleanup)."""
    db = await get_db()
    if agent_id:
        cursor = await db.execute(
            "DELETE FROM episodes WHERE id = ? AND agent_id = ?",
            (episode_id, agent_id),
        )
    else:
        cursor = await db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    await db.commit()
    if cursor.rowcount == 0:
        return {"error": f"Episode {episode_id} not found or not owned by agent"}
    return {"ok": True, "deleted_id": episode_id}


async def do_export_memories(agent_id: str, output_path: str, include_embeddings: bool = False) -> dict:
    """Export memories, episodes, and profiles to a JSONL file."""
    db = await get_db()

    agent_filter = " WHERE agent_id = ?" if agent_id else ""
    agent_params: tuple = (agent_id,) if agent_id else ()

    mem_count = (await db.execute_fetchall(f"SELECT COUNT(*) FROM memories{agent_filter}", agent_params))[0][0]
    ep_count = (await db.execute_fetchall(f"SELECT COUNT(*) FROM episodes{agent_filter}", agent_params))[0][0]
    prof_count = (await db.execute_fetchall(f"SELECT COUNT(*) FROM profiles{agent_filter}", agent_params))[0][0]

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    exported_memories = 0
    exported_episodes = 0
    exported_profiles = 0

    with open(output_path, "w", encoding="utf-8") as f:
        header = {
            "_type": "header",
            "version": "cpersona-export/1.0",
            "agent_id": agent_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "memory_count": mem_count,
            "episode_count": ep_count,
            "has_profile": prof_count > 0,
        }
        f.write(json.dumps(header, ensure_ascii=False) + "\n")

        rows = await db.execute_fetchall(
            "SELECT id, agent_id, msg_id, content, source, timestamp, metadata, embedding, created_at"
            f" FROM memories{agent_filter} ORDER BY id",
            agent_params,
        )
        for row in rows:
            record: dict = {
                "_type": "memory",
                "id": row[0],
                "agent_id": row[1],
                "msg_id": row[2],
                "content": row[3],
                "source": _try_parse_json(row[4]) if row[4] else {},
                "timestamp": row[5],
                "metadata": _try_parse_json(row[6]) if row[6] else {},
                "created_at": row[8],
            }
            if include_embeddings and row[7]:
                record["embedding_b64"] = base64.b64encode(row[7]).decode("ascii")
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported_memories += 1

        rows = await db.execute_fetchall(
            "SELECT id, agent_id, summary, keywords, start_time, end_time, embedding, created_at, resolved"
            f" FROM episodes{agent_filter} ORDER BY id",
            agent_params,
        )
        for row in rows:
            record = {
                "_type": "episode",
                "id": row[0],
                "agent_id": row[1],
                "summary": row[2],
                "keywords": row[3],
                "start_time": row[4],
                "end_time": row[5],
                "created_at": row[7],
                "resolved": bool(row[8]) if row[8] else False,
            }
            if include_embeddings and row[6]:
                record["embedding_b64"] = base64.b64encode(row[6]).decode("ascii")
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported_episodes += 1

        rows = await db.execute_fetchall(
            f"SELECT agent_id, user_id, content, updated_at FROM profiles{agent_filter} ORDER BY agent_id",
            agent_params,
        )
        for row in rows:
            record = {
                "_type": "profile",
                "agent_id": row[0],
                "user_id": row[1],
                "content": row[2],
                "updated_at": row[3],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported_profiles += 1

    return {
        "ok": True,
        "path": output_path,
        "memories": exported_memories,
        "episodes": exported_episodes,
        "profiles": exported_profiles,
    }


async def do_import_memories(input_path: str, target_agent_id: str = "", dry_run: bool = False) -> dict:
    """Import memories, episodes, and profiles from a JSONL file."""
    if not os.path.exists(input_path):
        return {"error": f"File not found: {input_path}"}

    db = await get_db()

    imported_memories = 0
    skipped_memories = 0
    imported_episodes = 0
    profile_updated = False
    errors: list[str] = []

    with open(input_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"Line {line_num}: invalid JSON: {e}")
                continue

            rtype = record.get("_type", "")

            if rtype == "header":
                continue

            elif rtype == "memory":
                aid = target_agent_id or record.get("agent_id", "")
                if not aid:
                    errors.append(f"Line {line_num}: memory missing agent_id")
                    continue

                content = record.get("content", "")
                if not content:
                    skipped_memories += 1
                    continue

                msg_id = record.get("msg_id", "")

                if msg_id:
                    existing = await db.execute_fetchall(
                        "SELECT id FROM memories WHERE agent_id = ? AND msg_id = ? LIMIT 1",
                        (aid, msg_id),
                    )
                    if existing:
                        skipped_memories += 1
                        continue

                if not dry_run:
                    source = json.dumps(record.get("source", {}))
                    timestamp = record.get("timestamp", "")
                    metadata = json.dumps(record.get("metadata", {}))
                    await db.execute(
                        "INSERT INTO memories (agent_id, msg_id, content, source, timestamp, metadata)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (aid, msg_id, content, source, timestamp, metadata),
                    )
                imported_memories += 1

            elif rtype == "episode":
                aid = target_agent_id or record.get("agent_id", "")
                if not aid:
                    errors.append(f"Line {line_num}: episode missing agent_id")
                    continue

                summary = record.get("summary", "")
                if not summary:
                    continue

                if not dry_run:
                    keywords = record.get("keywords", "")
                    start_time = record.get("start_time")
                    end_time = record.get("end_time")
                    resolved = 1 if record.get("resolved") else 0
                    await db.execute(
                        "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, resolved)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (aid, summary, keywords, start_time, end_time, resolved),
                    )
                imported_episodes += 1

            elif rtype == "profile":
                aid = target_agent_id or record.get("agent_id", "")
                if not aid:
                    errors.append(f"Line {line_num}: profile missing agent_id")
                    continue

                content = record.get("content", "")
                if not content:
                    continue

                if not dry_run:
                    user_id = record.get("user_id", "")
                    await db.execute(
                        "INSERT INTO profiles (agent_id, user_id, content, updated_at)"
                        " VALUES (?, ?, ?, datetime('now'))"
                        " ON CONFLICT(agent_id, user_id) DO UPDATE SET"
                        "   content = excluded.content,"
                        "   updated_at = excluded.updated_at",
                        (aid, user_id, content),
                    )
                profile_updated = True

            else:
                if rtype:
                    errors.append(f"Line {line_num}: unknown type '{rtype}'")

    if not dry_run:
        await db.commit()

    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "imported_memories": imported_memories,
        "skipped_memories": skipped_memories,
        "imported_episodes": imported_episodes,
        "profile_updated": profile_updated,
    }
    if errors:
        result["errors"] = errors
    return result


async def do_merge_memories(
    source_agent_id: str,
    target_agent_id: str,
    strategy: str = "skip",
    mode: str = "copy",
    dry_run: bool = False,
) -> dict:
    """Merge memories, episodes, and profiles from one agent into another."""
    if not source_agent_id:
        return {"error": "source_agent_id is required"}
    if not target_agent_id:
        return {"error": "target_agent_id is required"}
    if source_agent_id == target_agent_id:
        return {"error": "source_agent_id and target_agent_id must differ"}
    if strategy != "skip":
        return {"error": f"Unsupported strategy '{strategy}'. Currently supported: 'skip'"}
    if mode not in ("copy", "move"):
        return {"error": f"Invalid mode '{mode}'. Supported: 'copy', 'move'"}

    db = await get_db()

    merged_memories = 0
    skipped_memories = 0
    merged_episodes = 0
    skipped_episodes = 0
    profile_copied = False
    skipped_profile = False

    rows = await db.execute_fetchall(
        "SELECT msg_id, content, source, timestamp, metadata, channel FROM memories WHERE agent_id = ?",
        (source_agent_id,),
    )
    for msg_id, content, source, timestamp, metadata, channel in rows:
        if not content:
            continue
        if msg_id:
            existing = await db.execute_fetchall(
                "SELECT id FROM memories WHERE agent_id = ? AND msg_id = ? LIMIT 1",
                (target_agent_id, msg_id),
            )
            if existing:
                skipped_memories += 1
                continue
        if not dry_run:
            await db.execute(
                "INSERT INTO memories (agent_id, msg_id, content, source, timestamp, metadata, channel)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (target_agent_id, msg_id, content, source, timestamp, metadata, channel),
            )
        merged_memories += 1

    rows = await db.execute_fetchall(
        "SELECT summary, keywords, start_time, end_time, resolved FROM episodes WHERE agent_id = ?",
        (source_agent_id,),
    )
    for summary, keywords, start_time, end_time, resolved in rows:
        if not summary:
            continue
        existing = await db.execute_fetchall(
            "SELECT id FROM episodes WHERE agent_id = ? AND summary = ? LIMIT 1",
            (target_agent_id, summary),
        )
        if existing:
            skipped_episodes += 1
            continue
        if not dry_run:
            await db.execute(
                "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, resolved)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (target_agent_id, summary, keywords, start_time, end_time, resolved),
            )
        merged_episodes += 1

    rows = await db.execute_fetchall(
        "SELECT user_id, content FROM profiles WHERE agent_id = ?",
        (source_agent_id,),
    )
    for user_id, content in rows:
        if not content:
            continue
        existing = await db.execute_fetchall(
            "SELECT id FROM profiles WHERE agent_id = ? AND user_id = ? LIMIT 1",
            (target_agent_id, user_id),
        )
        if existing:
            skipped_profile = True
            continue
        if not dry_run:
            await db.execute(
                "INSERT INTO profiles (agent_id, user_id, content, updated_at) VALUES (?, ?, ?, datetime('now'))",
                (target_agent_id, user_id, content),
            )
        profile_copied = True

    if not dry_run:
        await db.commit()

    move_result = None
    if mode == "move" and not dry_run:
        move_result = await do_delete_agent_data(source_agent_id)

    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "source_agent_id": source_agent_id,
        "target_agent_id": target_agent_id,
        "strategy": strategy,
        "mode": mode,
        "merged_memories": merged_memories,
        "skipped_memories": skipped_memories,
        "merged_episodes": merged_episodes,
        "skipped_episodes": skipped_episodes,
        "profile_copied": profile_copied,
        "skipped_profile": skipped_profile,
    }
    if move_result:
        result["source_deleted"] = move_result

    logger.info(
        "Merge %s → %s (%s, %s): %d memories (+%d skipped), %d episodes (+%d skipped), profile=%s%s",
        source_agent_id,
        target_agent_id,
        strategy,
        mode,
        merged_memories,
        skipped_memories,
        merged_episodes,
        skipped_episodes,
        "copied" if profile_copied else ("skipped" if skipped_profile else "none"),
        " [DRY RUN]" if dry_run else "",
    )
    return result


async def do_get_queue_status() -> dict:
    """Get the status of the background task queue."""
    if tasks._task_queue and TASK_QUEUE_ENABLED:
        return await tasks._task_queue.get_status()
    return {"enabled": False, "pending": 0}
