# Registration: recall latency on the reference machine, with the query embedded by a real model

Registered 2026-09-19, before any run it governs. The commit that adds this file is
pushed before the first measurement.

## Why this exists

[results-contiguous-index.md](results-contiguous-index.md) reports `do_recall` at
100,000 memories as 299 ms on the Intel N150, and
[results-recall-path-profile.md](results-recall-path-profile.md) reports 360.4 ms for a
broad query under the production configuration. Both harnesses hand the recall path a
stand-in query vector (`LocalEmbeddingClient` in `perf_contiguous_index.py`, a vector
drawn from a hash of the query text), so neither figure contains the time to embed the
query. The only end-to-end figure with a real model in the repository is from an
Apple M5 on v2.4.40 (`benchmarks/README.md`, "Measured results"). A statement of the form
"100,000 memories in under a second on a small PC" is therefore not yet measured for the
reference machine. This run measures it.

## What is measured

- **Machine**: the reference Intel N150 (4 threads, `governor=performance`), the same
  host and Python environment as the two records above (Python 3.11.2, numpy 2.4.6).
- **Code**: this branch's tree, which is `master` at `e255143` plus this file and the
  driver.
- **Corpus**: the synthetic corpus of `perf_contiguous_index.py` (`build_corpus`), 100,000
  memories, width equal to the model's, contiguous index built, scan window 100,000.
  Default configuration: `rrf`, confidence off, FTS on. No episodes.
- **Embedding**: CEmbedding 0.8.0 from PyPI (`cembedding[onnx]`), on the same machine,
  over HTTP at `127.0.0.1:8401/embed`, constructed the way `server.main()` constructs its
  client (cache size, TTL and timeout from `cpersona.config`).
- **Models**: `onnx_jina_v5_nano` (768 dimensions; the model CPersona is tuned for and the
  one the quick start installs) is the primary arm. `onnx_bge_m3` (1024 dimensions, the
  width the two earlier records used) is secondary and reported as measured; no
  statement depends on it.
- **Queries**: `topic {i} question`, the query shape of the earlier records (the word
  `topic` is in every row, so the lexical arms see the broad case). Three warm-up queries
  and 25 timed queries, every text distinct, so the client's cache never serves a timed
  query.
- **Pairing**: each timed text is recalled twice in alternating order — once with the
  stand-in vector (the earlier records' regime) and once with the real client — so the
  difference is the cost of embedding the query, measured under the same machine state.
- **Also reported**: the model's embed time alone (25 further distinct texts, direct
  client calls), load average before and after each arm, the process list's top CPU
  consumers before each arm.

## The rule, fixed now

For the primary arm (jina-v5-nano), with the 25 timed real-client `do_recall` samples:

| Outcome | What may be written |
| --- | --- |
| median < 1,000 ms **and** every sample < 1,000 ms | "Recall over 100,000 memories completes in under one second on an N100-class PC, including embedding the query" |
| median < 1,000 ms, some sample ≥ 1,000 ms | "The median is under one second" — stated as a median, with the maximum beside it |
| median ≥ 1,000 ms | No sub-second statement. The measured median and maximum are stated as they are |

The run is invalid, and is repeated once rather than read, if the one-minute load
average exceeds 1.0 when an arm starts, if any real-client sample errors or returns no
vector, or if the model's output width differs from the corpus width.

## What this does not measure

Recall quality (the corpus is synthetic), the production configuration with episodes,
concurrent requests, a cold start of the embedding server, or any machine other than
the reference one.
