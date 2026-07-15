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
| `mps_accel_equivalence_gate.py` | Equivalence gate proving `mps_accel` returns identical results to the original `_search_vector` (numpy backend: bitwise; torch: ≤1e-5), including a `do_recall` integration comparison. Deliberately NOT named `test_*.py`: it is a standalone script that mutates `os.environ` at import time, so pytest must never collect it |
| `run_trackb.sh` | Track B launcher encoding the official measurement regime |
| `benchmark_latency.py` | Production-stack latency runner: end-to-end `do_recall()` / `do_store()` wall clock against a REAL HTTP embedding backend (CEmbedding `/embed`), in both `local` and `remote` (matrix `/search`) vector-search modes |

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
   `--unclamp_limit` is obsolete since 2.5.0 and accepted as a no-op:
   `do_recall`'s in-library cap is now the scan window (MAX_MEMORIES), so
   the harness's `limit=corpus_size` full-ranking convention works against
   a stock checkout — the agent-facing 100 cap moved to the MCP boundary
   (JSON Schema `maximum`), which the library path does not traverse. Only
   pre-2.5.0 checkouts (v2.4.38..v2.4.40, which capped at 100 in-library
   and under-measured large tasks: bge-m3 LongMemEval 81.17 → 48.98 at
   depth 100) still need the flag.
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
LMEB_DIR=~/lmeb python benchmarks/mps_accel_equivalence_gate.py \
    --tasks LoCoMo --device mps \
    --model_path sentence-transformers/all-MiniLM-L6-v2 --backends numpy,torch
```

Long runs should be launched fully detached
(`nohup bash benchmarks/run_trackb.sh > run.log 2>&1 &`).

### Prompted / task-adapter models

Some models need extra flags on both tracks:

- `--trust_remote_code` — the model ships custom modeling code (loaded from
  the Hub). Required by `jinaai/jina-embeddings-v5-text-nano`.
- `--default_task retrieval` — task-LoRA models select an adapter at load
  time; jina v5 refuses to encode without one.

Asymmetric-prompt models (jina v5 exposes `query` / `document` prompts) are
handled automatically: both tracks apply the query prompt to queries and the
document prompt to the corpus. The embedding disk cache keys include the
prompt, so the same text cached under both roles stays distinct. Promptless
models (bge-m3, MiniLM) are unaffected — they encode and cache exactly as
before.

```bash
# jina-v5-text-nano, Track A:
LMEB_DIR=~/lmeb EMB_CACHE_DIR=~/lmeb/embcache_jinanano \
EMB_CACHE_MODEL=jinaai/jina-embeddings-v5-text-nano \
python benchmarks/benchmark_lmeb.py \
    --model_path jinaai/jina-embeddings-v5-text-nano \
    --trust_remote_code --default_task retrieval \
    --budget_encode --device mps

# jina-v5-text-nano, Track B (flags pass through the launcher):
MODEL_PATH=jinaai/jina-embeddings-v5-text-nano \
EMB_CACHE_DIR=~/lmeb/embcache_jinanano \
OUTPUT_DIR=trackb_results_jinanano \
bash benchmarks/run_trackb.sh --unclamp_limit \
    --trust_remote_code --default_task retrieval
```

## Latency benchmark (production stack)

`benchmark_latency.py` measures what the ranking tracks deliberately do
not: end-to-end recall/store wall clock with a real HTTP embedding
backend. Track B's LookupEmbeddingClient removes encode cost to isolate
ranking quality; the latency runner puts it back, constructing the same
`EmbeddingClient` that `server.main()` uses.

One invocation measures one (corpus_size, mode) point against a
persistent DB shared across modes. Regime: real LMEB queries (unique per
timed pass, warmup separated so the client's LRU cache cannot serve
repeats), `limit=10`, `recall_mode=rrf`, corpus documents truncated to
2,000 chars (`--max_doc_chars` — cpersona memories are chat messages and
summaries, not book chapters), `CPERSONA_MAX_MEMORIES` = corpus size so
the local scan window covers the whole corpus, truncation layers at
production defaults.

```bash
LMEB_DIR=~/lmeb python benchmarks/benchmark_latency.py \
    --db /tmp/latency.db --corpus_size 10000 --mode local \
    --embed_url http://127.0.0.1:8401/embed --out latency_10k_local.json
```

Remote mode additionally needs the CEmbedding index backfilled from the
same DB (`scripts/backfill_embedding_index.py` in the CEmbedding repo,
one index DB per corpus — the namespace is keyed by agent id) and a
server restart so the namespace matrix is resident.

### Measured results (2026-07-12)

cpersona v2.4.40, jina-v5-nano via CEmbedding 0.6.1 (`/embed`, pad-to-longest),
200 real queries per cell, Apple M5. `local` scans SQLite blobs in-process;
`remote` sends the query text to CEmbedding 0.6.1's namespace-resident
matrix `/search`.

| Corpus | Recall p50 / p95 (local) | Recall p50 / p95 (remote matrix) |
|---|---|---|
| 1,000 | 19.8 / 28.5 ms | 15.6 / 23.7 ms |
| 10,000 | 51.1 / 73.4 ms | 29.9 / 42.6 ms |
| 100,000 | 444.2 / 534.5 ms | 171.1 / 251.2 ms |

Reference points: a bare `/embed` round trip for a short query is ~8 ms
(the encode share of every local recall); store p50 ranges 19–85 ms in
local mode and roughly doubles in remote mode (the `/index` push re-encodes
the document server-side). At 100k the local-vs-remote gap (~270 ms) is the
per-query full-table blob scan that the resident matrix eliminates; most of
the remaining remote time is the FTS5 retriever, which both modes share
under `rrf`. Numbers predating CEmbedding 0.6.1 are not comparable: 0.6.0's
fixed-length padding put a ~620 ms encode floor under every recall.

## Results

Result JSON directories (`trackb_results*`, `lmeb_results*`,
`lat*_*.json`) and run logs are working artifacts and are not committed
to this repository. The latency table above is the documented record.
