# Pre-registration: sustained recall throughput under concurrency, across embedding thread settings

Registered 2026-09-20, before the harness for it exists. Nothing below is a result.

## The question

**What sustained recall throughput does the reference machine reach under concurrent load, and
does `ONNX_INTRA_OP_THREADS` order the arms the same way it does for a single request?**

## Why it is worth measuring

[`results-recall-latency-with-embedding.md`](results-recall-latency-with-embedding.md) measured a
single request at a time, and its own scope note says so: concurrent requests are not measured.
From that measurement one can only divide — 446.6 ms per recall with the embedding server on its
default thread setting gives 2.24 recalls/s, and 290.6 ms with the server held to one inference
thread gives 3.44 recalls/s. Those are **serialized upper bounds**, and a serialized bound is the
wrong number for anything that asks how much a machine can serve.

The second half of the question is the part that can overturn a conclusion rather than refine it.
The single-request measurement found that **limiting the embedding server to one inference thread
made the whole recall 35% faster**, because the server and the search were competing for the same
four cores. Under concurrency that reasoning may invert: with several requests in flight, a server
free to use every core can overlap work that one inference thread must serialize. **If the best
arm at concurrency 8 is not the best arm at concurrency 1, then `ONNX_INTRA_OP_THREADS` is a
throughput setting rather than a latency setting, and every figure derived from the serialized
bound has to be recomputed.**

## Fixed conditions

Held to the earlier measurement's conditions wherever they exist, so the two can be read together.

- **Machine**: the reference Intel N150, 4 threads, `governor=performance`. Occupancy is recorded
  immediately before each arm and again after it, because the host is shared with a running
  container; an arm whose before/after occupancy differ by more than 5% of one core is rerun.
- **Runtime**: the residual benchmark virtualenv, Python 3.11.2, numpy 2.4.6.
- **Corpus**: the synthetic corpus of `perf_contiguous_index.py` (`build_corpus`), 100,000
  memories, width equal to the model's, **contiguous index built**, scan window 100,000.
- **Model**: jina-v5-nano at 768 dimensions, served by the local embedding server over HTTP.
- **Call shape**: `limit` 10, one agent, no axis narrowing, queries drawn from the same generator
  the earlier measurement used.
- **Embedding cache**: disabled for the run, or every query text made distinct. A cache hit is not
  an embedding, and a run whose queries repeat would report the cache's throughput.

## Arms

`ONNX_INTRA_OP_THREADS` is set to 0 (the server's default — every physical core), 1, 2 and 4.
Each setting is run at offered concurrency 1, 2, 4 and 8.

Sixteen cells. Each cell runs until it has completed at least 200 recalls **after** a warmup of 20,
and the reported throughput is measured over the steady window only.

## What is recorded per cell

- completed recalls per second over the steady window
- latency p50 / p95 / max / min
- error count, and the count of responses that returned fewer than `limit` rows
- the before/after occupancy readings
- the embedding server's own reported dimension, to confirm the model did not change mid-run

## Registered decision rules

Written before any number exists, so that the reading is not chosen after seeing it.

| Observation | What it licenses |
| --- | --- |
| Throughput stops rising by more than 5% between two adjacent concurrency levels | That level is the **saturation point** for that arm, and the higher level is reported but not used for the cost figure |
| Saturated throughput of the best arm ≥ 2x its own serialized rate | "Concurrency recovers more than the serialized bound shows", and the serialized figures already published become **lower bounds** rather than estimates |
| Saturated throughput of the best arm < 1.25x its serialized rate | The serialized bound was close to the truth; the published figures stand as written |
| **The best arm at concurrency 8 differs from the best arm at concurrency 1** | `ONNX_INTRA_OP_THREADS` is a **throughput** setting. Every conclusion drawn from the single-request ordering must be recomputed, and the setting must be named in any sizing that assumes one |
| The best arm at concurrency 8 is the same as at concurrency 1 | The single-request ordering transfers, and the earlier reading needs no revision |

The cost figure this feeds is **node currency per million calls on this machine**, computed from
the saturated throughput. It is reported for each arm. **No figure for any other machine is
derived from this run** (see the scope note below).

## Instrument check, before any arm is read

The harness must be shown able to produce the difference it is looking for. A harness that issues
requests sequentially while believing it is concurrent would report throughput flat across
concurrency levels, and flat throughput is exactly what "already saturated at 1" looks like — the
two are indistinguishable from the output alone.

**Positive control**: on one arm, throughput at concurrency 4 must exceed throughput at
concurrency 1 by more than 25%. If it does not, the run is void and the harness is the suspect,
not the machine. The control is chosen on the arm where the effect should be largest
(`ONNX_INTRA_OP_THREADS=1`, where a single request leaves three cores idle).

**Negative control**: the count of distinct query texts reaching the embedding server must equal
the number of recalls issued. A shortfall means the cache was live and the arm is void.

## What this does not measure

- **Any machine other than the reference one.** The reference machine is x86 with 4 threads.
  Nothing here licenses a figure for a different architecture, core count, or provider.
- **Multiple tenants.** One corpus, one agent, one process. Throughput per tenant, and how memory
  and throughput move as tenants are added, is a separate measurement.
- **Ingest mixed with recall.** Ingest embeds whole windows rather than short queries, which costs
  a different amount per call; a mixed workload is a separate measurement.
- **The configuration without a contiguous index.** This run builds the index, as the earlier one
  did. An installation that has not built one is not represented.
- **Recall quality.** The corpus is synthetic.
- **Sustained load over hours.** The steady window is minutes, so thermal behaviour and any
  slow-growing resource use are outside the reading.
