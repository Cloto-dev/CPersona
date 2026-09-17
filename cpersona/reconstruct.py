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

from . import config, nodes, vector
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
    budget_cut = out.get("shortfall_reason") == SHORTFALL_BUDGET_EXHAUSTED or any(
        "excerpts_omitted" in item for item in out["items"]
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

    __slots__ = ("ref", "kind", "row_id", "content", "timestamp", "ts", "msg_id", "source_id", "source_type", "context", "rank")

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


def resolve_budget(requested: int | None) -> tuple[int, dict]:
    """The payload budget (section 7, "Breadth before depth").

        budget_base      = forced_budget ?? requested_budget ?? default_budget
        effective_budget = min(budget_base, max_budget)

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


def bundle(candidates: list[_Candidate], spans: dict[int, tuple[object, object]]) -> _Union:
    """Stage 2 — cluster by deterministic keys.

    These keys are the CEILING of what the server calls "the same memory".
    Semantic sameness is not judged here; nothing below looks at what a row says,
    only at the identifiers and timestamps it was stored with.
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

    return roles


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
) -> tuple[dict, int]:
    """Stage 4 — one cluster becomes one recall item.

    Ordered by time and by version; conflicting rows are kept and marked. Nothing
    is summarised and no text is merged: `content` is the head claim verbatim, and
    `head_ref` names it.
    """
    head = _head(members)
    # Every emitted claim and role target is a retained row. A cut keeps the
    # head, then the most relevant of the rest; age decides nothing about what
    # survives.
    dropped = max(0, len(members) - max_evidence)
    others = sorted((m for m in members if m is not head), key=_relevance_key)
    retained = {id(m) for m in [head, *others][:max_evidence]}
    ordered = sorted((m for m in members if id(m) in retained), key=_order_key)

    # One entry per retained row carries everything the item says about that row:
    # when it holds, why it is here, and how it relates to the others. The earlier
    # shape repeated each ref in parallel `timeline` and `evidence` arrays, which
    # for a one-row item cost more characters than the refs and reasons it carried;
    # a chronological view is `as_of` sorted, and `why` is the evidence. An empty
    # `roles` is omitted rather than sent as [].
    claims = []
    for m in ordered:
        claim: dict = {"ref": m.ref, "as_of": m.timestamp, "why": why.get(m.ref, "seed")}
        roles = _roles_for(m, ordered, spans)
        if roles:
            claim["roles"] = roles
        claims.append(claim)

    # The strongest key that formed this cluster answers "why is this a separate
    # item"; a row nothing linked to is independent because nothing claimed it.
    present = {why.get(m.ref) for m in members} - {None, "seed"}
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
    model = config.EMBEDDING_MODEL
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
                    if complete and all(g[4] == model and g[3] is not None for g in group):
                        out[f"{kind}:{parent_id}"] = (text, [g[:4] for g in group])
                    else:
                        not_current.add(f"{kind}:{parent_id}")
    return out, not_current


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


def _quote(
    claim: _Candidate, node_sets: dict, query_vec, query_grams: set[str], cap: int,
    not_current: frozenset[str] | set[str] = frozenset(),
    node_orders: dict[str, list[int]] | None = None,
) -> dict:
    """The quoted text for one claim, cut as the preview tier cuts.

    A quote that is cut and did not come from a node is only the record's start,
    not the part that matched; `node_unavailable` says so and why. A record quoted
    whole needs no such note, whether or not it has nodes.
    """
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
        for key in ("content_len", "content_truncated", "node", "node_unavailable", "expand"):
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
    from .memory_handlers import RECALL_LIBRARY_MAX_LIMIT, do_recall

    effective_count, count_policy = resolve_count(count)
    effective_budget, budget_policy = resolve_budget(budget)
    bounds_top_k = config.RECONSTRUCT_TOP_K if top_k is None else max(1, int(top_k))
    # bug-437: report the effective retrieval bound, not only the larger request.
    effective_top_k = min(bounds_top_k, RECALL_LIBRARY_MAX_LIMIT)
    candidate_bound_clamped = effective_top_k < bounds_top_k
    bounds_max_hops = config.RECONSTRUCT_MAX_HOPS if max_hops is None else max(0, int(max_hops))
    bounds_max_evidence = config.RECONSTRUCT_MAX_EVIDENCE if max_evidence is None else max(1, int(max_evidence))

    recall_result = await do_recall(
        agent_id,
        query,
        effective_top_k,  # the candidate depth — NOT `count` (invariant 7)
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
    for notice in ("advisory", "update"):
        if notice in recall_result:
            response[notice] = recall_result[notice]
    if recall_result.get("gate_fallback"):
        response["gate_fallback"] = True
    if trace:
        response["trace"] = {"candidate_refs": [c.ref for c in candidates], "clusters": []}
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

    uf = bundle(candidates, spans)
    grouped: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        grouped.setdefault(uf.find(i), []).append(i)
    clusters = [sorted(members) for _, members in sorted(grouped.items())]
    clusters, walk_truncated = walk(clusters, bounds_max_hops)
    response["reconstruction"]["cluster_count"] = len(clusters)
    if trace:
        response["trace"]["clusters"] = [[candidates[i].ref for i in group] for group in clusters]

    why_by_ref = {candidates[i].ref: uf.why[i] for i in uf.why}

    assembled = []
    for members in clusters:
        rows = [candidates[i] for i in members]
        item, _ = structure(rows, why_by_ref, spans, bounds_max_evidence)
        # Most relevant cluster first; the head ref makes the order total.
        assembled.append((min(r.rank for r in rows), item["head_ref"], item))

    assembled.sort(key=lambda t: (t[0], t[1]))
    selected = [item for _, _, item in assembled[:effective_count]]

    # Quote the selected items. Nodes are read only now, after the items and their
    # order are fixed, so the tree cannot change what comes back.
    by_ref = {c.ref: c for c in candidates}
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
    query_vec = await _query_vector(query) if node_sets else None
    if node_sets and query_vec is None:
        response["quote_selection"] = QUOTE_LEXICAL_ONLY
    query_grams = _trigrams(query)
    cap = config.RECALL_PREVIEW_CHARS
    entries = []
    # Unmeasured facts stay in the trace until an answer-reader evaluation shows a
    # reader uses them: the order the nodes of each quoted record ranked in (the best
    # few indices only -- the fused values are not calibrated across records), so the runner-up
    # is a place to read next, not a confidence.
    node_orders: dict[str, list[int]] | None = {} if trace else None
    for item, head, others in entries_claims:
        head_quote = _quote(head, node_sets, query_vec, query_grams, cap, not_current, node_orders)
        other_quotes = [
            {"ref": c.ref, **_quote(c, node_sets, query_vec, query_grams, cap, not_current, node_orders)}
            for c in others
        ]
        entries.append((item, head_quote, other_quotes))
    items, used_budget, budget_cut = allocate(entries, effective_budget)
    if node_orders:
        response["trace"]["node_order"] = node_orders
    response["used_budget"] = used_budget

    # Only items that are returned count: a cut inside an item the window or the
    # budget left out is not something this response withheld from its reader.
    omitted = [
        name
        for name, cut in (
            (BOUND_EVIDENCE, any("claims_omitted" in item for item in items)),
            (BOUND_HOPS, walk_truncated),
        )
        if cut
    ]
    if omitted:
        bounds["omitted"] = omitted

    response["items"] = items
    response["returned_count"] = len(items)
    response["reconstruction"]["selected_count"] = len(items)
    if effective_count == 0:
        response["shortfall_reason"] = "count_zero"
    if len(items) < effective_count:
        response["shortfall_reason"] = (
            SHORTFALL_BUDGET_EXHAUSTED
            if budget_cut
            else SHORTFALL_BELOW_QUALITY_THRESHOLD
            if recall_result.get("gate_fallback")
            else SHORTFALL_EXHAUSTED_CANDIDATES
        )
    return response if trace else _compact(response)
