"""Vector embedding client and similarity search for CPersona.

Holds the module-level `_embedding_client` singleton, set by `server.main()` at startup.
"""

import heapq
import logging

import aiosqlite
from mcp_common.embedding_client import EmbeddingClient

import config
from config import (
    MAX_MEMORIES,
    VECTOR_SEARCH_MODE,
)

logger = logging.getLogger(__name__)


_embedding_client: EmbeddingClient | None = None


async def _search_vector(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_similarity: float | None = None,
    channel: str = "",
) -> list[dict]:
    """Search memories and episodes using vector cosine similarity."""

    if VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url:
        try:
            base_url = _embedding_client._http_url.rsplit("/", 1)[0]
            resp = await _embedding_client._client.post(
                f"{base_url}/search",
                json={
                    "namespace": f"cpersona:{agent_id}",
                    "query": query,
                    "limit": limit,
                    "min_similarity": config.VECTOR_MIN_SIMILARITY,
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
                        row = await db.execute_fetchall(
                            "SELECT msg_id, content, source, timestamp FROM memories WHERE id = ? AND channel = ?",
                            (mem_id, channel),
                        )
                    else:
                        row = await db.execute_fetchall(
                            "SELECT msg_id, content, source, timestamp FROM memories WHERE id = ?",
                            (mem_id,),
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
                    ep_id = int(raw_id[3:])
                    row = await db.execute_fetchall(
                        "SELECT summary, start_time, resolved FROM episodes WHERE id = ?",
                        (ep_id,),
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
        return []
    query_vec = np.array(embeddings[0], dtype=np.float32)
    query_dim = len(query_vec)
    effective_min_sim = min_similarity if min_similarity is not None else config.VECTOR_MIN_SIMILARITY

    candidates: list[tuple[float, dict]] = []
    scan_limit = min(MAX_MEMORIES, max(limit * 10, 100))

    if channel:
        rows = await db.execute_fetchall(
            """SELECT id, msg_id, content, source, timestamp, embedding
               FROM memories
               WHERE agent_id = ? AND channel = ? AND embedding IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, channel, scan_limit),
        )
    else:
        rows = await db.execute_fetchall(
            """SELECT id, msg_id, content, source, timestamp, embedding
               FROM memories
               WHERE agent_id = ? AND embedding IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
            (agent_id, scan_limit),
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

    ep_rows = await db.execute_fetchall(
        """SELECT id, summary, start_time, embedding, resolved
           FROM episodes
           WHERE agent_id = ? AND embedding IS NOT NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (agent_id, scan_limit),
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
