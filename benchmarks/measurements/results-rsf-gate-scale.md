# Results: rsf's gate on a fixed scale, its order unchanged (bug-247, second design)

Companion to `prereg-rsf-gate-scale.md`. The registered controls and rules come
first, then what was reported beside them, then the finding that withdrew the
design. Summaries of the runs are in `results-rsf-gate-scale.json`.

**Withdrawn before release (2026-09-29). bug-247 stays open.** Both registered
rules held: on Track B no task fell (macro 57.96 → 57.97), and on LongMemEval
no question fell at ten rows. Both rules run under the heuristic gate a store uses before its fused gate is
calibrated, as registered. Measured outside the registration, the calibrated
fused gate a deployment runs by default picked a threshold on this design that
emptied or thinned most answers on a private pack of real agent memories
([the calibrated gate](#the-calibrated-gate)). A design that passes its rules
only where the default configuration does not run is not a fix, so it was not
merged.

## Setup as run

- The design: `_rsf_score` keeps 2.6.0a8's per-channel min-max order;
  `_rsf_gate_score` sums the same channels on the first fix's fixed scale
  (a cosine as itself, a keyword score `s` as `s / (s + 8)`, the far channel
  weighted as in the order, not divided), and the gate and its calibration read
  it. Implemented in pull request #354 (`4b31265`), which was closed without
  merging; its measuring harness is `benchmarks/rsf_scale_measure.py` at
  `d84b437` in the same pull request.
- Amendment 1 to the registration, written before any `gate` arm ran, reads
  control 2 as relative order, fixes how T is run (one process per task, numpy
  on the CPU) and how L's `legacy` is built.
- **T**: LMEB Track B, 22 tasks, bge-m3 from the embedding cache, `rsf`,
  `--fast`, fused gate and autocut off, the pool-size gate on, the vector
  threshold held at 0.3 (no `--auto_calibrate`). `legacy` is `7cfdb59`, the
  parent of the implementing change; `gate` is `4b31265`.
- **L**: LMEB LongMemEval, every scene in its own channel at its session
  times (237,655 records in one store), recall inside the question's channel,
  `limit = 10`, `rsf`, the fused gate unset for both variants. The vector
  threshold was calibrated once and held: 0.4796. All 500 questions. `legacy`
  is the implementing checkout with `_rsf_gate_score` removed after fusion, so
  the gate reads `_rsf_score` as 2.6.0a8 does.

## Controls

**Control 1, determinism of T: held.** `legacy` was run twice on LoCoMo,
EPBench and REALTALK. Both runs gave 46.51, 90.34 and 41.09, identical to the
subtask, and every query's ranking was identical (1,976, 3,644 and 679 queries).

**Control 2, order identity on real data: held.** On all 500 questions of L,
the rows both variants returned appear in the same relative order.

## Rule T

**Held.** No task fell, and the macro mean over the 22 tasks rose by 0.01
(57.96 → 57.97). Five tasks rose, by 0.01 to 0.11; seventeen were unchanged. The
scores are the harness's per-task NDCG@10, which it reports to two decimals.

| Task | Type | legacy | gate | Δ | rows per query (legacy → gate) |
| --- | --- | ---: | ---: | ---: | --- |
| EPBench | Episodic | 90.34 | 90.35 | +0.01 | 15.63 → 19.20 |
| KnowMeBench | Episodic | 53.33 | 53.33 | +0.00 | 20.00 → 20.00 |
| LoCoMo | Dialogue | 46.51 | 46.51 | +0.00 | 20.00 → 20.00 |
| LongMemEval | Dialogue | 81.42 | 81.42 | +0.00 | 20.00 → 20.00 |
| REALTALK | Dialogue | 41.09 | 41.09 | +0.00 | 20.00 → 20.00 |
| TMD | Dialogue | 22.89 | 22.89 | +0.00 | 20.00 → 20.00 |
| MemBench | Dialogue | 63.75 | 63.86 | +0.11 | 20.00 → 20.00 |
| ConvoMem | Dialogue | 60.77 | 60.78 | +0.01 | 20.00 → 20.00 |
| QASPER | Semantic | 47.44 | 47.48 | +0.04 | 20.00 → 20.00 |
| NovelQA | Semantic | 36.54 | 36.54 | +0.00 | 20.00 → 20.00 |
| PeerQA | Semantic | 29.79 | 29.79 | +0.00 | 20.00 → 20.00 |
| CovidQA | Semantic | 85.16 | 85.16 | +0.00 | 20.00 → 20.00 |
| ESGReports | Semantic | 45.46 | 45.46 | +0.00 | 20.00 → 20.00 |
| MLDR | Semantic | 82.25 | 82.25 | +0.00 | 19.97 → 20.00 |
| LooGLE | Semantic | 66.00 | 66.00 | +0.00 | 20.00 → 20.00 |
| LMEB_SciFact | Semantic | 73.20 | 73.20 | +0.00 | 20.00 → 20.00 |
| Gorilla | Procedural | 34.62 | 34.62 | +0.00 | 19.62 → 19.98 |
| ToolBench | Procedural | 54.10 | 54.10 | +0.00 | 20.00 → 20.00 |
| ReMe | Episodic | 59.16 | 59.23 | +0.07 | 18.69 → 19.99 |
| Proced_mem_bench | Procedural | 51.92 | 51.92 | +0.00 | 20.00 → 20.00 |
| MemGovern | Procedural | 90.65 | 90.65 | +0.00 | 20.00 → 20.00 |
| DeepPlanning | Procedural | 58.68 | 58.68 | +0.00 | 20.00 → 20.00 |
| **Macro** | | **57.96** | **57.97** | **+0.01** | |

Both parts of the rule hold: the macro mean did not fall, and no task fell at
all, against the 1.00 allowed. The rises come from rows the design admits that
the legacy gate dropped (on EPBench, ReMe, Gorilla and MLDR `gate` also returned
more rows per query). The relative order of the rows both arms return is
unchanged by construction, so the rises are not a better ranking.

## Rule L

**Held.** No question's NDCG@10 fell in any type, and the macro mean rose.

| Type | n | legacy | gate | fell | rose | allowance |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| knowledge_update | 78 | 93.50 | 93.50 | 0 | 0 | 4 |
| multi_session | 133 | 75.35 | 78.30 | 0 | 22 | 7 |
| single_session_assistant | 56 | 91.29 | 91.29 | 0 | 0 | 3 |
| single_session_preference | 30 | 56.46 | 59.65 | 0 | 3 | 2 |
| single_session_user | 70 | 84.40 | 84.88 | 0 | 1 | 4 |
| temporal_reasoning | 133 | 77.18 | 79.81 | 0 | 18 | 7 |
| **Macro** | 500 | **79.70** | **81.24** | | | |

The rise is not claimed. `legacy` returned 4.39 rows on average and `gate`
10.00: on 493 questions `gate` returned more rows, on none fewer, and on 44 of
the 493 the added rows held an answer. At ten rows under the heuristic gate the
design admits more, which is the defect's other face (a row pinned to 0.0 by
min-max is no longer dropped); it is not evidence of a better ranking, since the
order is unchanged by construction.

## The calibrated gate

Found by a code review after the registered runs had started, and measured
outside the registration. A deployment runs the fused gate calibrated
(`CPERSONA_FUSED_GATE_ENABLED`, on by default) and recalibrates it at startup
when the scoring version moves, which this design does. Calibration samples
stored records as pseudo-queries and separates the scores of rows stored close
in time from the rest. Under this design those positives are usually found by
more than one channel and sum above 1.0, so the separating threshold lands
where a row found by one channel rarely reaches.

On a private pack of real agent memories (4,478 records, 300 questions with
their evidence records, `rsf` with confidence on), the calibration picked 0.05
on 2.6.0a8 and 0.8913 on this design. With each calibration restored the way a
server restores it at startup, at ten rows:

| Question type | n | rows (2.6.0a8 → design) | empty answers | evidence reached |
| --- | ---: | --- | --- | --- |
| T1 | 50 | 9.72 → 1.92 | 0 → 11 | 46 → 32 of 51 |
| T2 | 50 | 9.94 → 1.60 | 0 → 15 | 43 → 21 of 56 |
| T3 | 50 | 10.00 → 1.98 | 0 → 18 | 36 → 17 of 64 |
| T4 | 50 | 10.00 → 3.02 | 0 → 4 | 83 → 62 of 114 |
| T5 | 50 | 9.90 → 2.54 | 0 → 4 | 87 → 72 of 104 |
| T6 | 50 | 9.80 → 1.88 | 0 → 11 | no evidence records |
| **All** | 300 | **9.89 → 2.16** | **0 → 63** | **295 → 204 of 389** |

Evidence was lost on 81 questions and gained on 6. Twenty English paraphrases
of the pack's questions gave the same picture (evidence 14 → 7 of 21, 7 empty
answers). Under the heuristic gate the same pack and design returned ten rows
on every question and lost no evidence, which is why the registered rules,
which run there, could not see this.

The gate defect is real under the calibrated gate too: on 2.6.0a8 a row whose
fused score min-max pins to 0.0 is still below a calibrated 0.05.

## Not run

The registration listed Track B in the standard regime (`--auto_calibrate`,
one run per arm) as reported beside the rule. It was not run: the design was
withdrawn on the calibrated-gate finding first, and that comparison would add
context to a design that is not shipping.

## What the numbers do not show

- Rule L's rise comes with twice as many rows returned, as the first fix's
  did. Whether the extra rows help an agent that reads them was not measured.
- The calibrated-gate finding comes from one private pack. It shows the design
  failing under the configuration a deployment runs; it does not measure how
  any other fix for bug-247 would behave there.
- A fix for bug-247 has to hold under the calibrated fused gate as well as the
  heuristic one. Two directions remain open: a gate score that does not grow
  with the number of channels a row was found by (for example the mean or the
  maximum of the row's own votes), or a change to how the `rsf` fused gate is
  calibrated. Either needs a registration that measures the calibrated gate.
