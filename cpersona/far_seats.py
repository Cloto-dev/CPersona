"""Far seats — `docs/BINARY_COARSE_SEARCH_DESIGN.md` §5.

Two places held after the block reservation for records past the scan window that
the coarse search reaches by meaning. A record there cannot be ranked by the vector
arm at all; this lets one into the answer without a vote in the fusion, so it
displaces nothing the fusion returned.

**Where it looks.** Scan positions from `max(MAX_MEMORIES, VECTOR_REACH)` to the end
of the store: the records no vector list of the recall ranked. The window's own rows
are never searched here — a near record the vector arm ranked low has been judged by
the arm whose job that is.

**How it ranks.** The coarse supplier (`coarse_search`) returns `K'` candidates by
Hamming distance. Their stored float32 vectors are read by id with the isolation and
source predicates re-applied, fail-closed, and ranked by the cosine the near scan
ranks by (`vector._cosine_batch`, the one matmul). A candidate below the vector arm's
own similarity floor is dropped: a far record is a whole record, the population that
floor was set for (decision F). Equal cosines go by scan position.

**How it is admitted** is recall's business (`memory_handlers`): after the block
reservation, the best ranked records the answer does not already hold take the
places, except one an ordinary arm reached and the gate or autocut refused. Only the
seated rows' payloads are read (invariant 5): `ranked` reads ids and vectors,
`seat_rows` reads the text of the few it seats.

Not on the remote vector path, which produces no query vector to quantise, and not
for episodes (design §11).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cpersona import coarse_index
from cpersona import coarse_search
from cpersona import config
from cpersona import vector
from cpersona.isolation import isolation_where, source_id_where

#: Fixed at the size of the block reservation (decision D): an upper bound on what a
#: bad far hit can cost, not a tuned number. Changing it needs a measurement with a
#: reader.
SEATS = 2


def enabled() -> bool:
    return config.FAR_SEATS_ENABLED


def scan_start() -> int:
    """The first scan position no vector list of the recall ranked.

    Read from the vector module, whose values the near scan and the far list use, so
    the far seats begin exactly where the vector arm stopped.
    """
    return max(vector.MAX_MEMORIES, vector.VECTOR_REACH)


@dataclass(frozen=True)
class FarHit:
    id: int
    cosine: float
    position: int


def _admitted(agent_id, project_id, channel, source_id) -> tuple[str, tuple]:
    """The authority's predicate, as both reads by id apply it: isolation and the source prefix."""
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src = source_id_where(source_id)
    return f"{iso.and_clause}{src.and_clause}", (*iso.params, *src.params)


async def ranked(db, query_vec, *, agent_id, project_id, channel, source_id, floor: float,
                 start: int | None = None, period: tuple | None = None) -> list[FarHit]:
    """The far records by cosine, best first, at or above `floor`. Ids and vectors only.

    By default the far seats' question: every record past the vector lists, any time.
    `start` and `period` ask it of a time cue's period from the cue's own cap instead
    (the cue arm's remainder, design §6); the period is re-applied when the
    candidates are read by id, as the authority's predicate is.
    """
    query = np.asarray(query_vec, dtype=np.float32)
    dim = int(query.shape[-1])
    found = await coarse_search.coarse_candidates(
        db, query, agent_id=agent_id, project_id=project_id, channel=channel,
        source_id=source_id, start=scan_start() if start is None else start, period=period,
        k=coarse_search.CANDIDATES,
    )
    if not found.ids:
        return []
    clause, params = _admitted(agent_id, project_id, channel, source_id)
    if period is not None:
        clause, params = f"{clause} AND {coarse_index.PERIOD_PREDICATE}", (*params, *period)
    stored = await vector._fetch_rows_by_id(
        db,
        f"SELECT id, embedding FROM memories WHERE id IN ({{ph}}) AND embedding IS NOT NULL{clause}",
        list(found.ids),
        params,
    )
    # A candidate the authority does not admit, or one whose vector changed width or
    # was cleared between the two statements, is dropped here: fail-closed. So is one
    # whose vector another model wrote, under reject.
    rejected = await vector.rejected_rids(db, [("mem", row_id) for row_id in stored])
    kept = [
        (row_id, position)
        for row_id, position in zip(found.ids, found.positions)
        if row_id in stored
        and stored[row_id][1] is not None
        and len(stored[row_id][1]) == dim * 4
        and ("mem", row_id) not in rejected
    ]
    if not kept:
        return []
    cosines = vector._cosine_batch(query, dim, [stored[row_id][1] for row_id, _ in kept])
    hits = [
        FarHit(row_id, float(cosine), position)
        for (row_id, position), cosine in zip(kept, cosines)
        if cosine >= floor
    ]
    hits.sort(key=lambda h: (-h.cosine, h.position))
    return hits


async def seat_rows(db, hits: list[FarHit], *, agent_id, project_id, channel, source_id, excluded) -> list[dict]:
    """The rows for at most `SEATS` of `hits`, in their order, read only as far as needed.

    `excluded(content)` is the caller's content exclusion; a row it rejects, or one
    deleted or moved out of scope since `ranked` read it, does not take a place and
    the next hit is read instead.
    """
    clause, params = _admitted(agent_id, project_id, channel, source_id)
    out: list[dict] = []
    for start in range(0, len(hits), SEATS):
        if len(out) >= SEATS:
            break
        chunk = hits[start : start + SEATS]
        rows = await vector._fetch_rows_by_id(
            db,
            f"SELECT id, msg_id, content, source, timestamp FROM memories WHERE id IN ({{ph}}){clause}",
            [h.id for h in chunk],
            params,
        )
        for hit in chunk:
            if len(out) >= SEATS:
                break
            row = rows.get(hit.id)
            if row is None or excluded(row[2] or ""):
                continue
            out.append({
                "id": row[0],
                "msg_id": row[1],
                "content": row[2],
                "source": row[3],
                "timestamp": row[4],
                "_rid": ("mem", row[0]),
                "_far_seat": True,
                "_far_cosine": hit.cosine,
            })
    return out
