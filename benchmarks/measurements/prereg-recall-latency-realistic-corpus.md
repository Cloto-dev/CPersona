# Registration: recall latency over 100,000 memories of real length, with their blocks

Registered 2026-10-04, before any run it governs. The commit that adds this file is
pushed before the first measurement on the reference machine.

## Why this exists

[results-recall-latency-with-embedding.md](results-recall-latency-with-embedding.md)
reports recall over 100,000 memories at a median of 446.6 ms on the Intel N150,
including embedding the query. Its corpus is the synthetic one of
`perf_contiguous_index.py`: every row is `memory row {n} about topic {n % 997}`
(nine cl100k tokens; 899,000 tokens for all 100,000 rows), written straight into the
table with a random vector. Two things a deployment's recall reads are missing from it:

- **Length.** The memory rows of one existing deployment, measured on 2026-10-04
  (5,707 rows), average 555.7 cl100k tokens (median 497, 90th percentile 977, longest
  3,353). At that length 100,000 memories are about 56 million tokens, about sixty times the
  synthetic corpus.
- **Blocks.** Since 2.6.0 the default configuration builds blocks for every record
  and reads them during recall. A direct insert builds none, so the earlier figure
  contains no block-arm work. The same deployment holds 12.5 blocks per memory.

A statement of the form "N memories, about M tokens of text, recalled in T" therefore
needs a corpus with real lengths, built through the store path. This run measures it.

## What is measured

- **Machine**: the reference Intel N150 (4 threads, `governor=performance`), the host
  and Python environment of the earlier record (Python 3.11.2, numpy 2.4.6).
- **Code**: tag `v2.6.4` plus this file, the corpus builder and the driver changes on
  this branch.
- **Embedding**: CEmbedding 0.8.0 from PyPI (`cembedding[onnx]`, onnxruntime 1.30.0) on
  the same machine at `127.0.0.1:8401`, `onnx_jina_v5_nano` (768 dimensions), on the
  CPU execution provider.
- **Corpus**: built by
  [build_realistic_corpus.py](build_realistic_corpus.py) with `--length-percentiles
  recall_latency_realistic/length_percentiles.json --rows 100000 --seed 20261004` from
  LongMemEval-M (`longmemeval_m.json`). Record lengths are drawn from the deployment's
  length percentiles and cut from consecutive turns of the file's distinct sessions;
  each record is stored through `do_store` as agent `perf.real`, the nodes and blocks
  the stores queue are built by the server's own functions, and the task queue is
  drained. The builder reports the realised length distribution, the cl100k token
  total and the row counts. It runs on a second machine (an Apple laptop, CEmbedding
  0.8.0 on the CPU execution provider, model files identical by SHA-256 to the
  reference machine's) and the finished database is copied to the reference machine.
- **Configuration**: the default (`rrf`, confidence off, FTS on, block building and
  block retrieval on), scan window 100,000 (`CPERSONA_MAX_MEMORIES=100000`), the
  contiguous index built by the driver, response limit 10, no episodes.
- **Queries**: the first 53 distinct questions of `longmemeval_m.json`, in file order:
  3 warm-up, 25 timed, 25 for the model's embed time alone. Each timed text is recalled
  twice in alternating order — once with the stand-in vector and once through the real
  client — as in the earlier record, so no timed query is served from the client's
  cache.
- **Settling**: after the warm-up, the driver waits until the one-minute load average
  is below 1.0, polling every 15 s for up to 15 minutes, and records how long it waited.
  The earlier record sampled the load right after building its corpus, so its validity
  check measured the driver's own build; here the corpus is built elsewhere and the
  check runs after the machine has come to rest.

### Two arms

| Arm | Embedding server | Purpose |
| --- | --- | --- |
| Primary | default threads (`ONNX_INTRA_OP_THREADS` unset: every physical core) | the statement below |
| Secondary | `ONNX_INTRA_OP_THREADS=1`, server restarted, same corpus | whether a thread limit should be recommended on machines this size |

The secondary arm runs after the primary. The earlier record found, without a
registration, that the default lets the embedding server take the cores recall needs
(about 170 ms); this arm puts that on a registered footing.

## The rules, fixed now

For the primary arm, with the 25 timed real-client `do_recall` samples:

| Outcome | What may be written |
| --- | --- |
| median < 1,000 ms **and** every sample < 1,000 ms | "Recall over 100,000 memories of real length (about M tokens in all) completes in under one second on an N100-class PC, including embedding the query." The median may be given in seconds to two decimals |
| median < 1,000 ms, some sample ≥ 1,000 ms | The median, stated as a median, with the maximum beside it |
| median ≥ 1,000 ms | No sub-second statement. The measured median and maximum are stated as they are |

M is the corpus's realised cl100k token total as the builder counted it, rounded to
two significant figures.

For the secondary arm: if its median is at least 10% below the primary arm's median
and its maximum is not above the primary arm's maximum, the record may recommend
`ONNX_INTRA_OP_THREADS=1` for an embedding server sharing four or fewer cores with
CPersona, stating both arms' medians and both arms' embed-alone times. Otherwise it
recommends nothing.

An arm is invalid, and is repeated once rather than read, if the load average does not
fall below 1.0 within the 15 minutes, if any real-client sample errors or returns no
vector, if the model's output width is not 768, if any recall returns no rows, or if
the corpus does not hold exactly 100,000 memories.

## What this does not measure

- Recall quality: the questions are real, but nothing here checks the answers.
- Japanese text. Blocks are cut by characters, and English text of a given token
  length has more characters than Japanese text of the same length: the 1,000-record
  trial of this builder made 31.6 blocks per memory, against the measured deployment's
  12.5. The corpus is heavier on the block arm than that deployment; it is
  representative of English memories of the same length.
- Episodes, concurrent requests, a cold start of the embedding server, or any machine
  other than the reference one.
