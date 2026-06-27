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

from _vendored_mcp_common import no_persist
from _vendored_mcp_common.isolation import gamma_clause

import config
import tasks
import vector
from config import (
    CALIBRATE_FLOOR,
    CALIBRATE_METHOD,
    CALIBRATE_PERCENTILE,
    CALIBRATE_SAMPLE_SIZE,
    CALIBRATE_TEMPORAL_WINDOW_MIN,
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
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "profiles_updated": 0}, "update_profile")
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


async def do_list_memories(agent_id: str, limit: int, project_id: str | None = None) -> dict:
    """List recent memories for dashboard display.

    project_id (v2.4.17): γ filter — None = no filter, '' = global pool only,
    'X' = bucket 'X' ∪ global pool.
    """
    db = await get_db()
    clauses: list[str] = []
    params: list = []
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    frag, p = gamma_clause("project_id", project_id)
    if frag:
        clauses.append(frag)
        params.extend(p)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.execute_fetchall(
        f"SELECT id, agent_id, project_id, msg_id, content, source, timestamp, created_at, locked "
        f"FROM memories {where} ORDER BY created_at DESC LIMIT ?",
        (*params, _clamp_limit(limit, 500)),
    )
    memories = []
    for row in rows:
        source = {}
        try:
            source = json.loads(row[5]) if row[5] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        memories.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "project_id": row[2],
                "content": row[4],
                "source": source,
                "timestamp": row[6],
                "created_at": row[7],
                "locked": bool(row[8]),
            }
        )
    return {"memories": memories, "count": len(memories)}


async def do_list_episodes(agent_id: str, limit: int, project_id: str | None = None) -> dict:
    """List archived episodes for dashboard display. Same γ semantics as do_list_memories."""
    db = await get_db()
    clauses: list[str] = []
    params: list = []
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    frag, p = gamma_clause("project_id", project_id)
    if frag:
        clauses.append(frag)
        params.extend(p)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.execute_fetchall(
        f"SELECT id, agent_id, project_id, summary, keywords, start_time, end_time, created_at "
        f"FROM episodes {where} ORDER BY created_at DESC LIMIT ?",
        (*params, _clamp_limit(limit, 200)),
    )
    episodes = []
    for row in rows:
        episodes.append(
            {
                "id": row[0],
                "agent_id": row[1],
                "project_id": row[2],
                "summary": row[3],
                "keywords": row[4],
                "start_time": row[5],
                "end_time": row[6],
                "created_at": row[7],
            }
        )
    return {"episodes": episodes, "count": len(episodes)}


async def do_delete_memory(memory_id: int, agent_id: str = "") -> dict:
    """Delete a single memory by ID.

    When agent_id is provided (non-empty), enforces ownership.
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "deleted_id": memory_id}, "delete_memory")
    db = await get_db()
    # aiosqlite 0.22 has execute_fetchall but no execute_fetchone — using the
    # former avoids a silent AttributeError that previously broke every delete.
    rows = await db.execute_fetchall("SELECT locked FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return {"error": f"Memory {memory_id} not found"}
    if rows[0][0]:
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
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "updated_id": memory_id}, "update_memory")
    if not content or not content.strip():
        return {"error": "Content cannot be empty"}

    db = await get_db()
    rows = await db.execute_fetchall("SELECT locked, agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return {"error": f"Memory {memory_id} not found"}
    row = rows[0]
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
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "locked_id": memory_id}, "lock_memory")
    db = await get_db()
    rows = await db.execute_fetchall("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return {"error": f"Memory {memory_id} not found"}
    if agent_id and rows[0][0] != agent_id:
        return {"error": f"Memory {memory_id} not owned by agent {agent_id}"}

    await db.execute("UPDATE memories SET locked = 1 WHERE id = ?", (memory_id,))
    await db.commit()
    return {"ok": True, "locked_id": memory_id}


async def do_unlock_memory(memory_id: int, agent_id: str = "") -> dict:
    """Unlock a memory to allow deletion and editing."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "unlocked_id": memory_id}, "unlock_memory")
    db = await get_db()
    rows = await db.execute_fetchall("SELECT agent_id FROM memories WHERE id = ?", (memory_id,))
    if not rows:
        return {"error": f"Memory {memory_id} not found"}
    if agent_id and rows[0][0] != agent_id:
        return {"error": f"Memory {memory_id} not owned by agent {agent_id}"}

    await db.execute("UPDATE memories SET locked = 0 WHERE id = ?", (memory_id,))
    await db.commit()
    return {"ok": True, "unlocked_id": memory_id}


async def do_delete_agent_data(agent_id: str) -> dict:
    """Delete ALL data for a specific agent (memories, profiles, episodes)."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {
                "ok": True,
                "agent_id": agent_id,
                "deleted_memories": 0,
                "deleted_profiles": 0,
                "deleted_episodes": 0,
            },
            "delete_agent_data",
        )
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


def _separation_threshold(null_sims, pos_sims, floor: float, beta: float = 1.0) -> tuple:
    """Two-population threshold: the point that best separates null from positives.

    Sweeps candidate thresholds and returns the one maximizing the weighted Youden
    objective ``sensitivity + beta*specificity`` (``TPR + beta*(1 - FPR)``) where
    positives are a label-free proxy for related pairs (e.g. same-session similarity
    or, for the post-fusion gate, fused scores of temporally-adjacent rows) and the
    null is the random-pair / unrelated-row distribution. Unlike the percentile
    method, the operating point is derived from the corpus's actual separability
    rather than a fixed quantile.

    ``beta`` is the precision point — knob 3 (Goal #132). ``beta == 1`` reproduces the
    balanced Youden's J point (``argmax TPR - FPR``); ``beta > 1`` favours specificity
    (strict — fewer contaminants, more misses); ``beta < 1`` favours sensitivity
    (lenient — fewer misses, more contaminants). The curve is calibrated from data;
    beta is the single policy choice of where on it to sit.

    Returns ``(threshold, youden_j)`` where ``youden_j`` is the true ``TPR - FPR`` at
    the chosen point (for observability), independent of ``beta``.
    """
    import numpy as np

    null = np.asarray(null_sims, dtype=np.float64)
    pos = np.asarray(pos_sims, dtype=np.float64)
    lo = min(float(null.min()), float(pos.min()))
    hi = max(float(null.max()), float(pos.max()))
    if hi <= lo:
        return float(max(lo, floor)), 0.0
    candidates = np.linspace(lo, hi, 256)
    tpr = (pos[None, :] >= candidates[:, None]).mean(axis=1)
    fpr = (null[None, :] >= candidates[:, None]).mean(axis=1)
    objective = tpr + beta * (1.0 - fpr)
    best = int(np.argmax(objective))
    return float(max(candidates[best], floor)), float(tpr[best] - fpr[best])


def _parse_ts_seconds(ts):
    """Parse an ISO-8601 timestamp to epoch seconds, or None when unparseable."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _adjacency_sims_core(times_seconds, vecs, window_sec: float):
    """Cosine similarities of memories stored within ``window_sec`` of each other.

    Memories sorted by time; consecutive pairs whose gap is within the window are a
    representative (non-extreme) proxy for related pairs — same-session content. Unlike
    the nearest-neighbour max, this samples the body of the related distribution rather
    than its extreme tail, which is what makes the two-population operating point useful.
    """
    import numpy as np

    t = np.asarray(times_seconds, dtype=np.float64)
    v = np.asarray(vecs, dtype=np.float64)
    if len(t) < 2:
        return np.array([])
    order = np.argsort(t)
    t, v = t[order], v[order]
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vn = v / norms
    mask = np.diff(t) <= window_sec
    if not mask.any():
        return np.array([])
    return np.sum(vn[:-1][mask] * vn[1:][mask], axis=1)


async def _temporal_adjacency_sims(db, agent_id: str, limit: int, window_min: float):
    """Fetch (timestamp, embedding) ordered by time and build same-session pair sims."""
    import numpy as np

    if agent_id:
        rows = await db.execute_fetchall(
            "SELECT timestamp, embedding FROM memories WHERE agent_id = ? AND embedding IS NOT NULL "
            "AND timestamp IS NOT NULL ORDER BY timestamp DESC LIMIT ?",
            (agent_id, limit),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT timestamp, embedding FROM memories WHERE embedding IS NOT NULL "
            "AND timestamp IS NOT NULL ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
    times, vecs = [], []
    for ts, blob in rows:
        sec = _parse_ts_seconds(ts)
        if sec is None:
            continue
        times.append(sec)
        vecs.append(np.frombuffer(blob, dtype=np.float32))
    if len(times) < 2:
        return np.array([])
    return _adjacency_sims_core(times, np.array(vecs), window_min * 60.0)


def _threshold_from_sims(
    pairwise_sims,
    *,
    method: str,
    z_factor: float,
    percentile: float,
    floor: float,
    pos_sims=None,
) -> dict:
    """Derive a vector-similarity threshold from a null (random-pair) distribution.

    The threshold is placed ABOVE the mean of the random-pair similarities so that
    unrelated pairs are rejected:

    - ``percentile``: the given quantile of the null distribution. Distribution-free
      and robust to the narrow, high-mean cosine geometry of anisotropic models such
      as bge-m3 (mean random-pair similarity ~0.51, small spread).
    - ``zscore``: ``mean + z*std`` — rejects pairs within +z standard deviations of
      the random baseline.
    - ``separation``: the operating point that best separates the null from a
      label-free positive proxy (``pos_sims``, the per-memory nearest-neighbour
      similarity), via Youden's J. Removes the fixed-quantile choice — the point is
      learned from the corpus's own separability. Requires ``pos_sims``.

    The pre-2.4.24 formula used ``mean - z*std``, which placed the floor BELOW the
    null mean and admitted the majority of unrelated pairs (topic-drift contamination).

    Returns the threshold plus distribution statistics for observability, including
    ``null_admit_rate`` (fraction of random pairs admitted — a lower value is stricter).
    """
    import numpy as np

    sims = np.asarray(pairwise_sims, dtype=np.float64)
    sim_mean = float(np.mean(sims))
    sim_std = float(np.std(sims))
    sim_median = float(np.median(sims))

    youden_j = None
    if method == "zscore":
        raw = sim_mean + z_factor * sim_std
    elif method == "separation":
        if pos_sims is None:
            raise ValueError("separation method requires pos_sims")
        raw, youden_j = _separation_threshold(sims, pos_sims, floor)
    else:  # "percentile" (default)
        raw = float(np.quantile(sims, percentile))

    threshold = round(max(raw, floor), 4)
    result = {
        "threshold": threshold,
        "mean": round(sim_mean, 4),
        "std": round(sim_std, 4),
        "median": round(sim_median, 4),
        "p95": round(float(np.quantile(sims, 0.95)), 4),
        "null_admit_rate": round(float(np.mean(sims >= threshold)), 4),
    }
    if pos_sims is not None:
        pos = np.asarray(pos_sims, dtype=np.float64)
        result["pos_mean"] = round(float(np.mean(pos)), 4)
        result["pos_admit_rate"] = round(float(np.mean(pos >= threshold)), 4)
    if youden_j is not None:
        result["youden_j"] = round(youden_j, 4)
    return result


def _calibration_sidecar_path() -> str:
    """Path of the JSON sidecar that persists calibration state next to the DB."""
    return config.DB_PATH + ".calibration.json"


def _save_calibration_state(
    embedding_dim: int,
    embedding_model: str,
    global_threshold: float | None,
    agent_thresholds: dict,
    global_fused_gate: float | None = None,
    agent_fused_gates: dict | None = None,
    fused_gate_signal: str | None = None,
    agent_betas: dict | None = None,
) -> None:
    """Persist calibrated thresholds + the embedding fingerprint to the sidecar.

    Persistence lets thresholds survive a restart without recomputation, and lets the
    startup guard detect an embedding-model (dimension) change. The post-fusion gate
    (v2.4.26) is persisted alongside the vector threshold and keyed by the same
    embedding fingerprint, plus the RECALL_MODE it was calibrated for. Per-agent precision
    overrides (knob 3, v2.4.29) are persisted next to the gates they produced so a restore
    keeps each agent's gate and the beta it sits on in sync.
    """
    payload = {
        "embedding_dim": embedding_dim,
        "embedding_model": embedding_model,
        "global_threshold": global_threshold,
        "agent_thresholds": agent_thresholds,
        "global_fused_gate": global_fused_gate,
        "agent_fused_gates": agent_fused_gates or {},
        "fused_gate_signal": fused_gate_signal,
        "agent_betas": agent_betas or {},
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(_calibration_sidecar_path(), "w") as fh:
            json.dump(payload, fh)
    except OSError as exc:
        logger.warning("Could not persist calibration sidecar: %s", exc)


def _load_calibration_state() -> dict | None:
    """Load the calibration sidecar, or None when absent/unreadable."""
    try:
        with open(_calibration_sidecar_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


async def _corpus_embedding_dim() -> int | None:
    """Return the float32 dimension of one stored embedding, or None when empty."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE embedding IS NOT NULL LIMIT 1"
    )
    if not rows or rows[0][0] is None:
        return None
    return len(rows[0][0]) // 4  # 4 bytes per float32


async def _calibrate_fused_gate(
    db,
    agent_id: str,
    sample_queries: int,
    window_min: float,
    beta: float,
    floor: float,
) -> dict | None:
    """Simulate-query calibration of the recall quality gate (Goal #132, v2.4.27).

    The quality gate keys on a per-row score that — unlike pairwise cosine similarity —
    only exists relative to a query: the confidence score when CONFIDENCE_ENABLED, else
    the fused score (``_rsf_score`` / ``_rrf_score``). The null and positive distributions
    are therefore produced by *simulation*: sample stored memories as pseudo-queries, run
    the live recall pipeline AND the same post-recall scoring do_recall applies
    (``_apply_recall_scoring`` — episode penalty + confidence), take each row's gate score
    via ``_gate_score``, and label it against the pseudo-query by temporal adjacency —
    rows stored within ``window_min`` (same-session ≈ related) are the positive proxy, the
    rest the null. Separation over the two populations gives the operating point. Only the
    rows whose gate signal matches the active one contribute, so the curve is built on the
    exact value the runtime gate compares. Cost is at most ``sample_queries`` recalls per
    calibration (an offline / startup event), never per user recall.

    Returns a stats dict, or None when there is no fusion/confidence gate to calibrate
    (cascade + confidence-off), the embedding client is absent, or too few samples were
    collected (the caller then keeps the heuristic gate). The calibration applies the
    same ``_apply_recall_scoring`` do_recall runs (episode penalty + confidence), so the
    operating point matches the runtime gate score rather than the raw fused score.
    """
    import numpy as np

    from memory_handlers import (
        _apply_recall_scoring,
        _gate_score,
        _recall_cascade,
        _recall_rrf,
        _recall_rsf,
    )

    mode = config.RECALL_MODE
    if mode == "rsf":
        recall_fn = _recall_rsf
    elif mode == "rrf":
        recall_fn = _recall_rrf
    else:
        recall_fn = _recall_cascade
    # The gate keys on confidence when enabled (it takes precedence in any mode), else on
    # the fused score. Cascade with confidence off has no fusion gate — the cosine vector
    # threshold owns precision there.
    if config.CONFIDENCE_ENABLED:
        signal = "confidence"
    elif mode in ("rsf", "rrf"):
        signal = mode
    else:
        return None
    if vector._embedding_client is None:
        return None

    rows = await db.execute_fetchall(
        "SELECT id, content, timestamp FROM memories "
        "WHERE agent_id = ? AND embedding IS NOT NULL AND content IS NOT NULL "
        "AND timestamp IS NOT NULL ORDER BY RANDOM() LIMIT ?",
        (agent_id, sample_queries),
    )
    window_sec = window_min * 60.0
    null_scores: list[float] = []
    pos_scores: list[float] = []
    queries_run = 0
    for qid, qcontent, qts in rows:
        if not qcontent or not qcontent.strip():
            continue
        q_sec = _parse_ts_seconds(qts)
        if q_sec is None:
            continue
        results = await recall_fn(db, agent_id, qcontent, 20, False)
        # Apply the same penalty + confidence scoring do_recall runs, so _gate_score
        # returns the exact value the runtime gate compares (confidence when enabled).
        results, _, _ = await _apply_recall_scoring(db, agent_id, results, False)
        queries_run += 1
        for r in results:
            rid = r.get("id")
            if not isinstance(rid, int) or rid <= 0 or rid == qid:
                continue  # skip the pseudo-query itself and profiles (-1)
            score, row_signal = _gate_score(r)
            if score is None or row_signal != signal:
                continue  # only the active gate signal contributes to the curve
            r_sec = _parse_ts_seconds(r.get("timestamp"))
            if r_sec is not None and abs(r_sec - q_sec) <= window_sec:
                pos_scores.append(float(score))
            else:
                null_scores.append(float(score))

    if len(null_scores) < 10 or len(pos_scores) < 5:
        return None  # insufficient separation data — keep the pool-size heuristic

    threshold, youden_j = _separation_threshold(null_scores, pos_scores, floor, beta)
    null = np.asarray(null_scores, dtype=np.float64)
    pos = np.asarray(pos_scores, dtype=np.float64)
    return {
        "threshold": round(threshold, 4),
        "signal": signal,
        "beta": beta,
        "youden_j": round(youden_j, 4),
        "queries_run": queries_run,
        "n_null": len(null_scores),
        "n_pos": len(pos_scores),
        "null_admit_rate": round(float((null >= threshold).mean()), 4),
        "pos_admit_rate": round(float((pos >= threshold).mean()), 4),
        "null_mean": round(float(null.mean()), 4),
        "pos_mean": round(float(pos.mean()), 4),
    }


async def do_calibrate_threshold(
    agent_id: str,
    sample_size: int = 0,
    z_factor: float = 0,
    method: str = "",
    percentile: float = 0,
) -> dict:
    """Auto-calibrate the vector-similarity threshold from the embedding distribution.

    Uses the null distribution of pairwise cosine similarities (mostly unrelated
    pairs). When *agent_id* is provided, writes a per-agent override into
    ``vector._agent_thresholds``; when empty, calibrates the global
    ``config.VECTOR_MIN_SIMILARITY`` from the all-agents corpus (v2.4.15).

    v2.4.24: the threshold is placed ABOVE the null mean (see ``_threshold_from_sims``);
    ``method`` defaults to ``percentile``. The result is persisted to a sidecar keyed by
    embedding dimension so a later embedding-model swap triggers recalibration at startup.
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {"ok": True, "old_threshold": None, "new_threshold": None, "sample_size": 0},
            "calibrate_threshold",
        )
    import numpy as np

    db = await get_db()
    sample_n = sample_size or CALIBRATE_SAMPLE_SIZE
    z = z_factor or CALIBRATE_Z_FACTOR
    cal_method = method or CALIBRATE_METHOD
    cal_percentile = percentile or CALIBRATE_PERCENTILE

    # Sample embeddings: per-agent when agent_id provided, all-agents when empty
    if agent_id:
        rows = await db.execute_fetchall(
            "SELECT embedding FROM memories WHERE agent_id = ? AND embedding IS NOT NULL ORDER BY RANDOM() LIMIT ?",
            (agent_id, sample_n),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT embedding FROM memories WHERE embedding IS NOT NULL ORDER BY RANDOM() LIMIT ?",
            (sample_n,),
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
    old_threshold = vector._get_vector_threshold(agent_id)

    # Positive proxy for the separation method (label-free). Preferred: temporal
    # adjacency (same-session memories ≈ related — a representative sample of the
    # related distribution). Fallback: nearest-neighbour max, used only when too few
    # temporally-adjacent pairs exist (it overestimates relatedness — extreme tail —
    # so the threshold trends high and recall suffers).
    pos_sims = None
    proxy_source = None
    if cal_method == "separation":
        pos_sims = await _temporal_adjacency_sims(
            db, agent_id, sample_n, CALIBRATE_TEMPORAL_WINDOW_MIN
        )
        proxy_source = "temporal"
        if pos_sims is None or len(pos_sims) < 10:
            nn = sim_matrix.copy()
            np.fill_diagonal(nn, -np.inf)
            pos_sims = nn.max(axis=1)
            proxy_source = "nn_fallback"

    stats = _threshold_from_sims(
        pairwise_sims,
        method=cal_method,
        z_factor=z,
        percentile=cal_percentile,
        floor=CALIBRATE_FLOOR,
        pos_sims=pos_sims,
    )
    new_threshold = stats["threshold"]
    embedding_dim = int(vecs.shape[1])

    # Apply: per-agent dict when agent_id provided, global fallback when empty
    if agent_id:
        vector._agent_thresholds[agent_id] = new_threshold
    else:
        config.VECTOR_MIN_SIMILARITY = new_threshold

    # Post-fusion quality-gate calibration (v2.4.26, Goal #132). Per-agent and
    # fusion-mode only: recall is per-agent, and the gate lives on the active mode's
    # fused-score scale. Calibrating the curve here makes precision driven by data in
    # every mode (cascade via the vector floor above, rsf/rrf via this gate) instead of
    # the pool-size heuristic _adaptive_min_score.
    fused_stats = None
    if agent_id and config.FUSED_GATE_ENABLED:
        # The simulate-query pass issues live fusion recalls; a flaky embedding backend
        # must not abort calibration and lose the vector threshold computed above (which
        # is persisted below). Degrade to the heuristic gate on any failure.
        try:
            fused_stats = await _calibrate_fused_gate(
                db,
                agent_id,
                config.FUSED_GATE_SAMPLE_QUERIES,
                CALIBRATE_TEMPORAL_WINDOW_MIN,
                vector._get_precision_beta(agent_id),
                CALIBRATE_FLOOR,
            )
        except Exception as exc:
            logger.warning(
                "Fused-gate calibration failed for [%s]; keeping the heuristic gate: %s",
                agent_id or "global",
                exc,
            )
            fused_stats = None
        if fused_stats is not None:
            vector._agent_fused_gates[agent_id] = fused_stats["threshold"]
            vector._fused_gate_signal = fused_stats["signal"]

    # Persist for restart survival + embedding-change detection (Tier 4).
    _save_calibration_state(
        embedding_dim,
        config.EMBEDDING_MODEL,
        config.VECTOR_MIN_SIMILARITY,
        dict(vector._agent_thresholds),
        global_fused_gate=vector._global_fused_gate,
        agent_fused_gates=dict(vector._agent_fused_gates),
        fused_gate_signal=vector._fused_gate_signal,
        agent_betas=dict(vector._agent_betas),
    )

    result = {
        "ok": True,
        "scope": "per_agent" if agent_id else "global",
        "agent_id": agent_id,
        "sampled_embeddings": n,
        "num_pairs": num_pairs,
        "method": cal_method,
        "z_factor": z,
        "percentile": cal_percentile,
        "embedding_dim": embedding_dim,
        "embedding_model": config.EMBEDDING_MODEL,
        "distribution": {
            "mean": stats["mean"],
            "std": stats["std"],
            "median": stats["median"],
            "p95": stats["p95"],
        },
        "null_admit_rate": stats["null_admit_rate"],
        "old_threshold": old_threshold,
        "new_threshold": new_threshold,
    }
    if proxy_source is not None:
        result["proxy_source"] = proxy_source
    if "youden_j" in stats:
        result["youden_j"] = stats["youden_j"]
    if "pos_admit_rate" in stats:
        result["pos_admit_rate"] = stats["pos_admit_rate"]
        result["pos_mean"] = stats["pos_mean"]
    if fused_stats is not None:
        result["fused_gate"] = fused_stats
    logger.info(
        "Calibrated threshold [%s]: %.4f -> %.4f (method=%s z=%.1f pct=%.2f of %d pairs, "
        "mean=%.4f std=%.4f admit=%.3f dim=%d)",
        agent_id or "global",
        old_threshold,
        new_threshold,
        cal_method,
        z,
        cal_percentile,
        num_pairs,
        stats["mean"],
        stats["std"],
        stats["null_admit_rate"],
        embedding_dim,
    )
    return result


async def do_set_recall_precision(agent_id: str, precision: str = "", beta: float = 0) -> dict:
    """Set an agent's recall precision (knob 3, v2.4.29, Goal #120) and recalibrate its gate.

    ``precision`` is one of ``strict`` / ``balanced`` / ``lenient``, mapped to a specificity
    weight (beta) of 2.0 / 1.0 / 0.5 in the gate separation objective
    (sensitivity + beta*specificity): higher beta sits the gate higher on the curve (fewer
    contaminants, more misses), lower beta lower (fewer misses, more contaminants). A raw
    ``beta`` > 0 overrides the named level. An empty ``precision`` with ``beta`` <= 0 clears
    the per-agent override, returning the agent to the global CPERSONA_RECALL_PRECISION
    default. The agent's post-fusion quality gate is recalibrated at the new beta
    immediately (no restart needed) and the (beta, gate) pair is persisted to the
    calibration sidecar. Unlike a recall argument, precision cannot be a per-call override:
    the gate threshold is precomputed on the separation curve at a fixed beta, so changing
    it requires recalibration, which this tool performs once rather than per recall.
    """
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {"ok": True, "agent_id": agent_id, "beta": None, "precision": None},
            "set_recall_precision",
        )
    if not agent_id:
        return {"ok": False, "error": "agent_id is required"}

    # Resolve the target beta. Raw beta wins; then the named level; then (empty + beta<=0)
    # is the clear-override signal.
    clear = False
    if beta and beta > 0:
        resolved_beta = float(beta)
        resolved_precision = precision.lower() or "custom"
    elif precision:
        level = precision.lower()
        if level not in config._PRECISION_BETA:
            return {
                "ok": False,
                "error": f"Unknown precision '{precision}'; expected strict / balanced / lenient",
            }
        resolved_beta = config._PRECISION_BETA[level]
        resolved_precision = level
    else:
        clear = True
        resolved_beta = config.FUSED_GATE_BETA
        resolved_precision = "default"

    # Apply the override, then recalibrate so the change takes effect now (this also
    # persists the sidecar, including agent_betas, via do_calibrate_threshold). Keep it
    # atomic: if calibration cannot run (e.g. an agent with too few embeddings returns
    # ok=False before the sidecar is saved), roll the in-memory override back so it never
    # diverges from the unpersisted sidecar.
    had_override = agent_id in vector._agent_betas
    prev_beta = vector._agent_betas.get(agent_id)
    if clear:
        vector._agent_betas.pop(agent_id, None)
    else:
        vector._agent_betas[agent_id] = resolved_beta

    cal = await do_calibrate_threshold(agent_id=agent_id)
    if not cal.get("ok"):
        if had_override:
            vector._agent_betas[agent_id] = prev_beta
        else:
            vector._agent_betas.pop(agent_id, None)
        return {
            "ok": False,
            "agent_id": agent_id,
            "precision": resolved_precision,
            "beta": resolved_beta,
            "cleared": clear,
            "error": cal.get("error", "calibration failed"),
        }
    return {
        "ok": True,
        "agent_id": agent_id,
        "precision": resolved_precision,
        "beta": resolved_beta,
        "cleared": clear,
        "fused_gate": cal.get("fused_gate"),
        "calibrate": {k: cal.get(k) for k in ("ok", "scope", "new_threshold", "error") if k in cal},
    }


def _precision_label(beta: float) -> str:
    """Invert a specificity weight (beta) back to its named precision level.

    The named levels store exact betas (strict=2.0 / balanced=1.0 / lenient=0.5), so an
    exact match is reliable; a raw beta set via the override returns 'custom'.
    """
    for name, value in config._PRECISION_BETA.items():
        if value == beta:
            return name
    return "custom"


async def do_get_recall_precision(agent_id: str) -> dict:
    """Read an agent's effective recall precision (knob 3, read-back for set_recall_precision).

    Returns the resolved specificity weight (``beta``) and its named ``precision`` level,
    flagging whether it comes from a per-agent override (``overridden``) or the global
    CPERSONA_RECALL_PRECISION default. This is the read companion to set_recall_precision:
    a client can load the current value, let the user edit it, and write it back, instead
    of the pill being write-only. Read-only — it never recalibrates and never persists, so
    it is not gated by no-persist pause (like recall).
    """
    if not agent_id:
        return {"ok": False, "error": "agent_id is required"}

    overridden = agent_id in vector._agent_betas
    beta = vector._get_precision_beta(agent_id)
    global_beta = config.FUSED_GATE_BETA
    return {
        "ok": True,
        "agent_id": agent_id,
        "precision": _precision_label(beta),
        "beta": beta,
        "overridden": overridden,
        "global_precision": _precision_label(global_beta),
        "global_beta": global_beta,
    }


def _restore_calibration_state(state: dict) -> None:
    """Load persisted thresholds from a sidecar payload into live config + dict.

    Backward compatible: a pre-v2.4.26 sidecar without the fused-gate keys restores the
    vector threshold only, leaving the fused gate uncalibrated (heuristic fallback).
    """
    global_threshold = state.get("global_threshold")
    if global_threshold is not None:
        config.VECTOR_MIN_SIMILARITY = global_threshold
    vector._agent_thresholds.update(state.get("agent_thresholds") or {})
    global_fused_gate = state.get("global_fused_gate")
    if global_fused_gate is not None:
        vector._global_fused_gate = global_fused_gate
    vector._agent_fused_gates.update(state.get("agent_fused_gates") or {})
    fused_gate_signal = state.get("fused_gate_signal")
    if fused_gate_signal is not None:
        vector._fused_gate_signal = fused_gate_signal
    # Per-agent precision overrides (knob 3, v2.4.29). Backward compatible: a pre-v2.4.29
    # sidecar has no key, leaving every agent on the global beta default.
    vector._agent_betas.update(state.get("agent_betas") or {})


async def ensure_calibrated_on_startup(auto_calibrate: bool, on_model_change: bool) -> dict:
    """Startup guard for the vector-similarity threshold (Tier 4, v2.4.24).

    Restores persisted thresholds when the embedding dimension is unchanged, and
    (re)calibrates on first run or on an embedding-dimension change (e.g. a silent
    jina 768d -> bge-m3 1024d swap), even when ``AUTO_CALIBRATE`` is off. A stale
    threshold calibrated for a previous embedding model is a known cause of recall
    contamination. Returns a small status dict for logging.
    """
    state = _load_calibration_state()
    live_dim = await _corpus_embedding_dim()
    dim_changed = (
        state is not None and live_dim is not None and state.get("embedding_dim") != live_dim
    )

    restored = False
    if state and not dim_changed and not auto_calibrate:
        _restore_calibration_state(state)
        restored = True
        # A pre-v2.4.27 sidecar (or one never gate-calibrated) restores the vector
        # threshold but carries no recall gate. With FUSED_GATE_ENABLED, fall through to
        # calibrate the gate so Goal #132 actually bites in production; otherwise the
        # restore is sufficient. (This is what activates the gate on a v2.4.25 -> v2.4.27
        # upgrade where the embedding dimension is unchanged, so no dim-change recalibrate
        # would otherwise fire.)
        if not (config.FUSED_GATE_ENABLED and vector._fused_gate_signal is None):
            return {"action": "restored", "embedding_dim": state.get("embedding_dim")}
        logger.info("Calibration sidecar has no recall-gate signal; calibrating the gate.")

    if not restored and not (auto_calibrate or (on_model_change and (state is None or dim_changed))):
        return {"action": "noop"}

    if dim_changed:
        logger.warning(
            "Embedding dimension changed (%s -> %s); recalibrating vector threshold. "
            "A stale threshold from a previous embedding model causes recall contamination.",
            state.get("embedding_dim"),
            live_dim,
        )

    db = await get_db()
    global_result = await do_calibrate_threshold(agent_id="")
    agents = []
    if global_result.get("ok"):
        agent_rows = await db.execute_fetchall(
            "SELECT DISTINCT agent_id FROM memories WHERE embedding IS NOT NULL"
        )
        for (aid,) in agent_rows:
            r = await do_calibrate_threshold(agent_id=aid)
            if r.get("ok"):
                agents.append(aid)
    return {
        "action": (
            "gate_calibrated" if restored
            else "recalibrated" if dim_changed
            else "auto" if auto_calibrate
            else "initial"
        ),
        "dim_changed": dim_changed,
        "global_ok": bool(global_result.get("ok")),
        "agents": agents,
    }


async def do_delete_episode(episode_id: int, agent_id: str = "") -> dict:
    """Delete a single episode by ID (FTS5 triggers handle index cleanup)."""
    if no_persist.is_paused():
        return no_persist.make_skipped_response({"ok": True, "deleted_id": episode_id}, "delete_episode")
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
    # Snapshot once: a TTL boundary mid-loop must not leave a half-written corpus.
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {
                "ok": True,
                "dry_run": dry_run,
                "imported_memories": 0,
                "skipped_memories": 0,
                "imported_episodes": 0,
                "profile_updated": False,
            },
            "import_memories",
        )
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
    # Snapshot once: a TTL boundary mid-loop must not leave a half-written corpus.
    if no_persist.is_paused():
        return no_persist.make_skipped_response(
            {
                "ok": True,
                "dry_run": dry_run,
                "merged_memories": 0,
                "skipped_memories": 0,
                "merged_episodes": 0,
                "skipped_episodes": 0,
                "profile_copied": False,
                "skipped_profile": False,
            },
            "merge_memories",
        )
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
