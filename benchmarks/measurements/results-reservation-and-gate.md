# What the reservation and the gate change, per task and per model

Measured 2026-09-10 with the frozen-stage replay (`benchmarks/frozen_replay.py`),
extended for this decision with three stages: the answer refilled from the
reservation, the gate with only its rank branch removed, and the gate with the
pool-size heuristic applied to no fused row. Eight tasks — the three the earlier
replay found affected, plus five on which admission and the gate are already
neutral — on three embedding models, 200 queries per subtask, 20,397 queries
replayed in all.

The stages, and what each one is:

| Stage | What it scores |
|---|---|
| S2 | the fusion, before any gate |
| S3 | after the pool-size gate — what the server returned through 2.5 |
| S3r | S3, then the reservation refills the answer to ten rows (D1 alone) |
| S3g1 | the gate with only the lexical **rank** cut removed, plus the refill |
| S3g2 | the pool-size heuristic applied to no fused row, plus the refill — **what ships** |

Two identities pin the arithmetic to the server rather than to this table.
**The reservation is the dense order**: on every one of the 20,397 queries, the
admitted rows refilled from the reservation scored exactly what the dense-only
ranking scored — 0 mismatches, which is what "admission is a prefix of the dense
order" means when it is measured instead of argued. And **the shipped shape is
what the live pipeline produces**: 900 sampled queries were additionally run
through the real `do_recall` and compared row for row with S3g2, with no
mismatch. (381 further samples compared the fusion only: on a task with a
candidate subset the harness filters *after* the recall, so the server counts its
ten rows over the whole corpus while the replay counts them inside the subset —
the one place the two cannot be compared.)

## jina-embeddings-v5-text-nano (mid)

| Task | S3 | S3r | S3g1 | S3g2 |
|---|---:|---:|---:|---:|
| EPBench | 77.47 | 86.44 | 81.29 | **87.67** |
| QASPER | 42.43 | 46.98 | 46.51 | 46.51 |
| Gorilla | 33.83 | 34.75 | 34.22 | 34.22 |
| ReMe | 61.32 | 61.41 | 61.32 | 60.68 |
| TMD | 23.72 | 23.72 | 23.59 | 23.59 |
| ESGReports | 41.19 | 41.19 | 41.19 | 41.19 |
| LMEB_SciFact | 76.81 | 76.81 | 76.81 | 76.81 |
| MLDR | 80.13 | 80.13 | 80.13 | 80.13 |

## BAAI/bge-m3 (strong)

| Task | S3 | S3r | S3g1 | S3g2 |
|---|---:|---:|---:|---:|
| EPBench | 89.94 | 90.05 | 89.97 | 90.11 |
| QASPER | 48.00 | 48.00 | 47.84 | 47.84 |
| Gorilla | 33.01 | 33.01 | 33.01 | 33.01 |
| ReMe | 58.88 | 58.88 | 58.88 | 58.88 |
| TMD | 23.00 | 23.00 | 23.00 | 23.00 |
| ESGReports | 41.36 | 41.36 | 41.36 | 41.36 |
| LMEB_SciFact | 74.33 | 74.33 | 74.33 | 74.33 |
| MLDR | 79.80 | 79.80 | 79.80 | 79.80 |

## all-MiniLM-L6-v2 (weakest)

| Task | S3 | S3r | S3g1 | S3g2 |
|---|---:|---:|---:|---:|
| EPBench | 57.73 | 71.83 | 71.75 | **76.72** |
| QASPER | 40.15 | 42.56 | 42.72 | 42.87 |
| Gorilla | 26.59 | 30.36 | 29.02 | 29.02 |
| ReMe | 57.70 | 58.20 | 57.74 | 59.20 |
| ESGReports | 35.54 | 36.41 | 35.54 | 35.54 |
| MLDR | 79.25 | 79.25 | 79.25 | 79.64 |
| TMD | 15.13 | 15.13 | 15.13 | 15.13 |
| LMEB_SciFact | 71.33 | 71.33 | 71.33 | 71.33 |

## What the numbers decided

**The reservation carries most of the recovery, and costs nothing anywhere.**
S3r is at or above S3 on every task of every model: +14.1 and +3.8 on the weakest
model's two worst tasks, +9.0 and +4.6 on the mid model's, and within 0.11 of
zero on the strong model, whose cosines sit above every floor so there is nothing
to starve.

**Dropping the heuristic is worth having on the weakest model and is a wash on
the other two.** Summed over the eight tasks, S3g2 − S3r is **+4.4** on
all-MiniLM-L6-v2, **−0.6** on jina-v5-text-nano and **−0.1** on bge-m3. Its
value is concentrated where the gate deleted the most, which is the model whose
cosines are lowest against a threshold calibrated for them.

**Removing only the rank cut is worse than removing neither.** On EPBench with
the mid model S3g1 scores 81.29 against S3r's 86.44 — the narrow reading
re-admits the lexical-only rows while the cosine branch still deletes the dense
rows they outrank, so the answer fills with the weaker arm. That result is why
the two branches ship as one decision rather than as a smaller change and a
larger one.

## Reproduction

```bash
LMEB_DIR=~/lmeb python benchmarks/frozen_replay.py \
    --model_path jinaai/jina-embeddings-v5-text-nano \
    --emb_cache_dir <cache> --trust_remote_code --default_task retrieval \
    --pin_from <a Track B output dir whose <task>.json carries calibration records> \
    --tasks EPBench,QASPER,Gorilla,LMEB_SciFact,ESGReports,TMD,ReMe,MLDR \
    --out_dir <out> --max_queries_per_subtask 200
```

with `--model_path BAAI/bge-m3` and `--model_path sentence-transformers/all-MiniLM-L6-v2`
for the other two rows (live calibration, no `--pin_from`). Each `<task>.json`
carries `S1r_reserved`, `S3r_reserved`, `S3g1_no_rank_cut` and
`S3g2_no_heuristic` in its `mean` block, the identity counters under `identity`,
and the per-query records in `<task>.queries.jsonl`.
