# Results: the Recall Depth floor sweep

Companion to `prereg-recall-depth-floor-sweep.md`. The numbers are quoted in
the registered order, before any exploratory observation. Per-arm figures,
the dev / test assignment and the latency pass are in
`results-recall-depth-floor-sweep.json`.

**Outcome: the floor stays at 0.** Deeper fusion at a fixed count of ten did
not improve the returned rows. From a floor of 200 on it lowered macro NDCG@10
by 1.60 points, and the 95% interval includes zero. Decision rule 1 (the
no-effect exit) applies. The test half was not read.

## Setup as run

- Code: master `7445b5b` (2.6.0a7 plus the harness additions registered
  before any arm ran: the per-call depth check, the dev / test split and the
  latency window).
- Regime R1: `rrf`, fused quality gate on, autocut on, `min_similarity` 0.3,
  no calibration, `--recall_limit 10`, `--max_memories 300000`, bge-m3 from
  the embedding cache. The NDCG pass used `--fast` with the numpy backend,
  which is the bitwise-identical path.
- Dev half: 250 queries, split within each subtask with seed 20260925. The
  249 queries that have a relevant document are scored.
- Machine: Apple M5, 2026-09-25. The NDCG arms ran up to three at a time,
  which does not affect them because they are deterministic (control 1). The
  latency arms ran one at a time with nothing else from this sweep running.

## Controls

| Control | Registered expectation | Result |
|---|---|---|
| 1. Replicate (F0 twice, two database copies) | agree exactly | 249/249 rankings byte-identical; spread 0.00 |
| 2. Negative (F10 in R1) | byte-identical to F0 | 249/249 byte-identical |
| 3. Positive (Ffull in R1) | returned set changes for ≥ 1 query | set changed for 238/249 queries |
| Per-call depth check | 0 mismatches | 0 mismatches in every arm (249 NDCG-pass calls; 498 in each latency run) |

## Metrics (dev, R1, bge-m3)

Δ is the paired difference against F0: the macro mean over subtasks of the
per-query difference, with a 95% bootstrap interval (10,000 resamples,
stratified by subtask).

| Arm | Floor | Macro NDCG@10 | Recall@10 | Returned | Δ NDCG@10 vs F0 [95% CI] | Set changed | Order changed | Mean Jaccard |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| F0 | 0 | 28.25 | 27.16 | 10.00 | — | — | — | — |
| F10 | 10 | 28.25 | 27.16 | 10.00 | +0.00 [+0.00, +0.00] | 0 | 0 | 1.0000 |
| F20 | 20 | 28.21 | 27.20 | 10.00 | −0.05 [−1.10, +0.94] | 95 | 118 | 0.8390 |
| F50 | 50 | 27.82 | 26.72 | 10.00 | −0.44 [−2.66, +1.87] | 201 | 208 | 0.5614 |
| F100 | 100 | 27.51 | 26.43 | 10.00 | −0.75 [−3.17, +1.67] | 230 | 233 | 0.4571 |
| F200 | 200 | 26.65 | 25.62 | 10.00 | −1.60 [−4.41, +1.12] | 233 | 236 | 0.4356 |
| F500 | 500 | 26.65 | 25.62 | 10.00 | −1.60 [−4.41, +1.12] | 235 | 238 | 0.4312 |
| F1000 | 1,000 | 26.65 | 25.62 | 10.00 | −1.60 [−4.41, +1.12] | 236 | 239 | 0.4330 |
| F2000 | 2,000 | 26.65 | 25.62 | 10.00 | −1.60 [−4.41, +1.12] | 237 | 241 | 0.4329 |
| Ffull | 300,000 | 26.65 | 25.62 | 10.00 | −1.60 [−4.41, +1.12] | 238 | 243 | 0.4328 |

"Set changed" and "Order changed" count queries out of 249. The returned
count is 10 in every arm: the gate and autocut never cut a list below the
count here.

Per-subtask NDCG@10:

| Arm | knowledge_update | multi_session | single_session_assistant | single_session_preference | single_session_user | temporal_reasoning |
|---|---:|---:|---:|---:|---:|---:|
| F0 / F10 | 45.50 | 11.64 | 82.14 | 0.00 | 20.00 | 10.24 |
| F20 | 43.35 | 11.64 | 82.14 | 0.00 | 20.00 | 12.10 |
| F50 | 38.80 | 13.16 | 85.71 | 0.00 | 17.14 | 12.10 |
| F100 | 38.80 | 12.94 | 82.14 | 0.00 | 20.00 | 11.17 |
| F200 … Ffull | 37.23 | 12.94 | 78.57 | 0.00 | 20.00 | 11.17 |

Latency of `do_recall(limit=10)`, shipped scan window and library ceiling
(10,000), not the `--fast` path:

| Arm | p50 ms | p95 ms | mean ms |
|---|---:|---:|---:|
| F0 | 308 | 744 | 351 |
| F10 | 304 | 743 | 347 |
| F20 | 301 | 740 | 345 |
| F50 | 308 | 739 | 350 |
| F100 | 304 | 734 | 346 |
| F200 | 306 | 739 | 348 |
| F500 | 326 | 776 | 368 |
| F1000 | 327 | 763 | 369 |
| F2000 | 360 | 800 | 402 |
| Ffull | 611 | 1,047 | 654 |

In this pass Ffull is clamped to the shipped library ceiling, so it measures
a depth of 10,000.

## Decision

1. **No-effect exit.** N(Ffull) − N(F0) = 26.65 − 28.25 = −1.60, which is
   below +1.0. The floor stays at 0.
2. Rule 2 (choice) was not reached. For the record, no floor meets
   N(f) ≥ N(Ffull) − 0.5 with any gain over F0. Ffull alone would exceed the
   1,000 ms p95 bound.
3. and 4. Rules 3 and 4 apply only to an adopted floor. The test half, R2
   (`limit=5`), R3 (`rsf`), R4 (jina-embeddings-v5-text-nano) and the other
   21 tasks were therefore not run.

## Exploratory observations (not part of the decision)

- **Deeper fusion changes the rows without improving them.** At a floor of
  200 or more, 94–96% of queries return a different set (mean Jaccard 0.43
  against F0), and NDCG does not move up. The loss is concentrated in
  knowledge_update (−8.27) and single_session_assistant (−3.57). The other
  four subtasks move by +1.9 at most, in any arm. From 200 on, the curve is
  flat. One reading is that rows ranked below 200 in every arm do not
  change the fused top ten. This reading was not tested.
- **Caveat about the regime.** This runner puts the whole LongMemEval corpus
  (237,655 documents, 500 haystacks) into one store. It then filters each
  query's global top ten down to that query's own haystack. At a returned
  count of ten, almost every returned row belongs to another haystack:
  measured on F0, 93 of the 2,490 returned rows (3.7%) are in the query's
  own haystack (Ffull: 86, 3.5%). The filter removes the rest, so a query is
  scored on 0.37 rows on average. This is why the absolute level (28.25)
  sits far below runs that search each haystack in its own scope. What this sweep settles
  is the question it registered: does the depth floor help at a fixed count
  in this harness? It does not. Whether depth matters in a store that holds
  one user's history is a separate question, which this sweep does not
  answer.
- **Earlier evidence.** The 81.17 against 48.98 contrast that
  `docs/RELIABLE_RECALL_2_6.md` §4 and the comment at the floor's definition
  in `cpersona/config.py` cite is a difference in the returned count, not in
  depth (see the registration). Both passages should now cite this result.
