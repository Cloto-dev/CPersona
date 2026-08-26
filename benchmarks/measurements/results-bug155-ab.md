# Results: bug-155 cosine-backfill A/B

Companion to `prereg-bug155-ab.md` (incl. Amendment 1). All numbers below are
the pre-registered metrics, quoted in the registered order, computed over the
16-task set (corpus ≤ 50k docs) with the registered regime: bge-m3, cached
embeddings, `CPERSONA_CONFIDENCE_ENABLED=true`, truncation layers off,
threshold pinned at 0.3 on both sides, `--recall_limit 10`, 18,293 paired
queries per mode. Sides: master = 7f406da (unfixed), fix = a9abcec head of
`fix-374-cosine-backfill`. Runs 2026-07-24, Apple M5, one sequential pass
(`run_full_ab.sh`); both sides rank byte-identical corpora.

## P2 — rsf (the production ctools mode)

**NDCG@10, per task (master → fix, Δ = fix − master):**

| Task | master | fix | Δ |
|---|---|---|---|
| DeepPlanning | 33.73 | 34.97 | +1.24 |
| TMD | 9.08 | 12.83 | +3.75 |
| Gorilla | 16.30 | 22.34 | +6.04 |
| PeerQA | 16.98 | 24.59 | +7.61 |
| Proced_mem_bench | 37.56 | 48.04 | +10.48 |
| ToolBench | 28.19 | 40.51 | +12.32 |
| REALTALK | 22.31 | 36.44 | +14.13 |
| KnowMeBench | 21.13 | 36.12 | +14.99 |
| LooGLE | 31.33 | 46.34 | +15.01 |
| MLDR | 55.51 | 71.07 | +15.56 |
| ESGReports | 23.58 | 39.89 | +16.31 |
| LMEB_SciFact | 45.99 | 64.20 | +18.21 |
| LoCoMo | 21.35 | 40.48 | +19.13 |
| CovidQA | 42.08 | 62.12 | +20.04 |
| ReMe | 29.90 | 56.59 | +26.69 |
| EPBench | 26.11 | 52.93 | +26.82 |
| **MEAN (16 tasks, unweighted)** | **28.82** | **43.09** | **+14.27** |

Every task improved; the smallest delta is +1.24, the largest +26.82.

**Prevalence (master side):** 99.9% of queries return ≥1 cosine-less row in
their top-10; mean 6.55 such rows per query = **67.8% of all returned top-10
rows**. Under the production mode the defect is not an edge case — it is the
dominant shape of the output.

**Disturbance (master vs fix):** top-10 membership changed for 99.4% of
queries, order for 99.9%; mean Jaccard 0.293.

## P3 — cascade (the ClotoCore workaround path)

**NDCG@10 mean: 44.20 → 44.20 (Δ +0.00).** Per-task deltas span −0.20
(REALTALK) to +0.22 (Gorilla); 11 of 16 tasks are exactly 0.00.

**Prevalence (master side): 0.1%** of queries; 0.0% of rows. Cascade's staged
architecture fills the top-10 from the vector stage, whose rows always carry
a cosine — the bug's precondition almost never materializes.

**Disturbance:** membership changed 0.1% of queries, order 16.0%, mean
Jaccard 0.999. (Residual note: the 16% order-change with near-identical
membership is tie-order movement among equal-scored rows; with Δ = 0.00 it is
immaterial and was not chased further.)

## Pre-registered expectations — outcome

- "Prevalence is substantial (>10%)" — **confirmed**, far beyond: 99.9% of
  queries under rsf.
- "On bge-m3, ΔNDCG ≥ 0" — **confirmed**: +14.27 mean, 16/16 tasks positive.
- MiniLM secondary (trigger: |ΔNDCG| ≤ 1.0 pt) — **not triggered**.

## What this does and does not say

- The production configuration (rsf + confidence) was measurably degraded by
  bug-155 across every task tested; the backfill recovers +14.27 NDCG@10 mean
  with no per-task regression.
- The cascade path — the mode ClotoCore's contamination workaround pins — is
  effectively untouched by both the bug and the fix, so shipping the fix does
  not perturb that consumer.
- Out of scope, per prereg + Amendment 1: the six >50k-doc tasks (incl.
  QASPER, the strongest lexical-rescue task), MiniLM behavior, calibrated
  (non-0.3) thresholds, and production truncation layers. The
  calibrated-gate operating point should be re-derived after deploying the
  fix, since the confidence score distribution changes.
