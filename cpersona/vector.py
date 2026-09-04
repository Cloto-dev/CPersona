"""Vector embedding client and similarity search for CPersona.

Holds the module-level `_embedding_client` singleton, set by `server.main()` at startup.
"""

import heapq
import json
import logging
import math
import struct

import aiosqlite
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.isolation import IsolationFilter, isolation_where

from cpersona import config
from cpersona import health
from cpersona import vector_index
from cpersona.config import (
    MAX_MEMORIES,
    REMOTE_SEARCH_TIMEOUT_SECS,
    VECTOR_FAR_LIMIT,
    VECTOR_REACH,
    VECTOR_SCAN_CHUNK_ROWS,
    VECTOR_SEARCH_MODE,
)
from cpersona.utils import episode_timestamp

logger = logging.getLogger(__name__)


_embedding_client: EmbeddingClient | None = None


def pack_for_storage(embedding: object) -> bytes | None:
    """The one place a vector becomes a BLOB this process will store.

    Returns the packed bytes, or ``None`` when the vector must not be stored — so
    a caller's "no embedding this time" branch is the one it already has, and a
    refusal leaves the column NULL for check_health's repair pass to retry later.

    Callers used to decide this for themselves with ``if embeddings and
    embeddings[0]``, which is a truthiness test: it accepts a vector of NaNs, a
    vector of strings, and a vector one element wide from a backend that changed
    model. A NaN that gets through is not recoverable by reading the row back —
    it scores against every query, and because the similarity floor is a ``<``
    comparison a NaN does not fall below it, so the bad row stays and pushes good
    rows out. This is the last point where that can still be stopped, which is why
    the check lives here rather than at each call site.

    The embedding client validates the backend's response at the wire; this
    validates what is about to be written. They are different questions: a new
    caller, an import, or a cached vector reaches storage without passing the wire
    again.
    """
    if not isinstance(embedding, (list, tuple)) or len(embedding) == 0:
        return None

    values: list[float] = []
    for value in embedding:
        # bool is a subclass of int, so a bare numeric check would pack True as 1.0.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            logger.warning("Refusing to store an embedding containing a non-number")
            return None
        number = float(value)
        if not math.isfinite(number):
            logger.warning("Refusing to store an embedding containing a non-finite value")
            return None
        values.append(number)

    try:
        # Finite in float64 is not enough: the store is float32, where 1e300 is inf.
        # struct.pack is what raises, and OverflowError is not a ValueError, so it
        # would otherwise escape the callers' except clauses.
        return EmbeddingClient.pack_embedding(values)
    except (OverflowError, struct.error):
        logger.warning("Refusing to store an embedding that does not fit in float32")
        return None


def stored_blob_is_finite(blob: object) -> bool:
    """Whether a BLOB already in the database holds only finite float32 values.

    The companion to ``pack_for_storage``, and deliberately its neighbour: the two
    are the same question asked in the two directions data travels, and a second
    idea of "finite" living somewhere else is how the write side and the read side
    come to disagree.

    They cannot share an implementation, because they do not share an input.
    ``pack_for_storage`` is handed Python numbers and can reject a bool or a string
    before any packing happens; this is handed bytes that were packed long ago, by
    a version that had no such seam. What they share is the verdict.

    A blob that is not a whole number of float32s is NOT reported here. That is
    ``embedding_dimension``'s finding, it has its own repair, and answering for it
    too would make two checks fix the same row -- so an unreadable width is left
    alone rather than called non-finite, which it may well not be.
    """
    import numpy as np  # local, as everywhere else in this module

    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return False
    raw = bytes(blob)
    if not raw or len(raw) % 4:
        return True
    # Vectorised rather than a per-value loop: this runs over every embedded row
    # in the scope on a health check, and a 768-wide corpus of any size would
    # otherwise spend its time in the interpreter.
    return bool(np.isfinite(np.frombuffer(raw, dtype=np.float32)).all())



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


def far_list_enabled() -> bool:
    """Whether the vector arm produces a second, FAR list beside its usual one.

    The one predicate every far path is gated on, so "off" is a single question
    asked in a single place. `CPERSONA_VECTOR_REACH` at or below the scan window
    means the region `[MAX_MEMORIES, VECTOR_REACH)` is empty, and an empty region
    is not scanned: the far list must not exist as code that runs at the default
    and returns nothing, because a scan that reads no rows still costs a
    statement, a matrix and a merge on every recall the server answers.

    Read from the module globals rather than closed over at import, for the same
    reason the scan reads its chunk size at call time: a value baked into a
    default argument could not be turned down by a test or by the environment.
    """
    return VECTOR_REACH > MAX_MEMORIES


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
    table: str = "memories",
    scan_offset: int = 0,
):
    """Phase 1 from the contiguous index, or None to use the SQL scan.

    `table` names which scan this stands in for. The two tables share the file
    format, the selection and the merge; they differ in one column — episodes
    carry no `source`, so `source_id` is meaningless there and the caller passes
    it empty (the episode scan applies its own source rule before it gets here).

    Returns `(ids, matrix)` in the scan's own order — `created_at` DESC, then
    `id` ASC — so everything downstream (the threshold, the stable top-`limit`
    cut, the hydrate) is handed exactly what the SQL read used to hand it.

    `scan_offset` names where in that order the returned rows start: the far
    list of `docs/SCAN_WINDOW_REACH_DESIGN.md` asks for scan positions
    `[MAX_MEMORIES, VECTOR_REACH)`, which is this function's ordinary answer with
    the first `scan_offset` rows dropped. It is 0 for every call the near list
    makes, and at 0 every line below is the line that was there before.

    The offset applies to the MERGED sequence, not to the selection: the rows
    written since the build are read out of the live table and interleaved into
    the index's own order, so they occupy scan positions like any other row.
    Offsetting the selection alone would skip `scan_offset` INDEXED rows and then
    hand back the tail as well — the newest rows in the corpus — which is the
    near window's content appearing in the far list. Hence the selection and the
    tail are both taken to `scan_offset + scan_limit` and the cut happens after
    the merge.

    None is the ordinary answer, not a failure: no index yet, a dimension that
    does not match, or any condition under which this path cannot promise the
    same answer. The scan it replaces stays correct, and the design's whole claim
    rests on that staying true.
    """
    try:
        index = vector_index.cached_index(table)
        if index is None or index.dim != query_dim:
            return None
        # Inside the guard (bug-276). Selection used to sit outside it, so any
        # exception raised while selecting escaped into recall instead of
        # falling back — which is the one thing a derived, optional artifact
        # must never do to the query it was meant to accelerate.
        positions = vector_index.select(
            index,
            agent_id=agent_id,
            project_id=project_id,
            channel=channel,
            source_id=source_id,
            limit=scan_offset + scan_limit,
        )
    except vector_index.IndexUnusable as exc:
        # Visible, not just logged at debug: an index that has been unusable for
        # a week otherwise reads as "somehow not faster", which is a failure this
        # project has already made once.
        logger.warning("Vector index unusable, falling back to the live scan: %s", exc)
        return None
    except Exception:  # noqa: BLE001 — fail open, deliberately
        # The backstop, not the fix: the two type errors this was filed for are
        # repaired at their sources. It is here because the claim to hold is
        # "recall survives a broken index", and that claim cannot rest on having
        # enumerated every way a mapped file and a numpy filter can fail. The
        # scan below is always correct, so falling back costs latency and
        # nothing else.
        logger.warning("Vector index raised, falling back to the live scan", exc_info=True)
        return None

    try:
        lost = await _index_rows_lost_embedding(
            db, index.ids[positions], agent_id=agent_id, table=table
        )
    except Exception:  # noqa: BLE001 — fail open, deliberately
        # bug-285: the probe used to sit outside every guard, so an operational
        # error raised by it (a parameter list longer than the build's
        # SQLITE_MAX_VARIABLE_NUMBER) escaped into recall instead of falling
        # back. The bound is repaired inside the probe; this is the same claim
        # as the guard above — "recall survives a broken index" — extended to
        # the one statement the index path issues against the database.
        logger.warning("Vector index probe raised, falling back to the live scan", exc_info=True)
        return None
    if lost:
        # bug-279, the mirror of bug-278: maintenance blanks an embedding in
        # place (the content sanitiser rewrites the text and nulls the vector;
        # the dimension repair nulls a mismatched one), and the matrix keeps the
        # bytes. The scan drops such a row — every phase filters
        # `embedding IS NOT NULL` — so continuing here would rank a row by a
        # vector that no longer exists and return one the scan excludes.
        #
        # Dropping the positions instead would still not match: the scan's window
        # is the newest `scan_limit` rows THAT HAVE an embedding, so removing k
        # rows from a saturated selection leaves the index short by k rows the
        # scan reaches. That divergence is invisible on any corpus smaller than
        # the window, which is where a test would look. Handing the question back
        # to the scan is exact at every window size, and it is what this function
        # already does for the other condition it cannot reproduce.
        logger.warning(
            "Vector index holds rows whose embedding was cleared since the build; "
            "falling back to the live scan until it is rebuilt"
        )
        return None

    tail = await _index_tail_rows(
        db,
        index,
        agent_id=agent_id,
        project_id=project_id,
        channel=channel,
        source_id=source_id,
        scan_limit=scan_offset + scan_limit,
        table=table,
    )
    if tail is None:
        return None

    return _merge_index_and_tail(
        index, positions, tail, scan_limit, query_dim, scan_offset=scan_offset
    )


# Rows per `IN (...)` when the selection is scattered. Well under 999, the
# compile-time SQLITE_MAX_VARIABLE_NUMBER of every SQLite before 3.32.0, so the
# probe fits on the smallest build a supported Python links against.
_LOST_EMBEDDING_PROBE_CHUNK = 500

# `+agent_id`: the unary plus stops the planner from constraining the isolation
# index with this term, so the id term stays the access path. Measured without
# it at 100,000 rows: the planner walked every row of the agent per statement,
# 43 ms for the range form instead of 25 and 3.0 s for the chunked form.
def _lost_embedding_range_sql(table: str = "memories") -> str:
    return (
        f"SELECT 1 FROM {table} WHERE id BETWEEN ? AND ? AND +agent_id = ?"
        " AND embedding IS NULL LIMIT 1"
    )


# The memories form, kept under the name the plan test pins.
_LOST_EMBEDDING_RANGE_SQL = _lost_embedding_range_sql("memories")


def _lost_embedding_chunk_sql(count: int, table: str = "memories") -> str:
    ph = ",".join("?" * count)
    return (
        f"SELECT 1 FROM {table} WHERE id IN ({ph}) AND +agent_id = ?"
        " AND embedding IS NULL LIMIT 1"
    )


async def _index_rows_lost_embedding(
    db: aiosqlite.Connection, ids, *, agent_id: str, table: str = "memories"
) -> bool:
    """Whether any of these indexed ids no longer carries an embedding.

    An existence probe rather than a list: the caller only needs to know whether
    to hand the query back to the scan, so `LIMIT 1` lets SQLite stop at the
    first hit.

    Bounded by the selection's *shape*, not its size (bug-285). One parameter
    per id made the statement's width the scan window, and a window above the
    build's SQLITE_MAX_VARIABLE_NUMBER (32,766 on the default build) turned the
    index from an accelerator into `too many SQL variables` on every recall. The
    ordinary all-index selection is one contiguous run of ids, which BETWEEN
    asks about with two parameters; a scattered selection (an axis filter that
    skips rows) is asked in fixed-size chunks that fit every supported build.

    The ids came from a selection that already applied every axis, so the
    question here is about the row's embedding rather than its ownership. The
    agent predicate is carried anyway: it cannot exclude a selected row (the
    selection admits only this agent's rows) and it keeps the statement inside
    the isolation contract every agent-scoped read is held to. No other axis is
    added — narrowing further could only make the probe miss a row it should
    have caught, which is the direction that fails silently.

    The unary plus on `agent_id` in both statements is load-bearing — see the
    note on `_LOST_EMBEDDING_RANGE_SQL`; a test pins the plan.
    """
    import numpy as np

    ids = np.asarray(ids)
    n = len(ids)
    if n == 0:
        return False
    lo, hi = int(ids.min()), int(ids.max())
    if hi - lo + 1 == n:
        # Ids are unique, so a span equal to the count means every id in
        # [lo, hi] is selected and the range asks exactly the same question.
        row = await db.execute_fetchall(_lost_embedding_range_sql(table), (lo, hi, agent_id))
        return bool(row)
    for start in range(0, n, _LOST_EMBEDDING_PROBE_CHUNK):
        chunk = ids[start : start + _LOST_EMBEDDING_PROBE_CHUNK].tolist()
        row = await db.execute_fetchall(
            _lost_embedding_chunk_sql(len(chunk), table), (*chunk, agent_id)
        )
        if row:
            return True
    return False


# Two of the tail read's terms and none of the rest: `id` is `INTEGER PRIMARY
# KEY` in both tables the index serves, so the range is a seek into the rowid
# b-tree, and the walk it costs is bounded by the rows written since the build
# rather than by the corpus. The embedding test, the project and channel axes
# and the ORDER BY are all left out on purpose — none of them can turn an empty
# result into a non-empty one, and each would put back some of the work the
# question is asked to avoid.
#
# `+agent_id`: the unary plus keeps the planner from constraining the isolation
# index with the agent term, which would walk every row of the agent instead of
# the far shorter range. Same reason, and same measurement, as the probe above.
def _tail_exists_sql(table: str = "memories") -> str:
    return f"SELECT EXISTS(SELECT 1 FROM {table} WHERE id > ? AND +agent_id = ?)"


async def _rows_written_since(
    db: aiosqlite.Connection, watermark: int, *, agent_id: str, table: str = "memories"
) -> bool:
    """Whether this agent owns any row the build did not see.

    Deliberately unguarded, like the statement it stands in front of: a failure
    here is the database failing, and the live scan this path falls back to
    reads the same database through the same connection. There is nothing to
    fall back TO, which is why the index's other probe — which can fail because
    of the index's own shape — is guarded and this one is not.
    """
    rows = await db.execute_fetchall(_tail_exists_sql(table), (watermark, agent_id))
    return bool(rows and rows[0][0])


async def _index_tail_rows(
    db: aiosqlite.Connection,
    index,
    *,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
    scan_limit: int,
    table: str = "memories",
):
    """The rows the index cannot answer for, read exactly.

    Three disjoint groups, all bounded: everything written since the build
    (`id > watermark`); the rows the fixed-width format could not spell; and the
    rows that carried no embedding when the build ran (bug-278). The build names
    the last two for exactly this purpose. Without them the index would silently
    stop returning rows the scan still returns — and the third group is the one
    routine maintenance creates, since filling a NULL embedding is what
    check_health(fix=True) does.

    Returns None when a tail row carries a foreign embedding width. The live scan
    applies its window BEFORE skipping such rows, so their presence changes which
    rows fall inside the window — the index cannot reproduce that, and a
    difference here is a different answer rather than a slower one. That state
    means a model swap began after the build; the scan handles it correctly.
    """
    # One IN clause for both named groups: they are disjoint by construction (a row
    # with no embedding is not in the meta query that produces the excluded list) and
    # the query treats them identically — read this id exactly, whatever the watermark
    # says. The build caps their sum for this reason.
    holes_ids = tuple(index.excluded_ids) + tuple(index.unembedded_ids)

    # The steady state — nothing written since the build, no named holes — is the
    # shape the index exists for, and in it the statement below is structurally
    # empty: `id > watermark AND agent_id = ?` is a necessary condition of its
    # WHERE (agent_id is a string here, and isolation_where binds any string,
    # '' included, as an exact match), so when no row satisfies that, none
    # satisfies the whole. Discovering it still costs a walk of the isolation
    # index, 12.53 ms of a 49.92 ms arm at 100,000 rows
    # (benchmarks/measurements/results-contiguous-index.md). The same question
    # asked over the rowid range is a seek.
    #
    # Gated on there being no holes, because a named hole is read by id WHATEVER
    # the watermark says: with one present the range stops being a necessary
    # condition, and the cheap question no longer decides the expensive one.
    if not holes_ids and not await _rows_written_since(
        db, index.watermark, agent_id=agent_id, table=table
    ):
        return []

    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    # Only memories carry a source column; the episode caller passes source_id
    # empty, and the guard here is what keeps a non-empty one from becoming a
    # reference to a column the table does not have.
    src_like = _escape_like_prefix(source_id) if table == "memories" else ""
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params = (src_like,) if src_like else ()

    # The holes travel as ONE parameter — a JSON array — rather than one placeholder
    # per id. A placeholder per id makes the cap on named holes a cap on SQL
    # variables as well: at 1,000 holes the statement already binds 1,000 ids plus
    # the watermark, the isolation parameters and the limit, past the 999
    # SQLITE_MAX_VARIABLE_NUMBER every SQLite older than 3.32.0 is compiled with —
    # the same builds `_LOST_EMBEDDING_PROBE_CHUNK` in this module says must keep
    # working. Raising the cap for a six-figure corpus would make that a certainty
    # instead of a boundary case.
    #
    # Binding was never what the read costs. Measured at 150,000 rows of 1024-d
    # float32: 1,000 holes 10.1 ms per-id vs 8.8 ms as json_each, 5,000 holes
    # 34.7 vs 34.5 ms, and 50,000 holes raised `too many SQL variables` per-id
    # while json_each answered in 325 ms. What is being paid for is reading the
    # hole rows that have since gained an embedding, 4 KB of blob each.
    #
    # JSON1 is not a new requirement: the source filter three lines up already
    # calls `json_extract` on this same statement.
    holes = " OR id IN (SELECT value FROM json_each(?))" if holes_ids else ""
    # Empty stays byte-identical to a statement that never had the clause, and
    # binds no extra parameter: a test pins the plan of the empty-tail shape.
    holes_params = (json.dumps([int(i) for i in holes_ids]),) if holes_ids else ()
    rows = await db.execute_fetchall(
        f"""SELECT id, created_at, embedding, length(embedding)
           FROM {table}
           WHERE (id > ?{holes}) AND embedding IS NOT NULL AND {iso.clause}{src_clause}
           ORDER BY created_at DESC, id ASC
           LIMIT ?""",
        (index.watermark, *holes_params, *iso.params, *src_params, scan_limit),
    )
    if any(r[3] != index.dim * 4 for r in rows):
        return None
    return rows


def _merge_index_and_tail(index, positions, tail, scan_limit: int, query_dim: int,
                          *, scan_offset: int = 0):
    """Interleave two already-sorted runs on (created_at DESC, id ASC).

    Not a concatenation. It is tempting to assume everything in the tail is newer
    than everything in the index and simply prepend it, but nothing promises
    that: the import path carries a restored record's original `created_at` while
    ids are assigned fresh, so an old export restored into a newer database
    produces new ids bearing old timestamps. Both runs are already ordered, so
    merging them costs nothing over prepending and does not have the failure mode.

    When the tail is empty there is nothing to interleave: the output is the
    selection itself, in the order `select()` already returns it, and it is
    taken as one numpy slice rather than walked one position at a time. Measured
    at 100,000 rows the walk was 79 ms of a 212 ms arm for a comparison it never
    needed to make (benchmarks/measurements/results-contiguous-index.md). The
    walk stays for the shape that needs it, and a test pins that the empty-tail
    shape does not enter it.

    `scan_offset` skips that many rows of the merged order before the returned
    `scan_limit` begins (the far list of `docs/SCAN_WINDOW_REACH_DESIGN.md`). The
    skipped rows are dropped BEFORE the matrix is built, not sliced off it
    afterwards: the empty-tail shape then still selects a contiguous run of the
    file and still answers with a view of it, so the far list costs one matmul
    over the rows it actually ranks rather than over everything above them too.
    """
    import numpy as np

    if not tail:
        positions = np.asarray(positions, dtype=np.int64)[scan_offset:scan_offset + scan_limit]
        merged_ids = index.ids[positions].tolist()
        from_index = positions
        from_tail: list[tuple[int, bytes]] = []
        index_slots = np.arange(len(positions))
    else:
        merged_ids, from_index, index_slots, from_tail = _interleave_index_and_tail(
            index, positions, tail, scan_limit, scan_offset=scan_offset
        )

    if not merged_ids:
        return [], np.empty((0, query_dim), dtype=np.float32)

    if not from_tail and _is_ascending_run(from_index):
        # The ordinary shape -- one agent, no axis narrowing, nothing written
        # since the build -- selects a contiguous run of the file in file order,
        # and the gather below then copies that run out of the mapped file one
        # scattered row at a time to rebuild a matrix that is already there.
        # Measured at 100,000 rows x 1024 dims, the gather is 440 ms of a 498 ms
        # vector arm against 26 ms for the same matmul over a view of the same
        # rows -- 4.8x on the term that is 88% of the time. A scattered gather
        # faults the mapped pages in scattered order; a matmul over a view
        # streams them (benchmarks/measurements/results-contiguous-index.md).
        #
        # A view of exactly the selected rows, and never "multiply the whole file
        # and then take the scores that were wanted": for a contiguous run the
        # view is the same bytes in the same order with the same row count, so
        # the scores are identical bit for bit, while the same measurement shows
        # a scattered selection scored that way DIFFERS -- it changes the
        # summation order, which is the exactness this design is built on. Hence
        # a condition this narrow rather than a general fast path.
        #
        # The slice is a read-only view into the mapped file, which is safe
        # because the candidate matrix is only ever read: its one consumer is
        # `_cosine_matrix`, whose `mat @ query_vec` allocates its own result.
        # Anything here that wrote into the matrix would have to copy first.
        start = int(from_index[0])
        return merged_ids, index.embeddings[start:start + len(from_index)]

    mat = np.empty((len(merged_ids), query_dim), dtype=np.float32)
    if len(from_index):
        # One vectorised gather: a memcpy out of the mapped file, never a Python
        # object per row, which is the 72.9% this whole change is about.
        mat[index_slots] = index.embeddings[from_index]
    for slot, blob in from_tail:
        mat[slot] = np.frombuffer(blob, dtype=np.float32)
    return merged_ids, mat


def _interleave_index_and_tail(index, positions, tail, scan_limit: int, *, scan_offset: int = 0):
    """The merge walk: one tuple comparison per output row, for a non-empty tail.

    Returns `(merged_ids, from_index, index_slots, from_tail)` — the ids in
    output order, the index positions taken and the output slots they land in,
    and the tail rows taken with theirs. Separate from `_merge_index_and_tail`
    so that the shape which does not need the walk can be pinned as not paying
    for it.

    `scan_offset` rows of the merged order are consumed and discarded before the
    output starts. The walk still visits them — their position in the merged
    order is exactly what decides which rows the far region contains, and that
    is a question about both runs at once — but nothing they name is collected,
    so no skipped row reaches the matrix. At the default of 0 the loop consumes
    and appends in lockstep, which is what it did before this parameter existed.
    """
    created = index.created_at
    ids_arr = index.ids

    merged_ids: list[int] = []
    from_index: list[int] = []   # positions in the index, in output order
    index_slots: list[int] = []  # where each of those lands in the matrix
    from_tail: list[tuple[int, bytes]] = []

    i = j = 0
    consumed = 0
    wanted = scan_offset + scan_limit
    while consumed < wanted and (i < len(positions) or j < len(tail)):
        take_index = j >= len(tail)
        if not take_index and i < len(positions):
            pos = positions[i]
            t_created = tail[j][1].encode("ascii")
            # created_at DESC, then id ASC: the exact key the SQL ORDER BY spells.
            take_index = (created[pos], -int(ids_arr[pos])) > (t_created, -int(tail[j][0]))
        if take_index:
            pos = int(positions[i])
            if consumed >= scan_offset:
                index_slots.append(len(merged_ids))
                from_index.append(pos)
                merged_ids.append(int(ids_arr[pos]))
            i += 1
        else:
            if consumed >= scan_offset:
                from_tail.append((len(merged_ids), tail[j][2]))
                merged_ids.append(int(tail[j][0]))
            j += 1
        consumed += 1
    return merged_ids, from_index, index_slots, from_tail


def _is_ascending_run(positions) -> bool:
    """Whether these positions are `a, a+1, ..., a+n-1`, in that order.

    Spelled exactly rather than cheaply: the span test (`last - first == n - 1`)
    also admits `[0, 0, 1, 3]`, and the caller uses this answer to let one slice
    of the mapped file stand in for the rows the selection names -- a predicate
    that is right about the common case and wrong about an unusual one would
    return the wrong rows rather than the slow ones. Element-wise against the
    arithmetic progression it claims to be, in numpy rather than one Python
    comparison per position: the same exact test, measured 7 ms cheaper at
    100,000 rows.

    An empty selection is not a run: the caller cannot reach it (it returns the
    empty matrix earlier), and answering True would hand back a zero-row slice
    of the file on some future path that could.
    """
    import numpy as np

    arr = np.asarray(positions)
    if arr.size == 0:
        return False
    first = int(arr[0])
    return bool(np.array_equal(arr, np.arange(first, first + arr.size)))


def _scan_offset_sql(scan_offset: int) -> tuple[str, tuple]:
    """The `OFFSET` half of a scan window's `LIMIT`, or nothing at all.

    One implementation for both scans, so the memory window and the episode
    window cannot end up counting their far region from different places.

    `OFFSET` applies where `LIMIT` applies — to the rows the statement returns,
    before the in-Python width filter — so a scan position means the same thing
    on both sides of the split: `LIMIT n OFFSET k` returns exactly the rows
    `LIMIT k + n` would have returned with its first `k` dropped, whatever widths
    they carry. A filter applied first would make the near and far regions
    overlap, or leave a gap between them, wherever a mid-flight model swap left a
    foreign-width row behind.

    Nothing is appended at offset 0, so the statement the near list issues stays
    byte-identical to the one it issued before this parameter existed.

    The price is that SQLite walks the rows the offset skips. That is what the
    index-less path already pays for its window, and a reach that makes it too
    slow is a reason to build the contiguous index — which answers the same
    question by slicing a file — rather than a reason for this path to
    approximate.
    """
    return (" OFFSET ?", (scan_offset,)) if scan_offset else ("", ())


async def _chunked_cosine_scan(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple,
    query_vec,
    query_dim: int,
    effective_min_sim: float,
    limit: int | None,
) -> list[tuple[int, int, float]]:
    """Cosine-rank a scan window without ever holding the window in memory.

    Returns the survivors as `(ordinal, id, score)` in scan order. `ordinal` is
    the row's position among the rows that passed the WIDTH FILTER, which is the
    position the tie-break has always used -- a skipped row must not advance it,
    or two corpora that differ only in a foreign-width row would break ties
    differently. `limit` is the pre-hydrate cut its caller applies; `None` keeps
    every survivor.

    The window used to be read with one `execute_fetchall`: a list holding one
    blob object per row, and then `b"".join` over that list, so the whole window
    existed twice at once. Measured at 20,000 rows x 768 dimensions, peak
    allocation was 127.3 MB against a 61.4 MB window -- 2.07x, as the two copies
    predict -- which extrapolates to about 6.1 GB at 1,000,000 rows. An 8 GB
    machine does not answer slowly there, it is killed, and the one path whose
    job is to answer when the index cannot is then not a fallback. Reading the
    cursor `VECTOR_SCAN_CHUNK_ROWS` rows at a time holds at most `2 * chunk - 1`
    rows (the lookahead below merges a short tail into the chunk before it), so
    1,023 rows at the default: 3.1 MB of embedding, and 4.8 MB of peak once
    `_cosine_batch` joins them. Measured on a window whose remainder is the
    worst case (20,991 rows x 768 dimensions): 4.8 MB against 133.6 MB for the
    old shape -- 0.08x instead of 2.07x, and flat in the size of the window.

    It is not a speed change and must not be sold as one. On that window the
    SQLite row read is 70.8% of the old shape and the join, the unpack and the
    matmul together are 12.2%; end to end the chunked form measured 24.2 ms
    against 28.1 ms, which is the same order.

    The cut is a bounded heap for the same reason. `heapq.nlargest` over every
    row that cleared the threshold would be exact but would hold a list whose
    length tracks the window -- and the fusion callers pass a deliberately
    permissive threshold, so that list is a FRACTION of the window rather than a
    constant. The heap key is `(score, -ordinal)` and it keeps the largest
    `limit` of them, which is the set `nlargest(limit, key=(score, -ordinal))`
    names: highest score first, earlier scan position wins a tie. Exact rather
    than approximate, because that key is a total order (ordinals are unique).

    On exactness, measured rather than assumed -- and the measurement did not
    say what the design expected. A row's dot product does not depend on the
    other rows in the matrix, so chunking looks like it cannot change a score.
    It can: `mat @ query_vec` is dispatched to the platform BLAS, and the BLAS
    picks its kernel from the ROW COUNT. Measured on this machine (Accelerate,
    aarch64, numpy 2.4.6) over 2,400 rows of 64 dimensions, every matrix of 64
    rows or more agrees bit for bit with the same rows scored inside the whole
    window, while every smaller one disagrees by about one ULP on a quarter
    (chunks of 1) to a half (chunks of 7) of its rows. At 768 dimensions the
    switch sits at 16 rows instead.

    So no matrix that is scored may be small, and the loop below is shaped by
    that rather than by the read:

    - A window that fits in ONE chunk is one matmul over exactly the rows the
      single-statement scan multiplied, so it is identical by construction, on
      any platform. That is the shipped shape for any corpus under 512 rows.
    - Above that, every scored matrix is `chunk` rows or `chunk + window %
      chunk`, because a short tail is carried into the chunk before it instead
      of being scored alone. Without that, `window % chunk` rows below the
      switch point moved by one ULP: measured at 2,050 rows in chunks of 512,
      one of the last two moved, and at 2,100 rows, 27 of the last 52 did. With
      it, both windows are bit-identical to the single matmul.
    - A chunk size below the switch point moves scores throughout, and a
      one-ULP move changes the ANSWER wherever the cut falls among rows that
      close. On a deliberately tie-dense corpus (2,400 rows, 218 sharing one
      vector exactly and 343 more a single ULP away, queried with that vector),
      chunks of 7 with a limit of 25 returned 24 different ids out of 25 --
      because the cut lands inside the tie group, where a last-place move is
      the whole decision. Sizes that small are a test instrument, not a
      configuration; the shipped default is 8x the larger measured switch
      point, and the merge keeps the tail there too.

    `tests/test_chunked_exact_scan.py` measures each of these on the machine it
    runs on rather than trusting this note.
    """
    survivors: list[tuple[int, int, float]] = []
    # (score, -ordinal, id): a min-heap of the best `limit` so far, so the row
    # popped is the lowest score and, among equal scores, the latest in scan
    # order -- the one `nlargest` on the same key would have dropped.
    best: list[tuple[float, int, int]] = []
    width = query_dim * 4
    # Read at call time, not at import: a chunk size baked into a default
    # argument could not be turned down by a test or by the environment.
    # max(1, ...) because a fetchmany(0) returns no rows forever, which would
    # make an empty answer look like an empty corpus.
    chunk_rows = max(1, VECTOR_SCAN_CHUNK_ROWS)
    ordinal = 0

    # `async with`, so the statement is finalised on the way out however the way
    # out is taken -- returning, raising, or being cancelled at an await inside
    # the loop. Nothing here catches: a scan that cannot read its window must
    # fail loudly rather than return a short answer that looks like a small
    # corpus.
    async with db.execute(sql, params) as cur:
        batch = await cur.fetchmany(chunk_rows)
        while batch:
            # One batch of lookahead, and the reason is the note above: no
            # matrix that is scored may be small. A window that does not divide
            # evenly ends in `window % chunk` rows, and scoring those few alone
            # is exactly the case the BLAS answers differently -- so the short
            # tail is carried into the chunk before it instead. Never more than
            # one batch ahead: reading the remainder until it is big enough
            # would be an unbounded read wearing a bound's clothing.
            pending: list = []
            if len(batch) == chunk_rows:
                pending = await cur.fetchmany(chunk_rows)
                if pending and len(pending) < chunk_rows:
                    batch = [*batch, *pending]
                    pending = []

            # Rows whose embedding is a foreign width are skipped rather than
            # reshaped: a mid-flight model swap leaves a mixed-dimension corpus
            # behind, and one stale row must not take the whole scan down.
            ids = []
            blobs = []
            for row in batch:
                blob = row[1]
                if blob and len(blob) == width:
                    ids.append(row[0])
                    blobs.append(blob)

            if ids:
                # THE existing batched unpack + matmul, per chunk. No arithmetic
                # is written here: a second implementation of the dot product is
                # what the exactness of this path is spent on.
                for row_id, sim_val in zip(ids, _cosine_batch(query_vec, query_dim, blobs)):
                    position = ordinal
                    ordinal += 1
                    if sim_val < effective_min_sim:
                        continue
                    score = float(sim_val)
                    if limit is None:
                        survivors.append((position, row_id, score))
                        continue
                    heapq.heappush(best, (score, -position, row_id))
                    if len(best) > limit:
                        heapq.heappop(best)

            batch = pending

    if limit is None:
        return survivors
    # Back into scan order, which is the order the caller's own tie-break reads.
    return sorted((-neg_position, row_id, score) for score, neg_position, row_id in best)


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
    scan_offset: int = 0,
) -> list[tuple[float, dict]]:
    """Cosine-rank `scan_limit` memory rows against the query vector.

    The rows are the newest ones: scan positions `[scan_offset, scan_offset +
    scan_limit)` in the scan's own order (`created_at` DESC, `id` ASC). The near
    list passes `scan_offset=0` and reads the newest `scan_limit` rows, which is
    every call this function had before the far list existed; the far list passes
    the near window's width, which is what makes the two regions disjoint by
    construction rather than by a de-duplicating pass afterwards
    (`docs/SCAN_WINDOW_REACH_DESIGN.md`).

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
        scan_offset=scan_offset,
    )
    if supplied is not None:
        valid_ids, mat = supplied
        if not valid_ids:
            return []
        sims = _cosine_matrix(query_vec, mat)
        # Survivors keep the scan's order (created_at DESC): heapq.nlargest in
        # _search_vector is stable, so this order is what breaks a tie between two
        # equally-similar rows, and nothing below may reorder them.
        survivors = [
            (valid_ids[i], float(sim_val))
            for i, sim_val in enumerate(sims)
            if sim_val >= effective_min_sim
        ]
        if limit < len(survivors):
            # The stable top-`limit`: score first, scan position as the tie-break, which
            # is the order `sorted(..., reverse=True)` -- and therefore `nlargest` --
            # would have produced. Re-sorted back into scan order so the ties the caller
            # breaks are the ties it broke before.
            keep = heapq.nlargest(
                limit, range(len(survivors)), key=lambda i: (survivors[i][1], -i)
            )
            survivors = [survivors[i] for i in sorted(keep)]
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
        #
        # Read in chunks, and the threshold and the top-`limit` cut applied as the
        # chunks arrive: the statement is the same one, but the window no longer
        # exists in memory all at once (see _chunked_cosine_scan). The survivors it
        # returns are already thresholded, already cut, and already back in scan
        # order, which is the same list the lines above build for the index path.
        #
        # The far list moves the window down the same statement instead of
        # widening it (empty at offset 0, see _scan_offset_sql), so the chunked
        # read keeps bounding the peak by the chunk rather than by the reach.
        offset_clause, offset_params = _scan_offset_sql(scan_offset)
        survivors = [
            (row_id, score)
            for _, row_id, score in await _chunked_cosine_scan(
                db,
                f"""SELECT id, embedding
               FROM memories
               WHERE {iso.clause} AND embedding IS NOT NULL{src_clause}
               ORDER BY created_at DESC, id ASC
               LIMIT ?{offset_clause}""",
                (*iso.params, *src_params, scan_limit, *offset_params),
                query_vec,
                query_dim,
                effective_min_sim,
                limit,
            )
        ]

    if not survivors:
        return []

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
    *,
    limit: int | None = None,
    agent_id: str = "",
    project_id: str | None = None,
    scan_offset: int = 0,
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

    Two phases, like the memory scan (bug-249): phase 1 ranks `(id, embedding)`
    over the window and phase 2 hydrates the summary only for the rows that
    cleared the threshold and the top-`limit` cut. The split is what lets the
    contiguous index supply phase 1 here as it does for memories — the episode
    table was measured to cost more per query, unindexed, than the indexed
    memory table five times its size (benchmarks/measurements/
    results-recall-path-profile.md). The source rule above is applied before
    either phase, so the index is never asked about a column episodes lack.

    `limit` bounds the hydrate by the same argument as for memories: the caller
    takes `heapq.nlargest(limit, ...)` over memories and episodes together, and
    an episode with `limit` episodes ranked above it cannot place whatever the
    memories do. `None` keeps every survivor (the pre-split behaviour); the
    caller passes its response limit.

    `scan_offset` moves this window down the scan order exactly as it does for
    memories: the two tables are scanned under the same window today, so they are
    split at the same position when the far list exists. An episode table smaller
    than the near window simply has no far region, which is the empty answer and
    not a special case.
    """
    if src_like and not channel:
        return []

    supplied = await _index_phase1(
        db,
        agent_id=agent_id,
        project_id=project_id,
        channel=channel,
        source_id="",
        scan_limit=scan_limit,
        query_dim=query_dim,
        table="episodes",
        scan_offset=scan_offset,
    )
    if supplied is not None:
        valid_ids, mat = supplied
        if not valid_ids:
            return []
        ep_sims = _cosine_matrix(query_vec, mat)
        # Survivors keep the scan's order, for the same reason as in the memory
        # scan: the caller's nlargest is stable, and this order is its tie-break.
        survivors = [
            (valid_ids[i], float(sim_val))
            for i, sim_val in enumerate(ep_sims)
            if sim_val >= effective_min_sim
        ]
        if limit is not None and limit < len(survivors):
            keep = heapq.nlargest(
                limit, range(len(survivors)), key=lambda i: (survivors[i][1], -i)
            )
            survivors = [survivors[i] for i in sorted(keep)]
    else:
        # The same chunked read as the memory scan, and the same reason for it:
        # this window is materialised one chunk at a time rather than whole.
        # `limit` is `None` here when the caller wants every survivor, which the
        # helper honours by skipping the cut entirely. The offset is the far
        # list's, and episodes are split at the same position memories are.
        offset_clause, offset_params = _scan_offset_sql(scan_offset)
        survivors = [
            (row_id, score)
            for _, row_id, score in await _chunked_cosine_scan(
                db,
                f"""SELECT id, embedding
               FROM episodes
               WHERE {iso.clause} AND embedding IS NOT NULL
               ORDER BY created_at DESC, id ASC
               LIMIT ?{offset_clause}""",
                (*iso.params, scan_limit, *offset_params),
                query_vec,
                query_dim,
                effective_min_sim,
                limit,
            )
        ]

    if not survivors:
        return []

    payload = await _fetch_rows_by_id(
        db,
        f"SELECT id, summary, start_time, resolved, created_at FROM episodes WHERE id IN ({{ph}})"
        f"{iso.and_clause}",
        [ep_id for ep_id, _ in survivors],
        tuple(iso.params),
    )

    candidates: list[tuple[float, dict]] = []
    for ep_id, sim in survivors:
        row = payload.get(ep_id)
        if row is None:
            continue
        _, summary, start_time, ep_resolved, ep_created_at = row
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


async def _search_vector_far(
    db: aiosqlite.Connection,
    *,
    iso: IsolationFilter,
    src_clause: str,
    src_params: tuple,
    src_like: str,
    limit: int,
    query_vec,
    query_dim: int,
    effective_min_sim: float,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
) -> list[dict]:
    """The far list: the top rows among scan positions `[N, REACH)`.

    Everything the near list does, one window further down the scan order —
    the same suppliers, the same threshold, the same stable top-k cut, the
    same memories-then-episodes merge — so a row's place on this list is decided
    the way its place on the other one would have been. What it is NOT is a
    re-ranking or a re-weighting: a far row is returned with its cosine, exactly
    as a near row is (`docs/SCAN_WINDOW_REACH_DESIGN.md` §5).

    How many rows that is, is `limit` unless `CPERSONA_VECTOR_FAR_LIMIT` asks for
    fewer. Shortening this list is a candidate-count bound — which far rows reach
    the fusion — and leaves every row that does reach it scored as before.

    The two regions are disjoint by position, so no row can appear on both lists
    and no row can be counted twice by the fusion that receives them.

    Takes the prepared query vector rather than a query string: it is a second
    list, not a second search, and embedding the query again would put a network
    call and a health observation on the recall path for a vector the caller is
    already holding.
    """
    if not far_list_enabled():
        # The function's own contract for a direct caller. `_search_vector` asks
        # the same predicate before it calls at all, so at the default this frame
        # is not even entered — see the note there on why an empty scan is not an
        # acceptable way for this setting to be switched off.
        return []

    # The near window's width IS the offset: the far region starts where the near
    # one ends, which is what makes the two disjoint without a de-duplicating
    # pass over the results.
    scan_offset = MAX_MEMORIES
    scan_limit = VECTOR_REACH - MAX_MEMORIES

    # How long this list is allowed to be. `0` means the response `limit`, which
    # is the list this function has always produced; a positive value shortens it
    # and can never lengthen it, so `min` is the whole rule. Read from the module
    # global at call time for the same reason `far_list_enabled` reads its two:
    # a value closed over at import could not be turned down by a test or by the
    # environment.
    far_limit = min(limit, VECTOR_FAR_LIMIT) if VECTOR_FAR_LIMIT else limit

    # Memories first, then episodes, and `nlargest` over both — the same merge
    # and the same tie-break the near list is built with. The per-table cut is
    # the same number as the final one: a table cannot contribute more rows than
    # the merged list can hold, and cutting each table at the response `limit`
    # while the merge cuts at `far_limit` would read and rank rows that cannot
    # place. The near list's cut is untouched; this is the far list's length.
    candidates = await _scan_memories_local(
        db, iso, src_clause, src_params, scan_limit, far_limit, query_vec, query_dim,
        effective_min_sim,
        agent_id=agent_id, project_id=project_id, channel=channel, source_id=source_id,
        scan_offset=scan_offset,
    )
    candidates += await _scan_episodes_local(
        db, iso, scan_limit, query_vec, query_dim, effective_min_sim, src_like, channel,
        limit=far_limit, agent_id=agent_id, project_id=project_id, scan_offset=scan_offset,
    )

    top_k = heapq.nlargest(far_limit, candidates, key=lambda x: x[0])
    return [c[1] for c in top_k]


async def _search_vector(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_similarity: float | None = None,
    channel: str = "",
    project_id: str | None = None,
    source_id: str = "",
    *,
    far_out: list[dict] | None = None,
) -> list[dict]:
    """Search memories and episodes using vector cosine similarity.

    Returns the vector arm's ranked list — the NEAR list, the newest
    `MAX_MEMORIES` rows by scan position, which is the only list this returned
    before `CPERSONA_VECTOR_REACH` existed and is bit-identical to it at the
    default.

    `far_out`, when a caller passes a list and the reach is set above the scan
    window, receives the FAR list: the rows at scan positions
    `[MAX_MEMORIES, VECTOR_REACH)`, ranked the same way, for the caller to fuse
    as one more ranked list (`docs/SCAN_WINDOW_REACH_DESIGN.md`). It stays empty
    otherwise, and a caller that passes nothing pays for nothing.

    An out-parameter rather than a `(near, far)` return, for two reasons that
    both outlive the taste question. The query must be embedded ONCE: a separate
    far entry point would either embed the same text again — a network call, and
    a health observation the recall did not make — or lean on a client-side cache
    whose lifetime nothing here controls. And the vector arm is ONE retriever
    making one call, which is what the pipeline documents and what a test pins by
    counting calls; two lists out of two calls would read as a fourth channel
    feeding the fusion, which is exactly what this is not (the far rows are
    disjoint from the near ones, so no row gains a second vote).

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
        # The service answered, and its answer IS the result. The reach applies
        # to the local scan only — the remote service ranks under its own window
        # — so `far_out` is left as the caller handed it over: empty.
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
        health.observe_failure(
            outcome.error or "the embedding call produced no usable vector",
            # bug-275: the success side below has asked this since bug-248; the
            # failure side believed a dead endpoint without asking. A call that
            # never left the process is evidence about configuration, not reach.
            attempted=outcome.attempted,
        )
        return []
    if outcome.attempted:
        # bug-248: only a call that actually reached the backend is an observation of it.
        # A repeated single-text query is answered from the client's process-local TTL LRU
        # cache without a request leaving the process (attempted=False, ok=True), and
        # re-arming on that cleared a latched fault, dropped the evidence a user was about
        # to be shown, and zeroed the two-strike debounce — so the next genuine failure had
        # to climb it again before the advisory could fire. A dead backend read as healthy
        # for as long as the query repeated within the cache TTL.
        #
        # Leaving the state untouched (rather than treating a cache hit as a failure) is
        # what a cache hit actually licenses: it says nothing about the backend either way,
        # so the last real observation remains the most recent thing known about it.
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
        db, iso, scan_limit, query_vec, query_dim, effective_min_sim, src_like, channel,
        limit=limit, agent_id=agent_id, project_id=project_id,
    )

    top_k = heapq.nlargest(limit, candidates, key=lambda x: x[0])

    # The far list, and the one place "off" is decided on this path. Asked here
    # rather than only inside the call, so that at the default there is no call
    # at all: a far scan that ran and returned nothing would still cost a
    # statement, a matrix and a merge on every recall the server answers, and
    # this setting has to be a guard rather than an empty scan.
    if far_out is not None and far_list_enabled():
        far_out.extend(await _search_vector_far(
            db,
            iso=iso,
            src_clause=src_clause,
            src_params=src_params,
            src_like=src_like,
            limit=limit,
            query_vec=query_vec,
            query_dim=query_dim,
            effective_min_sim=effective_min_sim,
            agent_id=agent_id,
            project_id=project_id,
            channel=channel,
            source_id=source_id,
        ))

    return [c[1] for c in top_k]
