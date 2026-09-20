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
