"""Vector embedding client and similarity search for CPersona.

Holds the module-level `_embedding_client` singleton, set by `server.main()` at startup.
"""

import heapq
import logging

import aiosqlite
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.isolation import IsolationFilter, isolation_where

from cpersona import config
from cpersona import health
from cpersona import vector_index
from cpersona.config import (
    MAX_MEMORIES,
    REMOTE_SEARCH_TIMEOUT_SECS,
    VECTOR_SEARCH_MODE,
)
from cpersona.utils import episode_timestamp

logger = logging.getLogger(__name__)


_embedding_client: EmbeddingClient | None = None


async def remote_index_upsert(agent_id: str, items: list[dict]) -> None:
    """Push memory items to an agent's remote vector index in bounded chunks.

    This is network I/O only and must be called outside the DB write seam.
    Failures are non-fatal, matching the store path's inline remote push.
    """
    if (
        VECTOR_SEARCH_MODE != "remote"
        or not _embedding_client
        or not _embedding_client._http_url
        or not _embedding_client._client
    ):
        return

    base_url = _embedding_client._http_url.rsplit("/", 1)[0]
    for start in range(0, len(items), 128):
        chunk = items[start : start + 128]
        try:
            await _embedding_client._client.post(
                f"{base_url}/index",
                json={"namespace": f"cpersona:{agent_id}", "items": chunk},
            )
        except Exception as e:
            logger.debug("Remote index failed (non-fatal): %s", e)

# Per-agent vector-similarity threshold overrides (v2.4.15).
# Populated by do_calibrate_threshold / startup auto-calibration; agents with
# no calibration data fall back to the global config.VECTOR_MIN_SIMILARITY.
_agent_thresholds: dict[str, float] = {}

# Per-agent post-fusion quality-gate thresholds (v2.4.26). Calibrated by
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

# Per-agent precision weight beta (knob 3, v2.4.29). The specificity weight
# the agent's fused gate is calibrated at: strict=2.0 (fewer contaminants, more misses) /
# balanced=1.0 (Youden's J) / lenient=0.5 (fewer misses, more contaminants). Only agents
# with an explicit override (set_recall_precision) are stored here; an absent agent uses
# the global config.FUSED_GATE_BETA, so changing the env still moves un-configured agents
# on their next calibration. Persisted in the calibration sidecar next to the gate it
# produced — the gate threshold sits on the separation curve at this exact beta, so the
# two must be restored together or they desync.
_agent_betas: dict[str, float] = {}


# --- calibration authority (bug-189 follow-up) ------------------------------
#
# The sidecar is written by merging the dicts above over the stored file, so the
# writer has to tell two identical-looking absences apart:
#
#   not authoritative   the dict has no entry because this process never learned
#                       one. The stored entry MUST be carried forward, or
#                       calibrating one agent deletes every other agent's
#                       threshold, gate and beta from the file (bug-189).
#   authoritative       the dict has no entry because this process REMOVED it.
#                       The stored entry MUST be dropped, or set_recall_precision's
#                       clear (and delete_agent_data's purge) come back on the next
#                       restart and the ``cleared: true`` the tool returned was a lie.
#
# A process becomes authoritative for an entry by loading it (the startup restore
# read the whole payload) or by writing it (a measurement, an override, a purge).
#
# Authority is tracked per AXIS, not per agent, because the two are genuinely
# independent: ensure_calibrated_on_startup deliberately preloads agent_betas
# while refusing to preload the thresholds and gates it is about to re-measure
# (bug-184), so a process legitimately owns an agent's beta while knowing nothing
# about that same agent's threshold. Collapsing the axes would make that boot drop
# stored thresholds it never looked at.
_CALIBRATION_AGENT_AXES = ("agent_thresholds", "agent_fused_gates", "agent_betas")
_CALIBRATION_GLOBAL_AXES = ("global_threshold", "global_fused_gate", "fused_gate_signal")

_calibration_authority: dict[str, set[str]] = {axis: set() for axis in _CALIBRATION_AGENT_AXES}
_global_calibration_authority: set[str] = set()


def _claim_agent_calibration(agent_id: str, *axes: str) -> None:
    """Record that this process owns *agent_id*'s entry on each named per-agent axis.

    An unknown axis raises ``KeyError`` on the spot: a silently mistyped axis would
    leave the entry non-authoritative forever, which reads as "carry the stored
    value" — i.e. a deletion that never reaches disk, the exact failure this
    bookkeeping exists to prevent.
    """
    for axis in axes:
        _calibration_authority[axis].add(agent_id)


def _release_agent_calibration(agent_id: str, *axes: str) -> None:
    """Give up ownership of *agent_id*'s entry on each named per-agent axis.

    Used when a write is rolled back to a state this process never loaded: after
    the rollback the dict's silence means "unknown" again, so the stored entry must
    be carried rather than dropped.
    """
    for axis in axes:
        _calibration_authority[axis].discard(agent_id)


def _claim_global_calibration(*axes: str) -> None:
    """Record that this process owns the named global calibration axes."""
    for axis in axes:
        if axis not in _CALIBRATION_GLOBAL_AXES:
            raise KeyError(axis)
        _global_calibration_authority.add(axis)


def _reset_calibration_authority() -> None:
    """Forget every claim — the companion of clearing the dicts above (tests)."""
    for owned in _calibration_authority.values():
        owned.clear()
    _global_calibration_authority.clear()


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


# One batched hydration statement stays well under SQLite's bound-variable ceiling
# (999 on older builds) with room for the isolation/source parameters riding along.
_ID_FETCH_CHUNK = 500


async def _fetch_rows_by_id(
    db: aiosqlite.Connection,
    sql_template: str,
    ids: list[int],
    extra_params: tuple,
) -> dict[int, tuple]:
    """Fetch the rows named by `ids` in batches, keyed by id.

    `sql_template` carries a literal ``{ph}`` where the placeholder list belongs and
    must SELECT the id column first; the ids are BOUND into those placeholders and
    never interpolated into the string. Chunking keeps each statement under the
    bound-variable ceiling that a large `limit` would otherwise breach — the per-hit
    loop this replaces could not reach that ceiling, and an exception raised here
    would silently demote the whole remote search to a local scan.

    Duplicate ids collapse for the query but not for the caller: the caller indexes
    this map per hit, so a repeated remote hit still yields a repeated result row.
    """
    if not ids:
        return {}
    unique = list(dict.fromkeys(ids))
    out: dict[int, tuple] = {}
    for start in range(0, len(unique), _ID_FETCH_CHUNK):
        chunk = unique[start : start + _ID_FETCH_CHUNK]
        sql = sql_template.replace("{ph}", ",".join("?" * len(chunk)))
        for row in await db.execute_fetchall(sql, (*chunk, *extra_params)):
            out[row[0]] = row
    return out


async def _search_vector_remote(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    effective_min_sim: float,
    *,
    iso_fetch: IsolationFilter,
    iso_ep_fetch: IsolationFilter,
    src_clause: str,
    src_params: tuple,
    src_like: str,
    channel: str,
) -> list[dict] | None:
    """Rank through the remote vector service, or decline so the caller scans locally.

    The return type carries the control flow, and `None` is NOT `[]`:

        list (even empty)   the service answered; that answer IS the result
        None                the service was unusable; the caller must scan locally

    Conflating the two is the mistake this signature exists to make hard. A
    remote query that legitimately matches nothing would turn into a full local
    scan, so the two retrievers would disagree on identical data in exactly the
    case where the corpus has nothing to say -- and silently, because a non-empty
    local result looks like a working search. `sv-remote-empty` in the
    behavioural snapshot (tests/behaviour_252.py) pins the distinction.

    The whole body stays inside one `try`: a fault while fetching the rows a hit
    names is as much a reason to fall back as a fault reaching the service.
    """
    if not (VECTOR_SEARCH_MODE == "remote" and _embedding_client and _embedding_client._http_url):
        return None

    try:
        base_url = _embedding_client._http_url.rsplit("/", 1)[0]
        resp = await _embedding_client._client.post(
            f"{base_url}/search",
            json={
                "namespace": f"cpersona:{agent_id}",
                "query": query,
                "limit": limit,
                "min_similarity": effective_min_sim,
            },
            # bug-033: bound the recall hot path with a dedicated short timeout
            # instead of inheriting the client's 30s DEFAULT_TIMEOUT_SECS. A
            # hung/flapping endpoint now falls back to local search in seconds.
            timeout=REMOTE_SEARCH_TIMEOUT_SECS,
        )
        resp.raise_for_status()
        data = resp.json()

        # 2.5.2a2 audit (C16): hydrate the hits with ONE query per row type instead of
        # one per hit. aiosqlite funnels every statement through a single background
        # thread, so the old per-hit SELECT made a limit=100 recall pay 100 sequential
        # round trips on the hot path. Parsing stays in this pass (a malformed id still
        # raises inside the try and falls back to the local scan, as before).
        ordered: list[tuple[str, int, float]] = []
        for hit in data.get("results", []):
            raw_id = hit["id"]
            score = hit["score"]
            if raw_id.startswith("mem:"):
                ordered.append(("mem", int(raw_id[4:]), score))
            elif raw_id.startswith("ep:"):
                # Episodes lack per-user source. A channel filter makes them
                # safe on the session-start grounding path (bug-046/075),
                # matching the local branch's bug-080 contract.
                if src_like and not channel:
                    continue
                ordered.append(("ep", int(raw_id[3:]), score))

        # ''=global (knob2 v2): a stored channel of '' is global and matches every
        # channel-scoped recall, so old/global memories are never orphaned by
        # per-channel filing. Both fetches keep every isolation axis they carried
        # per hit (bug-100) — the batching changes the statement's arity, nothing else.
        mem_rows = await _fetch_rows_by_id(
            db,
            f"SELECT id, msg_id, content, source, timestamp FROM memories WHERE id IN ({{ph}})"
            f"{iso_fetch.and_clause}{src_clause}",
            [i for kind, i, _ in ordered if kind == "mem"],
            (*iso_fetch.params, *src_params),
        )
        ep_rows = await _fetch_rows_by_id(
            db,
            f"SELECT id, summary, start_time, resolved, created_at FROM episodes WHERE id IN ({{ph}})"
            f"{iso_ep_fetch.and_clause}",
            [i for kind, i, _ in ordered if kind == "ep"],
            iso_ep_fetch.params,
        )

        # Re-order to the remote's score order: the IN() rows come back in whatever
        # order SQLite chose, but the service already ranked the hits and the caller
        # consumes that ranking. A hit with no row is skipped silently, exactly as the
        # per-hit `if row:` did — a stale remote index entry is not an error
        # (`sv-remote-stale-id` in the behavioural snapshot).
        results = []
        for kind, row_id, score in ordered:
            if kind == "mem":
                row = mem_rows.get(row_id)
                if row is not None:
                    results.append(
                        {
                            "id": row_id,
                            "_rid": ("mem", row_id),
                            "_cosine": score,
                            "msg_id": row[1],
                            "content": row[2],
                            "source": row[3],
                            "timestamp": row[4],
                        }
                    )
            else:
                row = ep_rows.get(row_id)
                if row is not None:
                    results.append(
                        {
                            "id": row_id,
                            "_rid": ("ep", row_id),
                            "_cosine": score,
                            "content": f"[Episode] {row[1]}",
                            "source": {"System": "episode"},
                            # bug-213: start_time is nullable; created_at is not.
                            "timestamp": episode_timestamp(row[2], row[4]),
                            "_resolved": bool(row[3]),
                        }
                    )
        return results
    except Exception as e:
        logger.warning("Remote vector search failed, falling back to local: %s", e)
        return None


def _cosine_batch(query_vec, query_dim: int, blobs: list[bytes]):
    """Batched cosine similarity of `query_vec` against pre-filtered float32
    blobs (each MUST be exactly ``query_dim * 4`` bytes).

    Extracted so the local memory / episode scanners and the ``_apply_recall_scoring``
    bug-155 cosine backfill share ONE unpack + matmul implementation; the two
    paths cannot silently drift on how a stored embedding is turned into a
    similarity. The caller is responsible for the width filter (a foreign-width
    blob in this list will crash the reshape) -- the scanners and the backfill
    both do that filter for their own reasons (bug-085 ragged-dim tolerance),
    keeping the helper's contract minimal.
    """
    import numpy as np

    mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), query_dim)
    return _cosine_matrix(query_vec, mat)


def _cosine_matrix(query_vec, mat):
    """The one matmul. Both suppliers of a candidate matrix end here.

    Kept as its own function so the contiguous index cannot acquire a second
    arithmetic: a row's dot product does not depend on any other row, so as long
    as every path reaches THIS call with the same float32 bytes in the same
    shape, the scores are identical bit for bit and the equivalence gate keeps
    its meaning. Re-implementing the sum is what would spend that -- measured on
    the same bytes, a hand-written dot product disagrees with numpy on 77.9% of
    rows.
    """
    return mat @ query_vec


async def _index_phase1(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
    scan_limit: int,
    query_dim: int,
):
    """Phase 1 from the contiguous index, or None to use the SQL scan.

    Returns `(ids, matrix)` in the scan's own order — `created_at` DESC, then
    `id` ASC — so everything downstream (the threshold, the stable top-`limit`
    cut, the hydrate) is handed exactly what the SQL read used to hand it.

    None is the ordinary answer, not a failure: no index yet, a dimension that
    does not match, or any condition under which this path cannot promise the
    same answer. The scan it replaces stays correct, and the design's whole claim
    rests on that staying true.
    """
    try:
        index = vector_index.cached_index("memories")
    except vector_index.IndexUnusable as exc:
        # Visible, not just logged at debug: an index that has been unusable for
        # a week otherwise reads as "somehow not faster", which is a failure this
        # project has already made once.
        logger.warning("Vector index unusable, falling back to the live scan: %s", exc)
        return None
    if index is None or index.dim != query_dim:
        return None

    positions = vector_index.select(
        index,
        agent_id=agent_id,
        project_id=project_id,
        channel=channel,
        source_id=source_id,
        limit=scan_limit,
    )

    tail = await _index_tail_rows(
        db,
        index,
        agent_id=agent_id,
        project_id=project_id,
        channel=channel,
        source_id=source_id,
        scan_limit=scan_limit,
    )
    if tail is None:
        return None

    return _merge_index_and_tail(index, positions, tail, scan_limit, query_dim)


async def _index_tail_rows(
    db: aiosqlite.Connection,
    index,
    *,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
    scan_limit: int,
):
    """The rows the index cannot answer for, read exactly.

    Two disjoint groups, both bounded: everything written since the build
    (`id > watermark`), and the rows the fixed-width format could not spell,
    which the build named for exactly this purpose. Without the second the index
    would silently stop returning them.

    Returns None when a tail row carries a foreign embedding width. The live scan
    applies its window BEFORE skipping such rows, so their presence changes which
    rows fall inside the window — the index cannot reproduce that, and a
    difference here is a different answer rather than a slower one. That state
    means a model swap began after the build; the scan handles it correctly.
    """
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src_like = _escape_like_prefix(source_id)
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params = (src_like,) if src_like else ()

    excluded = index.excluded_ids
    holes = f" OR id IN ({','.join('?' * len(excluded))})" if excluded else ""
    rows = await db.execute_fetchall(
        f"""SELECT id, created_at, embedding, length(embedding)
           FROM memories
           WHERE (id > ?{holes}) AND embedding IS NOT NULL AND {iso.clause}{src_clause}
           ORDER BY created_at DESC, id ASC
           LIMIT ?""",
        (index.watermark, *excluded, *iso.params, *src_params, scan_limit),
    )
    if any(r[3] != index.dim * 4 for r in rows):
        return None
    return rows


def _merge_index_and_tail(index, positions, tail, scan_limit: int, query_dim: int):
    """Interleave two already-sorted runs on (created_at DESC, id ASC).

    Not a concatenation. It is tempting to assume everything in the tail is newer
    than everything in the index and simply prepend it, but nothing promises
    that: the import path carries a restored record's original `created_at` while
    ids are assigned fresh, so an old export restored into a newer database
    produces new ids bearing old timestamps. Both runs are already ordered, so
    merging them costs nothing over prepending and does not have the failure mode.
    """
    import numpy as np

    created = index.created_at
    ids_arr = index.ids

    merged_ids: list[int] = []
    from_index: list[int] = []   # positions in the index, in output order
    index_slots: list[int] = []  # where each of those lands in the matrix
    from_tail: list[tuple[int, bytes]] = []

    i = j = 0
    while len(merged_ids) < scan_limit and (i < len(positions) or j < len(tail)):
        take_index = j >= len(tail)
        if not take_index and i < len(positions):
            pos = positions[i]
            t_created = tail[j][1].encode("ascii")
            # created_at DESC, then id ASC: the exact key the SQL ORDER BY spells.
            take_index = (created[pos], -int(ids_arr[pos])) > (t_created, -int(tail[j][0]))
        if take_index:
            pos = int(positions[i])
            index_slots.append(len(merged_ids))
            from_index.append(pos)
            merged_ids.append(int(ids_arr[pos]))
            i += 1
        else:
            from_tail.append((len(merged_ids), tail[j][2]))
            merged_ids.append(int(tail[j][0]))
            j += 1

    if not merged_ids:
        return [], np.empty((0, query_dim), dtype=np.float32)

    mat = np.empty((len(merged_ids), query_dim), dtype=np.float32)
    if from_index:
        # One vectorised gather: a memcpy out of the mapped file, never a Python
        # object per row, which is the 72.9% this whole change is about.
        mat[index_slots] = index.embeddings[from_index]
    for slot, blob in from_tail:
        mat[slot] = np.frombuffer(blob, dtype=np.float32)
    return merged_ids, mat

async def _scan_memories_local(
    db: aiosqlite.Connection,
    iso: IsolationFilter,
    src_clause: str,
    src_params: tuple,
    scan_limit: int,
    limit: int,
    query_vec,
    query_dim: int,
    effective_min_sim: float,
    *,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
) -> list[tuple[float, dict]]:
    """Cosine-rank the newest `scan_limit` memory rows against the query vector.

    Two phases, because the ranking and the answer need different columns
    (bug-249). Phase 1 reads `(id, embedding)` for the whole scan window and
    ranks it; phase 2 hydrates `msg_id/content/source/timestamp` only for the
    rows that cleared `effective_min_sim`. Selecting the payload columns up front
    made the scan's cost track `config.MAX_CONTENT_LENGTH`: that write cap was
    raised 2000 -> 16000, and this path -- which materialises every row of the
    window before a single similarity exists -- silently got 8x more text to
    carry per recall, text no cosine ever reads. The note on that constant states
    the rule the raise was granted under (a write bound must not enlarge a read
    one) and names the two full-text read budgets pinned for it; this is the same
    rule applied to the scan window rather than to a response.

    `limit` bounds the hydrate, and it has to: the threshold does not. The fusion
    callers pass `agent threshold x RRF_THRESHOLD_FACTOR`, which is deliberately
    permissive, so "hydrate whatever clears it" is a FRACTION of the window rather
    than a constant -- and `id IN (...)` is a b-tree lookup plus a row read each, so
    at high survival it is slower than the sequential read it replaced (measured at
    10000 rows x 16000 characters: 90 ms to hydrate every row by id against 52 ms
    for the single query). Only the top `limit` memory candidates can reach a
    response: the caller merges these with the episode candidates and takes
    `heapq.nlargest(limit, ...)`, so a memory that already has `limit` memories
    ranked above it cannot place, whatever the episodes do. Dropping those before
    the hydrate leaves the caller's answer identical (`nlargest` is stable and the
    bound keeps the scan order) and caps the payload read at `limit` rows -- which
    is what makes the split a win at every threshold instead of only a strict one.
    The win is threshold-wide, not unconditional: at the library ceiling
    (limit = MAX_MEMORIES) combined with a threshold that admits nearly the whole
    window, the bound cuts nothing and the by-id hydrate is SLOWER than the
    sequential read it replaced (measured 52 -> 90 ms; the MCP tools cap limit at
    100, so only bench/bulk-export callers can reach that shape). Numbers and
    regime: benchmarks/measurements/results-bug249-two-phase-scan.md.

    Rows whose embedding is a foreign width are skipped rather than reshaped: a
    mid-flight model swap leaves a mixed-dimension corpus behind, and one stale
    row must not take the whole scan down with a reshape error.
    """
    # Phase 1 has two suppliers and one contract: `(ids, similarities)` in the
    # scan's order. The contiguous index answers when it can promise the same
    # rows; otherwise this is the read it has always been.
    supplied = await _index_phase1(
        db,
        agent_id=agent_id,
        project_id=project_id,
        channel=channel,
        source_id=source_id,
        scan_limit=scan_limit,
        query_dim=query_dim,
    )
    if supplied is not None:
        valid_ids, mat = supplied
        if not valid_ids:
            return []
        sims = _cosine_matrix(query_vec, mat)
    else:
        # ''=global (knob2 v2): a stored channel of '' matches every channel-scoped
        # recall (as on the remote by-id path in _search_vector_remote) -- the
        # channel axis rides in `iso`.
        #
        # ORDER BY names `id` as well as `created_at`, which the index has to
        # reproduce exactly: `created_at` has one-second resolution, so ties are
        # ordinary, and the tie-break decides membership at the window edge, not
        # just presentation. SQLite already returned this order (equal index keys
        # come back by rowid ascending) and adding the term was measured to keep
        # the same plan -- so this states a contract rather than changing one.
        rows = await db.execute_fetchall(
            f"""SELECT id, embedding
               FROM memories
               WHERE {iso.clause} AND embedding IS NOT NULL{src_clause}
               ORDER BY created_at DESC, id ASC
               LIMIT ?""",
            (*iso.params, *src_params, scan_limit),
        )
        if not rows:
            return []

        valid_ids = []
        blobs = []
        for row in rows:
            blob = row[1]
            if blob and len(blob) == query_dim * 4:
                valid_ids.append(row[0])
                blobs.append(blob)

        if not valid_ids:
            return []

        sims = _cosine_batch(query_vec, query_dim, blobs)

    # Survivors keep the scan's order (created_at DESC): heapq.nlargest in
    # _search_vector is stable, so this order is what breaks a tie between two
    # equally-similar rows, and nothing below may reorder them.
    survivors = [
        (valid_ids[i], float(sim_val))
        for i, sim_val in enumerate(sims)
        if sim_val >= effective_min_sim
    ]
    if not survivors:
        return []

    if limit < len(survivors):
        # The stable top-`limit`: score first, scan position as the tie-break, which
        # is the order `sorted(..., reverse=True)` -- and therefore `nlargest` --
        # would have produced. Re-sorted back into scan order so the ties the caller
        # breaks are the ties it broke before.
        keep = heapq.nlargest(limit, range(len(survivors)), key=lambda i: (survivors[i][1], -i))
        survivors = [survivors[i] for i in sorted(keep)]

    # The hydrate re-applies the isolation axes and the source filter. That is
    # NOT the bug-100 fail-closed argument (that one guards ids supplied by the
    # REMOTE index, whose ownership the DB never confirmed) -- these ids came from
    # the phase-1 read. It is for the window the split opens between the two
    # statements: a row re-tagged or re-sourced in between must not be hydrated
    # under axes it no longer has.
    payload = await _fetch_rows_by_id(
        db,
        f"SELECT id, msg_id, content, source, timestamp FROM memories WHERE id IN ({{ph}})"
        f"{iso.and_clause}{src_clause}",
        [mem_id for mem_id, _ in survivors],
        (*iso.params, *src_params),
    )

    candidates: list[tuple[float, dict]] = []
    for mem_id, sim in survivors:
        row = payload.get(mem_id)
        # A survivor with no row was deleted (or moved out of scope) between the
        # two statements. Skip it rather than emit a half-empty result -- the same
        # silent-skip the remote branch applies to a stale index hit.
        if row is None:
            continue
        candidates.append(
            (
                sim,
                {
                    "id": mem_id,
                    "_rid": ("mem", mem_id),
                    "_cosine": sim,
                    "msg_id": row[1],
                    "content": row[2],
                    "source": row[3],
                    "timestamp": row[4],
                },
            )
        )
    return candidates


async def _scan_episodes_local(
    db: aiosqlite.Connection,
    iso: IsolationFilter,
    scan_limit: int,
    query_vec,
    query_dim: int,
    effective_min_sim: float,
    src_like: str,
    channel: str,
) -> list[tuple[float, dict]]:
    """Cosine-rank episode summaries, structurally mirroring the memory scan.

    Episodes lack per-user source tagging — skip them when source_id is set.
    bug-045: gate episodes by the channel axis exactly like the memory branch
    (and like _search_episodes_fts). Without this a channel-scoped recall
    cosine-scores and returns episode summaries from EVERY other channel of the
    agent — the cross-channel contamination the v2.4.22 channel axis exists to
    prevent, on the recall hot path. ''=global still matches every scoped recall.
    bug-080: honor the do_recall contract the FTS drivers already implement
    (`not source_id or channel`): a channel filter makes episodes safe to return
    even when source_id is set (the session-start grounding path) because the
    bug-045 channel clause scopes the fetch. Dropping ALL episodes on src_like
    silently defeated semantic episode recall on exactly that grounding path.
    The remote episode fetch carries the same channel predicate and gate, so both
    vector branches stay symmetric (bug-046/075).
    """
    if src_like and not channel:
        return []

    ep_rows = await db.execute_fetchall(
        f"""SELECT id, summary, start_time, embedding, resolved, created_at
           FROM episodes
           WHERE {iso.clause} AND embedding IS NOT NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (*iso.params, scan_limit),
    )
    if not ep_rows:
        return []

    valid_ep_rows = []
    ep_blobs = []
    for row in ep_rows:
        blob = row[3]
        if blob and len(blob) == query_dim * 4:
            valid_ep_rows.append(row)
            ep_blobs.append(blob)

    if not valid_ep_rows:
        return []

    ep_sims = _cosine_batch(query_vec, query_dim, ep_blobs)

    candidates: list[tuple[float, dict]] = []
    for i, sim_val in enumerate(ep_sims):
        if sim_val >= effective_min_sim:
            ep_id, summary, start_time, _, ep_resolved, ep_created_at = valid_ep_rows[i]
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
                        # bug-213: start_time is nullable; created_at is not.
                        "timestamp": episode_timestamp(start_time, ep_created_at),
                        "_resolved": bool(ep_resolved),
                    },
                )
            )
    return candidates


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
    # isolation_where composes the axes. `iso` (agent + γ project +
    # knob2 v2 channel) scopes the local scans; `iso_fetch` scopes the remote
    # by-id fetches. The remote memory and episode fetches carry the same three
    # isolation axes, symmetric with the local scans (bug-046/075).
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    # bug-100: the by-id fetches carry agent_id too. Row identity IS pinned by the
    # remote hit's id, but ownership then rests entirely on the remote index's
    # namespace matching DB ownership — a desynced or mis-seeded index would have
    # surfaced another agent's row. The predicate makes the fetch fail closed.
    iso_fetch = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    iso_ep_fetch = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)

    src_like = _escape_like_prefix(source_id)
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params = (src_like,) if src_like else ()

    # bug-027: honor the caller's min_similarity in the remote branch too. The
    # local branch (below) lowers the threshold for _recall_rrf/_recall_rsf so
    # fusion has more candidates to rank; the remote /search previously hardcoded
    # the full per-agent threshold, over-filtering and returning a smaller,
    # differently-ranked candidate set than local for identical data.
    effective_min_sim = min_similarity if min_similarity is not None else _get_vector_threshold(agent_id)

    # None means "not answered" and is the only value that continues to the local
    # scan below; an empty list is an answer. See _search_vector_remote.
    remote_results = await _search_vector_remote(
        db,
        agent_id,
        query,
        limit,
        effective_min_sim,
        iso_fetch=iso_fetch,
        iso_ep_fetch=iso_ep_fetch,
        src_clause=src_clause,
        src_params=src_params,
        src_like=src_like,
        channel=channel,
    )
    if remote_results is not None:
        return remote_results

    import numpy as np

    embeddings, outcome = await _embedding_client.embed_with_outcome([query])
    if not embeddings or not embeddings[0]:
        # The client exists here (mode != "none"), so a falsy embed of the query text
        # itself is a genuine embed failure, not an empty-corpus no-match. The call that
        # failed now carries its own evidence, so there is nothing left to re-probe.
        #
        # Reading the real call rather than a second one removes a disagreement that was
        # possible before: the probe could come back 2xx while the recall's own embed had
        # failed, and this branch would then record health as OK while returning nothing.
        # It also reaches the modes the probe could not — the probe posted the local
        # server's payload shape to `_http_url`, which an api-mode client does not even
        # have, so `mode=api` could only ever produce "embedding client unavailable".
        health.observe_failure(outcome.error or "the embedding call produced no usable vector")
        return []
    health.observe_ok()  # embed succeeded — re-arm after any prior degradation
    query_vec = np.array(embeddings[0], dtype=np.float32)
    query_dim = len(query_vec)
    # effective_min_sim computed once near the top (shared with the remote branch, bug-027).

    # bug-085: the scan window must NOT be derived from the response limit. The
    # old `min(MAX_MEMORIES, max(limit * 10, 100))` coupling meant a default
    # limit=10 recall ranked only the newest 100 rows — anything older was
    # structurally invisible to the vector retriever (and the 2.4.38 limit clamp
    # closed the only escape hatch, collapsing LMEB LongMemEval 78→38.68). Scan
    # breadth and response size are independent concepts: rank the newest
    # MAX_MEMORIES rows regardless of how many the caller asked to receive.
    scan_limit = MAX_MEMORIES

    # Memories first, then episodes: nlargest is stable, so this order is what
    # breaks a tie between a memory and an episode of equal similarity. The memory
    # scan already returns at most `limit` candidates (bug-249) -- it applies this
    # same cut, with this same tie-break, before paying to read their text.
    candidates = await _scan_memories_local(
        db, iso, src_clause, src_params, scan_limit, limit, query_vec, query_dim, effective_min_sim,
        agent_id=agent_id, project_id=project_id, channel=channel, source_id=source_id,
    )
    candidates += await _scan_episodes_local(
        db, iso, scan_limit, query_vec, query_dim, effective_min_sim, src_like, channel
    )

    top_k = heapq.nlargest(limit, candidates, key=lambda x: x[0])
    return [c[1] for c in top_k]
