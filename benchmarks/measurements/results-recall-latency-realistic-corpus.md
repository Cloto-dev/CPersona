# Results: recall latency over 100,000 memories of real length, with their blocks

Registration: [prereg-recall-latency-realistic-corpus.md](prereg-recall-latency-realistic-corpus.md),
pushed in `926157b` before the first run on the reference machine. Corpus builder:
[build_realistic_corpus.py](build_realistic_corpus.py). Driver:
[perf_recall_with_embedding.py](perf_recall_with_embedding.py). Records:
[recall_latency_realistic/](recall_latency_realistic/).

**Verdict.** On the Intel N150, recall over 100,000 memories of real length (about
56 million cl100k tokens in all) took a median of **20.73 s** including embedding the
query, with a maximum of **23.14 s**. The registered rule allows no sub-second
statement; it allows the median and the maximum, stated as they are. The secondary arm
(the embedding server limited to one thread) was 5.8% faster at the median, short of the
registered 10%, so the record recommends nothing about threads.

The earlier record, [results-recall-latency-with-embedding.md](results-recall-latency-with-embedding.md),
measured 446.6 ms on the same machine. Its corpus was synthetic: nine tokens a row and no
blocks. That figure stands for that corpus and does not describe memories of real length
on the default configuration of v2.6.4. Nearly all of the difference is one statement in
the block arm, which is a defect, now recorded as bug-504; see
[where the time went](#where-the-time-went-not-registered).

## Setup

Intel N150 (4 threads, `governor=performance`), Python 3.11.2, numpy 2.4.6. CEmbedding
0.8.0 from PyPI (`cembedding[onnx]`, onnxruntime 1.30.0) on the same machine at
`127.0.0.1:8401`, `onnx_jina_v5_nano` (768 dimensions), CPU execution provider. The tree
was tag `v2.6.4` plus the registration, the builder and the driver changes (`926157b`).
Default configuration (`rrf`, confidence off, FTS on, block building and block retrieval
on), scan window 100,000, contiguous index built by the driver, response limit 10, no
episodes. Queries: the first 53 distinct questions of LongMemEval-M in file order — 3
warm-up, 25 timed, 25 for the model's embed time alone.

## The corpus

Built on a second machine (an Apple laptop, CEmbedding 0.8.0 on the CPU execution
provider, model files identical by SHA-256) in 10.8 hours, through `do_store` with the
nodes and blocks the stores queued built by the server's own functions, and copied to the
reference machine (SHA-256 identical on both sides). From
[corpus_meta.json](recall_latency_realistic/corpus_meta.json):

| | corpus | the deployment it was drawn to match |
| --- | ---: | ---: |
| memories | 100,000 | 5,707 |
| cl100k tokens per memory, mean | 562.9 | 555.7 |
| median | 496 | 497 |
| 90th percentile | 979 | 977 |
| longest | 3,335 | 3,353 |
| cl100k tokens in all | 56,293,636 | — |
| blocks | 3,480,069 (34.8 per memory) | 12.5 per memory |
| nodes | 1,503 | — |

M in the registered sentence is 56 million. The corpus holds 2.8 times the deployment's
blocks per memory, because the text is English: blocks are cut by characters, and the
registration said so before the run.

## Primary arm: embedding server on its default threads

From [primary-default-threads.json](recall_latency_realistic/primary-default-threads.json).

| | median | p95 | max | min |
| --- | ---: | ---: | ---: | ---: |
| `do_recall`, query embedded by the model | **20,731.97 ms** | 22,812.82 | **23,138.71** | 19,275.21 |
| `do_recall`, stand-in vector (same texts, same run) | 20,042.96 ms | 21,538.71 | 22,730.33 | 18,753.00 |
| paired difference (real − stand-in) | 516.65 ms | 1,883.53 | 2,816.32 | −550.88 |
| embedding one query, client call alone | 81.75 ms | 120.85 | 125.71 | 63.13 |

The median is above 1,000 ms, so the third row of the registered rule applies: no
sub-second statement. Embedding the query is about 0.4% of a recall.

**Validity.** The load fell below 1.0 after 15 s of waiting; every one of the 25 recalls
through each client returned rows (12, the response limit plus the block arm's two
reserved places); no sample errored; the model's width was 768; the corpus held exactly
100,000 memories.

## Secondary arm: one inference thread

From [secondary-one-thread.json](recall_latency_realistic/secondary-one-thread.json). The
embedding server was restarted with `ONNX_INTRA_OP_THREADS=1`; same corpus, same driver.

| | median | p95 | max | min |
| --- | ---: | ---: | ---: | ---: |
| `do_recall`, query embedded by the model | **19,533.39 ms** | 21,091.74 | **21,614.87** | 18,745.96 |
| `do_recall`, stand-in vector | 19,392.94 ms | 22,886.48 | 22,954.24 | 18,384.74 |
| paired difference (real − stand-in) | 137.23 ms | 2,221.93 | 2,351.77 | −2,727.20 |
| embedding one query, client call alone | 207.69 ms | 320.41 | 337.79 | 148.23 |

The median is 5.8% below the primary arm's (19,533 against 20,732 ms) and the maximum is
lower, but the rule asks for at least 10% at the median, so nothing is recommended. Both
embed-alone times are given above, as the rule requires. The arm passed the same validity
checks (15 s of waiting, 12 rows on every recall, no errors).

## Where the time went (not registered)

Nothing in this section was registered; it is the diagnosis that followed the verdict, on
the laptop that built the corpus, with stand-in query vectors and the measured questions.

A segment profile of three recalls on a copy of the corpus put 9.7 s of a 10.2 s recall in
one SQL statement: the block arm's read of the rows it examines (`blocks._examined`). The
lexical arm over memories took 0.47 s, the vector arm 9 ms.

That statement numbered each record's admitted blocks with `ROW_NUMBER()` and cut with
`LIMIT 250000`. SQLite finishes the window before the limit applies, and with
`agent_id = ?` in the filter it chose the axes index and sorted every block of the agent
first (`USE TEMP B-TREE FOR ORDER BY` in the plan). The examined cap therefore bounded
what came back, not what was read: the read grew with the whole block table. This is
bug-504.

The fix reads the rows in primary key order through the key's own index and stops when
the cap is full, counting both caps over the admitted rows. On the same copy:

| | before | after |
| --- | ---: | ---: |
| the examined read alone (SQLite, 250,000 rows) | 2.6 s | 0.11 s |
| `do_recall`, median of 8 questions | 4,037 ms | 624 ms |
| the examined read inside those recalls, median | 3,469 ms | 105 ms |
| responses identical to the previous statement's | — | 8 of 8 |

The before-figures here are lower than the 9.7 s above because the copy was by then in
the page cache. These are a laptop's numbers; what the fix gives on the reference
machine is for a new registration to measure, not for this record to predict.

## Limits

- One machine, one corpus, one configuration, as registered. Recall quality was not
  checked.
- English text, so more blocks per memory than a Japanese deployment of the same token
  length; the block arm's share of the time scales with the block count.
- The corpus was built on another machine and copied; nothing about the build's speed is
  a statement about the reference machine.
