# Behavior Contracts

> **Applies to: CPersona {{ version_line }}.** Statements here are verified against the
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
  **chronological** merge, not a score ordering. Chronological means the
  **instant** each timestamp names, not the text it is written in: a stamp in
  any UTC offset — and a naive one, which is read as UTC — lands where it
  belongs against every other stamp, whichever side of the merge wrote it. A
  message whose timestamp is missing or unparseable names no instant, so it is
  placed **ahead of every dated message**, in the order it was merged in; the
  end of the list is reserved for what is genuinely most recent.

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
rrf** — a scored row's `match_reason.signal` reports which branch actually keyed
for it. Rows that were never scored omit the key entirely: the FTS / keyword
rows a `cascade` recall fills with, and — with confidence off — the injected
profile row. With confidence on, the profile row is scored like any other and
carries `match_reason` too. Treat `match_reason` as present-or-absent, not as a
field on every row.

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

**The window is also a recency prior.** By keeping only the newest rows it
hands every recent memory a candidate field of N instead of the whole corpus,
and that is worth accuracy rather than costing it. Measured on 237,654 stored
documents, widening the window from 10,000 to 200,000 gained 4.93 NDCG@10 where
the answer lay below the window and lost 20.19 where it lay inside it, with no
result truncated. The loss is rank displacement: the vector retriever hands the
fusion its top `limit` rows, so a recent answer that ranked third among 10,000
candidates and thirtieth among 200,000 is not lower on that list, it is *off*
it, and its vote is gone. **So raising this number is not a pure relaxation** —
it extends the reach by removing the prior.

`CPERSONA_VECTOR_REACH` (default `0`) separates the two. It must be set **above**
`CPERSONA_MAX_MEMORIES` to do anything; at or below it, nothing changes and no
extra work runs. Above it, the rows between the window and the reach are ranked
as a **second list** — same threshold, same cut, same tie-break — and handed to
the fusion as one more ranked list. The window keeps its width, so every row
that places today keeps the vote it has today and older rows can only be added.
Two limits on where it applies: **fusion only** (`CPERSONA_RECALL_MODE=rrf` or
`rsf`; `cascade` concatenates stages rather than fusing lists, so it ignores the
setting), and **local vector search only** (with `CPERSONA_VECTOR_SEARCH_MODE=remote`
the service ranks under its own window). Under `rsf` the far list is fused as a
fourth channel, which lowers every fused score against the cosine-scale
`min_score` because the sum is divided by the number of active channels; the
setting's measurement is registered for `rrf`, and no claim is made about `rsf`.

`CPERSONA_VECTOR_FAR_LIMIT` (default `0`) bounds how many rows of that second
list are handed to the fusion — at the default, the response `limit`, which is
the list the reach produces on its own; above `0`, the smaller of that and the
number given, taken from the head of the same list, so it decides how many far
rows may vote and nothing about how any row is scored.

**Raising the window extends the reach and removes the prior in the same
motion** — measured, it cost 20 NDCG@10 points on recent answers for 5 on old
ones (`REACH_AND_RECENCY_PLAN.md`), so it is a knob with a price rather than
the supported answer for a larger corpus, and the default does not move until
the far vote is priced. Cost estimate: a
768-dimension float32 embedding is ~3 KB/row, so a 10,000-row window reads up
to ~60 MB per recall in the worst case (memories + episodes). Turning the reach
on costs the same way, per row: a recall reads `CPERSONA_VECTOR_REACH` −
`CPERSONA_MAX_MEMORIES` more embedding rows than it does today. With the
contiguous vector index built that read is the index's fast path; without one it
is the chunked table scan, whose latency at a reach of 200,000 on a 237,654-row
corpus was roughly double the default's, with the keyword channel as the floor
in both cases. Memory does not grow with either number beyond the chunk the scan
holds and the index file it maps. No archival or thinning routine is required:
the long-term model is *no physical deletion — old rows sink via windows and
decay*.

## 5. Dedup semantics: skip, not upsert

`store` deduplicates two ways, and the two are scoped differently:

- **`msg_id` dedup** — a `store` carrying a `msg_id` that already exists is
  **skipped** (`result: "skipped"`, echoing the existing row's id). This probe
  spans agent and project but **not `channel`**: the same `msg_id` written to a
  second channel is skipped against the first channel's row.
- **Content dedup** — an identical content string is likewise skipped, scoped to
  agent, project and channel. A unique index backs it, but only within an exact
  bucket (`agent_id, project_id, channel, content`), while the probe that runs
  first also sees the global pool. Two writers racing into *different* project
  buckets can therefore both land.

The critical consequence: **there is no upsert**. Re-storing a *changed*
content under the *same* `msg_id` does **not** update the stored row — it is
skipped. To change a stored memory, use `update_memory` (re-embeds
automatically), or `delete_memory` + `store`.

The flip side is a guarantee you can lean on: re-submitting **unchanged**
content is harmless by construction, which makes naive full re-submission of
a corpus safe. See the
[corpus indexing patterns](operations.md#corpus-indexing-and-sync-patterns) for
how to run a document index on top of these semantics.

A `store` that reaches the handler carries `result`: `stored` (row written),
`skipped` (dedup hit or persistence paused — nothing wrong), or `rejected`
(refused, with `reason`). One layer sits above that and answers in the generic
shape instead: with an ACL configured, a call the client is not permitted to
make returns `{ok: false, error: "permission_denied", tool, client_id}` and no
`result`. Branch on `ok is false` first, then on `result`.

## 6. Autocut fires only on similarity-scale signals

Autocut (largest-score-gap truncation) assumes score gaps encode relevance
breaks. That is only true of similarity-scale signals:

- **Fires**: under confidence scoring, or on a homogeneous raw-cosine list
  where every row carries the signal.
- **Deliberately inert**: under `rsf` and `rrf` ordering. Rank-fusion scores
  decay hyperbolically by construction — their gaps encode retriever overlap,
  not relevance breaks. Fusion-ordered results rely on the fused quality gate
  for contamination control instead — and since 2.6 on that gate alone, because
  the pool-size heuristic that used to stand behind it no longer applies to a
  fused row ([§12](#12-what-filters-a-fused-order)).

So in the default configuration (confidence off, `rrf` or `rsf`), tuning
`CPERSONA_AUTOCUT_MIN_RESULTS` **has no effect on recall size**. The knob that
does move the gate under fusion modes is `set_recall_precision` — see the
[tuning runbook](operations.md#tuning-recall).

## 7. Profile rows carry no score

The `update_profile` row is appended to recall responses as an injection row —
it does not participate in scoring. There is at most one: `profiles` is unique
on `(agent_id, user_id)` and every write path binds `user_id` to `''`, so a
second `update_profile` replaces the first rather than accumulating.

- The row is dropped **before any scoring branch** on a pool of fewer than 50
  rows, with confidence on or off. The pool is the summed memories + episodes
  count for the recall's isolation scope, so a small or narrowly scoped corpus
  gets no profile row however the rest of the recall is configured. Measured on
  a 30-row pool with a query no stored row answers — so `limit` cuts nothing —
  a confidence-on recall returned no messages at all, while the 50-row control
  in the same run returned the profile.
- Above that threshold, with **confidence off** (the default), profile rows have
  no score, sort last, and are **cut by `limit`** when the scored results
  already fill it. Measured under `rsf` with `limit=10` on a full corpus:
  **0 profile rows survived**.
- Above that threshold, with **confidence on**, profile rows receive a high
  confidence score and reliably surface near the top.

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

**The rescue path exists only under confidence scoring.** The rows it returns
are marked by the same backfill that runs when `CPERSONA_CONFIDENCE_ENABLED`
is on, so in the default configuration `gate_fallback` can never appear.

What an all-below-gate recall returns instead changed in 2.6: no qualified row,
and the reservation's marked rows in their place
([§11](#11-an-answer-is-never-shorter-than-the-reservation)). It no longer
returns nothing.

## 9. `lock_memory` protects; it does not boost

`lock_memory` protects a row from deletion and editing. It does **not**
affect ranking — a locked memory can still lose a recall. If the requirement
is "must never be *lost*", lock it. If the requirement is "must always be *in
context*", use deterministic injection (and see §7 for the profile caveat).

## 10. Response shapes: how to tell success from failure

Since **v2.5.2** the rule is uniform: **branch on `ok is false`, and treat any
response carrying `error` as a failure whether or not `ok` is present.**

Three things changed in that release, each because the previous shape made a
failure readable as a success:

- **`store` reports its outcome in `result`** — `stored` / `skipped` /
  `rejected` — instead of an always-true `ok`. A rejected write used to look
  like a successful one (see [§5](#5-dedup-semantics-skip-not-upsert)).
- **`check_health` reports the single `status` verdict**, without the former
  `healthy` boolean.
- **Every tool-level failure a handler returns now carries `ok: false`.** Most
  used to return `error` alone, with no `ok` to branch on. The explanation
  still travels in `error` — except on `store`, which puts it in `reason`.

Two shapes stay outside that rule, and always did:

- **The outermost MCP dispatch** answers an unknown tool name, or an exception
  escaping a handler, with a bare `error` and no `ok`. That layer is vendored
  from a library shared with the other Cloto servers, so aligning it is an
  upstream change rather than a local edit.
- **A successful read** (`get_contents`, `list_memories`, `list_episodes`,
  `get_profile`) returns its payload with no `ok` either.

Both are covered by the rule above, which is why the rule is phrased as "treat
a response carrying `error` as a failure" rather than "check `ok`".

## 11. An answer is never shorter than the reservation

Since **2.6** a recall over a scope that holds embedded rows does not come back
empty. When the retrieval leaves fewer than ten rows, the dense arm's top rows —
reserved before the admission floor, so a floor calibrated for a large corpus
cannot empty a small universe — are **appended** to the answer, and each one
carries `fallback: true`. The response reports how many in `fallback_rows`
(absent when none were appended).

A reservation row is not a hit. It promises no relevance; it says *this is what
there was*. Concretely:

- It is **appended**, so under the last-is-best order of [§1](#1-recall-return-order-last-is-best)
  every reservation row sits **before** every qualified row. A caller that
  ignores the marker still never finds one where the best answer belongs.
- It earns **no `recall_count` credit**, for the same reason a `gate_fallback`
  row earns none: the count raises a row's own confidence floor, and a row that
  came back because nothing else filled the answer must not climb on that.
- It obeys `exclude_contents`, and it stays inside `limit` — the reservation is
  a floor **inside** the caller's ceiling, never above it. The reservation holds
  ten candidates, so an exclusion shortens it rather than reaching deeper.
- It changes nothing about the rows that qualified: the same candidates, the
  same scores, the same order. The reservation is a cardinality contract over
  the path, not a scoring change.

**What replaced the empty response.** Before 2.6 an absolute admission floor
could leave the top ten empty — measured, for a third of one benchmark task's
queries — and the response was indistinguishable from "there is nothing here".
The reservation makes the two distinguishable in the other direction: rows plus
a marker, rather than silence.

A genuinely empty scope still answers empty. So does an empty-query listing,
which has no dense arm to reserve from.

## 12. What filters a fused order

Three things do, and since **2.6** the pool-size heuristic is not one of them.

- The **admission floor** the dense arm applies — the per-agent vector
  threshold, calibrated for this corpus.
- The **calibrated fused gate**, on the branch it was calibrated for
  (`CPERSONA_FUSED_GATE_ENABLED`, on by default; the operating point comes from
  `calibrate_threshold`).
- Nothing else. On a fused order the pool-size heuristic is not applied: against
  a reciprocal-rank score it was a cut at a lexical *rank* that moved with the
  pool — rank 40 above 500 rows, rank 21 at 196, and no lexical row at all at 30
  or fewer — and against a dense row's cosine it was a second absolute floor
  above the calibrated one the retriever had already applied. Measured, the two
  together cost one benchmark task 10.2 points on a mid embedding model and 18.9
  on the weakest.

On a **dense-only order** (`cascade`) the heuristic keeps its role: the argument
above is about a fused score, and there is none there.

The practical consequence is that turning the calibrated gate off under `rrf` or
`rsf` leaves the admission floor as the only filter, and that an uncalibrated
corpus is filtered by a default threshold rather than by one measured for it.
Calibrate rather than disable — see the
[tuning runbook](operations.md#tuning-recall).
