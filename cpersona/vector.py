"""Vector embedding client and similarity search for CPersona.

Holds the module-level `_embedding_client` singleton, set by `server.main()` at startup.
"""

import heapq
import logging

import aiosqlite
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona._vendored_mcp_common.isolation import gamma_clause

from cpersona import config
from cpersona import health
from cpersona.config import (
    MAX_MEMORIES,
    VECTOR_SEARCH_MODE,
)

logger = logging.getLogger(__name__)

# Short, dedicated timeout for the degraded-advisory health probe — do NOT inherit the
# embed client's 30s timeout, so a hung endpoint does not add it again to a failing recall.
PROBE_TIMEOUT_SECS = 3.0


_embedding_client: EmbeddingClient | None = None

# Per-agent vector-similarity threshold overrides (v2.4.15).
# Populated by do_calibrate_threshold / startup auto-calibration; agents with
# no calibration data fall back to the global config.VECTOR_MIN_SIMILARITY.
_agent_thresholds: dict[str, float] = {}

# Per-agent post-fusion quality-gate thresholds (v2.4.26, Goal #132). Calibrated by
# simulate-query separation in admin_handlers over the fused-score distribution.
# An absent agent falls back to the global gate; a None global falls back to the
# pool-size heuristic _adaptive_min_score in memory_handlers.
_agent_fused_gates: dict[str, float] = {}
_global_fused_gate: float | None = None
# The gate signal the fused gate was calibrated for (v2.4.27): "confidence" / "rsf" /
# "rrf" / "cosine" — the quality-gate branch the value lives on. _apply_quality_gate
# applies the gate only to the matching branch, so a gate from a different config (e.g.
# calibrated under confidence-on, now confidence-off) is simply never used.
_fused_gate_signal: str | None = None

# Per-agent precision weight beta (knob 3, v2.4.29, Goal #120). The specificity weight
# the agent's fused gate is calibrated at: strict=2.0 (fewer contaminants, more misses) /
# balanced=1.0 (Youden's J) / lenient=0.5 (fewer misses, more contaminants). Only agents
# with an explicit override (set_recall_precision) are stored here; an absent agent uses
# the global config.FUSED_GATE_BETA, so changing the env still moves un-configured agents
# on their next calibration. Persisted in the calibration sidecar next to the gate it
# produced — the gate threshold sits on the separation curve at this exact beta, so the
# two must be restored together or they desync.
_agent_betas: dict[str, float] = {}


def _get_vector_threshold(agent_id: str) -> float:
    """Return the per-agent threshold when available, otherwise the global default."""
    return _agent_thresholds.get(agent_id, config.VECTOR_MIN_SIMILARITY)


def _get_precision_beta(agent_id: str) -> float:
    """Return the per-agent precision weight (beta) when set, else the global default."""
    return _agent_betas.get(agent_id, config.FUSED_GATE_BETA)


def _get_fused_gate(agent_id: str) -> float | None:
    """Calibrated post-fusion gate for an agent, the global fallback, or None.

    None signals the caller to fall back to the pool-size heuristic. The companion
    ``_fused_gate_signal`` records which gate branch the value was calibrated for.
    """
    if agent_id in _agent_fused_gates:
        return _agent_fused_gates[agent_id]
    return _global_fused_gate


def _escape_like_prefix(s: str) -> str:
    """Escape SQL LIKE wildcards and append '%' for prefix-match semantics."""
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _probe_embedding_health() -> tuple[bool, str | None]:
    """Non-swallowing health POST to the embedding endpoint to capture the real error.

    ``EmbeddingClient.embed()`` swallows the transport error and returns ``None``, so when an
    embed fails we re-probe here to recover the actual failure string for the advisory's
    evidence slot (Route B). Returns ``(ok, evidence)``: ``(True, None)`` on a 2xx, else
    ``(False, "mode=<m> / POST <url> failed: <error>")``. Uses a short dedicated timeout so a
    hung endpoint does not add the full embed timeout again.
    """
    client = _embedding_client
    if client is None or not client._http_url or client._client is None:
        return False, "embedding client unavailable"
    try:
        resp = await client._client.post(
            client._http_url,
            json={"texts": ["health-probe"]},
            timeout=PROBE_TIMEOUT_SECS,
        )
        resp.raise_for_status()
        return True, None
    except Exception as e:
        return False, f"mode={client.mode} / POST {client._http_url} failed: {e}"


async def _search_vector(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_similarity: float | None = None,
    channel: str = "",
    project_id: str | None = None,
    source_id: str = "",
) -> list[dict]:
    """Search memories and episodes using vector cosine similarity.

    project_id (v2.4.17): γ filter applied to the row-fetch SQL after the
    cosine ranking. The remote vector namespace is still f'cpersona:{agent_id}'
    — top-K candidates are post-filtered by project, so a tightly-tagged query
    may receive fewer than `limit` results. Namespace partitioning is a
    follow-up (out of v2.4.17 scope).

    source_id (v2.4.20): optional prefix filter against
    ``json_extract(source, '$.id')`` applied to memory rows (not episodes).
    Used by Discord multi-user sessions to prevent cross-user contamination.
    """
    proj_frag, proj_params = gamma_clause("project_id", project_id)
    proj_extra = (" AND " + proj_frag) if proj_frag else ""

    src_like = _escape_like_prefix(source_id)
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params = (src_like,) if src_like else ()

    if VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url:
        try:
            base_url = _embedding_client._http_url.rsplit("/", 1)[0]
            resp = await _embedding_client._client.post(
                f"{base_url}/search",
                json={
                    "namespace": f"cpersona:{agent_id}",
                    "query": query,
                    "limit": limit,
                    "min_similarity": _get_vector_threshold(agent_id),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for hit in data.get("results", []):
                raw_id = hit["id"]
                score = hit["score"]
                if raw_id.startswith("mem:"):
                    mem_id = int(raw_id[4:])
                    if channel:
                        # ''=global (knob2 v2): a stored channel of '' is global
                        # and matches every channel-scoped recall, so old/global
                        # memories are never orphaned by per-channel filing.
                        row = await db.execute_fetchall(
                            f"SELECT msg_id, content, source, timestamp FROM memories "
                            f"WHERE id = ? AND (channel = ? OR channel = ''){proj_extra}{src_clause}",
                            (mem_id, channel, *proj_params, *src_params),
                        )
                    else:
                        row = await db.execute_fetchall(
                            f"SELECT msg_id, content, source, timestamp FROM memories WHERE id = ?{proj_extra}{src_clause}",
                            (mem_id, *proj_params, *src_params),
                        )
                    if row:
                        results.append(
                            {
                                "id": mem_id,
                                "_rid": ("mem", mem_id),
                                "_cosine": score,
                                "msg_id": row[0][0],
                                "content": row[0][1],
                                "source": row[0][2],
                                "timestamp": row[0][3],
                            }
                        )
                elif raw_id.startswith("ep:"):
                    # Episodes lack per-user source — skip when source_id is set.
                    if src_like:
                        continue
                    ep_id = int(raw_id[3:])
                    row = await db.execute_fetchall(
                        f"SELECT summary, start_time, resolved FROM episodes WHERE id = ?{proj_extra}",
                        (ep_id, *proj_params),
                    )
                    if row:
                        results.append(
                            {
                                "id": ep_id,
                                "_rid": ("ep", ep_id),
                                "_cosine": score,
                                "content": f"[Episode] {row[0][0]}",
                                "source": {"System": "episode"},
                                "timestamp": row[0][1] or "",
                                "_resolved": bool(row[0][2]),
                            }
                        )
            return results
        except Exception as e:
            logger.warning("Remote vector search failed, falling back to local: %s", e)

    import numpy as np

    embeddings = await _embedding_client.embed([query])
    if not embeddings or not embeddings[0]:
        # The client exists here (mode != "none"), so a falsy embed of the query text
        # itself is a genuine embed failure, not an empty-corpus no-match. Re-probe to
        # capture the real error for the degraded-advisory, unless already latched into
        # fault (bounds probe I/O to the promotion window; recovery is seen on success).
        if not health.is_faulted():
            ok, evidence = await _probe_embedding_health()
            if ok:
                health.observe_ok()
            else:
                health.observe_failure(evidence)
        return []
    health.observe_ok()  # embed succeeded — re-arm after any prior degradation
    query_vec = np.array(embeddings[0], dtype=np.float32)
    query_dim = len(query_vec)
    effective_min_sim = min_similarity if min_similarity is not None else _get_vector_threshold(agent_id)

    candidates: list[tuple[float, dict]] = []
    scan_limit = min(MAX_MEMORIES, max(limit * 10, 100))

    if channel:
        # ''=global (knob2 v2): stored channel '' matches every channel-scoped
        # recall (see the by-id path above).
        rows = await db.execute_fetchall(
            f"""SELECT id, msg_id, content, source, timestamp, embedding
               FROM memories
               WHERE agent_id = ? AND (channel = ? OR channel = '') AND embedding IS NOT NULL{proj_extra}{src_clause}
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, channel, *proj_params, *src_params, scan_limit),
        )
    else:
        rows = await db.execute_fetchall(
            f"""SELECT id, msg_id, content, source, timestamp, embedding
               FROM memories
               WHERE agent_id = ? AND embedding IS NOT NULL{proj_extra}{src_clause}
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, *proj_params, *src_params, scan_limit),
        )

    if rows:
        valid_rows = []
        blobs = []
        for row in rows:
            blob = row[5]
            if blob and len(blob) == query_dim * 4:
                valid_rows.append(row)
                blobs.append(blob)

        if valid_rows:
            mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), query_dim)
            sims = mat @ query_vec

            for i, sim_val in enumerate(sims):
                if sim_val >= effective_min_sim:
                    mem_id, msg_id, content, source, timestamp, _ = valid_rows[i]
                    sim = float(sim_val)
                    candidates.append(
                        (
                            sim,
                            {
                                "id": mem_id,
                                "_rid": ("mem", mem_id),
                                "_cosine": sim,
                                "msg_id": msg_id,
                                "content": content,
                                "source": source,
                                "timestamp": timestamp,
                            },
                        )
                    )

    # Episodes lack per-user source tagging — skip when source_id is set.
    ep_rows = (
        []
        if src_like
        else await db.execute_fetchall(
            f"""SELECT id, summary, start_time, embedding, resolved
               FROM episodes
               WHERE agent_id = ? AND embedding IS NOT NULL{proj_extra}
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, *proj_params, scan_limit),
        )
    )

    if ep_rows:
        valid_ep_rows = []
        ep_blobs = []
        for row in ep_rows:
            blob = row[3]
            if blob and len(blob) == query_dim * 4:
                valid_ep_rows.append(row)
                ep_blobs.append(blob)

        if valid_ep_rows:
            ep_mat = np.frombuffer(b"".join(ep_blobs), dtype=np.float32).reshape(len(ep_blobs), query_dim)
            ep_sims = ep_mat @ query_vec

            for i, sim_val in enumerate(ep_sims):
                if sim_val >= effective_min_sim:
                    ep_id, summary, start_time, _, ep_resolved = valid_ep_rows[i]
                    sim = float(sim_val)
                    candidates.append(
                        (
                            sim,
                            {
                                "id": ep_id,
                                "_rid": ("ep", ep_id),
                                "_cosine": sim,
                                "content": f"[Episode] {summary}",
                                "source": {"System": "episode"},
                                "timestamp": start_time or "",
                                "_resolved": bool(ep_resolved),
                            },
                        )
                    )

    top_k = heapq.nlargest(limit, candidates, key=lambda x: x[0])
    return [c[1] for c in top_k]
