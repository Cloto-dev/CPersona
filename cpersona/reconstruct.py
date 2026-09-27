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
3. *Bounded relation walk* — from each item's direct candidates, through the
   entities they are declared to mention and the entity → entity relations
   declared on those, up to ``max_hops``, to the records that mention the
   entities reached. Those records become evidence inside the item, never an
   item (docs/ASSOCIATIVE_MEMORY_DESIGN.md §3). With nothing declared it is the
   identity.
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
4. Boundedness — nothing is scanned past the declared bounds; rows a bound
   dropped are named in ``bounds.omitted`` and a bound that was merely met in
   ``bounds.reached`` (see "What the response admits" below).
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

import numpy as np

from . import associations, blocks, config, excerpts, generation, nodes, vector
from .database import connection
from .utils import _parse_timestamp_utc, error_response

logger = logging.getLogger(__name__)

# The role vocabulary is FIXED now and filled in stages, so that a reader can be
# written against the whole set before the server can derive all of it. v0
# derives the two the stored rows can justify without a relation table:
# `supersedes` (same msg_id + time order) and `supports` (episode containment).
# `corrects` / `qualifies` / `contradicts` need a source of truth the server does
# not have — an in-place update leaves no history — so they come only from a
# declared record → record relation whose predicate is the role word. Any role
# may be declared that way. A reader ignores a role it does not know.
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
    "cluster:relation",  # a declared record -> record relation joins its two endpoints
    "cluster:chain",  # reserved for the overflow chain; no chain exists yet, so it never fires
)

# `why` of a row a declared relation admitted: `relation:<predicate>`. A row the
# bundling joined carries it with no `hops`; a row the walk reached carries `hops`.
WHY_RELATION = "relation:"

# What the response admits about its own limits. Two different facts used to share
# one boolean (`bounds.truncated`), and on a pool that fills its depth -- every
# measured call -- it was always true, so it told a caller nothing:
#
# * `bounds.omitted` names a bound that DROPPED rows the tool held: the evidence
#   bound cut a cluster (each cut item also says how many in `claims_omitted`), or
#   the relation walk stopped at its hop limit.
# * `bounds.reached` names a bound that was MET without the tool knowing whether
#   anything lay beyond it: retrieval returned exactly as many rows as it was
#   allowed. Deeper rows may or may not exist.
#
# Both are absent when empty, and absence is not a verdict: it does not say the
# items suffice to answer, that the whole store was searched, or that the rows were
# checked for contradiction (`conflicts` sees only one narrow case).
BOUND_EVIDENCE = "max_evidence"
BOUND_HOPS = "max_hops"
BOUND_TOP_K = "top_k"

# How a quote was chosen when it was not chosen the intended way. Absent otherwise.
QUOTE_LEXICAL_ONLY = "lexical_only"  # no query embedding: nodes ranked by trigrams alone
NODES_NONE = "no_nodes"  # the record has no nodes; its cut quote is simply its start
NODES_NOT_CURRENT = "not_current"  # nodes exist but are partial or another model's

# The envelope. A search that did what it was asked says so in two integers.
#
# Every response used to restate its whole policy -- requested / effective /
# returned, both policies, the bounds it was given, the pool counts -- about 500
# characters on a call where nothing happened, against none on the recall it sits
# beside. An agent searches several times per question, so that was paid several
# times, and it is where reconstruct's search response outgrew recall's: the rows
# themselves cost the same. The rule now: state `effective_count` and
# `returned_count` always, and the rest only when the server did something other
# than what was asked or withheld something the caller could act on --
#
# * `requested_count` + `count_policy`: the count was clamped or operator-forced;
# * `requested_budget` + `budget_policy`: the budget was clamped, raised or forced;
# * `effective_budget` + `used_budget`: the budget cut something (an item or an excerpt);
# * `bounds`: a bound dropped rows, was reached, or was lowered by the library ceiling;
# * `reconstruction.excluded_without_provenance`: rows were excluded.
#
# `trace=true` returns the full audit as before, including the pool counts.
_ASKED = ("caller", "server_default")


def _compact(response: dict) -> dict:
    out = dict(response)
    count_policy, budget_policy = out["count_policy"], out["budget_policy"]
    if not count_policy["clamped"] and count_policy["source"] in _ASKED:
        del out["requested_count"], out["count_policy"]
    if not budget_policy["clamped"] and budget_policy["source"] in _ASKED:
        del out["requested_budget"], out["budget_policy"]
    budget_cut = (
        out.get("shortfall_reason") == SHORTFALL_BUDGET_EXHAUSTED
        or "reserved_omitted" in out
        or any("excerpts_omitted" in item for item in out["items"])
    )
    if not budget_cut:
        del out["effective_budget"], out["used_budget"]
    if not any(key in out["bounds"] for key in ("omitted", "reached", "effective_top_k")):
        del out["bounds"]
    excluded = out.pop("reconstruction")["excluded_without_provenance"]
    if excluded:
        out["reconstruction"] = {"excluded_without_provenance": excluded}
    return out


# How many of a quoted record's nodes the trace lists, best first.
TRACE_NODE_ORDER = 3

# Why a response carried fewer items than the window allowed. A short return is a
# normal result, but it is never silent: section 7 requires a reason.
SHORTFALL_NO_RELEVANT_EVIDENCE = "no_relevant_evidence"
SHORTFALL_BELOW_QUALITY_THRESHOLD = "below_quality_threshold"
SHORTFALL_EXHAUSTED_CANDIDATES = "exhausted_candidates"
SHORTFALL_BUDGET_EXHAUSTED = "budget_exhausted"


class _Candidate:
    """One recall row, normalised to the fields the four stages read.

    Rows without a `ref` are dropped before this is built: the injected profile
    row (id=-1 sentinel) and external-context echoes have no provenance handle,
    and invariant 5 requires that every element of an item can say where it came
    from.
    """

    __slots__ = (
        "ref", "kind", "row_id", "content", "timestamp", "ts", "msg_id", "source_id", "source_type", "context", "rank",
        "reserved",
    )

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
        self.source_type = source.get("type", "") if isinstance(source, dict) else ""
        self.context = None  # (project, channel), hydrated from the scoped candidate rows
        # Relevance rank, 0 = most relevant. do_recall emits its ranked list
        # REVERSED (most relevant last) so that a truncated tail keeps the
        # valuable end; the caller of this module converts before constructing.
        self.rank = rank
        # A row recall admitted into a place held for the block arm, not through
        # the gate (docs/BLOCK_REACH_DESIGN.md §5). It carries no gate score, so
        # its rank is below every admitted row by construction.
        reason = msg.get("match_reason")
        self.reserved = isinstance(reason, dict) and reason.get("admission") == "reservation"


def _order_key(c: _Candidate) -> tuple:
    """The written-down total order of `claims` (invariant 3).

    Newest first, so a supersession chain reads from its current statement back.
    Then memories before episodes (a memory is a statement; an episode summary is
    context), then the relevance rank the retrieval produced, then the row id. The
    last element makes the order total: two rows cannot tie on all four.

    This orders claims; it does not choose the head. See `_head`.
    """
    return (
        1 if c.ts is None else 0,
        -c.ts.timestamp() if c.ts is not None else 0.0,
        0 if c.kind == "mem" else 1,
        c.rank,
        c.row_id,
    )


def _timeline_key(c: _Candidate) -> tuple:
    """Chronological order — oldest first, total by (kind, id)."""
    return (c.ts.timestamp() if c.ts is not None else 0.0, c.kind, c.row_id)


def _relevance_key(c: _Candidate) -> tuple:
    """Most relevant first, total by row id."""
    return (c.rank, c.kind, c.row_id)


def _message_key(c: _Candidate) -> tuple[str, str] | None:
    """bug-435: message IDs identify a record only within its stored project.

    The agent is already scoped by retrieval. Unknown context cannot establish
    identity; simultaneous visibility of two projects does not equate their IDs.
    """
    if c.kind != "mem" or not c.msg_id or c.context is None:
        return None
    return c.context[0], c.msg_id


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


def _head_cap() -> int:
    """Characters one head quote may carry: the filled quote's size, or the preview tier's when filling is off."""
    return config.RECONSTRUCT_QUOTE_CHARS if config.RECONSTRUCT_QUOTE_CHARS > 0 else config.RECALL_PREVIEW_CHARS


def resolve_budget(requested: int | None, count: int = 1) -> tuple[int, dict]:
    """The payload budget (section 7, "Breadth before depth").

        budget_base      = forced_budget ?? requested_budget ?? default_budget(count)
        effective_budget = min(budget_base, max_budget)

    The default is the configured default, or one preview-tier quote per item of
    the window when that is more. A caller that names a count and leaves the budget
    alone used to get at most default / preview = 8 items however many it asked for:
    a default it never set cut the breadth it did set, which is the wrong way round
    when breadth comes before depth. Only the default moves. A budget the caller or
    an operator names is taken as given, and a window of eight or fewer resolves
    exactly as before.

    Counted in characters of quoted text. A caller's request below one
    preview-tier excerpt is raised to it, because the first item must always fit;
    configured values below it are refused at startup instead
    (config.validate_reconstruct_counts). Returns `(effective, budget_policy)`.
    """
    maximum = config.RECONSTRUCT_MAX_BUDGET
    forced = config.RECONSTRUCT_FORCED_BUDGET
    if forced is not None:
        base, source, reason = forced, "operator_forced", "forced_budget_set"
    elif requested is not None:
        base, source, reason = requested, "caller", "budget_requested"
    else:
        base, source, reason = config.RECONSTRUCT_DEFAULT_BUDGET, "server_default", "budget_omitted"
        heads = int(count) * max(_head_cap(), 0)
        if heads > base:
            base, reason = heads, "default_fits_the_window"
    base = int(base)
    budget = min(base, maximum)
    floor = max(config.RECALL_PREVIEW_CHARS, 1)
    if budget < floor:
        budget, reason = floor, "raised_to_one_excerpt"
    return budget, {"source": source, "clamped": budget != base, "reason": reason}


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

    def union(self, a: int, b: int, key: str, why: str | None = None) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Lower index wins so the representative does not depend on merge order.
        hi, lo = (rb, ra) if ra < rb else (ra, rb)
        self._parent[hi] = lo
        self.keys_used.add(key)
        # The row that JOINED gets the why. Both endpoints may already carry one
        # from a stronger key; first key wins, which is why CLUSTER_KEYS is
        # ordered and iterated rather than applied as a set. A declared relation
        # says which predicate joined the rows, so its why is more than its key.
        for idx in (a, b):
            self.why.setdefault(idx, why or key)


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


async def _candidate_context(agent_id: str, candidates: list[_Candidate]) -> None:
    """Read context for already selected refs, never discover additional rows.

    Recall deliberately omits project/channel from its public row shape. They
    are nevertheless essential to distinguish simultaneous conversations.
    Missing rows stay ungroupable; a deletion between reads is not provenance.
    """
    async with connection() as db:
        for kind, table in (("mem", "memories"), ("ep", "episodes")):
            rows = [c for c in candidates if c.kind == kind and c.row_id > 0]
            by_id = {c.row_id: c for c in rows}
            ids = list(by_id)
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor = await db.execute(
                    f"SELECT id, project_id, channel FROM {table} "
                    f"WHERE agent_id = ? AND id IN ({placeholders})",
                    [agent_id, *batch],
                )
                for row_id, project, channel in await cursor.fetchall():
                    by_id[row_id].context = (project or "", channel or "")


def bundle(
    candidates: list[_Candidate],
    spans: dict[int, tuple[object, object]],
    links: list[tuple[str, str, str]] = (),
) -> _Union:
    """Stage 2 — cluster by deterministic keys.

    These keys are the CEILING of what the server calls "the same memory".
    Semantic sameness is not judged here; nothing below looks at what a row says,
    only at the identifiers and timestamps it was stored with, and at the
    record → record relations an agent declared between them (`links`).
    """
    uf = _Union(len(candidates))

    # Match the storage identity namespace, including when retrieval spans
    # sibling projects or a project's union with the global pool.
    by_msg_id: dict[tuple[str, str], list[int]] = {}
    for i, c in enumerate(candidates):
        key = _message_key(c)
        if key is not None:
            by_msg_id.setdefault(key, []).append(i)
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
            if mem.context is not None and mem.context == ep.context and mem.ts is not None and start <= mem.ts <= end:
                uf.union(ep_i, mem_i, "cluster:episode")

    # cluster:adjacent — the same source, close in time, is one conversational
    # moment. Source alone is NOT a key; see the module docstring.
    window = config.RECONSTRUCT_ADJACENCY_SECONDS
    if window > 0:
        by_source: dict[tuple, list[int]] = {}
        for i, c in enumerate(candidates):
            if c.source_id and c.ts is not None and c.context is not None:
                key = (c.source_type, c.source_id, c.context)
                by_source.setdefault(key, []).append(i)
        for _, members in sorted(by_source.items()):
            ordered = sorted(members, key=lambda i: _timeline_key(candidates[i]))
            # Bound the whole burst, not just each gap: a chain of short gaps
            # must not turn hours of conversation into one recall item.
            anchor = ordered[0]
            for right in ordered[1:]:
                gap = candidates[right].ts.timestamp() - candidates[anchor].ts.timestamp()
                if gap <= window:
                    uf.union(anchor, right, "cluster:adjacent")
                else:
                    anchor = right

    # cluster:relation — a declared record -> record relation joins its endpoints
    # when both are candidates. Sharing a mentioned ENTITY does not: every record
    # that mentions the project's name would fold into one item.
    index = {c.ref: i for i, c in enumerate(candidates)}
    for subject, predicate, obj in links:
        if subject in index and obj in index:
            uf.union(index[subject], index[obj], "cluster:relation", WHY_RELATION + predicate)

    # cluster:chain — reserved. An overflow chain is the natural evidence cluster
    # for a record split across nodes, and it plugs in here with no change to the
    # contract. There is no chain column in this schema, so the key is declared
    # and never fires.

    return uf


def walk(
    clusters: list[list[int]],
    candidates: list[_Candidate],
    graph: associations.WalkGraph | None,
    max_hops: int,
) -> tuple[list[list[tuple[str, str, int]]], list[set[str]]]:
    """Stage 3 — the bounded relation walk. A pure function of the pool and the graph.

    For each cluster, in the order given (item order): start from the entities
    its candidates are declared to mention, follow entity → entity relations in
    either direction up to `max_hops`, and collect the records that mention an
    entity reached through at least one relation. Sharing an entity with a
    candidate is not a reason to be evidence; a declared relation is. Returns, per
    cluster, `(ref, why, hops)` in the written order -- fewer hops first, then the
    more recently declared relation, then the lower record id -- and, per cluster,
    the bounds that cut it: `max_hops` when a relation was left unfollowed at the
    hop bound, `max_evidence` when a reached entity had more records than the
    graph read held (see `associations.walk_graph`).

    A reached record is never an item and never repeated: one already in the
    candidate pool is left where retrieval put it, and one an earlier item
    reached is not added to a later one.
    """
    reached_by_cluster: list[list[tuple[str, str, int]]] = [[] for _ in clusters]
    cuts: list[set[str]] = [set() for _ in clusters]
    if graph is None or not graph.mentions:
        return reached_by_cluster, cuts
    pool = {c.ref for c in candidates}
    taken: set[str] = set()
    for position, members in enumerate(clusters):
        distance: dict[int, int] = {}
        via: dict[int, tuple[int, str]] = {}  # entity -> (recency, predicate) of the edge that reached it
        for i in members:
            for entity in graph.mentions.get(candidates[i].ref, ()):
                distance[entity] = 0
        frontier = sorted(distance)
        for hop in range(1, max_hops + 1):
            step: dict[int, tuple[int, str]] = {}
            for entity in frontier:
                for recency, _, other, predicate in graph.adjacency.get(entity, ()):
                    if other not in distance and (other not in step or recency < step[other][0]):
                        step[other] = (recency, predicate)
            for other, edge in step.items():
                distance[other] = hop
                via[other] = edge
            frontier = sorted(step)
        if any(other not in distance for entity in frontier for _, _, other, _ in graph.adjacency.get(entity, ())):
            cuts[position].add(BOUND_HOPS)
        best: dict[str, tuple] = {}
        for entity, hops in distance.items():
            if hops == 0:
                continue
            if entity in graph.records_cut:
                cuts[position].add(BOUND_EVIDENCE)
            recency, predicate = via[entity]
            for ref in graph.records.get(entity, ()):
                if ref in pool or ref in taken:
                    continue
                kind, _, raw = ref.partition(":")
                key = (hops, recency, int(raw), kind, predicate)
                if ref not in best or key < best[ref]:
                    best[ref] = key
        ordered = sorted(best.items(), key=lambda kv: kv[1])
        reached_by_cluster[position] = [(ref, WHY_RELATION + key[4], key[0]) for ref, key in ordered]
        taken.update(best)
    return reached_by_cluster, cuts


def _roles_for(
    claim: _Candidate, members: list[_Candidate], spans: dict, links: list[tuple[str, str, str]] = ()
) -> list[dict]:
    """The roles of one claim: derived from stored rows, then declared.

    `roles[].role` names what the referenced row is TO this claim (the ref is the
    subject) — see the module docstring. A declared record -> record relation whose
    predicate is a role word says the same thing in the same direction: its subject
    is the ref, its object is the claim.
    """
    roles: list[dict] = []

    # supersedes: the next NEWER row carrying the same message id. Pointing at
    # the immediate successor rather than the newest makes the chain readable one
    # hop at a time, and is what makes a three-version record legible as two
    # edges instead of two claims pointing at the same head.
    identity = _message_key(claim)
    if identity is not None and claim.ts is not None:
        newer = [
            m
            for m in members
            if m.ref != claim.ref and _message_key(m) == identity and m.ts is not None and m.ts > claim.ts
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
            if (claim.context is not None and claim.context == m.context
                    and start is not None and end is not None and start <= claim.ts <= end):
                roles.append({"ref": m.ref, "role": "supports"})

    retained = {m.ref for m in members}
    for subject, predicate, obj in links:
        if obj == claim.ref and subject in retained and predicate in ROLE_VOCABULARY:
            role = {"ref": subject, "role": predicate}
            if role not in roles:
                roles.append(role)

    return roles


def _key_of(why: str | None) -> str | None:
    """The cluster key a bundled row's `why` names."""
    if why is not None and why.startswith(WHY_RELATION):
        return "cluster:relation"
    return why


def _conflicts(members: list[_Candidate]) -> list[dict]:
    """Two rows the keys cannot order, shown inside the one item (invariant 8).

    v0 recognises exactly one conflict it can prove from stored fields: rows that
    share a message id AND a timestamp, so version order cannot separate them.
    Disagreement of meaning is not judged here — that needs a model, and this
    path calls none.
    """
    groups: dict[tuple[tuple[str, str], float], list[str]] = {}
    for m in members:
        identity = _message_key(m)
        if identity is not None and m.ts is not None:
            groups.setdefault((identity, m.ts.timestamp()), []).append(m.ref)
    out = []
    for ((project, msg_id), _), refs in sorted(groups.items()):
        if len(refs) > 1:
            out.append({"refs": sorted(refs), "reason": "same_msg_id_same_timestamp", "msg_id": msg_id})
    return out


def _head(members: list[_Candidate]) -> _Candidate:
    """The claim an item quotes.

    A cluster is either versions of one record or distinct rows that belong
    together (one conversational burst, one episode). Newest is only meaningful
    for the first: the latest version is the current statement. Among distinct
    rows, the newest is merely the last thing said, so the head is the row the
    retrieval ranked highest. Both rules compose: take the most relevant row, then,
    if newer versions of that same record are in the cluster, its latest version.
    """
    lead = min(members, key=_relevance_key)
    identity = _message_key(lead)
    if identity is None or lead.ts is None:
        return lead
    versions = [m for m in members if _message_key(m) == identity and m.ts is not None]
    return min(versions, key=_order_key)


def structure(
    members: list[_Candidate],
    why: dict[str, str],
    spans: dict,
    max_evidence: int,
    reached: list[tuple[_Candidate, str, int]] = (),
    links: list[tuple[str, str, str]] = (),
) -> tuple[dict, int]:
    """Stage 4 — one cluster becomes one recall item.

    Ordered by time and by version; conflicting rows are kept and marked. Nothing
    is summarised and no text is merged: `content` is the head claim verbatim, and
    `head_ref` names it. `reached` are the rows the walk added, in its written
    order; they are evidence, never the head.
    """
    head = _head(members)
    # Every emitted claim and role target is a retained row. A cut keeps the
    # head, then the most relevant of the rest, then what the walk reached in the
    # walk's order; age decides nothing about what survives.
    walked = [row for row, _, _ in reached]
    dropped = max(0, len(members) + len(walked) - max_evidence)
    others = sorted((m for m in members if m is not head), key=_relevance_key)
    retained = {id(m) for m in [head, *others, *walked][:max_evidence]}
    ordered = sorted((m for m in [*members, *walked] if id(m) in retained), key=_order_key)
    walk_why = {row.ref: (label, hops) for row, label, hops in reached}

    # One entry per retained row carries everything the item says about that row:
    # when it holds, why it is here, and how it relates to the others. The earlier
    # shape repeated each ref in parallel `timeline` and `evidence` arrays, which
    # for a one-row item cost more characters than the refs and reasons it carried;
    # a chronological view is `as_of` sorted, and `why` is the evidence. An empty
    # `roles` is omitted rather than sent as [].
    claims = []
    for m in ordered:
        claim: dict = {"ref": m.ref, "as_of": m.timestamp, "why": why.get(m.ref, "seed")}
        if m.ref in walk_why:
            claim["why"], claim["hops"] = walk_why[m.ref]
        roles = _roles_for(m, ordered, spans, links)
        if roles:
            claim["roles"] = roles
        claims.append(claim)

    # The strongest key that formed this cluster answers "why is this a separate
    # item"; a row nothing linked to is independent because nothing claimed it.
    # What the walk reached did not form the cluster and is not counted.
    present = {_key_of(why.get(m.ref)) for m in members} - {None, "seed"}
    independence = next((k for k in CLUSTER_KEYS if k in present), "singleton")

    item: dict = {
        "content": head.content,
        "head_ref": head.ref,
        "claims": claims,
        "independence_reason": independence,
    }
    conflicts = _conflicts(ordered)
    if conflicts:
        item["conflicts"] = conflicts
    if dropped:
        item["claims_omitted"] = dropped
    return item, dropped


# ------------------------------------------------------------------------------------
# Quoting: which part of a claim's record the item carries
# ------------------------------------------------------------------------------------
#
# A claim is quoted from the node of its record that best matches the query when
# the record has a current node set (docs/OVERFLOW_TREE_DESIGN.md section 6), and
# from the start of the record otherwise, as before. Nodes are read only here, after
# retrieval, bundling and item order are settled, so they cannot change which items
# come back or in what order (tree invariant 1).
#
# Within one record, nodes are ranked twice -- by cosine to the query embedding and
# by how many of the query's character trigrams they contain, the unit the keyword
# index matches on -- and the two ranks are fused the way recall fuses its arms
# (reciprocal rank, CPERSONA_RRF_K). Equal values share a rank, so a query with no
# literal match leaves the choice to the embedding rather than to node order. Ties
# go to the earlier node. No score is reported: it is not calibrated across
# records, models or corpora.


def _trigrams(text: str) -> set[str]:
    folded = text.lower()
    if len(folded) < 3:
        return {folded} if folded else set()
    return {folded[i : i + 3] for i in range(len(folded) - 2)}


def _shared_ranks(values: list[float]) -> list[int]:
    """Rank by value, highest first; equal values share a rank (0 = best)."""
    distinct = sorted(set(values), reverse=True)
    position = {v: i for i, v in enumerate(distinct)}
    return [position[v] for v in values]


def rank_nodes(text: str, node_rows: list[tuple], query_vec, query_grams: set[str]) -> list[tuple]:
    """The nodes of `text`, best match first. `node_rows` are (index, start, end, blob) in order."""
    lexical = [float(len(query_grams & _trigrams(text[start:end]))) for _, start, end, _ in node_rows]
    ranks = [_shared_ranks(lexical)]
    if query_vec is not None:
        cosines = []
        for _, _, _, blob in node_rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            if vec.shape != query_vec.shape:
                cosines = None
                break
            norm = float(np.linalg.norm(vec)) * float(np.linalg.norm(query_vec))
            cosines.append(float(vec @ query_vec) / norm if norm else 0.0)
        if cosines is not None:
            ranks.append(_shared_ranks(cosines))
    k = config.RRF_K
    fused = [sum(1.0 / (k + 1 + r[i]) for r in ranks) for i in range(len(node_rows))]
    order = sorted(range(len(node_rows)), key=lambda i: (-fused[i], node_rows[i][0]))
    return [node_rows[i] for i in order]


def rank_blocks(
    text: str, block_rows: list[tuple], query_bits: bytes | None, query_grams: set[str]
) -> list[tuple]:
    """The blocks of ``text``, best match first. ``block_rows`` are
    (index, start, end, bits) in order.

    The same two-list fusion the node path uses, with Hamming distance standing
    in for cosine because that is the representation a block has. Equal values
    share a rank, so a query with no literal match leaves the choice to the
    vector rather than to block order; ties go to the earlier block. No score is
    reported — a Hamming distance is not calibrated across records or models.
    """
    lexical = [float(len(query_grams & _trigrams(text[start:end]))) for _, start, end, _ in block_rows]
    ranks = [_shared_ranks(lexical)]
    if query_bits is not None:
        import numpy as np

        width = len(query_bits)
        usable = all(bits is not None and len(bits) == width for _, _, _, bits in block_rows)
        if usable and block_rows:
            packed = np.frombuffer(
                b"".join(bits for _, _, _, bits in block_rows), dtype=np.uint8
            ).reshape(len(block_rows), width)
            query = np.frombuffer(query_bits, dtype=np.uint8)
            distances = blocks._popcount_table()[np.bitwise_xor(packed, query)].sum(axis=1)
            # Ranked highest-first like the others, so a nearer block sorts first.
            ranks.append(_shared_ranks([-float(d) for d in distances]))
    k = config.RRF_K
    fused = [sum(1.0 / (k + 1 + r[i]) for r in ranks) for i in range(len(block_rows))]
    order = sorted(range(len(block_rows)), key=lambda i: (-fused[i], block_rows[i][0]))
    return [block_rows[i] for i in order]


def best_node(text: str, node_rows: list[tuple], query_vec, query_grams: set[str]) -> tuple:
    """The node of `text` to quote."""
    return rank_nodes(text, node_rows, query_vec, query_grams)[0]


async def _current_node_sets(
    agent_id: str, claims: list[_Candidate]
) -> tuple[dict[str, tuple[str, list[tuple]]], set[str]]:
    """`ref -> (stored text, nodes)` for the claims whose record has a current node set,
    and the refs whose record has nodes that are not current.

    Current means complete over the stored text and built by the embedding model this
    server runs; anything else is quoted from the record's start, which is exactly
    what a record without nodes gets. Reads are keyed on refs retrieval already
    scoped to this agent.
    """
    keys = generation.node_keys()
    out: dict[str, tuple[str, list[tuple]]] = {}
    not_current: set[str] = set()
    async with connection() as db:
        for kind, (table, column) in nodes.PARENT_TEXT.items():
            ids = sorted({c.row_id for c in claims if c.kind == kind and c.row_id > 0})
            for offset in range(0, len(ids), 500):
                batch = ids[offset : offset + 500]
                marks = ",".join("?" for _ in batch)
                texts = dict(
                    await db.execute_fetchall(
                        f"SELECT id, {column} FROM {table} WHERE agent_id = ? AND id IN ({marks})",
                        [agent_id, *batch],
                    )
                )
                rows = await db.execute_fetchall(
                    "SELECT parent_id, node_index, start_char, end_char, embedding, embedding_model "
                    f"FROM record_nodes WHERE parent_kind = ? AND parent_id IN ({marks}) "
                    "ORDER BY parent_id, node_index",
                    [kind, *batch],
                )
                grouped: dict[int, list] = {}
                for parent_id, index, start, end, blob, node_model in rows:
                    grouped.setdefault(parent_id, []).append((index, start, end, blob, node_model))
                for parent_id, group in grouped.items():
                    text = texts.get(parent_id)
                    if text is None:
                        continue
                    contiguous = all(a[2] == b[1] for a, b in zip(group, group[1:]))
                    complete = group[0][1] == 0 and group[-1][2] == len(text) and contiguous
                    if complete and all(g[4] in keys and g[3] is not None for g in group):
                        out[f"{kind}:{parent_id}"] = (text, [g[:4] for g in group])
                    else:
                        not_current.add(f"{kind}:{parent_id}")
    return out, not_current


async def _current_block_sets(
    agent_id: str, claims: list[_Candidate]
) -> dict[str, tuple[str, list[tuple]]]:
    """`ref -> (stored text, blocks)` for the claims whose record has a current
    block set (docs/BLOCK_REACH_DESIGN.md §6).

    Current is the same test the builder and the backfill share: the set covers
    the stored text with no gap, and every row came from the model this server
    reports. Anything else quotes the way it did before — from a node, or from
    the record's start — because a partial set would put an offset into text it
    was not measured in.
    """
    keys = generation.block_keys()
    out: dict[str, tuple[str, list[tuple]]] = {}
    async with connection() as db:
        for kind, (table, column) in nodes.PARENT_TEXT.items():
            ids = sorted({c.row_id for c in claims if c.kind == kind and c.row_id > 0})
            for offset in range(0, len(ids), 500):
                batch = ids[offset : offset + 500]
                marks = ",".join("?" for _ in batch)
                texts = dict(
                    await db.execute_fetchall(
                        f"SELECT id, {column} FROM {table} WHERE agent_id = ? AND id IN ({marks})",
                        [agent_id, *batch],
                    )
                )
                rows = await db.execute_fetchall(
                    "SELECT parent_id, block_index, start_char, end_char, embedding_bits, "
                    f"embedding_model FROM record_blocks WHERE parent_kind = ? AND parent_id IN ({marks}) "
                    "ORDER BY parent_id, block_index",
                    [kind, *batch],
                )
                grouped: dict[int, list] = {}
                for parent_id, index, start, end, bits, block_model in rows:
                    grouped.setdefault(parent_id, []).append((index, start, end, bits, block_model))
                for parent_id, group in grouped.items():
                    text = texts.get(parent_id)
                    if text is None:
                        continue
                    contiguous = all(a[2] == b[1] for a, b in zip(group, group[1:]))
                    complete = group[0][1] == 0 and group[-1][2] == len(text) and contiguous
                    if complete and all(g[4] in keys for g in group):
                        out[f"{kind}:{parent_id}"] = (text, [g[:4] for g in group])
    return out


async def _query_vector(query: str):
    client = vector._embedding_client
    if client is None or not query:
        return None
    try:
        vectors = await client.embed([query])
    except Exception:  # an excerpt choice is not worth failing the read over
        return None
    if not vectors:
        return None
    return np.asarray(vectors[0], dtype=np.float32)


def _block_quote(claim: _Candidate, entry: tuple, query_bits, query_grams: set[str]) -> dict:
    """The quoted text for one claim, at block granularity (§4.3, invariant 9).

    A block is a clause, which is small enough to be read and small enough to
    say the opposite of its record. So what is quoted is not the block but the
    contiguous range that governs it: the sentence it sits in, and a neighbour
    that qualifies it. Where the rule wanted more than the context limit allows,
    the quote carries `context_incomplete` and an `expand` that names the block
    range it was reaching for — it is a passage to read further from, not
    complete evidence.

    The expand carries the revision of the text these offsets were measured in.
    A block range validates itself (the triggers drop a record's blocks when its
    text changes), and the revision is what makes that refusal legible rather
    than leaving a caller to infer it from an empty answer.
    """
    text, block_rows = entry
    ranked = rank_blocks(text, block_rows, query_bits, query_grams)
    index, _, _, _ = ranked[0]
    spans = [(start, end) for _, start, end, _ in block_rows]
    start, end, complete = blocks.context_range(text, spans, index)
    quote: dict = {
        "content": text[start:end],
        "block": {"index": index, "of": len(block_rows), "span": [start, end]},
    }
    if not complete:
        first = next(i for i, (s, e) in enumerate(spans) if s == start)
        last = next(i for i, (s, e) in enumerate(spans) if e == end)
        wanted = [max(0, first - 1), min(len(spans) - 1, last + 1)]
        quote["context_incomplete"] = True
        quote["expand"] = {
            "ref": claim.ref,
            "block": wanted,
            "revision": blocks.text_revision(text),
        }
    return quote


def _quote(
    claim: _Candidate, node_sets: dict, query_vec, query_grams: set[str], cap: int,
    not_current: frozenset[str] | set[str] = frozenset(),
    node_orders: dict[str, list[int]] | None = None,
    block_sets: dict | None = None,
    query_bits: bytes | None = None,
) -> dict:
    """The quoted text for one claim, cut as the preview tier cuts.

    A record with a current block set is quoted at block granularity; one with a
    current node set and no blocks is quoted from its best node, as before; one
    with neither is quoted from its start. A quote that is cut and did not come
    from a node is only the record's start, not the part that matched;
    `node_unavailable` says so and why. A record quoted whole needs no such
    note, whether or not it has nodes.
    """
    block_entry = (block_sets or {}).get(claim.ref)
    if block_entry is not None:
        quote = _block_quote(claim, block_entry, query_bits, query_grams)
        content = quote["content"]
        if cap > 0 and len(content) > cap:
            quote["content_len"] = len(content)
            quote["content"] = content[:cap]
            quote["content_truncated"] = True
            span_start = quote["block"]["span"][0]
            quote["block"]["span"] = [span_start, span_start + cap]
            # Cut, so what is shown is a prefix of the context rather than the
            # context: the same treatment a cut node gets, and the same reason.
            quote["context_incomplete"] = True
            quote.setdefault(
                "expand",
                {
                    "ref": claim.ref,
                    "block": quote["block"]["index"],
                    "revision": blocks.text_revision(block_entry[0]),
                },
            )
        return quote
    entry = node_sets.get(claim.ref)
    if entry is None:
        quote: dict = {"content": claim.content}
    else:
        text, node_rows = entry
        ranked = rank_nodes(text, node_rows, query_vec, query_grams)
        index, start, end, _ = ranked[0]
        if node_orders is not None:
            node_orders[claim.ref] = [row[0] for row in ranked[:TRACE_NODE_ORDER]]
        quote = {"content": text[start:end], "node": {"index": index, "of": len(node_rows), "span": [start, end]}}
    content = quote["content"]
    if cap > 0 and len(content) > cap:
        quote["content_len"] = len(content)
        quote["content"] = content[:cap]
        quote["content_truncated"] = True
        if "node" in quote:
            start = quote["node"]["span"][0]
            quote["node"]["span"] = [start, start + cap]
            # The quote is the start of a node several times its length. Reading the
            # rest of that node is the cheapest next step, so it is handed over as the
            # argument get_contents takes, rather than left to be assembled from `node`.
            quote["expand"] = {"ref": claim.ref, "node": quote["node"]["index"]}
        else:
            quote["node_unavailable"] = NODES_NOT_CURRENT if claim.ref in not_current else NODES_NONE
    return quote


def _filled_quote(claim: _Candidate, block_entry: tuple | None, query_bits, query_grams: set[str], cap: int) -> dict:
    """An item's head quote: the parts of its record that matched, filled to `cap` (2.6).

    The ranking, the governing-context rule and the filling are the recall excerpt's
    (`excerpts.fill_ranges`), so a head quote and a recall excerpt of the same record for the
    same query are the same passages. With a current block set (read only while block
    retrieval is on) the blocks are ranked lexically and by their bits; without one the
    record is divided at read time and ranked by shared words. A record no longer than the
    cap is quoted whole, and one that divides into a single block from its start.

    `quote_basis` says which of those it was and `ranges` gives the quoted spans in the
    record's text, in text order; the spans are joined by `excerpts.SEPARATOR`.
    """
    text = block_entry[0] if block_entry is not None else claim.content
    if len(text) <= cap:
        return {"content": text, "quote_basis": "whole", "ranges": [[0, len(text)]]}
    if block_entry is not None:
        block_rows, basis, bits = block_entry[1], "blocks", query_bits
    else:
        divided = blocks.segment(text)
        block_rows = [(i, s.start, s.end, None) for i, s in enumerate(divided)]
        basis, bits = "lexical", None
    quote: dict = {"content_len": len(text), "content_truncated": True}
    if len(block_rows) <= 1:
        return {**quote, "content": text[:cap], "quote_basis": "start", "ranges": [[0, cap]],
                "expand": {"ref": claim.ref, "span": [0, len(text)]}}
    spans = [(start, end) for _, start, end, _ in block_rows]
    ranked = rank_blocks(text, block_rows, bits, query_grams)
    ranges, severed = excerpts.fill_ranges(text, spans, ranked, cap)
    quote.update(content=excerpts.SEPARATOR.join(text[s:e] for s, e in ranges), quote_basis=basis,
                 ranges=[[s, e] for s, e in ranges])
    if severed:
        # The best passage alone was longer than the cap: what is shown is its start.
        start = ranges[0][0]
        end = blocks.context_range(text, spans, ranked[0][0])[1]
        quote["context_incomplete"] = True
        quote["expand"] = {"ref": claim.ref, "span": [start, end]}
    return quote


def allocate(entries: list[tuple[dict, dict, list[dict]]], budget: int) -> tuple[list[dict], int, bool]:
    """Section 7's one fixed sequence, cut to the budget.

    `entries` are (item, head quote, other quotes most relevant first), in item
    order. The sequence is every head in item order, then each item's first
    remaining excerpt in item order, then each item's second, and so on. The
    response carries the longest prefix of it that fits `budget`: an item whose
    head falls outside is not returned, and an excerpt outside is omitted while its
    claim and ref stay. Because the budget only chooses the prefix length, raising it
    alone never removes an item or an excerpt (invariant 9).

    The first head is always admitted. A budget is never below one preview-tier
    excerpt, so it fits whenever the preview tier is on; with the tier disabled the
    first item is still returned whole rather than returning nothing.

    Returns (items, used characters, whether a head was cut).
    """
    used = 0
    heads = 0
    for position, (_, head, _) in enumerate(entries):
        cost = len(head["content"])
        if position and used + cost > budget:
            break
        used += cost
        heads += 1
    admitted = entries[:heads]
    taken = [0] * heads
    stopped = heads < len(entries)
    rounds = max((len(others) for _, _, others in admitted), default=0)
    for r in range(rounds):
        if stopped:
            break
        for i, (_, _, others) in enumerate(admitted):
            if r >= len(others):
                continue
            cost = len(others[r]["content"])
            if used + cost > budget:
                stopped = True
                break
            used += cost
            taken[i] += 1
    items = []
    for (item, head, others), n in zip(admitted, taken):
        item = dict(item)
        item["content"] = head["content"]
        # An allowlist, so a key a quote grows does not reach the response by
        # accident — and does not fail to reach it by accident either: `block`
        # and `context_incomplete` are here because a reader that cannot see
        # them cannot tell a complete quotation from a severed one.
        for key in (
            "content_len",
            "content_truncated",
            "node",
            "node_unavailable",
            "expand",
            "block",
            "context_incomplete",
            "quote_basis",
            "ranges",
        ):
            if key in head:
                item[key] = head[key]
        # Both are omitted when empty: no excerpts carried, none withheld.
        if n:
            item["excerpts"] = others[:n]
        omitted = len(others) - n
        if omitted:
            item["excerpts_omitted"] = omitted
        items.append(item)
    return items, used, heads < len(entries)


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
    trace: bool = False,
    budget: int | None = None,
    time_cue: dict | None = None,
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
    from . import cue as _time_cue
    from . import providers
    from .memory_handlers import RECALL_LIBRARY_MAX_LIMIT

    # The providers this reconstruction runs with, read once (cpersona/providers.py).
    p = providers.active()
    # The time cue (docs/RECALL_PROCESS_DESIGN.md §2) is the candidate recall's: it
    # orders the candidate pool this reconstruction reads. Read here as well, so a cue
    # that cannot be read is refused rather than taken for an empty candidate pool.
    try:
        p.cue_interpreter.parse(time_cue)
    except _time_cue.TimeCueError as exc:
        return error_response(str(exc), items=[], returned_count=0)
    effective_count, count_policy = resolve_count(count)
    effective_budget, budget_policy = resolve_budget(budget, effective_count)
    bounds_top_k = config.RECONSTRUCT_TOP_K if top_k is None else max(1, int(top_k))
    # bug-437: report the effective retrieval bound, not only the larger request.
    effective_top_k = min(bounds_top_k, RECALL_LIBRARY_MAX_LIMIT)
    candidate_bound_clamped = effective_top_k < bounds_top_k
    bounds_max_hops = config.RECONSTRUCT_MAX_HOPS if max_hops is None else max(0, int(max_hops))
    bounds_max_evidence = config.RECONSTRUCT_MAX_EVIDENCE if max_evidence is None else max(1, int(max_evidence))

    # Stage 1: the declared names and aliases of the entities the query mentions go
    # to the lexical arm only. With none, retrieval is called exactly as before.
    cue_terms, cue_report = await associations.query_terms(
        agent_id, query, project_id=project_id, channel=channel
    )
    recall_result = await p.reconstruct_candidates.candidates(
        agent_id,
        query,
        effective_top_k,  # the candidate depth — NOT `count` (invariant 7)
        deep=deep,
        channel=channel,
        project_id=project_id,
        source_id=source_id,
        session_key=session_key,
        **({"lexical_terms": cue_terms} if cue_terms else {}),
        # The recall trace of the call this reconstruction rests on
        # (docs/RECALL_PROCESS_DESIGN.md §1.1), returned as trace.recall.
        **({"trace": True} if trace else {}),
        **({"time_cue": time_cue} if time_cue else {}),
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

    bounds: dict = {"top_k": bounds_top_k, "max_hops": bounds_max_hops, "max_evidence": bounds_max_evidence}
    if candidate_bound_clamped:
        bounds["effective_top_k"] = effective_top_k
    # Retrieval handed back exactly as many rows as it was allowed to. Whether more
    # lay beyond is not known here, so this is "reached", never "omitted".
    if total >= effective_top_k:
        bounds["reached"] = [BOUND_TOP_K]
    response: dict = {
        "items": [],
        "requested_count": count,
        "effective_count": effective_count,
        "returned_count": 0,
        "count_policy": count_policy,
        "requested_budget": budget,
        "effective_budget": effective_budget,
        "used_budget": 0,
        "budget_policy": budget_policy,
        "bounds": bounds,
    }

    response["reconstruction"] = {
        "policy": "v1", "candidate_count": len(candidates),
        "cluster_count": 0, "selected_count": 0,
        "excluded_without_provenance": total - len(candidates),
    }
    # bug-436: do_recall has already marked these notices as delivered to this
    # session. Forward them even when no candidate survives reconstruction.
    for notice in ("advisory", "update", "time_cue"):
        if notice in recall_result:
            response[notice] = recall_result[notice]
    if recall_result.get("gate_fallback"):
        response["gate_fallback"] = True
    if trace:
        response["trace"] = {"candidate_refs": [c.ref for c in candidates], "clusters": []}
        if cue_report:
            response["trace"]["cues"] = cue_report
        if "trace" in recall_result:
            response["trace"]["recall"] = recall_result["trace"]
    if not candidates:
        response["shortfall_reason"] = (
            "count_zero" if effective_count == 0 else (
                SHORTFALL_BELOW_QUALITY_THRESHOLD
                if recall_result.get("gate_fallback")
                else SHORTFALL_NO_RELEVANT_EVIDENCE
            )
        )
        return response if trace else _compact(response)

    await _candidate_context(agent_id, candidates)
    spans = await _episode_spans(agent_id, [c.row_id for c in candidates if c.kind == "ep" and c.row_id > 0])

    pool_refs = [c.ref for c in candidates]
    links = await associations.record_links(agent_id, pool_refs, project_id=project_id, channel=channel)
    uf = p.reconstructor.bundle(candidates, spans, links)
    grouped: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        grouped.setdefault(uf.find(i), []).append(i)
    clusters = [sorted(members) for _, members in sorted(grouped.items())]
    response["reconstruction"]["cluster_count"] = len(clusters)
    if trace:
        response["trace"]["clusters"] = [[candidates[i].ref for i in group] for group in clusters]

    why_by_ref = {candidates[i].ref: uf.why[i] for i in uf.why}

    # Item order is fixed before the walk: most relevant cluster first. Ranks are
    # distinct, so the order is total. The walk runs on the items the window
    # holds, in that order, so what it adds to an item does not depend on `count`
    # (an earlier item is always in the window when a later one is).
    ordered = sorted(clusters, key=lambda group: min(candidates[i].rank for i in group))
    # The window is filled by clusters the gate admitted a row of. A cluster of
    # reserved rows only never competes for it: a reserved row has no gate score,
    # so ranking it among admitted rows would put it last and a full window would
    # never reach it, and with room to spare it would take a place the gate did not
    # grant. It gets the place recall gives it instead (docs/BLOCK_REACH_DESIGN.md
    # §5): beside the window, after it, up to the same fixed bound, displacing
    # nothing. A cluster the window left out that holds a reserved row is eligible
    # for those places too, because the row is there only for the reservation. The
    # bound is recall's: it reserved at most blocks.BLOCK_RESERVATION rows, and a
    # held cluster holds at least one of them.
    window = [group for group in ordered if any(not candidates[i].reserved for i in group)][:effective_count]
    in_window = {id(group) for group in window}
    held = (
        [group for group in ordered if id(group) not in in_window and any(candidates[i].reserved for i in group)]
        if effective_count > 0
        else []
    )
    chosen = window + held
    if held:
        # The default budget fits one head per item of the window; the held places
        # are items too, so the default is taken for both. A budget the caller or an
        # operator named is not widened (resolve_budget takes it as given) -- it
        # bounds the payload, and the held items, being last, are cut first.
        effective_budget, budget_policy = resolve_budget(budget, effective_count + len(held))
        response["effective_budget"], response["budget_policy"] = effective_budget, budget_policy
    graph = await associations.walk_graph(
        agent_id,
        [candidates[i].ref for group in chosen for i in group],
        pool_refs,
        max_hops=bounds_max_hops,
        per_entity=bounds_max_evidence,
        project_id=project_id,
        channel=channel,
        source_id=source_id,
    )
    reached, walk_cuts = p.reconstructor.walk(chosen, candidates, graph, bounds_max_hops)
    providers.check_walk(reached, chosen, graph.rows if graph is not None else {})

    by_ref = {c.ref: c for c in candidates}
    selected = []
    for position, (members, walked) in enumerate(zip(chosen, reached)):
        rows = [candidates[i] for i in members]
        extra = []
        for ref, label, hops in walked:
            row = _Candidate(graph.rows[ref], rank=len(by_ref))
            row.context = graph.rows[ref]["context"]
            by_ref[row.ref] = row
            extra.append((row, label, hops))
        item, _ = p.reconstructor.structure(rows, why_by_ref, spans, bounds_max_evidence, extra, links)
        providers.check_structure(item, {row.ref for row in rows} | {row.ref for row, _, _ in extra})
        if position >= len(window):
            # Said in the words recall uses for the same row, so one reading covers both.
            item["admission"] = "reservation"
        selected.append(item)

    # Quote the selected items. Nodes are read only now, after the items and their
    # order are fixed, so the tree cannot change what comes back.
    entries_claims = []
    for item in selected:
        head = by_ref[item["head_ref"]]
        others = sorted(
            (by_ref[claim["ref"]] for claim in item["claims"] if claim["ref"] != item["head_ref"]),
            key=_relevance_key,
        )
        entries_claims.append((item, head, others))
    all_claims = [c for _, head, others in entries_claims for c in (head, *others)]
    node_sets, not_current = await _current_node_sets(agent_id, all_claims) if all_claims else ({}, set())
    # Blocks are read behind the same switch that lets the block arm run: off
    # means the index may exist and nothing reads it, and quoting is a read.
    block_sets = (
        await _current_block_sets(agent_id, all_claims)
        if all_claims and blocks.retrieval_enabled()
        else {}
    )
    query_vec = await _query_vector(query) if (node_sets or block_sets) else None
    if (node_sets or block_sets) and query_vec is None:
        response["quote_selection"] = QUOTE_LEXICAL_ONLY
    # The same vector, quantised the way a stored block is. One embedding call,
    # two representations: a block holds the sign of each dimension, and nothing
    # else about the query is needed to rank against it.
    query_bits = (
        blocks.pack_bits(query_vec.tolist()) if (block_sets and query_vec is not None) else None
    )
    query_grams = _trigrams(query)
    cap = config.RECALL_PREVIEW_CHARS
    entries = []
    # Unmeasured facts stay in the trace until an answer-reader evaluation shows a
    # reader uses them: the order the nodes of each quoted record ranked in (the best
    # few indices only -- the fused values are not calibrated across records), so the runner-up
    # is a place to read next, not a confidence.
    node_orders: dict[str, list[int]] | None = {} if trace else None
    head_cap = config.RECONSTRUCT_QUOTE_CHARS
    for item, head, others in entries_claims:
        head_quote = (
            _filled_quote(head, block_sets.get(head.ref), query_bits, query_grams, head_cap)
            if head_cap > 0
            else _quote(
                head, node_sets, query_vec, query_grams, cap, not_current, node_orders,
                block_sets, query_bits,
            )
        )
        other_quotes = [
            {
                "ref": c.ref,
                **_quote(
                    c, node_sets, query_vec, query_grams, cap, not_current, node_orders,
                    block_sets, query_bits,
                ),
            }
            for c in others
        ]
        entries.append((item, head_quote, other_quotes))
    items, used_budget, budget_cut = p.reconstructor.allocate(entries, effective_budget)
    providers.check_allocation(entries, items, used_budget, effective_budget, budget_cut)
    if node_orders:
        response["trace"]["node_order"] = node_orders
    response["used_budget"] = used_budget

    # Only items that are returned count: a cut inside an item the window or the
    # budget left out is not something this response withheld from its reader.
    returned_cuts = set().union(*walk_cuts[:len(items)])
    omitted = [
        name
        for name, cut in (
            (BOUND_EVIDENCE, any("claims_omitted" in item for item in items)),
            (BOUND_HOPS, BOUND_HOPS in returned_cuts),
        )
        if cut
    ]
    if omitted:
        bounds["omitted"] = omitted
    # The graph read holds a bounded number of records per reached entity. When
    # one had more, the evidence bound was met without knowing what lay beyond.
    if BOUND_EVIDENCE in returned_cuts and BOUND_EVIDENCE not in bounds.get("reached", []):
        bounds.setdefault("reached", []).append(BOUND_EVIDENCE)

    response["items"] = items
    response["returned_count"] = len(items)
    response["reconstruction"]["selected_count"] = len(items)
    held_returned = sum(1 for item in items if item.get("admission") == "reservation")
    providers.check_reconstruct_count(
        len(items), held_returned, effective_count, blocks.BLOCK_RESERVATION + _time_cue.SEATS
    )
    if held_returned:
        # Beside the window, not in it: returned_count may exceed effective_count by this.
        response["reserved_count"] = held_returned
    if len(held) > held_returned:
        # Held items come last, so a budget that cuts anything cuts them first. Not a
        # short window, but a bound dropped them, and invariant 4 says which bound.
        response["reserved_omitted"] = len(held) - held_returned
    if effective_count == 0:
        response["shortfall_reason"] = "count_zero"
    # A held item does not fill the window, so a short window is judged without them.
    if len(items) - held_returned < effective_count:
        response["shortfall_reason"] = (
            SHORTFALL_BUDGET_EXHAUSTED
            if budget_cut
            else SHORTFALL_BELOW_QUALITY_THRESHOLD
            if recall_result.get("gate_fallback")
            else SHORTFALL_EXHAUSTED_CANDIDATES
        )
    return response if trace else _compact(response)
