"""The propagation seat (2.6): one more row, found by following the answer's first row.

A recall returns its window -- the rows the gate, autocut and the prior admitted,
cut to `limit`. When a question needs two records, the second is often admitted
but ranked just below the cut. The propagation seat is one held place for it:
the same recall is also ranked at a deeper Recall Depth, the rows the window
did not already return are the candidates, and the candidate that best combines
its place in that deeper fused order with its closeness to the window's first
row takes the seat.

    score(c) = (1 - COSINE_WEIGHT) / (RANK_OFFSET + fused_rank(c))
               + COSINE_WEIGHT / (RANK_OFFSET + cosine_rank(c))

`fused_rank` is the 1-based position among the candidates in the deeper order;
`cosine_rank` is the 1-based position by cosine to the window's first row. Ties
keep the deeper order (every sort is stable). A candidate with no usable vector
ranks after every candidate that has one.

Like the time cue's seat, it displaces nothing: the window keeps every row and
every place it had, and the seat is added after it. Off by default
(CPERSONA_RECALL_PROPAGATION_SEAT), and with it off nothing here runs.

Every value a measurement chose -- the depth, the weight, the offset, the number
of seats -- belongs to the policy version, `POLICY`, which a traced recall
records. They were measured on a real-use memory pack of two-record questions,
first under rrf and then under rsf with confidence (the production setting),
and are not re-tuned here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

POLICY = "propagation-v1"

# How deep the second ranking digs, per arm. The window keeps its own depth.
DEPTH = 100
# The weight of closeness to the window's first row; the fused order has the rest.
COSINE_WEIGHT = 0.35
# The reciprocal-rank offset both terms use.
RANK_OFFSET = 60
# Places held for the seat.
SEATS = 1


def unit(blob: bytes | None, dim: int | None = None) -> np.ndarray | None:
    """A stored float32 vector as a unit float64 vector, or None when it cannot be one."""
    if not blob:
        return None
    raw = np.frombuffer(blob, dtype=np.float32) if len(blob) % 4 == 0 else None
    if raw is None or raw.size == 0 or (dim is not None and raw.size != dim):
        return None
    vec = raw.astype(np.float64)
    if not np.isfinite(vec).all():
        return None
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else None


def order(
    candidates: Sequence[dict],
    anchor: np.ndarray,
    vectors: Mapping[int, np.ndarray],
) -> list[int]:
    """Candidate positions, best first. `vectors` maps a position to its unit vector.

    Positions are the candidates' places in the deeper fused order (0-based), so
    the fused rank of position i is i + 1.
    """
    n = len(candidates)
    if n == 0:
        return []
    sims = np.full(n, -np.inf)
    for i, vec in vectors.items():
        sims[i] = float(vec @ anchor)
    by_cosine = np.argsort(-sims, kind="stable")
    cosine_rank = np.empty(n, dtype=np.int64)
    cosine_rank[by_cosine] = np.arange(1, n + 1)
    fused_rank = np.arange(1, n + 1)
    score = (1 - COSINE_WEIGHT) / (RANK_OFFSET + fused_rank) + COSINE_WEIGHT / (RANK_OFFSET + cosine_rank)
    return [int(i) for i in np.argsort(-score, kind="stable")]


def ranks(candidates: Sequence[dict], anchor: np.ndarray, vectors: Mapping[int, np.ndarray]) -> dict[int, int]:
    """Position -> 1-based cosine rank, for the response to say why a seat was taken."""
    n = len(candidates)
    sims = np.full(n, -np.inf)
    for i, vec in vectors.items():
        sims[i] = float(vec @ anchor)
    by_cosine = np.argsort(-sims, kind="stable")
    return {int(i): r + 1 for r, i in enumerate(by_cosine)}
