# Results: rsf on a fixed channel scale (bug-247)

Companion to `prereg-rsf-fixed-scale.md`. The numbers are quoted in the
registered order: the dev selection, then the one test decision, then what was
reported but is not part of the rule. Summaries of both runs are in
`results-rsf-fixed-scale.json`.

**Outcome: the fix ships with divisor `none` and H = 8.** Both decision rules
held on the test half: no type fell by more than its allowance, and the macro
mean NDCG@10 did not fall (it rose by 2.14 points). The rise is not claimed as
an improvement; see [what the numbers do not show](#what-the-numbers-do-not-show).

## Setup as run

- Instrument: `benchmarks/rsf_scale_measure.py`, LMEB LongMemEval, `rsf`,
  `limit=10`, bge-m3 from the embedding cache, every scene in its own channel
  (237,655 records pooled in one store).
- Fused gate unset for every variant (Amendment 1). The vector threshold was
  calibrated once per run and held for its variants: 0.4796 on dev, 0.4807 on
  test.
- Split: 50/50 within each type, seed 20260928. Dev 249 questions, test 251.
- Dev: code at `1f77635` (the registration and Amendment 1). Test: code at
  `9770edd`, where the chosen constants had become the defaults before the test
  half was read.
- Machine: Apple M5, 2026-09-27 (dev) and 2026-09-28 (test).

## Selection on dev

| Variant | Macro NDCG@10 | knowledge_update | multi_session | ss_assistant | ss_preference | ss_user | temporal | Rows returned |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy | 77.94 | 91.75 | 75.60 | 87.25 | 49.93 | 86.96 | 76.14 | 4.55 |
| none, H=1 | 76.55 | 90.12 | 74.07 | 89.00 | 42.33 | 86.85 | 76.94 | 10.00 |
| none, H=2 | 76.57 | 90.91 | 74.13 | 87.68 | 42.33 | 86.85 | 77.54 | 10.00 |
| none, H=4 | 77.51 | 90.12 | 74.67 | 89.00 | 45.67 | 87.91 | 77.71 | 10.00 |
| **none, H=8** | **79.97** | 91.56 | 80.34 | 87.97 | 53.65 | 88.35 | 77.97 | 10.00 |
| present, H=1 | 33.53 | 40.22 | 33.14 | 29.66 | 19.07 | 39.96 | 39.15 | 10.00 |
| present, H=2 | 37.12 | 48.80 | 33.99 | 32.47 | 19.36 | 48.71 | 39.39 | 10.00 |
| present, H=4 | 57.57 | 78.04 | 51.78 | 66.18 | 29.45 | 70.37 | 49.59 | 10.00 |
| present, H=8 | 75.62 | 90.44 | 75.64 | 81.85 | 49.34 | 84.44 | 72.01 | 10.00 |

The best fixed-scale variant is `none`, H = 8. The next, `none`, H = 4, is 2.46
points below, outside the 0.10-point tie band, so no tie-break applied.

## Decision on test

The chosen variant against `legacy`, paired per question. "Net" is the
questions whose NDCG@10 fell minus those whose NDCG@10 rose; the allowance is
max(2, ⌈0.05 n⌉).

| Type | n | Fell | Rose | Same | Net | Allowance | legacy | none, H=8 | Rows (legacy → fix) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| knowledge_update | 39 | 3 | 2 | 34 | +1 | 2 | 95.25 | 94.70 | 3.77 → 10.00 |
| multi_session | 67 | 11 | 17 | 39 | −6 | 4 | 75.11 | 77.17 | 4.91 → 10.00 |
| single_session_assistant | 28 | 0 | 1 | 27 | −1 | 2 | 95.33 | 96.05 | 1.96 → 10.00 |
| single_session_preference | 15 | 1 | 4 | 10 | −3 | 2 | 62.98 | 69.19 | 5.20 → 10.00 |
| single_session_user | 35 | 2 | 3 | 30 | −1 | 2 | 81.85 | 83.00 | 4.37 → 10.00 |
| temporal_reasoning | 67 | 10 | 16 | 41 | −6 | 4 | 78.21 | 81.50 | 4.46 → 10.00 |
| **Macro** | 251 | | | | | | **81.45** | **83.60** | 4.23 → 10.00 |

1. **No type falls**: holds. The largest net fall is +1 (knowledge_update,
   allowance 2).
2. **The mean holds**: holds. The macro mean changed by +2.14 points; the rule
   required a fall of less than 1.0.

Counting a change only when it exceeds 1e-9 gives the same fell and rose
counts in every type.

## What the numbers do not show

- **The fix returned more rows.** Under the uncalibrated gate the fixed scale
  returned the full ten rows for every question, against 4.23 for `legacy` on
  test. NDCG@10 over the rows returned can rise from the extra rows alone, so
  the +2.14 is not a comparison at the same number of rows, and it is not an
  improvement claim. The registration asks only whether the fix holds ground.
- **This is the regime before the fused gate is calibrated.** A deployment
  recalibrates its fused gate when the scoring version moves, and that gate
  was not measured here (Amendment 1 says why it cannot be on this dataset).
  How many rows a calibrated gate lets through under the fixed scale is not
  known from this measurement.
- **H = 8 is the edge of the registered grid.** Under `none` the dev macro rose
  with H at every step (76.55, 76.57, 77.51, 79.97). A larger H may score
  higher; finding out would be a new, exploratory measurement, and the test
  half has now been read.

## Track B

Reported after the decision, not part of the rule: LMEB Track B (22 tasks,
`rsf`, `--fast`) for `legacy` (the checkout before the fix, `8d124bc`) and for
the fix. Pending.
