# Pre-registration — does the shipped release carry the long-memory recovery?

Registered before the run started.

## The question

A fall in retrieval quality was found across versions, traced by bisection to
the handling of punctuation in the query handed to full-text search, and fixed
by a policy that keeps the symbols inside identifiers while treating sentence
punctuation. The fix shipped in 2.6.0a1.

The figures behind "it recovered" come from an exploratory screen: the
candidate policy applied on top of the pre-fix development commit. That is a
screen, not a release. This run measures the long-memory task on the release a
user can install today.

## The arms

| | Build | Where the number comes from |
|---|---|---|
| Pre-fix line | `e43ad34` | recorded: `trackb_results_dev_e43ad34_bgem3/LongMemEval.json`, macro 79.79 |
| Oldest kept record | v2.4.40 | recorded: `trackb_results_v2440_bgem3/LongMemEval.json`, macro 81.17 |
| This run | v2.6.0a4 (commit `1f3e896`) | to be measured |

## Held fixed

The launcher and every flag it pins (`--fast`, `--recall_mode rrf`,
`--auto_calibrate`, autocut and the fused gate disabled by the benchmark
doctrine), the model (bge-m3), the embedding cache directory, the eval data,
the machine, the single task, and no cap on queries per subtask.

## One difference, chosen deliberately

The harness comes from the current head; the package under test comes from the
tag, through `CPERSONA_REPO`. The tag's own copy of the Track B runner predates
a harness-only change that keeps an empty default prompt out of the embedding
cache key. Running the tag's copy would compute different cache keys, re-encode
the corpus, and measure a different regime from the arm it is being compared
against. On the four files the Track B path uses — the runner, the launcher,
the acceleration module and the batching module — the head's harness is
byte-identical to the harness that produced the pre-fix record.

## A known difference that is not the treatment

The v2.4.40 record was produced in July with an older harness. It is quoted as
the historical level, not as an arm run under today's conditions. The 81.17 the
screen compared itself against was a diagnostic intervention that reproduced
this record to two decimal places, not a newly run historical arm.

## The decision rule, fixed before the run

The measure is the macro NDCG@10 over the six subtask means, as the task file
reports it.

- **At or above 81.17** — the release stands at or above the level held before
  the fall.
- **Between 79.79 and 81.17** — the release improves on the pre-fix line without
  reaching that level; the shortfall is reported as a number.
- **At or below 79.79** — the release does not carry the gain the screen showed.
  That is reported, and every document citing the screen is corrected.

Reported in all three cases: the six subtask deltas against both records, and
how many subtasks move each way.

## Invalidation conditions, checked before the table is read

- Every batch hits the embedding cache in full — no partial `cache: k/n` lines.
- The `--fast` self-check reports no mismatch against the native path.
- The task completes with no cap on queries per subtask.

## What will not be claimed

Statistical significance, or a check on held-out data. The other twenty-one
tasks, which are not run here. That the overall fall of 1.55 points across
those tasks is resolved. Any latency figure from this run's clock: the machine
is shared and the regime is the full-ranking one, not the shipped path.

---

# Amendment — registered after the first run was invalidated, before its score was read

The first run finished and **its own invalidation condition fired**: the
acceleration self-check reported three mismatches out of three sampled queries,
and the harness printed `treat these scores as suspect until investigated`.

**The score of that run has not been read.** This amendment is written and
published first, so that what counts as a result is still fixed before anyone
knows what the result is.

## Why the condition is not simply dropped

The tempting reading is that the rule was too strict: the metric is NDCG@10, it
reads the first ten positions, and the three divergences are at ranks 597, 1121
and 1970 — two of them between entries holding exactly the same score, one at
the seventh decimal. On that reading nothing that matters moved.

That reading is incomplete, because the same check on the same task was clean
on the builds this run is compared against — seven checks and no mismatch on
the pre-fix arm, one check and no mismatch on the other. Three of three is not
the same instrument reporting the same thing it always reported. **Something
about this build makes the accelerated path and the reference path order
candidates differently**, and weakening the rule after seeing it fire would
delete that finding rather than answer it.

## The refined condition, and the evidence it requires

The condition becomes: **no divergence reaches the ten positions the metric
reads.** It is not satisfied by argument; it is satisfied by measurement:

- The run is repeated with the self-check sampling raised from one per cent to
  ten, so roughly fifty queries are checked against the reference path instead
  of three.
- Each mismatch line carries the **first** rank at which the two orders differ,
  zero-based. The evidence is the **minimum** of those ranks across every
  checked query.
- **At or below rank 9** on any checked query — a divergence reached the window
  the metric reads. The run is invalid, no score is read from it, and the
  reference document keeps citing the screen.
- **Above rank 9 on every checked query** — no sampled query's top ten was
  touched. The score may then be read and reported under the original decision
  rule.

## What this evidence does not settle

Fifty of five hundred queries are checked, not all of them. A minimum rank
above nine means no *sampled* query's window moved; it does not prove that none
of the unchecked queries' did. That limit is part of the result and is carried
into anything that cites it.

**The divergence itself remains a finding either way.** Whatever the score
turns out to be, this build orders equal-scoring candidates differently between
the two paths where earlier builds did not, and that is recorded as something
to investigate rather than something the amendment disposed of.

## What is kept

The first run's output directory is kept and marked as invalidated, following
this repository's practice of keeping invalid runs rather than deleting them.
The repeat writes to its own directory. The two runs compute the same scores by
construction — the self-check adds a reference computation, it does not change
the ranking that is scored — so if their macro figures differ, that difference
is itself reported.
