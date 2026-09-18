# Associative Memory — design

Status: design for the 2.6 line, not behaviour. This page fixes a declared
graph layer — registered terms with aliases, and relations an agent asserts —
and the one place that reads it first: [Reconstructive
Recall](RELIABLE_RECALL_2_6.md#7-reconstructive-recall-the-exit). It does not
change what `recall` returns, and with nothing declared it changes nothing at
all.

## 0. What this adds, and where it acts

Recall has two retrieval paths: the vector arm, which is probabilistic, and the
lexical arm, which matches surface text. Both miss the same kind of question —
one whose answer sits two records away from anything the question's words or
meaning reach directly, or one that names a thing under a different name than
the record does. Measured on a dialogue benchmark, multi-hop and open-domain
questions are where the fused ranking loses most.

Associative memory is a third path that is **deterministic by construction**.
An agent declares entities, their aliases, and subject–predicate–object
relations; the server stores the declarations verbatim and follows them with
SQL and pure functions. No model is called on either side of the server
boundary: extraction is the agent's job, storage and traversal are the
server's. Every fuzzy expansion tried on this store regressed on the
contamination benchmark, which is why association is exact where the other two
arms are approximate.

The first step is deliberately narrow:

| | this design | decided separately |
|---|---|---|
| Entities, aliases and relations are stored and walked | yes | |
| Reconstructive recall reads them (cues, bundling, evidence, roles) | yes | |
| A caller can ask for an entity's neighbourhood directly | yes (`traverse`) | |
| `recall` expands its own rows through the graph | **no** | yes — see §7 |
| Entities carry a stable embedding of their own | **no** | 2.6.1 — see §7 |
| The server infers relations it was not told | **no** | a later, separate layer — see §7 |

Reconstructive recall was chosen as the first reader because its contract
already has the seats: a bundling key for rows joined by a declared relation, a
bounded relation walk that is the identity today, and a role vocabulary of
which four words wait for declared relations. Nothing in that contract changes;
stages that were identity maps start doing work.

## 1. Schema

Four new tables. Nothing in `memories` or `episodes` changes. Every row carries
the same three isolation axes as a memory (`agent_id`, `project_id`,
`channel`), and a read follows the same rules a recall does: an agent never
sees another agent's graph, and a project bucket is read together with the
global pool.

```sql
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    project_id  TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,             -- the canonical name, as declared
    normalized  TEXT NOT NULL,             -- name after normalization (§2)
    declared_by TEXT NOT NULL,             -- 'agent' | 'operator'
    created_at  TEXT NOT NULL,
    UNIQUE (agent_id, project_id, channel, normalized)
);

CREATE TABLE entity_aliases (
    entity_id   INTEGER NOT NULL,
    alias       TEXT NOT NULL,
    normalized  TEXT NOT NULL,
    PRIMARY KEY (entity_id, normalized)
);
-- one normalized alias resolves to at most one entity within a scope:
CREATE UNIQUE INDEX entity_aliases_scope ON entity_aliases (normalized, entity_id);

CREATE TABLE entity_mentions (
    entity_id   INTEGER NOT NULL,
    ref         TEXT NOT NULL,             -- 'mem:<id>' or 'ep:<id>'
    declared_by TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (entity_id, ref)
);

CREATE TABLE relations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    project_id   TEXT NOT NULL DEFAULT '',
    channel      TEXT NOT NULL DEFAULT '',
    subject_kind TEXT NOT NULL,            -- 'entity' | 'mem' | 'ep'
    subject_id   INTEGER NOT NULL,
    predicate    TEXT NOT NULL,            -- normalized (§2)
    object_kind  TEXT NOT NULL,
    object_id    INTEGER NOT NULL,
    anchor_ref   TEXT NOT NULL DEFAULT '', -- the record that evidences it, or ''
    declared_by  TEXT NOT NULL,
    declared_at  TEXT NOT NULL,
    UNIQUE (agent_id, project_id, channel,
            subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref)
);
```

Two kinds of relation share the table, told apart by their endpoint kinds:

- **entity → entity** (`Kirari` *maintains* `mizeye`): what the walk follows.
- **record → record** (`mem:<newer>` *corrects* `mem:<older>`): what bundles two
  candidates into one item and what the role vocabulary reads.

Mixed endpoints (an entity related to a record) are accepted and stored but
are not read by this step.

This database does not use foreign keys, so consistency is held by triggers,
as it is for the overflow tree: deleting an entity deletes its aliases,
mentions and relations; deleting a memory or episode deletes its mentions and
every relation that names it as an endpoint or as its anchor. A declaration
whose evidence is gone is gone with it — a relation is never left pointing at
a row that no longer exists.

## 2. Declaring

Declarations come from two sources, and the server records which: an agent
through the tools below, or an operator through the dashboard. The server
never adds a third; it does not extract entities from stored text and does not
infer a relation from two that exist. Coverage is therefore exactly the
coverage of what was declared, which is the trade this layer makes: narrow and
certain, where the two other arms are wide and approximate.

**Normalization** is the only processing a declaration receives, and it is
lossless on the stored form: the declared `name`, `alias` and `predicate` are
kept as written, and a `normalized` twin — Unicode NFKC, case-folded,
whitespace collapsed to one space, trimmed — is what lookups compare. Two
declarations that normalize alike are one entity. No tokenizer, no
named-entity recognizer: a deterministic cut of a Japanese proper noun does not
exist, so the server does not attempt one.

**Riding on `store`.** The moment a memory is stored is the moment the agent
knows what it mentions. `store` accepts an optional `associations` object:

```jsonc
"associations": {
  "entities":  [{ "name": "MizEye", "aliases": ["mizeye", "ミズアイ"] }],
  "relations": [{ "subject": "Kirari", "predicate": "maintains", "object": "MizEye" }]
}
```

Entities named here are registered if new, and the stored memory is recorded
as mentioning each of them. Relations name their endpoints by entity name or
by record ref (`mem:<id>`); an endpoint that names an unknown entity is
registered too. The stored memory becomes the relation's `anchor_ref`. The
memory is stored whether or not the associations are valid: a malformed
declaration is reported in the response and dropped, never a reason to lose the
memory.

**Standalone.** `declare_associations` takes the same object plus an optional
`anchor_ref`, for declarations made after the fact or from the dashboard, and
`retract` — a list of relation ids and mention pairs to remove. Retraction is
the only way a declaration leaves the store; there is no automatic expiry, and
a wrong declaration stays wrong until retracted.

## 3. Where the graph is read: reconstructive recall

Reconstructive recall has four stages. The graph enters three of them; the
fourth, structuring, is unchanged.

**Stage 1 — candidates.** Before retrieval, the query is matched against the
aliases in scope (normalized substring match, longest alias first). For every
entity the query mentions, its canonical name and its other aliases are handed
to the **lexical arm only** as additional terms. The vector arm receives the
query unchanged: an appended alias would move the query embedding, and the
point of the expansion is to find the record that used a different name, not
to change what the query means. The candidate depth (`top_k`) is unchanged;
with no matching alias, retrieval is exactly what it is today.

**Stage 2 — bundling.** A new key, `cluster:relation`, joins two candidates
that a declared record → record relation connects. It sits after
`cluster:adjacent` in the key order. Sharing a mentioned entity does **not**
bundle: every memory that mentions the project's name would otherwise fold
into one item, which is the contamination this layer must not reintroduce.

**Stage 3 — the bounded relation walk.** Today the identity. Now, from each
candidate that retrieval surfaced directly, the walk follows: the entities the
candidate mentions; the entity → entity relations declared on them, up to
`max_hops`; and the records that mention the entities reached. Records the
walk reaches become **evidence inside the item** whose direct candidate led to
them — never a new item. Each carries `why: "relation:<predicate>"` and its
hop count. The walk stops at `max_hops`; what it dropped is reported in
`bounds.omitted` as it is for the evidence bound. Which reached records are
kept when there are more than `max_evidence` allows is decided by one written
order: fewer hops first, then the most recently declared relation, then the
lower record id.

**Roles.** A declared record → record relation whose predicate is a word of
the role vocabulary (`supports`, `supersedes`, `corrects`, `qualifies`,
`contradicts`, `temporal_predecessor`) is emitted as that role, in the
direction the vocabulary already fixes: the referenced row is the subject. A
relation with any other predicate still bundles and still explains itself in
`why`; it just is not a role.

The walk is one function — a candidate pool and the relations in scope in, an
extended pool with provenance out — and it is called exactly once per
reconstruction. That shape is what lets the recall process of section 1 call
the same function once per iteration later (§6).

## 4. `traverse`

Some questions want the entity, not the records: what is known to relate to
*MizEye*, and through which records. `traverse(entity, max_hops, limit)`
returns the entity's neighbourhood — its aliases, the relations declared on it
to the hop bound, the entities those reach, and the refs of the records that
mention each — as a graph, deterministic in order and bounded by `limit`.
It is the query tool for the graph itself. It returns no record text; a ref
expands through `get_contents` as it does everywhere.

## 5. Invariants

1. **`recall` is unchanged.** Nothing in this design is read on the `recall`
   path. Test: the recall test suite runs unmodified against a database with
   the new tables populated.
2. **An empty graph is a no-op.** With no entities, mentions or relations,
   `reconstruct` returns byte-identical output to the same call without the
   tables. Test: a fixture corpus reconstructed with and without schema v15
   compares equal; a mutation that consults the graph unconditionally must turn
   it red.
3. **Association never creates or reorders an item.** Item heads and item
   order with the graph populated equal those without it; the graph adds
   evidence inside items, bundles by declared record relations, and labels
   roles. Test: assert head refs and their order; a mutation that lets a
   reached record become an item must turn it red.
4. **No model is called.** Declarations are stored as written; the walk is
   SQL and pure functions.
5. **Bounded and deterministic.** Nothing is followed past `max_hops` or
   kept past `max_evidence`; truncation follows the written order of §3, and
   the same database, query and bounds give the same output.
6. **Every element says where it came from.** A relation records who declared
   it and, when it has one, the record that evidences it; a record the walk
   added says by which predicate and in how many hops.
7. **Isolation holds.** The walk never crosses `agent_id`; `project_id` and
   `channel` follow the read semantics of the call, and a relation is never
   followed into a scope the call could not read directly.
8. **No declaration outlives its endpoints.** Deleting an entity, memory or
   episode removes every alias, mention and relation that depends on it.

## 6. Relationship to the recall process, and to `recall`

The recall process of [section 1](RELIABLE_RECALL_2_6.md#1-deliberative-recall-the-recall-process)
is a later line. When it lands, declared relations become cues: a hit on a
registered entity names the relations declared on it, and the next iteration
looks there without a second fetch from the index. That is the walk of §3 run
once per iteration instead of once per call, which is why §3 keeps it a pure
function of a pool and the relations in scope. This design does not build the
loop; it builds the edges the loop will follow.

`recall` stays flat. An earlier decision had the walk inside `recall` behind a
gate, adding related rows below the direct hits. That is now deferred (§7),
for three reasons: `recall` keeps its byte-stable contract with no gate and no
A/B run; a related row added to a ranked list is exactly where fuzzy expansion
regressed before, whereas evidence inside an item is bounded, labelled and
never reorders; and with one walk call site the hop budget cannot compound
between a recall-side expansion and a reconstruct-side one.

## 7. Decided separately

- **Association inside `recall`** — rows reached through the graph ranked
  below direct hits, behind a default-off gate, admitted by an A/B run on the
  contamination benchmark. Deferred until the recall process has been measured;
  it may then be subsumed by it.
- **Entities as results** — a reconstruct item that *is* an entity rather than
  a quotation. Not in this step; `traverse` is the entity-centric query.
- **Entity embeddings** (2.6.1) — a stable vector per registered term, so a
  record whose wording drifted from the name still reaches it. Same feature,
  no new tool.
- **Inferred relations** — a layer that proposes relations with a confidence,
  grounded on this declared set. A different table and a different trust
  level; never merged into this one.
- **Keeping a relation whose anchor was deleted**, with the anchor cleared.
  This step deletes it, so that every anchor resolves; the alternative keeps
  the assertion and loses the evidence.

## 8. How the step is judged

The value this step claims is a capability: a deterministic path exists that
did not. It does not claim a benchmark gain, and it cannot be given one by
default — benchmark corpora contain no declarations, and with none the output
is byte-identical by invariant 2. A gain is measurable only once something
declares, and that measurement is registered before it runs:

- **Oracle arm**: relations derived mechanically from a benchmark's own
  annotations (evidence sessions, entity labels). Bounds what the server-side
  walk can contribute.
- **Extractor arm**: relations declared by an extraction step in the harness,
  outside the server. Measures what a real deployment gets — extraction quality
  multiplied by the walk.
- **Control**: the same questions with no declarations, which must equal the
  pre-change output.

Expected shape, stated in advance: any gain concentrates in multi-hop and
entity-centric question types; single-hop questions are unchanged, and a change
there counts as a regression of bundling, not a gain. Items that rise are
inspected one by one for whether the reached evidence answered the question or
merely surrounded it.

Before that measurement, the step ships on invariants 1–8, each with a test and
a named mutation that turns it red.
