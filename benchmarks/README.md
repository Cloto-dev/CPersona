# CPersona LMEB Benchmark Harness

Measurement harness for the LMEB benchmark (22 tasks, arXiv:2603.12572,
which subsumes LoCoMo / LongMemEval and 20 other memory-retrieval tasks).
This is the official regime behind the benchmark numbers published in the
top-level README.

Two tracks are measured:

- **Track A** (`benchmark_lmeb.py`) — the raw embedding model on LMEB via
  mteb. This is the baseline: what the embedding alone can do.
- **Track B** (`benchmark_trackb_lmeb.py`) — the same embeddings routed
  through CPersona's real `do_store()` / `do_recall()` code paths (SQLite,
  FTS5, RRF fusion, auto-calibration). The only substitution is the
  embedding client, which returns pre-computed vectors instead of calling
  an HTTP backend. Track B − Track A is the pipeline's contribution.

## Files

| File | Role |
| --- | --- |
| `benchmark_lmeb.py` | Track A runner (raw embedding baseline via mteb) |
| `benchmark_trackb_lmeb.py` | Track B runner (real cpersona store/recall paths) |
| `budget_batching.py` | Token-budget dynamic batching for SentenceTransformer encode (MPS pathologies workaround); shared by both tracks |
| `mps_accel.py` | Optional behavior-invariant recall acceleration (`--fast`): preloads each corpus group's embeddings into one matrix instead of per-query full-table scans. Zero changes to cpersona itself |
| `test_mps_accel_equivalence.py` | Equivalence gate proving `mps_accel` returns identical results to the original `_search_vector` (numpy backend: bitwise; torch: ≤1e-5), including a `do_recall` integration comparison |
| `run_trackb.sh` | Track B launcher encoding the official measurement regime |

## Prerequisites

- The LMEB evaluation framework and datasets checked out locally
  (`LMEB_DIR`, default `~/lmeb`), with datasets under `$LMEB_DIR/eval_data`.
- A Python environment with `sentence-transformers`, `mteb`, `numpy`,
  `torch`, and cpersona's own requirements installed.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `CPERSONA_REPO` | this repo (auto-detected from the script location) | cpersona checkout to benchmark; set it to point at another worktree (e.g. when bisecting) |
| `LMEB_DIR` | `~/lmeb` (launcher) | LMEB framework + `eval_data` location |
| `EMB_CACHE_DIR` | `~/lmeb/embcache` (launcher) | pre-computed embedding cache; makes re-runs encode-free |
| `EMB_CACHE_MODEL` | `$MODEL_PATH` | model label for cache keys |

**Embedding cache — never share a cache directory between models.** Cache
keys include the model label, but a misconfigured label silently serves
another model's vectors and invalidates the measurement. One directory per
model (e.g. `embcache`, `embcache_minilm`) is the safe pattern.

## Measurement regime (doctrine)

1. **Truncation layers off.** Full-precision ranking benchmarks run with
   `CPERSONA_AUTOCUT_ENABLED=false` and `CPERSONA_FUSED_GATE_ENABLED=false`
   (the launcher sets both). These layers exist for contamination
   prevention in live use; on a ranking metric they can only remove items
   from an already-ranked list, so they are neutral at best and lossy at
   worst. Their value is measured separately with precision-type metrics,
   not with NDCG.
2. **Fixed flags.** `--recall_mode rrf --auto_calibrate --budget_encode`,
   `--dtype float16`. Keep flags identical across runs you intend to
   compare.
3. **Calibration noise.** Auto-calibration samples with `ORDER BY
   RANDOM()`, so run-to-run NDCG noise of roughly ±1–3 pt per subtask
   (±1–2 pt per task mean) is inherent. Equivalence comparisons must share
   one in-process calibration state (that is what the equivalence gate
   does).
4. **`--fast` acceleration.** Behavior invariance is proven by the
   equivalence gate and monitored during runs via `--selfcheck_rate`
   sampling. It is safe for exploration and regression hunting; for final
   published numbers, prefer a run without `--fast`.

## Usage

Track A (baseline):

```bash
LMEB_DIR=~/lmeb python benchmarks/benchmark_lmeb.py \
    --model_path BAAI/bge-m3 --budget_encode --device mps
```

Track B (official regime via launcher; extra args pass through):

```bash
OUTPUT_DIR=trackb_results bash benchmarks/run_trackb.sh
# exploration run with acceleration:
OUTPUT_DIR=trackb_results_fast bash benchmarks/run_trackb.sh --fast --batch_size 1024
```

Equivalence gate (run after touching recall internals or `mps_accel.py`):

```bash
LMEB_DIR=~/lmeb python benchmarks/test_mps_accel_equivalence.py \
    --tasks LoCoMo --device mps \
    --model_path sentence-transformers/all-MiniLM-L6-v2 --backends numpy,torch
```

Long runs should be launched fully detached
(`nohup bash benchmarks/run_trackb.sh > run.log 2>&1 &`).

## Results

Result JSON directories (`trackb_results*`, `lmeb_results*`) and run logs
are working artifacts and are not committed to this repository.
