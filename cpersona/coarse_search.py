"""Coarse candidate supply — the supplier contract of `docs/BINARY_COARSE_SEARCH_DESIGN.md` §4.

One question, asked by the far seats (§5) and by the cue arm's remainder (§6):

    Within these axes, these scan positions and, optionally, this period, which
    `k` records have the smallest Hamming distance to the query's bits?

The answer is `Candidates`: record ids with their distances and scan positions,
ordered by distance and then by scan position. No text and no metadata. A scan
position is a row's place in the scan the near window reads — the embedded
records the axes (and the period, when one is given) admit, `created_at` DESC
then `id` ASC — counted from 0, so `start = CPERSONA_MAX_MEMORIES` is the first
row past the window.

Two suppliers run one algorithm:

- **the coarse index** (`coarse_index`), with the rows written since its build
  and the rows it names read live and merged into its order;
- **the live store**, when the index is absent, unusable, of another dimension,
  or in a state it cannot answer for exactly: the stored float32 vectors read in
  scan order, in bounded chunks, quantised by the same function.

Both feed the same selection (`_Best`), and quantising a stored vector is
deterministic, so they return the same `Candidates` for the same store: building
or deleting the index changes what this costs, never what it returns. The index
falls back to the live store under the conditions the contiguous index falls
back under — a row it holds whose embedding was cleared since the build, a row
it must read live that carries another width — and, as the contiguous index does
for its survivors (bug-315), when a candidate it would return no longer exists.
Like the contiguous index it does not see a row deleted or retagged since the
build that is not among its candidates, which can move a range boundary by that
row; a rebuild removes the difference.

Nothing here decides who may see a row. `isolation_where()` stays the authority:
the index narrows by the same axis codes the contiguous index uses
(`vector_index.select`), and the caller hydrates candidates by id with the
isolation and source predicates re-applied.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import numpy as np

from cpersona import blocks
from cpersona import coarse_index
from cpersona import vector
from cpersona import vector_index
from cpersona.config import VECTOR_SCAN_CHUNK_ROWS
from cpersona.isolation import isolation_where, source_id_where
from cpersona.vector_index import IndexUnusable

logger = logging.getLogger(__name__)

#: Candidates the Hamming pass keeps: server policy, not a setting, like the far
#: seats. Set by the measurement of design §9 — the smallest value on its grid whose
#: seats agree with an exact scan's at least 95% of the time at every store size and
#: regime measured, up to 1,000,000 records
#: (benchmarks/measurements/results-binary-coarse-search.md). A test holds it to the
#: decision recorded there.
CANDIDATES = 256

#: Index rows whose bits are gathered at once. It bounds the copy a scattered
#: selection makes out of the mapped file (128 bytes a row at 1,024 dimensions,
#: 8 MB here) and changes no result.
_GATHER_ROWS = 65536


@dataclass(frozen=True)
class Candidates:
    """The supplier's answer, best first: smallest distance, then earliest scan position."""

    ids: tuple[int, ...]
    distances: tuple[int, ...]
    positions: tuple[int, ...]
    #: "index" or "live": which supplier answered. Reported, never part of the answer.
    source: str

    def rows(self) -> list[tuple[int, int, int]]:
        return list(zip(self.ids, self.distances, self.positions))


def _top(ids: np.ndarray, distances: np.ndarray, positions: np.ndarray, k: int, dim: int):
    """The best `k` rows by (distance, position), selected by counting, in that order.

    A distance is an integer from 0 to `dim`, so one histogram finds the cut-off
    distance: every row below it is kept, and at the cut-off the rows with the
    smallest positions fill what is left. Positions are unique, so the result
    is a total order and does not depend on the order the rows arrived in. Only
    the at most `k` survivors are sorted, for the order of the answer.
    """
    if len(distances) > k:
        cumulative = np.cumsum(np.bincount(distances, minlength=dim + 1))
        cut = int(np.searchsorted(cumulative, k))  # the first distance at which k rows are reached
        below = np.flatnonzero(distances < cut)
        at = np.flatnonzero(distances == cut)
        need = k - len(below)
        if need < len(at):
            at = at[np.argpartition(positions[at], need - 1)[:need]] if need > 0 else at[:0]
        keep = np.concatenate([below, at])
    else:
        keep = np.arange(len(distances))
    order = keep[np.lexsort((positions[keep], distances[keep]))]
    return ids[order], distances[order], positions[order]


class _Best:
    """The running best `k`, fed one chunk at a time by either supplier."""

    def __init__(self, k: int, dim: int):
        self.k, self.dim = k, dim
        self.ids = np.empty(0, dtype=np.int64)
        self.distances = np.empty(0, dtype=np.int64)
        self.positions = np.empty(0, dtype=np.int64)

    def add(self, ids, distances, positions) -> None:
        if len(ids) == 0:
            return
        self.ids, self.distances, self.positions = _top(
            np.concatenate([self.ids, np.asarray(ids, dtype=np.int64)]),
            np.concatenate([self.distances, np.asarray(distances, dtype=np.int64)]),
            np.concatenate([self.positions, np.asarray(positions, dtype=np.int64)]),
            self.k,
            self.dim,
        )

    def answer(self, source: str) -> Candidates:
        return Candidates(
            ids=tuple(int(i) for i in self.ids),
            distances=tuple(int(d) for d in self.distances),
            positions=tuple(int(p) for p in self.positions),
            source=source,
        )


def _empty(source: str) -> Candidates:
    return Candidates((), (), (), source)


async def coarse_candidates(
    db,
    query_vec,
    *,
    agent_id: str,
    project_id: str | None = None,
    channel: str = "",
    source_id: str = "",
    start: int = 0,
    end: int | None = None,
    period: tuple | None = None,
    k: int = CANDIDATES,
    path: str | None = None,
) -> Candidates:
    """The `k` records nearest the query's bits at scan positions `[start, end)`.

    `period` is `(start, end)` as the cue arm binds it (`cue.sql_instant`
    strings); the records it holds are those `coarse_index.PERIOD_PREDICATE`
    admits, and positions are counted among them. `end=None` runs to the end of
    the store. `path` names an index file other than the default, for tests.
    """
    if start < 0:
        raise ValueError(f"start must be a scan position, got {start}")
    query = np.asarray(query_vec, dtype=np.float32)
    dim = int(query.shape[-1])
    if k <= 0 or (end is not None and end <= start):
        return _empty("live")
    query_bits = coarse_index.sign_bits(query[None, :])[0]
    scope = dict(agent_id=agent_id, project_id=project_id, channel=channel, source_id=source_id,
                 start=start, end=end, period=period)
    found = await _from_index(db, query_bits, dim, k, path, **scope)
    if found is not None:
        return found
    return await _from_live(db, query_bits, dim, k, **scope)


async def _from_live(db, query_bits, dim, k, *, agent_id, project_id, channel, source_id, start, end, period):
    """The live store: the scan read in order, quantised a chunk at a time.

    A row of another width occupies its scan position and is skipped, as the near
    scan applies its window before it skips such rows (invariant 6): the range is
    a range of the scan, whatever the rows in it can be compared with.
    """
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src = source_id_where(source_id)
    period_clause, period_params = ("", ()) if period is None else (f" AND {coarse_index.PERIOD_PREDICATE}", tuple(period))
    limit = -1 if end is None else end - start
    best = _Best(k, dim)
    width = dim * 4
    position = start
    async with db.execute(
        f"""SELECT id, embedding FROM memories
           WHERE {iso.clause} AND embedding IS NOT NULL{src.and_clause}{period_clause}
           ORDER BY created_at DESC, id ASC
           LIMIT ? OFFSET ?""",
        (*iso.params, *src.params, *period_params, limit, start),
    ) as cursor:
        while True:
            rows = await cursor.fetchmany(max(1, VECTOR_SCAN_CHUNK_ROWS))
            if not rows:
                break
            ids, blobs, positions = [], [], []
            for row_id, blob in rows:
                if len(blob) == width:
                    ids.append(row_id)
                    blobs.append(blob)
                    positions.append(position)
                position += 1
            if blobs:
                matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), dim)
                best.add(ids, blocks.hamming_matrix(coarse_index.sign_bits(matrix), query_bits), positions)
    return best.answer("live")


async def _from_index(db, query_bits, dim, k, path, *, agent_id, project_id, channel, source_id, start, end, period):
    """The coarse index, or None for the live store to answer.

    None is the ordinary answer for no index or another dimension, and the
    deliberate one for every state the index cannot answer for exactly. Any
    exception is a fallback too, for the reason the contiguous index gives: the
    live supplier is always correct, so a broken derived file costs time and
    nothing else.
    """
    try:
        index = coarse_index.cached_coarse_index("memories", path)
        if index is None or index.dim != dim:
            return None
        selected = vector_index.select(
            index, agent_id=agent_id, project_id=project_id, channel=channel, source_id=source_id, limit=0,
        )
    except IndexUnusable as exc:
        logger.warning("Coarse index unusable, falling back to the live store: %s", exc)
        return None
    except Exception:  # noqa: BLE001 — fail open, deliberately
        logger.warning("Coarse index raised, falling back to the live store", exc_info=True)
        return None

    try:
        if period is not None:
            bounds = await db.execute_fetchall("SELECT datetime(?), datetime(?)", tuple(period))
            low, high = bounds[0]
            if low is None or high is None:
                # SQL compares against NULL and admits nothing; so does this.
                return _empty("index")
            stamps = index.timestamp[selected]
            selected = selected[(stamps >= low.encode("ascii")) & (stamps < high.encode("ascii"))]

        if await _lost_since_build(db, index, index.ids[selected], agent_id):
            logger.warning(
                "Coarse index holds rows whose embedding was cleared since the build; "
                "falling back to the live store until it is rebuilt"
            )
            return None

        tail = await vector._index_tail_rows(
            db, index, agent_id=agent_id, project_id=project_id, channel=channel,
            source_id=source_id, scan_limit=-1, table="memories", window=period,
        )
        if tail is None:
            return None

        found = _rank(index, selected, tail, query_bits, dim, k, start, end)
        if not await _all_exist(db, list(found.ids)):
            logger.warning(
                "Coarse index offered a record deleted since the build; "
                "falling back to the live store until it is rebuilt"
            )
            return None
        return found
    except Exception:  # noqa: BLE001 — fail open, deliberately
        logger.warning("Coarse index read raised, falling back to the live store", exc_info=True)
        return None


def _rank(index, selected, tail, query_bits, dim, k, start, end) -> Candidates:
    """Merge the index rows and the live tail into scan order, cut the range, keep the best `k`.

    The tail is interleaved, not prepended: an imported record keeps its own
    `created_at` under a fresh id, so a row written after the build can sort
    anywhere (`vector._merge_index_and_tail`). Each tail row's place is found by
    a binary search over the index rows on the key the SQL orders by, so the
    merge costs a search per tail row rather than a comparison per index row.
    """
    n, t = len(selected), len(tail)
    if t:
        inserted = np.array([_insertion(index, selected, row) for row in tail], dtype=np.int64)
        index_place = np.arange(n, dtype=np.int64) + np.searchsorted(inserted, np.arange(n), side="right")
        tail_place = inserted + np.arange(t, dtype=np.int64)
    else:
        index_place = np.arange(n, dtype=np.int64)
        tail_place = np.empty(0, dtype=np.int64)
    stop = n + t if end is None else end

    best = _Best(k, dim)
    take = np.flatnonzero((index_place >= start) & (index_place < stop))
    for lo in range(0, len(take), _GATHER_ROWS):
        part = take[lo : lo + _GATHER_ROWS]
        positions = selected[part]
        best.add(index.ids[positions], blocks.hamming_matrix(index.bits[positions], query_bits), index_place[part])

    in_range = [j for j in range(t) if start <= tail_place[j] < stop]
    if in_range:
        matrix = np.frombuffer(b"".join(tail[j][2] for j in in_range), dtype="<f4").reshape(len(in_range), dim)
        best.add(
            [tail[j][0] for j in in_range],
            blocks.hamming_matrix(coarse_index.sign_bits(matrix), query_bits),
            tail_place[in_range],
        )
    return best.answer("index")


def _insertion(index, selected, row) -> int:
    """How many of the selected index rows come before the tail row `row` in scan order."""
    # The key the SQL orders by, as `vector._interleave_index_and_tail` spells it:
    # created_at DESC then id ASC, the tail's text encoded as SQLite's BINARY
    # collation compares it (bug-316: a tail row may not be canonical).
    key = ((row[1] or "").encode("utf-8", "replace"), -int(row[0]))
    lo, hi = 0, len(selected)
    while lo < hi:
        mid = (lo + hi) // 2
        position = selected[mid]
        if (bytes(index.created_at[position]), -int(index.ids[position])) > key:
            lo = mid + 1
        else:
            hi = mid
    return lo


async def _lost_since_build(db, index, selected_ids, agent_id: str) -> bool:
    """Whether a row the index holds for this selection has lost its embedding since the build.

    Such a row has left the scan (every scan reads `embedding IS NOT NULL`), so
    every row after it has moved up a position the index still counts. One
    statement over the partial index of rows without an embedding
    (`idx_memories_lost_embedding`) — the rows that never had one are the build's
    named holes, are not in the file, and so cannot match. `+agent_id` keeps the
    planner on that index, as in `vector._index_rows_lost_embedding`.
    """
    rows = await db.execute_fetchall(
        "SELECT id FROM memories WHERE embedding IS NULL AND id <= ? AND +agent_id = ?",
        (index.watermark, agent_id),
    )
    if not rows:
        return False
    lost = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    return bool(np.isin(np.asarray(selected_ids, dtype=np.int64), lost).any())


async def _all_exist(db, ids: list[int]) -> bool:
    """Whether every candidate the index would return is still a stored row."""
    if not ids:
        return True
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    )
    return int(rows[0][0]) == len(ids)
