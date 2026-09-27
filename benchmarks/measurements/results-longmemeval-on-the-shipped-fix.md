# Result — the long-memory task on the shipped release

Registered in `prereg-longmemeval-on-the-shipped-fix.md`, which was published
before the first run and amended, before any score was read, when that run's own
check stopped it.

## The conditions, as measured

| Condition | Measured |
|---|---|
| Every batch hits the embedding cache in full | no partial `cache: k/n` line in the run |
| No fallback to the unaccelerated path | `fallbacks: 0` |
| No cap on queries per subtask | none applied |
| No divergence reaches the ten positions the metric reads | 52 queries checked, 52 diverged, **smallest first-divergent rank 82** (zero-based) |

The amended rule reads the score when every checked query's divergence stays
outside the window the metric reads. It does here, by 72 positions.

## The result

| | macro NDCG@10 | source |
|---|---:|---|
| v2.4.40 | 81.17 | recorded, July, `trackb_results_v2440_bgem3` |
| `e43ad34`, before the fix | 79.79 | recorded, September, `trackb_results_dev_e43ad34_bgem3` |
| **v2.6.0a4 (commit `1f3e896`)** | **81.87** | this run, `trackb_results_v260a4_bgem3` |

**+0.70 against the level held before the fall, +2.08 against the line it
replaced.** Per subtask, against those two:

| subtask | v2.4.40 | before the fix | v2.6.0a4 | vs v2.4.40 | vs before |
|---|---:|---:|---:|---:|---:|
| knowledge_update | 92.12 | 92.19 | 93.01 | +0.89 | +0.82 |
| multi_session | 79.24 | 77.48 | 79.92 | +0.68 | +2.44 |
| single_session_assistant | 91.11 | 91.40 | 91.77 | +0.66 | +0.37 |
| single_session_preference | 59.28 | 58.69 | 59.28 | +0.00 | +0.59 |
| single_session_user | 84.97 | 81.02 | 86.63 | +1.66 | +5.61 |
| temporal_reasoning | 80.29 | 77.97 | 80.62 | +0.33 | +2.65 |

Five of six subtasks stand above the July record and the sixth is equal to it;
all six stand above the line before the fix.

## Two consistency checks that were not asked for and are worth recording

The screen that motivated this run reported 81.872540 on the candidate policy
applied to the pre-fix commit. The release reports the same figure to the two
decimals the task file keeps, and its per-type figures match the screen's where
the screen reported them. The gain measured on a candidate is the gain that
shipped.

The invalidated first run — same build, same regime, self-check sampling ten
times lower — produced the same macro. The self-check adds a reference
computation beside the scored one; this is the evidence that it does not
perturb what is scored.

## What this does not settle

- **One task of twenty-two.** The 1.55-point fall measured across the full set
  is not answered by this run, and no claim is made about it.
- **The July record was produced with an older harness.** It is quoted as the
  historical level, not as an arm run under today's conditions.
- **52 of 500 queries were checked** against the reference path. No sampled
  query's window moved; the unchecked ones were not observed.
- Neither statistical significance nor a check on held-out data.

## An open finding, not disposed of by the amendment

Every one of the 52 checked queries diverges between the accelerated path and
the reference path, from rank 82 downward. On the builds this run is compared
against, the same check on the same task was clean: seven sampled and one
sampled, none diverging.

Where the first divergence was printed in full it sits between entries holding
the same score — two records tied to the seventh decimal, ordered one way by
the matrix path and the other by the scan. That is consistent with this build
producing more exact ties deep in the candidate list than its predecessors did,
and it is a hypothesis, not a diagnosis. It does not touch this result, whose
condition was measured rather than assumed. It is worth an answer on its own:
a check that reports a mismatch on every query it looks at has stopped
discriminating between builds, and the next run that needs it will be told
nothing.
