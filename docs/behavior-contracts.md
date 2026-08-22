# Behavior Contracts

> **Applies to: CPersona 2.5.x.** Statements here are verified against the
> source of the current release line. Behaviors documented on this page are
> **contracts**: callers may rely on them, and a change goes through the
> pre-release ladder and release notes
> (see [RELEASE_LIFECYCLE_STANDARD](RELEASE_LIFECYCLE_STANDARD.md)) — it will
> not change silently.

This page collects the behaviors that are easy to assume wrong from the tool
names alone. Several of them were surfaced by production operators measuring
CPersona from the outside; where a behavior looks surprising, the rationale is
stated next to it.

---

## 1. Recall return order: **last is best**

`recall` sorts candidates best-to-worst internally, cuts to `limit`, then
**reverses** the slice. The response is ordered by ascending score — **the
final element is the strongest match**.

This is deliberate. LLMs attend most strongly to the end of their context
("lost in the middle"), so the strongest memory is placed on the near side of
the injection point.

Consequences:

- **Evaluation**: if you measure hit@k against a recall response, index from
  the **tail**. Measuring from the head inverts the result.
- **`recall_with_context` has a different contract**: it merges recalled
  memories with the conversation history you pass in and returns a
  **chronological** merge, not a score ordering.

## 2. Confidence scoring overrides the fusion mode

`CPERSONA_CONFIDENCE_ENABLED` (default `false`) is not a metadata-only switch.
With it **on**:

- the result set is **re-sorted by the confidence score**, and
- the quality gate keys on confidence instead of the fused score.

The fusion mode (`CPERSONA_RECALL_MODE=rrf|rsf|cascade`) still selects *which
candidates enter* the result set, but no longer decides the order you get
back. Measured on a 1,545-document corpus with 394 queries: with confidence
on, `rsf` and `rrf` returned identical rows in identical order for all 394
queries; with it off, they agreed on fewer than 10%.

The ranking / gate signal priority chain is: **confidence > rsf > cosine >
rrf** — each recall row's `match_reason.signal` reports which branch actually
keyed for that row.

Note that confidence is **not match strength**: it blends cosine similarity,
time decay, resolved status, and recall count into a separate quantity. An
exact-match row can legitimately score below a paraphrase row on this scale.

## 3. Episode boundary penalty

When episodes exist, memories older than the **latest episode boundary** are
multiplied by a decay factor:

```
factor = max(exp(-RATE × hours_before_boundary), FLOOR)
```

| Knob | Env var | Default |
|------|---------|---------|
| Enabled | `CPERSONA_EPISODE_PENALTY_ENABLED` | `true` |
| Rate | `CPERSONA_EPISODE_DECAY_RATE` | `0.01` |
| Floor | `CPERSONA_EPISODE_DECAY_FLOOR` | `0.5` |

- The **boundary is the latest episode's `created_at`**, scoped to the same
  isolation axes (agent / project / channel) as the query — an unrelated
  bucket's episode does not move your boundary.
- Memories at or after the boundary (the current session) are untouched
  (factor 1.0).
- With the defaults, the factor reaches the floor after **~69 hours**
  (`ln 2 / 0.01`); everything older than ~3 days is uniformly halved. The
  mechanism is a *soft preference for the current session*, not a fine-grained
  recency ranking — ordering decisions *within* the last few days are outside
  its resolution. (`RATE=0.002` stretches the ramp to ~2 weeks if you want a
  slower curve.)

**Bulk-import hazard**: the boundary is simply the newest episode row. If you
backfill historical conversations with `archive_episode`, the *import time*
becomes the boundary and every pre-existing memory falls into the penalized
region. Either do not backfill episodes, or set
`CPERSONA_EPISODE_PENALTY_ENABLED=false` for the import.

## 4. The vector scan window (`CPERSONA_MAX_MEMORIES`)

`CPERSONA_MAX_MEMORIES` (default `10000`) is **not a storage cap**. It is the
**vector retriever's scan window**: vector search considers the most recent N
rows (memories and episodes are each scanned under the window). Rows older
than the window are invisible to vector search — but remain reachable through
the FTS and keyword channels, which are not window-limited.

**Raising the env var is the supported answer** for larger corpora — the
constant exists as a knob, not a limit to engineer around. Cost estimate: a
768-dimension float32 embedding is ~3 KB/row, so a 10,000-row window reads up
to ~60 MB per recall in the worst case (memories + episodes). No archival or
thinning routine is required: the long-term model is *no physical deletion —
old rows sink via windows and decay*.

## 5. Dedup semantics: skip, not upsert

`store` deduplicates two ways, both scoped to the isolation axes:

- **`msg_id` dedup** — a `store` carrying a `msg_id` that already exists is
  **skipped** (`result: "skipped"`, echoing the existing row's id).
- **Content dedup** — an identical content string is likewise skipped
  (backed by unique indexes, so concurrent writers cannot race past it).

The critical consequence: **there is no upsert**. Re-storing a *changed*
content under the *same* `msg_id` does **not** update the stored row — it is
skipped. To change a stored memory, use `update_memory` (re-embeds
automatically), or `delete_memory` + `store`.

The flip side is a guarantee you can lean on: re-submitting **unchanged**
content is harmless by construction, which makes naive full re-submission of
a corpus safe. See the
[corpus indexing patterns](operations.md#corpus-indexing-and-sync-patterns) for
how to run a document index on top of these semantics.

`store` responses always carry `result`: `stored` (row written), `skipped`
(dedup hit or persistence paused — nothing wrong), or `rejected` (refused,
with `reason`).

## 6. Autocut fires only on similarity-scale signals

Autocut (largest-score-gap truncation) assumes score gaps encode relevance
breaks. That is only true of similarity-scale signals:

- **Fires**: under confidence scoring, or on a homogeneous raw-cosine list
  where every row carries the signal.
- **Deliberately inert**: under `rsf` and `rrf` ordering. Rank-fusion scores
  decay hyperbolically by construction — their gaps encode retriever overlap,
  not relevance breaks. Fusion-ordered results rely on the fused quality gate
  for contamination control instead.

So in the default configuration (confidence off, `rrf` or `rsf`), tuning
`CPERSONA_AUTOCUT_MIN_RESULTS` **has no effect on recall size**. The knob that
does move the gate under fusion modes is `set_recall_precision` — see the
[tuning runbook](operations.md#tuning-recall).

## 7. Profile rows carry no score

`update_profile` rows (up to **3**, most recently updated first) are appended
to recall responses as injection rows — they do not participate in scoring.

- With **confidence off** (the default), profile rows have no score, sort
  last, and are **cut by `limit`** when the scored results already fill it.
  Measured under `rsf` with `limit=10` on a full corpus: **0 profile rows
  survived**.
- With **confidence on**, profile rows receive a high confidence score and
  reliably surface near the top.

Do not treat the profile as a guaranteed always-injected channel unless you
run with confidence enabled. For *must-always-be-present* facts, the correct
mechanism is deterministic injection (your `CLAUDE.md` / system prompt), not
probabilistic recall — see [When not to use recall](operations.md#when-not-to-rely-on-recall).

## 8. `gate_fallback` responses are low-confidence

A recall response carrying `gate_fallback: true` (absent otherwise) means
**every candidate fell below the quality gate**, and the below-gate lexical
matches were returned instead of an empty result. Treat these rows as
low-confidence — typical for identifier/hash lookups whose exact match is
semantically distant from the query text.

## 9. `lock_memory` protects; it does not boost

`lock_memory` protects a row from deletion and editing. It does **not**
affect ranking — a locked memory can still lose a recall. If the requirement
is "must never be *lost*", lock it. If the requirement is "must always be *in
context*", use deterministic injection (and see §7 for the profile caveat).
