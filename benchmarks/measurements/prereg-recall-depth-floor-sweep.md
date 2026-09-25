# Pre-registration: choosing the default Recall Depth floor

Registered BEFORE any arm was executed or inspected. The instrument, arms,
regime, metrics, controls and decision rule below are fixed by this document;
any later deviation is written here as an amendment, with what had already
been seen when it was written.

## Question

Since 2.6.0a2, `limit` on `recall` is the number of rows returned, and the
per-arm depth handed to fusion is `max(limit, CPERSONA_RECALL_DEPTH_FLOOR)`
(`cpersona/config.py`, `cpersona/memory_handlers.py::_recall_depth`). The floor
ships at 0, so the depth still equals the count: a call with `limit=10` fuses
ten candidates per arm, and `limit=5` fuses five. The code comment at the
floor's definition says its default "is decided by measurement ... not here".
This is that measurement.

It answers, at a fixed returned count:

1. **Benefit** — does fusing deeper candidate lists improve the rows that come
   back, and at what depth does the gain stop?
2. **Cost** — what does each depth add to recall latency?

The floor applies only in the fusing modes (`rrf`, `rsf`) and only when the
query is non-empty; the cascade path fuses nothing and is out of scope.

## What earlier evidence does and does not show

The contrast most often cited for this change — LongMemEval macro NDCG@10 of
81.17 with full ranking against 48.98 with the library capped at 100
(`README.md` in this directory, the `CLAMPED-depth100` record) — **is not
evidence of a depth effect at a fixed count.** In that record the cap cut the
*returned list* to 100 rows. The harness then filters a query's global ranking
down to that query's own candidate set (the LMEB subset-retrieval convention,
`benchmark_trackb_lmeb.py`), so an answer ranked below 100 across the whole
corpus was scored as a miss. The two numbers differ in how many rows came
back, not only in how deep fusion looked. A production call with `limit=10`
does not return a row ranked 101st either way.

The size of the depth effect at a fixed count is therefore **unknown** before
this run. A result of "little or no effect" is a possible and reportable
outcome, and the decision rule below keeps the floor at 0 in that case.

## Instrument

The Track B runner (`benchmark_trackb_lmeb.py`) through `do_recall()`, on the
LMEB LongMemEval task (six subtasks, 500 queries, 237,655 documents).

- **Returned count fixed by `--recall_limit`.** The NDCG pass calls
  `do_recall(limit=N)`; only `CPERSONA_RECALL_DEPTH_FLOOR` differs between
  arms. The full-ranking convention (`--recall_limit 0`) is not used for any
  arm, since it is exactly the returned-count difference described above.
- **Scan window wide enough to see the whole corpus** (`--max_memories
  300000`), so the vector arm's scan window is not a second variable. The
  shipped window is used in the latency pass only (below).
- **Embeddings from the existing disk cache** (bge-m3); nothing is re-encoded
  for the primary model.
- **Dev / test split.** Within each subtask the queries are split 50/50 by a
  shuffle with seed 20260925 before any arm is run. The floor is
  chosen on dev and confirmed on test; test is not read until dev has chosen.

### Harness additions, committed before any arm runs

1. **A per-call depth check.** Every recall response in a run must report the
   depth this registration expects for it (`depth` is present exactly when it
   differs from `limit`). One mismatch invalidates the run; the floor is read
   from the environment at import, and a run that silently used the default
   would otherwise look like a null result.
2. **A latency pass at the shipped scan window.** The existing
   production-shaped latency pass pins the window at 500, a value that no
   longer ships. The pass used here sets `CPERSONA_MAX_MEMORIES` to the
   shipped default (10,000) and measures `do_recall(limit=10)` for every arm.
3. **The dev / test split** above (the runner has no seed option today), with
   the assignment written to the output so it cannot be redrawn after an arm
   is seen.

## Arms

Floor values (the depth is `max(limit, floor)`):

| Arm | Floor | Note |
|-----|------:|------|
| F0 | 0 | what ships today |
| F10 | 10 | equals `limit` in R1: negative control |
| F20 | 20 | |
| F50 | 50 | |
| F100 | 100 | |
| F200 | 200 | |
| F500 | 500 | |
| F1000 | 1,000 | |
| F2000 | 2,000 | |
| Ffull | 300,000 | clamped to the library ceiling = the whole corpus: the plateau reference |

Every arm builds its database from the same embedding cache in the same
store order. That the build is deterministic is not assumed: the replicate
control below measures it.

## Regime

Shipped defaults of 2.6.0a7, not benchmark doctrine, because the mechanisms a
larger candidate pool may disturb — the fused quality gate and autocut — are
part of what a user gets:

- `CPERSONA_RECALL_MODE=rrf` (the shipped default), fused quality gate **on**,
  autocut **on**, `VECTOR_MIN_SIMILARITY=0.3`, no `--auto_calibrate` (its
  sampling is the harness's only run-to-run noise source).
- **R1 (primary)**: `limit=10`, the MCP default.
- **R2 (secondary)**: `limit=5`, the shape of a caller that asks for few rows.
- **R3 (secondary)**: R1 with `CPERSONA_RECALL_MODE=rsf`.
- **R4 (secondary)**: R1 with jina-embeddings-v5-text-nano in place of bge-m3.

## Metrics (fixed)

Per query, from the returned list, against the dataset's qrels:

- **NDCG@10** — headline. Aggregated as the macro mean over the six subtasks
  (the mean of subtask means), the form the recorded LongMemEval numbers use.
- **Recall@10.**
- **Returned count** — how many rows survived the gate and autocut. A deeper
  pool moves the gate's and autocut's inputs, so the count can change even
  though `limit` does not.
- **Disturbance** against F0 on the same query: share of queries whose
  returned set changed, share whose order changed, mean Jaccard of the sets.
- **Latency** p50 / p95 of `do_recall(limit=10)` per arm, from the latency
  pass at the shipped scan window, on one machine that is named in the
  results.

No maximum statistics. Confidence intervals are 95% paired bootstraps over
queries, stratified by subtask, 10,000 resamples.

## Controls

1. **Replicate** — F0 is run twice on two copies of the database. With
   calibration off and the corpus fixed, the two runs are expected to agree
   exactly. Any spread is reported, and if it exceeds 0.2 NDCG points the
   thresholds below are re-derived from it before any other arm is read.
2. **Negative control** — in R1, F10 must return byte-identical rankings to F0
   for every query (the floor does nothing at or below the count). Any
   difference means the floor reaches something it should not, and the run
   stops.
3. **Positive control** — Ffull must change the returned set against F0 for at
   least one query in R1. If it changes nothing, the floor is presumed not to
   be applied and the instrument is repaired before any conclusion is drawn.
   "No membership change anywhere" is a dead detector, not a null result.
   (Membership changes with no NDCG change *are* a valid null.)

## Decision rule (stated before the run)

On **dev**, R1, bge-m3, let N(f) be macro NDCG@10 at floor f.

1. **No-effect exit.** If N(Ffull) − N(F0) < 1.0, the floor stays at 0. Depth
   is not where this task's quality is decided at `limit=10`, and a default
   that adds latency for less than a point is not adopted.
2. **Choice.** Otherwise f\* is the smallest floor in the table with
   N(f\*) ≥ N(Ffull) − 0.5 **and** a latency-pass p95 ≤ 1,000 ms. The latency
   bound exists because an agent calls recall several times per question and
   waits on each call. If no floor meets both, the floor stays at 0 and the
   cost is reported.
3. **Confirmation on test.** f\* is adopted as the new default only if, on the
   test half, the per-query paired difference f\* − F0 has a macro mean
   ≥ +1.0 and a 95% interval whose lower end is above 0.
4. **No regression elsewhere.** At f\*, each of the following must hold, or f\*
   is not adopted and the regressing case is reported:
   - R2 (`limit=5`): macro Δ ≥ −0.5 against F0.
   - R3 (`rsf`): macro Δ ≥ −1.0 against F0.
   - R4 (jina-embeddings-v5-text-nano): macro Δ ≥ −1.0 against F0.
   - Each of the other 21 tasks in the runner's task list (`TASK_MAP` in
     `benchmark_trackb_lmeb.py`), R1, bge-m3: Δ ≥ −1.0 against F0 on each
     task.

A change in the returned set without a change in NDCG is not counted as a
regression: "the rows moved" and "the rows got worse" are different claims,
and only the second is priced here.

Changing the floor's default changes default behaviour, so an adopted f\* ships
through the pre-release ladder with the old and new values in the changelog.

## Outputs

`results-recall-depth-floor-sweep.md` next to this file, quoting the metrics
above in that order before any exploratory observation; the per-arm JSON the
runner writes; the dev / test assignment; and the latency pass. The per-subtask
table is reported for every arm, including the ones the decision does not use.
