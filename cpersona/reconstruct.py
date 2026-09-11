"""Reconstructive Recall — the exit (docs/RELIABLE_RECALL_2_6.md section 7).

``recall`` returns candidate rows. This returns **recall items**: units of memory
assembled from those candidates, each traceable to the canonical rows that
support it. "Reconstruct" means *select, order and assign roles* — never
compose. The server does not write a sentence it did not store, and it calls no
model: ``content`` is a quotation of a stored row.

It is a separate tool rather than a mode of ``recall``, which keeps the recall
contract untouched (invariant 6) and avoids two knobs with different meanings on
one response.

Processing is four stages, all SQL and pure functions:

1. *Candidates* — the pool the recall process produced, unchanged. Its depth is
   the section 4 knob (``top_k``), never derived from ``count``.
2. *Bundling* — cluster candidates by deterministic keys (below).
3. *Bounded relation walk* — the identity here: there is no relation table to
   walk yet, and neither is the overflow chain that will feed it. The hop bound
   is declared and reported anyway, so the contract does not change when the
   stage stops being the identity.
4. *Structuring* — order by time and by version; where two rows on the same
   subject cannot be ordered, keep both and mark the conflict. No summarising,
   no merging of text.

Two readings of the design doc are decided here, because the draft left them
open. Both are stated in the tool description as well, so a caller does not have
to read this file to know what it got:

* **Role direction.** ``roles[].role`` names what the *referenced row is to this
  claim* — the ref is the subject, the claim is the object: the referenced
  episode *supports* this claim; the referenced newer row *supersedes* it. This
  is the only direction under which all six role words read the same way round
  (``temporal_predecessor`` only makes sense as a property of the ref), and the
  design's example was ambiguous between the two.
* **"adjacent timestamps, same source" is ONE key, not two.** Source alone
  cannot bundle: in a single-agent store ``source.id`` is constant, so a source
  key would fold the entire candidate pool into one item on every call.

Invariants this module holds (section 7, numbered as the doc numbers them):

1. Stored memories are never modified — this is a read path. The only writes are
   the recall counters ``do_recall`` already bumps.
2. No model is called. Embedding is allowed (the recall arms use it); generation
   is not. ``content`` is a quotation.
3. Determinism — same database state, same query, same bounds, same output. Ties
   are broken by a total order that is written down (``_order_key``).
4. Boundedness — nothing is scanned past the declared bounds; a cut is reported
   in ``bounds.truncated``.
5. Explainability — every element says why it is present (``evidence[].why``,
   ``independence_reason``, ``count_policy``, ``shortfall_reason``).
6. The existing ``recall`` contract is untouched.
7. Count and breadth are decoupled — none of the breadth bounds may be derived
   from ``count``. The test is ``test_count_alone_does_not_move_the_pool``.
8. No padding — one cluster is one item. A shortfall is never filled by splitting
   a cluster, repeating a row, or promoting a fragment.
"""

from __future__ import annotations

import logging

from . import config
from .database import connection
from .utils import _parse_timestamp_utc

logger = logging.getLogger(__name__)

# The role vocabulary is FIXED now and filled in stages, so that a reader can be
# written against the whole set before the server can derive all of it. v0
# derives the two the stored rows can justify without a relation table:
# `supersedes` (same msg_id + time order) and `supports` (episode containment).
# `corrects` / `qualifies` / `contradicts` need a source of truth the server does
# not have — an in-place update leaves no history — and appear when declared
# relations do. A reader ignores a role it does not know.
ROLE_VOCABULARY: tuple[str, ...] = (
    "supports",
    "supersedes",
    "corrects",
    "qualifies",
    "contradicts",
    "temporal_predecessor",
)
V0_DERIVED_ROLES: frozenset[str] = frozenset({"supports", "supersedes"})

# Stage 2 cluster keys, strongest first. The order is the tie-break for
# `independence_reason` when a cluster was formed by more than one key, and it is
# the order in which the keys are applied, so `evidence[].why` is deterministic.
CLUSTER_KEYS: tuple[str, ...] = (
    "cluster:msg_id",
    "cluster:episode",
    "cluster:adjacent",
    "cluster:chain",  # reserved for the overflow chain; no chain exists yet, so it never fires
)

# Why a response carried fewer items than the window allowed. A short return is a
# normal result, but it is never silent: section 7 requires a reason.
SHORTFALL_NO_RELEVANT_EVIDENCE = "no_relevant_evidence"
SHORTFALL_BELOW_QUALITY_THRESHOLD = "below_quality_threshold"
SHORTFALL_EXHAUSTED_CANDIDATES = "exhausted_candidates"


class _Candidate:
    """One recall row, normalised to the fields the four stages read.

    Rows without a `ref` are dropped before this is built: the injected profile
    row (id=-1 sentinel) and external-context echoes have no provenance handle,
    and invariant 5 requires that every element of an item can say where it came
    from.
    """

    __slots__ = ("ref", "kind", "row_id", "content", "timestamp", "ts", "msg_id", "source_id", "rank")

    def __init__(self, msg: dict, rank: int) -> None:
        ref = msg["ref"]
        kind, _, raw_id = ref.partition(":")
        self.ref = ref
        self.kind = kind
        self.row_id = int(raw_id) if raw_id.isdigit() else -1
        self.content = msg.get("content") or ""
        self.timestamp = msg.get("timestamp") or ""
        self.ts = _parse_timestamp_utc(self.timestamp)
        self.msg_id = (msg.get("id") or "").strip()
        source = msg.get("source")
        self.source_id = (source.get("id") or "").strip() if isinstance(source, dict) else ""
        # Relevance rank, 0 = most relevant. do_recall emits its ranked list
        # REVERSED (most relevant last) so that a truncated tail keeps the
        # valuable end; the caller of this module converts before constructing.
        self.rank = rank


def _order_key(c: _Candidate) -> tuple:
    """The written-down total order (invariant 3).

    Newest first, because the head of a cluster is its *current* statement — the
    one a supersession chain ends at. Then memories before episodes (a memory is
    a statement; an episode summary is context), then the relevance rank the
    retrieval produced, then the row id. The last element makes the order total:
    two rows cannot tie on all four.
    """
    return (
        1 if c.ts is None else 0,
        -c.ts.timestamp() if c.ts is not None else 0.0,
        0 if c.kind == "mem" else 1,
        c.rank,
        c.row_id,
    )


def _timeline_key(c: _Candidate) -> tuple:
    """Chronological order for `timeline` — oldest first, total by (kind, id)."""
    return (c.ts.timestamp() if c.ts is not None else 0.0, c.kind, c.row_id)


def resolve_count(requested: int | None) -> tuple[int, dict]:
    """The Reconstruction Window (section 7).

        base      = forced_count ?? requested_count ?? default_count
        effective = min(base, max_count)
        0 <= returned <= effective

    `count` is the CEILING on items returned. It is not a fill target and it is
    not a search depth. Returns `(effective, count_policy)`; the policy says
    where the base came from and whether the maximum cut it, so a caller can see
    what the server did with the request.
    """
    maximum = config.RECONSTRUCT_MAX_COUNT
    forced = config.RECONSTRUCT_FORCED_COUNT
    if forced is not None:
        base, source, reason = forced, "operator_forced", "forced_count_set"
    elif requested is not None:
        base, source, reason = requested, "caller", "count_requested"
    else:
        base, source, reason = config.RECONSTRUCT_DEFAULT_COUNT, "server_default", "count_omitted"
    base = max(0, int(base))
    effective = min(base, maximum)
    return effective, {"source": source, "clamped": effective < base, "reason": reason}


class _Union:
    """Union-find over candidate indices, deterministic by construction.

    Each merge records the key that caused it, so `evidence[].why` is the key
    that first admitted a row rather than a guess made afterwards. Keys are
    applied in CLUSTER_KEYS order and candidates are visited in `_order_key`
    order, which is total — so the same database state produces the same
    clusters, the same representatives and the same `why` strings.
    """

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self.why: dict[int, str] = {}
        self.keys_used: set[str] = set()

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, a: int, b: int, key: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Lower index wins so the representative does not depend on merge order.
        hi, lo = (rb, ra) if ra < rb else (ra, rb)
        self._parent[hi] = lo
        self.keys_used.add(key)
        # The row that JOINED gets the why. Both endpoints may already carry one
        # from a stronger key; first key wins, which is why CLUSTER_KEYS is
        # ordered and iterated rather than applied as a set.
        for idx in (a, b):
            self.why.setdefault(idx, key)


async def _episode_spans(agent_id: str, episode_ids: list[int]) -> dict[int, tuple[object, object]]:
    """`[start_time, end_time]` for candidate episodes.

    The recall response carries an episode's `timestamp` but not its `end_time`
    (the episode FTS arm selects id / summary / start_time / resolved /
    created_at), and stage 2 needs the span to decide containment. This is one
    bounded lookup over ids the retrieval already scoped to this agent — it adds
    no reach, only the column the key reads.
    """
    if not episode_ids:
        return {}
    placeholders = ",".join("?" for _ in episode_ids)
    spans: dict[int, tuple[object, object]] = {}
    async with connection() as db:
        cursor = await db.execute(
            f"SELECT id, start_time, end_time FROM episodes WHERE agent_id = ? AND id IN ({placeholders})",
            [agent_id, *episode_ids],
        )
        for row_id, start, end in await cursor.fetchall():
            spans[row_id] = (_parse_timestamp_utc(start or ""), _parse_timestamp_utc(end or ""))
    return spans


def bundle(candidates: list[_Candidate], spans: dict[int, tuple[object, object]]) -> _Union:
    """Stage 2 — cluster by deterministic keys.

    These keys are the CEILING of what the server calls "the same memory".
    Semantic sameness is not judged here; nothing below looks at what a row says,
    only at the identifiers and timestamps it was stored with.
    """
    uf = _Union(len(candidates))

    # cluster:msg_id — the same caller-supplied message id is the same logical
    # record by definition, so this is the strongest key.
    by_msg_id: dict[str, list[int]] = {}
    for i, c in enumerate(candidates):
        if c.msg_id and c.kind == "mem":
            by_msg_id.setdefault(c.msg_id, []).append(i)
    for _, members in sorted(by_msg_id.items()):
        for other in members[1:]:
            uf.union(members[0], other, "cluster:msg_id")

    # cluster:episode — a memory whose timestamp falls inside a candidate
    # episode's span is evidence the episode covers. Only episodes in the pool
    # bundle: an episode that retrieval did not surface is not reach this tool
    # may add (invariant 4).
    episodes = [(i, c) for i, c in enumerate(candidates) if c.kind == "ep"]
    memories = [(i, c) for i, c in enumerate(candidates) if c.kind == "mem"]
    for ep_i, ep in episodes:
        start, end = spans.get(ep.row_id, (None, None))
        if start is None or end is None:
            continue
        for mem_i, mem in memories:
            if mem.ts is not None and start <= mem.ts <= end:
                uf.union(ep_i, mem_i, "cluster:episode")

    # cluster:adjacent — the same source, close in time, is one conversational
    # moment. Source alone is NOT a key; see the module docstring.
    window = config.RECONSTRUCT_ADJACENCY_SECONDS
    if window > 0:
        by_source: dict[str, list[int]] = {}
        for i, c in enumerate(candidates):
            if c.source_id and c.ts is not None:
                by_source.setdefault(c.source_id, []).append(i)
        for _, members in sorted(by_source.items()):
            ordered = sorted(members, key=lambda i: _timeline_key(candidates[i]))
            for left, right in zip(ordered, ordered[1:]):
                gap = abs(candidates[right].ts.timestamp() - candidates[left].ts.timestamp())
                if gap <= window:
                    uf.union(left, right, "cluster:adjacent")

    # cluster:chain — reserved. An overflow chain is the natural evidence cluster
    # for a record split across nodes, and it plugs in here with no change to the
    # contract. There is no chain column in this schema, so the key is declared
    # and never fires.

    return uf


def walk(clusters: list[list[int]], max_hops: int) -> tuple[list[list[int]], bool]:
    """Stage 3 — bounded relation walk. The identity in v0.

    With no relation table and no overflow chain there is nothing to follow, so
    this returns its input and reports that it cut nothing.
    It exists as a named stage because the bound is part of the contract now: a
    caller that reads `bounds.max_hops` today gets the same field when the stage
    starts following edges, rather than a new one.
    """
    del max_hops  # no edges to bound yet; the declared bound is reported as given
    return clusters, False


def _roles_for(claim: _Candidate, members: list[_Candidate], spans: dict) -> list[dict]:
    """The roles v0 can derive from stored rows alone.

    `roles[].role` names what the referenced row is TO this claim (the ref is the
    subject) — see the module docstring.
    """
    roles: list[dict] = []

    # supersedes: the next NEWER row carrying the same message id. Pointing at
    # the immediate successor rather than the newest makes the chain readable one
    # hop at a time, and is what makes a three-version record legible as two
    # edges instead of two claims pointing at the same head.
    if claim.msg_id and claim.kind == "mem" and claim.ts is not None:
        newer = [
            m
            for m in members
            if m.ref != claim.ref and m.kind == "mem" and m.msg_id == claim.msg_id and m.ts is not None and m.ts > claim.ts
        ]
        if newer:
            successor = min(newer, key=lambda m: (m.ts.timestamp(), m.row_id))
            roles.append({"ref": successor.ref, "role": "supersedes"})

    # supports: a candidate episode whose span contains this memory's timestamp.
    if claim.kind == "mem" and claim.ts is not None:
        for m in members:
            if m.kind != "ep":
                continue
            start, end = spans.get(m.row_id, (None, None))
            if start is not None and end is not None and start <= claim.ts <= end:
                roles.append({"ref": m.ref, "role": "supports"})

    return roles


def _conflicts(members: list[_Candidate]) -> list[dict]:
    """Two rows the keys cannot order, shown inside the one item (invariant 8).

    v0 recognises exactly one conflict it can prove from stored fields: rows that
    share a message id AND a timestamp, so version order cannot separate them.
    Disagreement of meaning is not judged here — that needs a model, and this
    path calls none.
    """
    groups: dict[tuple[str, float], list[str]] = {}
    for m in members:
        if m.msg_id and m.kind == "mem" and m.ts is not None:
            groups.setdefault((m.msg_id, m.ts.timestamp()), []).append(m.ref)
    out = []
    for (msg_id, _), refs in sorted(groups.items()):
        if len(refs) > 1:
            out.append({"refs": sorted(refs), "reason": "same_msg_id_same_timestamp", "msg_id": msg_id})
    return out


def structure(
    members: list[_Candidate],
    why: dict[str, str],
    spans: dict,
    max_evidence: int,
) -> tuple[dict, bool]:
    """Stage 4 — one cluster becomes one recall item.

    Ordered by time and by version; conflicting rows are kept and marked. Nothing
    is summarised and no text is merged: `content` is the head claim verbatim.
    """
    ordered = sorted(members, key=_order_key)
    head = ordered[0]

    claims = [
        {
            "ref": m.ref,
            "as_of": m.timestamp,
            "roles": _roles_for(m, ordered, spans),
        }
        for m in ordered
    ]

    timeline = [{"at": m.timestamp, "ref": m.ref} for m in sorted(members, key=_timeline_key) if m.ts is not None]

    evidence_rows = sorted(members, key=_order_key)
    truncated = len(evidence_rows) > max_evidence
    evidence = [{"ref": m.ref, "why": why.get(m.ref, "seed")} for m in evidence_rows[:max_evidence]]

    # The strongest key that formed this cluster answers "why is this a separate
    # item"; a row nothing linked to is independent because nothing claimed it.
    present = {why.get(m.ref) for m in members} - {None, "seed"}
    independence = next((k for k in CLUSTER_KEYS if k in present), "singleton")

    item: dict = {
        "content": head.content,
        "claims": claims,
        "timeline": timeline,
        "evidence": evidence,
        "independence_reason": independence,
    }
    conflicts = _conflicts(members)
    if conflicts:
        item["conflicts"] = conflicts
    return item, truncated


async def do_reconstruct(
    agent_id: str,
    query: str,
    count: int | None = None,
    top_k: int | None = None,
    max_hops: int | None = None,
    max_evidence: int | None = None,
    deep: bool = False,
    channel: str = "",
    project_id: str | None = None,
    source_id: str = "",
    session_key: str = "",
) -> dict:
    """Assemble recall items from the candidate pool the recall process produced.

    `count` bounds only how many items come back. The breadth bounds — `top_k`,
    `max_hops`, `max_evidence` — are declared separately and are never derived
    from it (invariant 7): `top_k` is handed to the retrieval as its response
    count, so changing `count` alone leaves the candidate id set untouched.
    """
    # Imported here rather than at module scope: memory_handlers imports the
    # config and database modules this one uses, and a top-level import would
    # make the pair mutually importable depending on which the server touches
    # first.
    from .memory_handlers import do_recall

    effective_count, count_policy = resolve_count(count)
    bounds_top_k = config.RECONSTRUCT_TOP_K if top_k is None else max(1, int(top_k))
    bounds_max_hops = config.RECONSTRUCT_MAX_HOPS if max_hops is None else max(0, int(max_hops))
    bounds_max_evidence = config.RECONSTRUCT_MAX_EVIDENCE if max_evidence is None else max(1, int(max_evidence))

    recall_result = await do_recall(
        agent_id,
        query,
        bounds_top_k,  # the candidate depth — NOT `count` (invariant 7)
        deep=deep,
        channel=channel,
        project_id=project_id,
        source_id=source_id,
        session_key=session_key,
    )
    messages = recall_result.get("messages", [])

    # do_recall emits its ranked list reversed (most relevant LAST), so that a
    # consumer trimming a payload keeps the valuable end. Rank 0 is most relevant.
    total = len(messages)
    candidates = [
        _Candidate(m, rank=total - 1 - i)
        for i, m in enumerate(messages)
        if isinstance(m.get("ref"), str) and ":" in m["ref"]
    ]

    bounds = {
        "top_k": bounds_top_k,
        "max_hops": bounds_max_hops,
        "max_evidence": bounds_max_evidence,
        "truncated": False,
    }
    response: dict = {
        "items": [],
        "requested_count": count,
        "effective_count": effective_count,
        "returned_count": 0,
        "count_policy": count_policy,
        "bounds": bounds,
    }

    if not candidates:
        response["shortfall_reason"] = (
            SHORTFALL_BELOW_QUALITY_THRESHOLD
            if recall_result.get("gate_fallback")
            else SHORTFALL_NO_RELEVANT_EVIDENCE
        )
        return response

    spans = await _episode_spans(agent_id, [c.row_id for c in candidates if c.kind == "ep" and c.row_id > 0])

    uf = bundle(candidates, spans)
    grouped: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        grouped.setdefault(uf.find(i), []).append(i)
    clusters = [sorted(members) for _, members in sorted(grouped.items())]
    clusters, walk_truncated = walk(clusters, bounds_max_hops)

    why_by_ref = {candidates[i].ref: uf.why[i] for i in uf.why}

    assembled = []
    evidence_truncated = False
    for members in clusters:
        rows = [candidates[i] for i in members]
        item, cut = structure(rows, why_by_ref, spans, bounds_max_evidence)
        evidence_truncated = evidence_truncated or cut
        # Most relevant cluster first; the head ref makes the order total.
        assembled.append((min(r.rank for r in rows), item["claims"][0]["ref"], item))

    assembled.sort(key=lambda t: (t[0], t[1]))
    items = [item for _, _, item in assembled[:effective_count]]

    # The retrieval was cut if it returned exactly as many rows as it was allowed
    # to; the tool cannot tell a full pool from a cut one, and says so rather than
    # implying it saw everything.
    pool_truncated = total >= bounds_top_k
    bounds["truncated"] = bool(evidence_truncated or walk_truncated or pool_truncated)

    response["items"] = items
    response["returned_count"] = len(items)
    if len(items) < effective_count:
        response["shortfall_reason"] = (
            SHORTFALL_BELOW_QUALITY_THRESHOLD
            if recall_result.get("gate_fallback")
            else SHORTFALL_EXHAUSTED_CANDIDATES
        )
    return response
