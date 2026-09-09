# What the pool-size gate costs, per task and per model

Measured 2026-09-09 with the frozen-stage replay (`benchmarks/frozen_replay.py`),
which scores the same fused ranking before the gate (S2) and after it (S3 =
Track B) on identical candidates. Where the two differ, the gap is the gate's
doing, not the fusion's. Twenty-two tasks, three embedding models; tasks with
more than 200 queries per subtask are 200-query-per-subtask subsamples. Every
sampled query matched the live pipeline row for row: 8,408 identity checks,
zero mismatches.

The mechanism, and why the gate is running at all in a regime whose doctrine
says the truncation layers are off, are in
[the research note](../../docs/research/frozen-stage-replay-2026-09.md).

## Means over the twenty-two tasks

| Model | Pre-gate (S2) | Track B (S3) | Gate |
|---|---:|---:|---:|
| all-MiniLM-L6-v2 | 50.11 | 48.30 | −1.80 |
| jina-v5-text-nano | 56.08 | 55.42 | −0.66 |
| bge-m3 | 55.91 | 55.92 | +0.01 |

## Per task, where the gate moves any model by at least 0.2 points

| Task | MiniLM S2 → S3 | jina-v5-text-nano S2 → S3 | bge-m3 S2 → S3 |
|---|---|---|---|
| EPBench | 76.64 → 57.70 (−18.94) | 87.67 → 77.47 (−10.19) | 90.11 → 89.94 (−0.17) |
| DeepPlanning | 37.53 → 29.99 (−7.54) | 54.21 → 54.21 (0.00) | 53.89 → 53.89 (0.00) |
| CovidQA | 76.50 → 72.45 (−4.05) | 82.84 → 81.03 (−1.80) | 81.91 → 81.91 (0.00) |
| QASPER | 42.69 → 40.15 (−2.53) | 46.11 → 42.43 (−3.68) | 47.84 → 48.00 (+0.16) |
| Gorilla | 29.02 → 26.59 (−2.43) | 34.22 → 33.83 (−0.39) | 33.01 → 33.01 (0.00) |
| ReMe | 59.20 → 57.70 (−1.50) | 60.68 → 61.32 (+0.64) | 58.88 → 58.88 (0.00) |
| LongMemEval | 76.79 → 75.62 (−1.17) | 80.09 → 80.76 (+0.67) | 79.78 → 79.78 (0.00) |
| LooGLE | 56.17 → 55.00 (−1.17) | 58.73 → 58.04 (−0.69) | 59.64 → 59.69 (+0.04) |
| NovelQA | 26.44 → 25.79 (−0.65) | 31.62 → 31.81 (+0.19) | 33.99 → 34.03 (+0.04) |
| MemBench | 62.60 → 62.79 (+0.19) | 64.71 → 65.50 (+0.78) | 64.07 → 64.31 (+0.23) |
| PeerQA | 26.05 → 26.74 (+0.69) | 29.65 → 29.66 (+0.01) | 29.11 → 29.11 (0.00) |
| MLDR | 79.64 → 79.25 (−0.39) | 80.13 → 80.13 (0.00) | 79.80 → 79.80 (0.00) |

The ten tasks not listed move every model by less than 0.2: ConvoMem,
ESGReports, KnowMeBench, LoCoMo, MemGovern, Proced_mem_bench, REALTALK,
SciFact, TMD, ToolBench.

## Reading

- **The loss concentrates where pools are tiny.** 99.6 % of EPBench's gate loss
  on the mid model sits in its four corpus groups of nineteen and twenty rows,
  where no lexical-only row can pass and the cosine branch also cuts relevant
  dense rows.
- **The published Track B figure for EPBench on the mid model — 3.4 points
  below the raw embedding — is a fused score 6.8 points above it that the gate
  reversed.**
- **The small positive entries are the gate working.** On larger pools it
  removes lexical-tail intruders, which is what it exists for.
- **It is not a fixed cost of the regime.** On bge-m3 it is inert on every one
  of the twenty-two tasks, because that model's dense rows clear the cosine
  threshold and fill the top ten on their own.

## Reproduction

```bash
LMEB_DIR=~/lmeb python benchmarks/frozen_replay.py \
    --model_path <model> --emb_cache_dir <per-model cache> \
    --tasks <the 22 task names> --out_dir replay_<model> \
    --max_queries_per_subtask 200
REPLAY_ROOT=. python benchmarks/replay_summary.py replay_<model>=<label> ...
```
